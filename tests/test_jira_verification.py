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


def test_retest_requires_jira_and_maestro_pass(monkeypatch, tmp_path):
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
    )
    (tmp_path / "after.png").write_bytes(b"\x89PNG\r\n\x1a\n")

    monkeypatch.setattr("nodes.retest_agent.build_app", lambda _c: MagicMock(success=True, artifact_path=tmp_path / "app.apk", error=None))
    monkeypatch.setattr("nodes.retest_agent.select_device", lambda _c: MagicMock(success=True, details={"device_id": "dev-1"}, error=None))
    monkeypatch.setattr("nodes.retest_agent.install_app", lambda *_a, **_k: MagicMock(success=True, error=None))
    monkeypatch.setattr("nodes.retest_agent.run_qa_check", lambda *_a, **_k: pass_finding)
    monkeypatch.setattr(
        "nodes.retest_agent.verify_against_jira_baseline",
        lambda *_a, **_k: JiraVerificationResult(
            passed=False,
            issue_resolved=False,
            description="Still broken per Jira repro",
            expected_behavior="e",
            actual_behavior="still bad",
            confidence=0.8,
        ),
    )

    from state import initial_state

    state = initial_state("sample_android", cfg, mode="real", flow_name="login.yaml")
    state["jira_issue_input"] = True
    state["jira_baseline"] = {"issue_key": "TST-9", "reproduction_steps": ["step"]}
    state["dev_finding"] = {"build": {"success": True}, "files_changed": ["a.kt"]}
    state["attempt_count"] = 1
    state["max_attempts"] = 3

    update = retest_agent(state)
    assert update["passed"] is False
    assert update["status"] == "retest_failed"
    assert "Jira verification failed" in update["qa_finding"]["description"]
    assert update["retest_finding"]["jira_verification"]["passed"] is False


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
