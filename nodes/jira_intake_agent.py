"""Jira intake: start the pipeline from an existing Jira issue instead of Maestro QA."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from state import PipelineState, log_node
from utils.jira import normalize_issue_input
from utils.jira_intake import intake_from_jira
from utils.maestro_flow_gen import DEFAULT_APP_ID, generate_and_save_flow, str_is_clear

_ORCHESTRATOR_ROOT = Path(__file__).resolve().parent.parent


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
    repro = list(finding.get("reproduction_steps") or [])
    # Prefer structured steps from baseline description when intake left placeholders
    baseline = result.jira_baseline or {}
    if not str_is_clear(repro, finding.get("description") or ""):
        # Try extracting from description_text lines
        desc = str(baseline.get("description_text") or "")
        extracted = _extract_steps_from_description(desc)
        if extracted:
            repro = extracted
            finding["reproduction_steps"] = repro
            baseline["reproduction_steps"] = repro

    if not str_is_clear(repro, finding.get("description") or ""):
        finding["failure_type"] = "unverified"
        finding["passed"] = False
        finding["confidence"] = 0.0
        finding["description"] = (
            (finding.get("description") or "")
            + "\n\nUnresolved: reproduction steps are missing or too unclear to generate a Maestro flow."
        ).strip()
        history = log_node(
            "jira_intake_agent",
            "intake_unverified",
            attempt_count,
            detail="STR unclear; stopping without RCA/fix.",
        )
        return {
            "passed": False,
            "status": "intake_unverified",
            "jira_issue_input": True,
            "jira_issue_key": result.issue_key,
            "ticket_id": result.issue_key,
            "ticket_url": result.issue_url,
            "jira_baseline": baseline,
            "qa_finding": finding,
            "screenshots": [p for p in finding.get("evidence_paths") or [] if p.lower().endswith(".png")],
            "before_video_paths": list(finding.get("video_paths") or []),
            "execution_history": [history],
        }

    package_id = DEFAULT_APP_ID
    try:
        package_id = app_config.build.package_id or DEFAULT_APP_ID
    except Exception:
        pass

    flow_gen = generate_and_save_flow(
        result.issue_key,
        repro,
        orchestrator_root=_ORCHESTRATOR_ROOT,
        description=str(finding.get("description") or ""),
        app_id=package_id,
        use_llm=True,
        cwd=result.intake_dir,
    )
    if not flow_gen.success:
        ft = flow_gen.failure_type or "unverified"
        finding["failure_type"] = ft
        finding["confidence"] = 0.0
        finding["description"] = (
            (finding.get("description") or "") + f"\n\nFlow generation failed: {flow_gen.error}"
        ).strip()
        status = "intake_unverified" if ft == "unverified" else "intake_failed"
        history = log_node("jira_intake_agent", status, attempt_count, detail=flow_gen.error or "")
        return {
            "passed": False,
            "status": status,
            "jira_issue_input": True,
            "jira_issue_key": result.issue_key,
            "ticket_id": result.issue_key,
            "ticket_url": result.issue_url,
            "jira_baseline": baseline,
            "qa_finding": finding,
            "last_error": flow_gen.error,
            "screenshots": [p for p in finding.get("evidence_paths") or [] if p.lower().endswith(".png")],
            "before_video_paths": list(finding.get("video_paths") or []),
            "execution_history": [history],
        }

    maestro_flow_path = str(flow_gen.flow_path)
    finding["flow_name"] = f"runs/{result.issue_key}/repro.yaml"
    detail = (
        f"Loaded {result.issue_key} with {len(result.downloaded_files)} attachment(s); "
        f"wrote Maestro flow {maestro_flow_path}."
    )
    history = log_node("jira_intake_agent", "bug_detected", attempt_count, detail=detail)

    return {
        "passed": False,
        "status": "bug_detected",
        "jira_issue_input": True,
        "jira_issue_key": result.issue_key,
        "ticket_id": result.issue_key,
        "ticket_url": result.issue_url,
        "jira_baseline": baseline,
        "qa_finding": finding,
        "flow_name": finding["flow_name"],
        "maestro_flow_path": maestro_flow_path,
        "screenshots": [p for p in finding.get("evidence_paths") or [] if p.lower().endswith(".png")],
        "before_video_paths": list(finding.get("video_paths") or []),
        "execution_history": [history],
    }


def _extract_steps_from_description(description: str) -> list[str]:
    """Best-effort STR lines from a free-form Jira description."""
    lines = []
    for raw in description.splitlines():
        line = raw.strip().lstrip("-*").strip()
        if not line:
            continue
        lower = line.lower()
        if lower.startswith("steps to reproduce") or lower.startswith("reproduction"):
            continue
        if lower.startswith("expected") or lower.startswith("actual"):
            break
        # Numbered steps
        if line[:2].isdigit() or (len(line) > 2 and line[0].isdigit() and line[1] in ".)"):
            line = line.lstrip("0123456789.) ").strip()
        if len(line) >= 8:
            lines.append(line)
    return lines[:20]
