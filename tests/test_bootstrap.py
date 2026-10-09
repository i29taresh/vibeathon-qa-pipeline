"""Tests for project bootstrap (clone, build order, repo map, ready gate)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from config import load_app_config, load_project_graph
from graph import PipelineSafetyError, run_pipeline
from state import initial_state
from utils import bootstrap as bootstrap_mod
from utils.bootstrap import (
    bootstrap_project,
    dependency_build_order,
    ensure_checkout,
    generate_repo_map,
    is_ready,
    marker_path,
    write_repo_map,
)
from utils.platform import PlatformResult
from utils.runner import CommandResult


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / ".git").mkdir()
    return path


def test_dependency_build_order_libraries_before_app():
    root = Path(__file__).resolve().parents[1]
    graph = load_project_graph("b2b_android", project_root=root)
    order = dependency_build_order(graph, "b2b_android")
    assert order.index("payzyshared") < order.index("b2b_android")


def test_ensure_checkout_skips_existing_git_repo(tmp_path):
    cfg = load_app_config("sample_android")
    cfg.clone_path = _git_repo(tmp_path / "app")
    err, cloned = ensure_checkout(cfg)
    assert err is None
    assert cloned is False


def test_ensure_checkout_fails_for_non_git_directory(tmp_path):
    cfg = load_app_config("sample_android")
    cfg.clone_path = tmp_path / "not-git"
    cfg.clone_path.mkdir()
    err, cloned = ensure_checkout(cfg)
    assert err is not None
    assert "not a git repository" in err
    assert cloned is False


def test_ensure_checkout_fails_when_parent_missing(tmp_path):
    cfg = load_app_config("sample_android")
    cfg.clone_path = tmp_path / "missing_parent" / "app"
    err, cloned = ensure_checkout(cfg)
    assert err is not None
    assert "Parent directory" in err
    assert cloned is False


def test_ensure_checkout_clones_when_missing(monkeypatch, tmp_path):
    from config import GitLabIntegration

    cfg = load_app_config("sample_android")
    dest = tmp_path / "workspace" / "app"
    dest.parent.mkdir()
    cfg.clone_path = dest
    cfg.integrations.gitlab = GitLabIntegration(host="gitlab.example.com", project_path="group/sample")

    calls: list[list[str]] = []

    def fake_run(argv, cwd, timeout=120.0, env=None):
        calls.append(list(argv))
        if argv[:2] == ["git", "clone"]:
            dest.mkdir(parents=True)
            (dest / ".git").mkdir()
            return CommandResult(argv=list(argv), returncode=0, stdout="", stderr="")
        return CommandResult(argv=list(argv), returncode=0, stdout="abc123\n", stderr="")

    monkeypatch.setenv("GITLAB_TOKEN", "glpat-testtoken1234567890")
    monkeypatch.setattr(bootstrap_mod, "run_command", fake_run)

    err, cloned = ensure_checkout(cfg)
    assert err is None
    assert cloned is True
    clone_calls = [c for c in calls if c[:2] == ["git", "clone"]]
    assert len(clone_calls) == 1
    assert "oauth2:" in clone_calls[0][3] or any("oauth2:" in a for a in clone_calls[0])


def test_generate_repo_map_from_fake_gradle_tree(tmp_path):
    cfg = load_app_config("sample_android")
    cfg.clone_path = tmp_path
    (tmp_path / "settings.gradle.kts").write_text(
        'include(":app")\ninclude(":feature:consents:presentation")\n',
        encoding="utf-8",
    )
    screen = tmp_path / "src" / "ods" / "consents" / "detail" / "ui"
    screen.mkdir(parents=True)
    (screen.parent / "ConsentProps.kt").write_text("class ConsentProps", encoding="utf-8")
    (tmp_path / "Theme.kt").write_text("val neutralScheme = 1\nval blackScheme = 2\n", encoding="utf-8")

    text = generate_repo_map(cfg, "sample_android")
    assert ":feature:consents:presentation" in text
    assert "ods/consents/detail/ui" in text
    assert "ConsentProps.kt" in text
    assert "neutralScheme" in text


def test_repo_map_injected_into_rca_prompt(monkeypatch, tmp_path):
    from nodes import rca_agent as rca_mod

    cfg = load_app_config("sample_android")
    cfg.clone_path = _git_repo(tmp_path / "clone")
    write_repo_map(cfg, "sample_android", project_root=tmp_path)
    # write_repo_map uses PROJECT_ROOT by default for map path — force via map_path_for
    from utils.bootstrap import map_path_for

    map_file = map_path_for("sample_android", tmp_path)
    map_file.parent.mkdir(parents=True, exist_ok=True)
    map_file.write_text("# Repo map: sample_android\n- module `:app`\n", encoding="utf-8")

    captured: dict = {}

    def fake_ask(prompt, **kwargs):
        captured["prompt"] = prompt
        return MagicMock(success=True, data={
            "target_app": "sample_android",
            "root_cause_hypothesis": "x",
            "suspected_files": [],
            "suspected_methods": [],
            "supporting_evidence": [],
            "confidence": 0.1,
            "suggested_fix": "",
            "missing_information": [],
        })

    monkeypatch.setattr(rca_mod, "ask_for_json", fake_ask)
    monkeypatch.setattr(rca_mod, "get_device_logs", lambda _c: MagicMock(success=False, logs="", error="skip"))
    monkeypatch.setattr(rca_mod, "search_repository", lambda *_a, **_k: [])
    monkeypatch.setattr(rca_mod, "inspect_git_diff", lambda _c: MagicMock(success=False, changed_files=[], diff_text="", error="skip"))
    monkeypatch.setattr(rca_mod, "read_relevant_files", lambda *_a, **_k: {})
    monkeypatch.setattr(
        "utils.project_context.load_project_repo_maps",
        lambda _g, project_root=None: map_file.read_text(encoding="utf-8"),
    )

    qa = {
        "flow_name": "login.yaml",
        "failure_type": "functional",
        "description": "button missing",
        "expected_behavior": "shown",
        "actual_behavior": "hidden",
        "reproduction_steps": ["open"],
        "evidence_paths": [],
    }
    rca_mod._ask_llm_for_root_cause(
        "sample_android",
        {"sample_android": cfg},
        qa,
        MagicMock(success=False, logs="", error="skip"),
        [],
        {"sample_android": MagicMock(success=False, changed_files=[], diff_text="", error="skip")},
        {},
        [],
    )
    assert "Repo map: sample_android" in captured["prompt"]
    assert "Project structure (from bootstrap repo map" in captured["prompt"]


def test_bootstrap_build_order_and_marker(monkeypatch, tmp_path):
    root = Path(__file__).resolve().parents[1]
    # Use sample_android alone — rewrite clone paths via monkeypatch on load_project_graph
    cfg = load_app_config("sample_android", project_root=root)
    clone = _git_repo(tmp_path / "app")
    cfg.clone_path = clone
    (clone / "settings.gradle").write_text('include(":app")\n', encoding="utf-8")

    build_calls: list[str] = []

    def fake_build(app_config):
        build_calls.append(app_config.build.build_command)
        return PlatformResult(success=True, logs="ok")

    monkeypatch.setattr(bootstrap_mod, "load_project_graph", lambda name, project_root=None: {name: cfg})
    monkeypatch.setattr("graph.ensure_source_isolated", lambda _p: None)
    monkeypatch.setattr(bootstrap_mod, "build_app", fake_build)
    monkeypatch.setattr(bootstrap_mod, "head_sha", lambda _p: "deadbeef")

    result = bootstrap_project("sample_android", project_root=tmp_path, skip_build=False)
    assert result.success
    assert build_calls == [cfg.build.build_command]
    assert is_ready("sample_android", project_root=tmp_path)
    assert marker_path("sample_android", tmp_path).is_file()


def test_bootstrap_skip_build(monkeypatch, tmp_path):
    cfg = load_app_config("sample_android")
    cfg.clone_path = _git_repo(tmp_path / "app")

    def boom(_cfg):
        raise AssertionError("build_app must not be called")

    monkeypatch.setattr(bootstrap_mod, "load_project_graph", lambda name, project_root=None: {name: cfg})
    monkeypatch.setattr("graph.ensure_source_isolated", lambda _p: None)
    monkeypatch.setattr(bootstrap_mod, "build_app", boom)
    monkeypatch.setattr(bootstrap_mod, "head_sha", lambda _p: "abc")

    result = bootstrap_project("sample_android", project_root=tmp_path, skip_build=True)
    assert result.success
    assert result.repos[0].build_success is None


def test_run_pipeline_blocks_without_ready_marker(tmp_path):
    cfg = load_app_config("sample_android")
    cfg.clone_path = _git_repo(tmp_path / "safe-clone")
    state = initial_state(app_name="sample_android", app_config=cfg, mode="real")
    with pytest.raises(PipelineSafetyError, match="not been bootstrapped"):
        run_pipeline(state)


def test_run_pipeline_allows_force_unbootstrapped(monkeypatch, tmp_path):
    cfg = load_app_config("sample_android")
    cfg.clone_path = _git_repo(tmp_path / "safe-clone")
    state = initial_state(
        app_name="sample_android",
        app_config=cfg,
        mode="real",
        force_unbootstrapped=True,
    )

    class _FakePipeline:
        def stream(self, initial, stream_mode="values"):
            yield dict(initial)
            yield {**dict(initial), "status": "no_bugs_found", "passed": True}

    monkeypatch.setattr("graph.build_graph", lambda: _FakePipeline())
    final = run_pipeline(state)
    assert final["status"] == "no_bugs_found"


def test_preflight_warns_when_off_base_branch(monkeypatch, tmp_path):
    from main import _check_base_branch_warnings

    cfg = load_app_config("sample_android")
    cfg.clone_path = _git_repo(tmp_path / "app")
    cfg.base_branch = "develop"
    monkeypatch.setattr("utils.bootstrap.current_branch", lambda _p: "feature/x")
    checks = _check_base_branch_warnings({"sample_android": cfg})
    assert len(checks) == 1
    assert checks[0]["critical"] is False
    assert checks[0]["passed"] is False
    assert "feature/x" in checks[0]["detail"]
