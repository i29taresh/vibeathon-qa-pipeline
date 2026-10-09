"""Jira intake: start the pipeline from an existing Jira issue instead of Maestro QA."""

from __future__ import annotations

from dataclasses import asdict

from state import PipelineState, log_node
from utils.jira import normalize_issue_input
from utils.jira_intake import intake_from_jira


def jira_intake_agent(state: PipelineState) -> dict:
    if state.get("mode", "mock") == "mock":
        return _run_mock(state)
    return _run_real(state)


def _run_mock(state: PipelineState) -> dict:
    issue_key = state.get("jira_issue_key") or "MOCK-1"
    attempt_count = state.get("attempt_count", 0)
    flow_name = state.get("flow_name", "default_flow")
    detail = f"Mock Jira intake for {issue_key} (no attachments downloaded)."
    history = log_node("jira_intake_agent", "bug_detected", attempt_count, detail=detail)
    qa_finding = {
        "flow_name": flow_name,
        "passed": False,
        "failure_type": "functional",
        "description": f"Mock defect from Jira {issue_key}",
        "expected_behavior": "Expected behavior per Jira ticket.",
        "actual_behavior": "Actual behavior per Jira ticket.",
        "confidence": 0.5,
        "reproduction_steps": ["See Jira issue."],
        "evidence_paths": [],
    }
    return {
        "passed": False,
        "status": "bug_detected",
        "jira_issue_input": True,
        "ticket_id": issue_key,
        "ticket_url": f"https://jira.example/browse/{issue_key}",
        "jira_baseline": {
            "issue_key": issue_key,
            "issue_url": f"https://jira.example/browse/{issue_key}",
            "summary": f"Mock Jira {issue_key}",
            "description_text": "",
            "reproduction_steps": qa_finding["reproduction_steps"],
            "expected_behavior": qa_finding["expected_behavior"],
            "actual_behavior": qa_finding["actual_behavior"],
            "baseline_evidence_paths": [],
            "issue_description": qa_finding["description"],
        },
        "qa_finding": qa_finding,
        "execution_history": [history],
    }


def _run_real(state: PipelineState) -> dict:
    app_config = state["app_config"]
    attempt_count = state.get("attempt_count", 0)
    flow_name = state.get("flow_name", "default_flow")
    raw_key = state.get("jira_issue_key") or ""
    issue_key = normalize_issue_input(raw_key) or raw_key.strip()

    print(f"\n[jira_intake_agent] Loading Jira issue {issue_key!r} for '{app_config.name}'...")

    if not issue_key:
        history = log_node(
            "jira_intake_agent",
            "intake_failed",
            attempt_count,
            detail="No jira_issue_key in pipeline state.",
        )
        return {
            "status": "intake_failed",
            "last_error": "jira_issue_key is required for Jira intake mode.",
            "execution_history": [history],
        }

    # Jira summary/description are already structured. A Cursor pass here only
    # rephrases them and was the multi-minute stall (agent startup + image read).
    # RCA still reads the downloaded evidence.
    result = intake_from_jira(app_config, issue_key, flow_name, use_llm=False)
    if not result.success or result.finding is None:
        err = result.error or "Jira intake failed."
        history = log_node("jira_intake_agent", "intake_failed", attempt_count, detail=err)
        return {
            "status": "intake_failed",
            "last_error": err,
            "execution_history": [history],
        }

    finding = asdict(result.finding)
    detail = (
        f"Loaded {result.issue_key} with {len(result.downloaded_files)} attachment(s) "
        f"and {len(finding.get('evidence_paths') or [])} evidence file(s)."
    )
    history = log_node("jira_intake_agent", "bug_detected", attempt_count, detail=detail)

    return {
        "passed": False,
        "status": "bug_detected",
        "jira_issue_input": True,
        "jira_issue_key": result.issue_key,
        "ticket_id": result.issue_key,
        "ticket_url": result.issue_url,
        "jira_baseline": result.jira_baseline,
        "qa_finding": finding,
        "screenshots": [p for p in finding.get("evidence_paths") or [] if p.lower().endswith(".png")],
        "execution_history": [history],
    }
