"""Unit tests for nodes.rca_agent's "real" mode.

utils.platform.{select_device,capture_logs}, utils.runner.run_command (git),
and utils.llm.ask_for_json are mocked at the nodes.rca_agent module level for
the node-level tests - no real adb/xcrun/git/LLM call happens there. The
four tools (get_device_logs, search_repository, inspect_git_diff,
read_relevant_files) are also exercised directly against a real temp
directory tree, with only the actual git/device calls mocked out.
"""

from __future__ import annotations

from pathlib import Path

import nodes.rca_agent as rca_agent_mod
from config import load_app_config
from nodes.rca_agent import (
    get_device_logs,
    inspect_git_diff,
    rca_agent,
    read_relevant_files,
    search_repository,
)
from state import initial_state
from utils.llm import LLMResult
from utils.platform import PlatformResult
from utils.runner import CommandResult


def _real_state(app_config, qa_finding):
    state = initial_state(app_name=app_config.name, app_config=app_config, mode="real")
    state["qa_finding"] = qa_finding
    return state


def _make_config(tmp_path: Path, app_name: str = "sample_android"):
    cfg = load_app_config(app_name)
    cfg.clone_path = tmp_path / "clone"
    cfg.clone_path.mkdir(parents=True)
    return cfg


def _functional_qa_finding(**overrides):
    finding = {
        "flow_name": "login.yaml",
        "passed": False,
        "failure_type": "functional",
        "description": 'Maestro assertion(s) failed: Element not found: "LoginButton"',
        "expected_behavior": "Tapping LoginButton should navigate to the home screen.",
        "actual_behavior": 'Element not found: "LoginButton"',
        "reproduction_steps": ["launch and log in"],
        "evidence_paths": [],
        "confidence": 1.0,
    }
    finding.update(overrides)
    return finding


def _patch_device_and_logs(monkeypatch, logs="E/LoginActivity: NullPointerException at LoginActivity.kt:42\n", success=True):
    def fake_select_device(app_config, timeout=30.0):
        if success:
            return PlatformResult(success=True, details={"device_id": "emulator-5554"})
        return PlatformResult(success=False, error="emulator not booted")

    def fake_capture_logs(app_config, device_id, timeout=60.0):
        return PlatformResult(success=success, logs=logs if success else "", error=None if success else "adb not found")

    monkeypatch.setattr(rca_agent_mod, "select_device", fake_select_device)
    monkeypatch.setattr(rca_agent_mod, "capture_logs", fake_capture_logs)


def _patch_git(monkeypatch, changed_files="LoginActivity.kt\n", diff="diff --git a/LoginActivity.kt b/LoginActivity.kt\n+// bug here\n", ok=True):
    def fake_run_command(argv, cwd, timeout=120.0, env=None):
        if "--name-only" in argv:
            return CommandResult(argv=argv, returncode=0 if ok else 1, stdout=changed_files if ok else "", stderr="" if ok else "fatal: bad revision")
        return CommandResult(argv=argv, returncode=0 if ok else 1, stdout=diff if ok else "", stderr="")

    monkeypatch.setattr(rca_agent_mod, "run_command", fake_run_command)


def _patch_llm(monkeypatch, data):
    def fake_ask_for_json(prompt, images=None, **kwargs):
        return LLMResult(success=True, data=data)

    monkeypatch.setattr(rca_agent_mod, "ask_for_json", fake_ask_for_json)


def _write_source_file(cfg, relative_path, content):
    path = cfg.clone_path / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _init_git_dir(cfg):
    (cfg.clone_path / ".git").mkdir()


# --------------------------------------------------------------------------
# End-to-end node tests
# --------------------------------------------------------------------------

def test_rca_agent_resolves_functional_failure_with_grounded_hypothesis(tmp_path, monkeypatch):
    cfg = _make_config(tmp_path)
    _write_source_file(
        cfg,
        "app/src/main/java/com/example/LoginActivity.kt",
        "class LoginActivity {\n    fun onLoginButtonClick() {\n        // LoginButton handling\n    }\n}\n",
    )
    _init_git_dir(cfg)
    _patch_device_and_logs(monkeypatch)
    _patch_git(monkeypatch, changed_files="app/src/main/java/com/example/LoginActivity.kt\n")
    _patch_llm(
        monkeypatch,
        data={
            "root_cause_hypothesis": "LoginButton click handler throws before navigation completes.",
            "suspected_files": ["app/src/main/java/com/example/LoginActivity.kt"],
            "suspected_methods": ["onLoginButtonClick"],
            "supporting_evidence": ["NullPointerException at LoginActivity.kt:42"],
            "confidence": 0.8,
            "suggested_fix": "Null-check the view before binding the click listener.",
            "missing_information": [],
        },
    )

    update = rca_agent(_real_state(cfg, _functional_qa_finding()))

    assert update["status"] == "rca_complete"
    finding = update["rca_finding"]
    assert finding["confidence"] == 0.8
    assert finding["suspected_files"] == ["app/src/main/java/com/example/LoginActivity.kt"]
    assert finding["suspected_methods"] == ["onLoginButtonClick"]
    assert "LoginButton" in finding["root_cause_hypothesis"]
    assert update["root_cause"] == finding["root_cause_hypothesis"]
    assert update["suspected_files"] == finding["suspected_files"]


def test_rca_agent_drops_hallucinated_file_and_method(tmp_path, monkeypatch):
    cfg = _make_config(tmp_path)
    _write_source_file(cfg, "app/LoginActivity.kt", "class LoginActivity {\n  fun onLoginButtonClick() {}\n}\n")
    _init_git_dir(cfg)
    _patch_device_and_logs(monkeypatch)
    _patch_git(monkeypatch, changed_files="app/LoginActivity.kt\n")
    _patch_llm(
        monkeypatch,
        data={
            "root_cause_hypothesis": "Something in an unrelated file.",
            "suspected_files": ["app/LoginActivity.kt", "app/TotallyMadeUpFile.kt"],
            "suspected_methods": ["onLoginButtonClick", "someInventedMethod"],
            "confidence": 0.9,
        },
    )

    update = rca_agent(_real_state(cfg, _functional_qa_finding()))

    finding = update["rca_finding"]
    assert finding["suspected_files"] == ["app/LoginActivity.kt"]
    assert finding["suspected_methods"] == ["onLoginButtonClick"]
    assert any("TotallyMadeUpFile.kt" in m for m in finding["missing_information"])
    assert any("someInventedMethod" in m for m in finding["missing_information"])
    # A hallucinated reference caps our trust in the whole hypothesis.
    assert finding["confidence"] <= 0.5


def test_rca_agent_infrastructure_failure_short_circuits(tmp_path, monkeypatch):
    cfg = _make_config(tmp_path)
    device_calls = []
    monkeypatch.setattr(rca_agent_mod, "select_device", lambda *a, **k: device_calls.append(1))

    qa_finding = {
        "flow_name": "login.yaml",
        "failure_type": "infrastructure",
        "description": "Could not select a device: emulator not booted",
        "evidence_paths": [],
    }

    update = rca_agent(_real_state(cfg, qa_finding))

    finding = update["rca_finding"]
    assert "infrastructure" in finding["root_cause_hypothesis"].lower()
    assert finding["confidence"] == 0.9
    assert finding["suspected_files"] == []
    # No repo/device analysis should have been attempted for an infra failure.
    assert device_calls == []


def test_rca_agent_unverified_qa_result_is_unresolved(tmp_path, monkeypatch):
    cfg = _make_config(tmp_path)
    qa_finding = {
        "flow_name": "login.yaml",
        "failure_type": "unverified",
        "description": "No acceptance criteria found",
        "evidence_paths": [],
    }

    update = rca_agent(_real_state(cfg, qa_finding))

    finding = update["rca_finding"]
    assert finding["confidence"] == 0.0
    assert "unresolved" in finding["root_cause_hypothesis"].lower()


def test_rca_agent_missing_qa_finding_is_unresolved(tmp_path):
    cfg = _make_config(tmp_path)
    state = initial_state(app_name=cfg.name, app_config=cfg, mode="real")

    update = rca_agent(state)

    finding = update["rca_finding"]
    assert finding["confidence"] == 0.0
    assert "No QA finding" in finding["missing_information"][0]


def test_rca_agent_no_evidence_gathered_is_unresolved(tmp_path, monkeypatch):
    cfg = _make_config(tmp_path)
    _patch_device_and_logs(monkeypatch, success=False)
    _patch_git(monkeypatch, ok=False)

    update = rca_agent(_real_state(cfg, _functional_qa_finding(description="Totally unrelated gibberish zzz")))

    finding = update["rca_finding"]
    assert finding["confidence"] == 0.0
    assert "insufficient evidence" in finding["root_cause_hypothesis"].lower()


def test_rca_agent_llm_failure_is_unresolved(tmp_path, monkeypatch):
    cfg = _make_config(tmp_path)
    _write_source_file(cfg, "app/LoginActivity.kt", "class LoginActivity {}\n")
    _init_git_dir(cfg)
    _patch_device_and_logs(monkeypatch)
    _patch_git(monkeypatch, changed_files="app/LoginActivity.kt\n")
    monkeypatch.setattr(rca_agent_mod, "ask_for_json", lambda prompt, images=None, **k: LLMResult(success=False, error="timed out"))

    update = rca_agent(_real_state(cfg, _functional_qa_finding()))

    finding = update["rca_finding"]
    assert finding["confidence"] == 0.0
    assert "unresolved" in finding["root_cause_hypothesis"].lower()


def test_rca_agent_works_for_ios_config(tmp_path, monkeypatch):
    cfg = _make_config(tmp_path, app_name="sample_ios")
    _write_source_file(cfg, "App/LoginView.swift", "struct LoginView: View {\n  func onLoginButtonTap() {}\n}\n")
    _init_git_dir(cfg)
    _patch_device_and_logs(monkeypatch, logs="LoginView: button not responding\n")
    _patch_git(monkeypatch, changed_files="App/LoginView.swift\n")
    _patch_llm(
        monkeypatch,
        data={
            "root_cause_hypothesis": "LoginButton tap handler is not wired up.",
            "suspected_files": ["App/LoginView.swift"],
            "suspected_methods": ["onLoginButtonTap"],
            "confidence": 0.75,
        },
    )

    update = rca_agent(_real_state(cfg, _functional_qa_finding()))

    finding = update["rca_finding"]
    assert finding["suspected_files"] == ["App/LoginView.swift"]
    assert finding["confidence"] == 0.75


def test_mock_mode_is_unchanged(tmp_path):
    cfg = _make_config(tmp_path)
    state = initial_state(app_name=cfg.name, app_config=cfg, mock_scenario="fix_success")

    update = rca_agent(state)

    assert update["root_cause"].startswith("Mock RCA:")
    assert update["suspected_files"] == ["mock/suspected_file_1", "mock/suspected_file_2"]
    assert "rca_finding" not in update


# --------------------------------------------------------------------------
# Tool-level tests
# --------------------------------------------------------------------------

def test_get_device_logs_truncates_long_output(tmp_path, monkeypatch):
    cfg = _make_config(tmp_path)
    _patch_device_and_logs(monkeypatch, logs="x" * 20_000)

    result = get_device_logs(cfg, max_chars=100)

    assert result.success
    assert len(result.logs) < 20_000
    assert result.logs.endswith("x" * 100)


def test_get_device_logs_reports_device_failure(tmp_path, monkeypatch):
    cfg = _make_config(tmp_path)
    _patch_device_and_logs(monkeypatch, success=False)

    result = get_device_logs(cfg)

    assert not result.success
    assert "emulator not booted" in result.error


def test_search_repository_finds_matches_across_nested_dirs(tmp_path):
    cfg = _make_config(tmp_path)
    _write_source_file(cfg, "a/b/c/Deep.kt", "fun doThing() {\n    throwSomethingBad()\n}\n")
    _write_source_file(cfg, "ignored.txt", "throwSomethingBad should not match - not a source extension\n")

    hits = search_repository(cfg, "throwSomethingBad")

    assert len(hits) == 1
    assert hits[0].file_path == "a/b/c/Deep.kt"
    assert hits[0].line_number == 2


def test_search_repository_respects_max_matches(tmp_path):
    cfg = _make_config(tmp_path)
    _write_source_file(cfg, "Many.kt", "\n".join(f"findme line {i}" for i in range(20)))

    hits = search_repository(cfg, "findme", max_matches=3)

    assert len(hits) == 3


def test_search_repository_rejects_escape_outside_clone_path(tmp_path):
    cfg = _make_config(tmp_path)
    outside = tmp_path / "outside.kt"
    outside.write_text("findme outside\n")

    hits = search_repository(cfg, "findme")

    assert hits == []


def test_read_relevant_files_bounds_line_count_and_skips_missing(tmp_path):
    cfg = _make_config(tmp_path)
    _write_source_file(cfg, "Big.kt", "\n".join(f"line {i}" for i in range(500)))

    excerpts = read_relevant_files(cfg, ["Big.kt", "DoesNotExist.kt"], max_lines_per_file=10)

    assert "DoesNotExist.kt" not in excerpts
    assert excerpts["Big.kt"].count("\n") == 10


def test_read_relevant_files_rejects_path_traversal(tmp_path):
    cfg = _make_config(tmp_path)
    secret = tmp_path / "secret.txt"
    secret.write_text("top secret\n")

    excerpts = read_relevant_files(cfg, ["../secret.txt"])

    assert excerpts == {}


def test_inspect_git_diff_not_a_repo(tmp_path):
    cfg = _make_config(tmp_path)

    result = inspect_git_diff(cfg)

    assert not result.success
    assert "No git repository" in result.error


def test_inspect_git_diff_success_and_truncation(tmp_path, monkeypatch):
    cfg = _make_config(tmp_path)
    _init_git_dir(cfg)
    _patch_git(monkeypatch, changed_files="a.kt\nb.kt\n", diff="x" * 10_000)

    result = inspect_git_diff(cfg, max_chars=50)

    assert result.success
    assert result.changed_files == ["a.kt", "b.kt"]
    assert len(result.diff_text) < 10_000
    assert result.diff_text.endswith("[diff truncated]")
