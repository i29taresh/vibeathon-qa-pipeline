"""Unit tests for the lean Android POC path (mocked externals only)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from graph import _route_after_jira, _route_after_retest
from nodes.finalize_local import finalize_local
from utils.maestro_flow_gen import (
    build_yaml,
    generate_and_save_flow,
    str_is_clear,
    validate_maestro_steps,
)
from utils.maven_local import (
    cleanup_maven_local,
    pin_b2b_payzy_shared,
    read_payzy_shared_pin,
    ticket_version,
)
from utils.run_history import list_attempts, list_jira_keys, load_run, save_run
from utils.video_verification import VerifierFinding, verify_after_video


# --------------------------------------------------------------------------
# Maestro flow generation
# --------------------------------------------------------------------------

def test_str_is_clear_rejects_placeholder():
    assert not str_is_clear(["Reproduce using the steps in the linked Jira issue."])
    assert not str_is_clear([])
    assert str_is_clear(["1. Tap Settings", "2. Open Notifications"])


def test_validate_rejects_eval_script():
    cleaned, errors = validate_maestro_steps([{"evalScript": "1+1"}, {"tapOn": "OK"}])
    assert cleaned == [{"tapOn": "OK"}]
    assert any("evalScript" in e for e in errors)


def test_validate_rejects_unknown_command():
    cleaned, errors = validate_maestro_steps([{"flyToMoon": True}])
    assert cleaned == []
    assert errors


def test_generate_flow_writes_login_prefix(tmp_path, monkeypatch):
    monkeypatch.setenv("MAESTRO_EMAIL", "user@example.com")
    monkeypatch.setenv("MAESTRO_PASSWORD", "secret")
    result = generate_and_save_flow(
        "WFDR-1",
        ["Tap the Settings button", "Assert Notifications is visible"],
        orchestrator_root=tmp_path,
        description="User cannot open settings",
        use_llm=False,
    )
    assert result.success
    assert result.flow_path is not None
    text = result.flow_path.read_text(encoding="utf-8")
    assert "appId: de.payzy.pro.pre_prod" in text
    assert "user@example.com" in text
    assert "000000" in text
    assert "launchApp" in text
    assert "Settings" in text or "assertVisible" in text
    # Default appId (de.payzy.pro.pre_prod) always includes Business Owner onboarding.
    assert "login__role_selection__business_owner_card" in text


def test_business_owner_ticket_gets_onboarding_prefix(tmp_path, monkeypatch):
    from utils.maestro_flow_gen import mentions_business_owner

    assert mentions_business_owner("Login as a Business Owner and open Tips")
    monkeypatch.setenv("MAESTRO_EMAIL", "user@example.com")
    monkeypatch.setenv("MAESTRO_PASSWORD", "secret")
    result = generate_and_save_flow(
        "WFDR-BO",
        ["As a business owner, open Tips and change the default"],
        orchestrator_root=tmp_path,
        description="Business Owner cannot save tip settings",
        use_llm=False,
    )
    assert result.success
    text = result.flow_path.read_text(encoding="utf-8")
    assert "login__role_selection__business_owner_card" in text
    assert "Next" in text
    assert "Login" in text or "Login/register" in text
    # Order: launch → role → intro → login fields
    assert text.index("launchApp") < text.index("login__role_selection__business_owner_card")
    assert text.index("login__role_selection__business_owner_card") < text.index("user@example.com")


def test_generate_flow_unverified_when_str_unclear(tmp_path, monkeypatch):
    monkeypatch.setenv("MAESTRO_EMAIL", "a@b.com")
    monkeypatch.setenv("MAESTRO_PASSWORD", "x")
    result = generate_and_save_flow(
        "WFDR-2",
        ["See Jira"],
        orchestrator_root=tmp_path,
        use_llm=False,
    )
    assert not result.success
    assert result.failure_type == "unverified"


def test_build_yaml_structure():
    yaml_text = build_yaml("de.payzy.pro.pre_prod", [{"tapOn": "OK"}])
    assert yaml_text.startswith("appId: de.payzy.pro.pre_prod\n---\n")


# --------------------------------------------------------------------------
# Maven pin / version
# --------------------------------------------------------------------------

def test_ticket_version_literal():
    assert ticket_version("3.60.0-mqtt", "WFDR-25182") == "3.60.0-mqtt-WFDR-25182"


def test_pin_and_restore_toml(tmp_path):
    gradle = tmp_path / "gradle"
    gradle.mkdir()
    toml = gradle / "libs.versions.toml"
    toml.write_text('[versions]\npayzyShared = "3.60.0-mqtt"\n', encoding="utf-8")

    class _Cfg:
        clone_path = tmp_path

    pin = pin_b2b_payzy_shared(_Cfg(), "3.60.0-mqtt-WFDR-1")
    assert pin.success
    assert pin.original_pin == "3.60.0-mqtt"
    assert 'payzyShared = "3.60.0-mqtt-WFDR-1"' in toml.read_text(encoding="utf-8")

    cleanup = cleanup_maven_local("3.60.0-mqtt-WFDR-1", _Cfg(), pin.original_pin)
    assert cleanup["restored_pin"]
    assert read_payzy_shared_pin(_Cfg()) == "3.60.0-mqtt"


# --------------------------------------------------------------------------
# Graph routing
# --------------------------------------------------------------------------

def test_route_after_jira_unverified_blocked():
    assert _route_after_jira({"status": "intake_unverified"}) == "blocked"
    assert _route_after_jira({"qa_finding": {"failure_type": "unverified"}}) == "blocked"
    assert _route_after_jira({"status": "bug_detected", "qa_finding": {"failure_type": "functional"}}) == "rca_agent"


def test_route_after_retest_jira_goes_to_finalize():
    assert _route_after_retest({"passed": True, "jira_issue_input": True}) == "finalize"
    assert _route_after_retest({"passed": True}) == "merge"


def test_route_after_retest_verifier_fail_retries_then_stops():
    assert (
        _route_after_retest(
            {
                "passed": False,
                "status": "retest_failed",
                "jira_issue_input": True,
                "attempt_count": 1,
                "max_attempts": 3,
                "qa_finding": {"failure_type": "functional"},
            }
        )
        == "retry"
    )
    assert (
        _route_after_retest(
            {
                "passed": False,
                "status": "failed",
                "jira_issue_input": True,
                "attempt_count": 3,
                "max_attempts": 3,
                "qa_finding": {"failure_type": "functional"},
            }
        )
        == "stop"
    )


def test_password_recovery_flow_uses_device_ids(tmp_path, monkeypatch):
    monkeypatch.setenv("MAESTRO_EMAIL", "user@example.com")
    result = generate_and_save_flow(
        "WFDR-25776",
        ["Open forgot password", "Enter OTP", "See password page"],
        orchestrator_root=tmp_path,
        description="Blank password page after forgot password OTP",
        use_llm=False,
    )
    assert result.success
    text = result.flow_path.read_text(encoding="utf-8")
    assert "login__login__recover_password" in text
    assert "login__verify_with_otp__code_input" in text
    assert "000000" in text
    assert "Create new password" in text


# --------------------------------------------------------------------------
# Run history
# --------------------------------------------------------------------------

def test_run_history_save_load(tmp_path):
    state = {
        "jira_issue_key": "WFDR-99",
        "ticket_id": "WFDR-99",
        "status": "ready_for_review",
        "passed": True,
        "attempt_count": 1,
        "verifier_finding": {"passed": True, "issue_resolved": True, "comments": "ok", "confidence": 0.9},
        "finalize_finding": {"branch": "ai-fix/WFDR-99", "commit_sha": "abc123", "committed": True},
        "after_video_paths": [],
    }
    run_dir = save_run(state, runs_root=tmp_path)
    assert (run_dir / "manifest.json").is_file()
    assert "WFDR-99" in list_jira_keys(tmp_path)
    attempts = list_attempts("WFDR-99", tmp_path)
    assert len(attempts) == 1
    loaded = load_run("WFDR-99", run_dir.name, tmp_path)
    assert loaded is not None
    assert loaded["jira_key"] == "WFDR-99"
    assert loaded["commit_sha"] == "abc123"


# --------------------------------------------------------------------------
# Verifier
# --------------------------------------------------------------------------

def test_verifier_missing_video():
    finding = verify_after_video({"issue_key": "X-1"}, [])
    assert not finding.passed
    assert finding.error == "missing_after_video"


def test_verifier_pass_with_mocked_frames(tmp_path, monkeypatch):
    video = tmp_path / "after.mp4"
    video.write_bytes(b"fake")
    frame = tmp_path / "frame_001.png"
    frame.write_bytes(b"\x89PNG\r\n\x1a\n")

    monkeypatch.setattr(
        "utils.video_verification.extract_keyframes",
        lambda *a, **k: MagicMock(success=True, frames=[frame], error=None),
    )
    monkeypatch.setattr(
        "utils.video_verification.ask_for_json",
        lambda *a, **k: MagicMock(
            success=True,
            data={"issue_resolved": True, "comments": "fixed", "confidence": 0.95},
            error=None,
        ),
    )
    finding = verify_after_video({"issue_key": "WFDR-1", "summary": "bug"}, [str(video)])
    assert finding.passed
    assert finding.issue_resolved
    assert finding.comments == "fixed"


# --------------------------------------------------------------------------
# Dev defer commit (jira path)
# --------------------------------------------------------------------------

def test_dev_agent_jira_path_skips_commit(tmp_path, monkeypatch):
    import nodes.dev_agent as dev_agent_mod
    from nodes.dev_agent import dev_agent
    from state import initial_state
    from tests.test_dev_agent_real import _GitScript, _make_repo, _patch_coding_agent, _patch_issue_and_build

    cfg = _make_repo(tmp_path)
    script = _GitScript(agent_summary="Deferred commit fix.")
    monkeypatch.setattr(dev_agent_mod, "run_command", script)
    _patch_coding_agent(monkeypatch, script)
    _patch_issue_and_build(monkeypatch)

    monkeypatch.setattr("utils.git_workspace.working_tree_diff_stat", lambda root, max_chars=4000: " App.kt | 1 +\n")

    state = initial_state(app_name=cfg.name, app_config=cfg, mode="real")
    state["ticket_id"] = "42"
    state["jira_issue_input"] = True
    state["qa_finding"] = {
        "description": "bug",
        "expected_behavior": "ok",
        "actual_behavior": "bad",
        "reproduction_steps": [],
    }
    state["rca_finding"] = {"root_cause_hypothesis": "x", "suspected_files": ["app/LoginActivity.kt"]}

    update = dev_agent(state)
    assert update["status"] == "fix_applied"
    assert update["dev_finding"].get("committed") is False
    assert ["git", "commit"] not in [c[:2] for c in script.calls if len(c) >= 2]
    assert not script.committed


# --------------------------------------------------------------------------
# Finalize commits once
# --------------------------------------------------------------------------

def test_finalize_local_commits_once(tmp_path, monkeypatch):
    from config import load_app_config
    from state import initial_state

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True, capture_output=True)
    (repo / "App.kt").write_text("fun x()=1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "branch", "-M", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "checkout", "-b", "ai-fix/WFDR-1"], cwd=repo, check=True, capture_output=True)
    (repo / "App.kt").write_text("fun x()=2\n", encoding="utf-8")

    cfg = load_app_config("sample_android")
    cfg.clone_path = repo

    monkeypatch.setattr(
        "nodes.finalize_local.resolve_fix_app_config",
        lambda state: ("sample_android", cfg),
    )
    monkeypatch.setattr(
        "nodes.finalize_local.save_run",
        lambda state: tmp_path / "runs" / "WFDR-1" / "rid",
    )
    (tmp_path / "runs" / "WFDR-1" / "rid").mkdir(parents=True)

    video = tmp_path / "after.mp4"
    video.write_bytes(b"fake-mp4")
    commented: dict = {}

    def fake_comment(issue_key, body, cwd=None, attachments=None, timeout=30.0):
        commented["issue_key"] = issue_key
        commented["body"] = body
        commented["attachments"] = [str(p) for p in (attachments or [])]
        return MagicMock(success=True, error=None)

    monkeypatch.setattr("nodes.finalize_local.jira_api.comment_on_issue", fake_comment)

    state = initial_state("sample_android", cfg, mode="real")
    state.update(
        {
            "passed": True,
            "status": "retest_passed",
            "ticket_id": "WFDR-1",
            "jira_issue_key": "WFDR-1",
            "jira_issue_input": True,
            "verifier_finding": {"passed": True, "issue_resolved": True, "comments": "ok"},
            "after_video_paths": [str(video)],
            "dev_finding": {
                "branch": "ai-fix/WFDR-1",
                "files_changed": ["M App.kt"],
                "build": {"success": True},
            },
        }
    )
    update = finalize_local(state)
    assert update["status"] == "ready_for_review"
    assert update["finalize_finding"]["committed"] is True
    assert update["finalize_finding"]["commit_sha"]
    assert update["finalize_finding"]["branch"] == "ai-fix/WFDR-1"
    assert update["finalize_finding"]["recording_paths"] == [str(video)]
    assert commented["issue_key"] == "WFDR-1"
    assert "ai-fix/WFDR-1" in commented["body"]
    assert commented["attachments"] == [str(video)]
    assert update.get("pr_url") is None
    # tree clean after commit
    dirty = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=True)
    assert dirty.stdout.strip() == ""


def test_verifier_finding_dataclass():
    f = VerifierFinding(passed=True, issue_resolved=True, comments="x", confidence=1.0)
    assert f.to_dict()["issue_resolved"] is True
