"""Tests for Jira baseline verification after retest."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from config import load_app_config
from nodes.retest_agent import retest_agent
from utils.jira_verification import JiraVerificationResult, verify_against_jira_baseline
from utils.llm import LLMResult
from utils.qa_validation import QAFinding


def test_verify_against_jira_baseline_requires_current_screenshots(tmp_path):
    root = Path(__file__).resolve().parents[1]
    cfg = load_app_config("sample_android", project_root=root)
    baseline = {
        "issue_key": "TST-1",
        "summary": "Bug",
        "reproduction_steps": ["Tap login"],
        "expected_behavior": "Login works",
        "actual_behavior": "Crash",
        "baseline_evidence_paths": [],
    }
    result = verify_against_jira_baseline(cfg, baseline, [], "login.yaml")
    assert not result.passed
    assert result.error == "missing_current_evidence"


def test_retest_verifier_fail_retries_to_rca(monkeypatch, tmp_path):
    root = Path(__file__).resolve().parents[1]
    cfg = load_app_config("sample_android", project_root=root)

    pass_finding = QAFinding(
        flow_name="login.yaml",
        passed=True,
        failure_type="none",
        description="ok",
        expected_behavior="e",
        actual_behavior="a",
        confidence=1.0,
        evidence_paths=[str(tmp_path / "after.png")],
        video_paths=[str(tmp_path / "after.mp4")],
    )
    (tmp_path / "after.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (tmp_path / "after.mp4").write_bytes(b"fake")

    monkeypatch.setattr("nodes.retest_agent.build_app", lambda _c: MagicMock(success=True, artifact_path=tmp_path / "app.apk", error=None))
    monkeypatch.setattr("nodes.retest_agent.select_device", lambda _c: MagicMock(success=True, details={"device_id": "dev-1"}, error=None))
    monkeypatch.setattr("nodes.retest_agent.install_app", lambda *_a, **_k: MagicMock(success=True, error=None))
    monkeypatch.setattr("nodes.retest_agent.run_qa_check", lambda *_a, **_k: pass_finding)
    monkeypatch.setattr("nodes.retest_agent.write_patch", lambda *_a, **_k: True)
    monkeypatch.setattr("nodes.retest_agent.reset_hard", lambda *_a, **_k: True)
    monkeypatch.setattr("nodes.retest_agent.resolve_fix_app_config", lambda s: ("sample_android", cfg))
    monkeypatch.setattr(
        "nodes.retest_agent.verify_after_video",
        lambda *_a, **_k: MagicMock(
            passed=False,
            issue_resolved=False,
            comments="Still blank password page",
            confidence=0.9,
            error=None,
            frame_paths=[],
            video_path=str(tmp_path / "after.mp4"),
            to_dict=lambda: {
                "passed": False,
                "issue_resolved": False,
                "comments": "Still blank password page",
                "confidence": 0.9,
                "error": None,
                "frame_paths": [],
                "video_path": str(tmp_path / "after.mp4"),
            },
        ),
    )

    from state import initial_state

    state = initial_state("sample_android", cfg, mode="real", flow_name="login.yaml")
    state["jira_issue_input"] = True
    state["jira_baseline"] = {"issue_key": "TST-9", "reproduction_steps": ["step"], "expected_behavior": "fields"}
    state["dev_finding"] = {"build": {"success": True}, "files_changed": ["a.kt"], "branch": "ai-fix/TST-9"}
    state["attempt_count"] = 1
    state["max_attempts"] = 3

    update = retest_agent(state)
    assert update["passed"] is False
    assert update["status"] == "retest_failed"
    assert update["verifier_finding"]["passed"] is False
    assert update["retest_finding"]["jira_verification"]["passed"] is False


def test_retest_verifier_pass_even_if_maestro_assert_failed(monkeypatch, tmp_path):
    """Verifier is the hard gate for Jira POC — Maestro assert alone does not block pass."""
    root = Path(__file__).resolve().parents[1]
    cfg = load_app_config("sample_android", project_root=root)
    video = tmp_path / "after.mp4"
    video.write_bytes(b"fake")

    fail_finding = QAFinding(
        flow_name="repro.yaml",
        passed=False,
        failure_type="functional",
        description='Assertion is false: "New password" is visible',
        expected_behavior="fields",
        actual_behavior="missing",
        confidence=1.0,
        evidence_paths=[],
        video_paths=[str(video)],
    )
    monkeypatch.setattr("nodes.retest_agent.build_app", lambda _c: MagicMock(success=True, artifact_path=tmp_path / "app.apk", error=None))
    monkeypatch.setattr("nodes.retest_agent.select_device", lambda _c: MagicMock(success=True, details={"device_id": "dev-1"}, error=None))
    monkeypatch.setattr("nodes.retest_agent.install_app", lambda *_a, **_k: MagicMock(success=True, error=None))
    monkeypatch.setattr("nodes.retest_agent.run_qa_check", lambda *_a, **_k: fail_finding)
    monkeypatch.setattr(
        "nodes.retest_agent.verify_after_video",
        lambda *_a, **_k: MagicMock(
            passed=True,
            issue_resolved=True,
            comments="Password fields visible",
            confidence=0.95,
            error=None,
            frame_paths=[],
            video_path=str(video),
            to_dict=lambda: {
                "passed": True,
                "issue_resolved": True,
                "comments": "Password fields visible",
                "confidence": 0.95,
                "error": None,
                "frame_paths": [],
                "video_path": str(video),
            },
        ),
    )

    from state import initial_state

    state = initial_state("sample_android", cfg, mode="real", flow_name="repro.yaml")
    state["jira_issue_input"] = True
    state["jira_baseline"] = {"issue_key": "WFDR-1", "expected_behavior": "fields"}
    state["maestro_flow_path"] = str(tmp_path / "repro.yaml")
    (tmp_path / "repro.yaml").write_text("appId: x\n---\n- launchApp\n", encoding="utf-8")
    state["dev_finding"] = {
        "build": {"success": True},
        "files_changed": ["a.kt"],
        "branch": "ai-fix/WFDR-1",
    }
    state["attempt_count"] = 2
    state["max_attempts"] = 3

    update = retest_agent(state)
    assert update["passed"] is True
    assert update["status"] == "retest_passed"
    assert update["verifier_finding"]["passed"] is True
    assert update["retest_finding"]["fix_branch"] == "ai-fix/WFDR-1"
    assert update["after_video_paths"] == [str(video)]


def test_retest_verifier_exhausted_marks_failed(monkeypatch, tmp_path):
    root = Path(__file__).resolve().parents[1]
    cfg = load_app_config("sample_android", project_root=root)
    video = tmp_path / "after.mp4"
    video.write_bytes(b"fake")
    finding = QAFinding(
        flow_name="repro.yaml",
        passed=False,
        failure_type="functional",
        description="still broken",
        expected_behavior="e",
        actual_behavior="a",
        confidence=1.0,
        video_paths=[str(video)],
    )
    monkeypatch.setattr("nodes.retest_agent.build_app", lambda _c: MagicMock(success=True, artifact_path=tmp_path / "a.apk", error=None))
    monkeypatch.setattr("nodes.retest_agent.select_device", lambda _c: MagicMock(success=True, details={"device_id": "d"}, error=None))
    monkeypatch.setattr("nodes.retest_agent.install_app", lambda *_a, **_k: MagicMock(success=True, error=None))
    monkeypatch.setattr("nodes.retest_agent.run_qa_check", lambda *_a, **_k: finding)
    monkeypatch.setattr("nodes.retest_agent.write_patch", lambda *_a, **_k: True)
    monkeypatch.setattr("nodes.retest_agent.reset_hard", lambda *_a, **_k: True)
    monkeypatch.setattr("nodes.retest_agent.resolve_fix_app_config", lambda s: ("sample_android", cfg))
    monkeypatch.setattr(
        "nodes.retest_agent.verify_after_video",
        lambda *_a, **_k: MagicMock(
            passed=False,
            issue_resolved=False,
            comments="not fixed",
            confidence=0.9,
            error=None,
            frame_paths=[],
            video_path=str(video),
            to_dict=lambda: {
                "passed": False,
                "issue_resolved": False,
                "comments": "not fixed",
                "confidence": 0.9,
                "error": None,
                "frame_paths": [],
                "video_path": str(video),
            },
        ),
    )
    from state import initial_state

    state = initial_state("sample_android", cfg, mode="real", flow_name="repro.yaml")
    state.update(
        {
            "jira_issue_input": True,
            "jira_baseline": {"issue_key": "WFDR-1"},
            "maestro_flow_path": str(tmp_path / "repro.yaml"),
            "dev_finding": {"build": {"success": True}, "files_changed": ["a.kt"]},
            "attempt_count": 3,
            "max_attempts": 3,
        }
    )
    (tmp_path / "repro.yaml").write_text("appId: x\n---\n- launchApp\n", encoding="utf-8")
    update = retest_agent(state)
    assert update["passed"] is False
    assert update["status"] == "failed"


def test_verify_uses_llm_json(monkeypatch, tmp_path):
    root = Path(__file__).resolve().parents[1]
    cfg = load_app_config("sample_android", project_root=root)
    shot = tmp_path / "cur.png"
    shot.write_bytes(b"\x89PNG\r\n\x1a\n")

    monkeypatch.setattr(
        "utils.jira_verification.ask_for_json",
        lambda *_a, **_k: LLMResult(
            success=True,
            data={
                "issue_resolved": True,
                "description": "Looks fixed",
                "expected_behavior": "ok",
                "actual_behavior": "ok",
                "confidence": 0.9,
            },
            raw_text="{}",
            error=None,
        ),
    )

    baseline = {
        "issue_key": "TST-2",
        "summary": "s",
        "description_text": "d",
        "reproduction_steps": [],
        "expected_behavior": "e",
        "actual_behavior": "a",
        "baseline_evidence_paths": [],
    }
    result = verify_against_jira_baseline(cfg, baseline, [str(shot)], "login.yaml")
    assert result.passed
    assert result.issue_resolved
