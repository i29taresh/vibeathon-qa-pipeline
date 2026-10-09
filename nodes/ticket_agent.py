"""Ticket agent: files (or updates) a GitHub Issue for a confirmed QA defect.

Two modes, selected by `state["mode"]` (default "mock", set by
state.initial_state) - same pattern as nodes/qa_agent.py and
nodes/rca_agent.py:

- "mock" - the Phase 3 placeholder, unchanged.
- "real" - builds a structured bug report from state["qa_finding"] and
  state["rca_finding"] and files/updates a real GitHub Issue via
  utils/github.py, against `app_config.repo` (never a hardcoded repo).

Rules this node enforces:
- No ticket for a QA result that isn't a confirmed app defect: `failure_type`
  "none" (passed), "unverified" (no acceptance criteria), or "infrastructure"
  (unless `state["file_ticket_for_infrastructure"]` is explicitly set) all
  skip ticket creation rather than mislabeling something as an app bug.
- Retries never post comments (docile mode): when a ticket already exists the
  node reuses `ticket_id` and keeps the rendered follow-up body under
  `ticket_finding`, but does not call GitHub/Jira comment APIs.
- `state["dry_run"]=True` renders the full title/body and returns it under
  `state["ticket_finding"]` without ever calling `gh`.
- Any `gh`/GitHub failure (auth, permissions, API error) is reported via
  `status="ticket_failed"` + `last_error`, but the rendered report is still
  returned in `ticket_finding["body"]` and `qa_finding`/`rca_finding` are
  never touched - nothing gathered so far is lost just because publishing
  failed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from config import AppConfig, uses_jira_integration
from state import PipelineState, log_node
from utils import jira as jira_api
from utils.github import check_auth, create_issue


def ticket_agent(state: PipelineState) -> dict:
    if state.get("mode", "mock") == "mock":
        return _run_mock(state)
    return _run_real(state)


# --------------------------------------------------------------------------
# Mock mode (Phase 3 - unchanged)
# --------------------------------------------------------------------------

def _run_mock(state: PipelineState) -> dict:
    print("\n[ticket_agent] Filing ticket (mock)...")

    attempt_count = state.get("attempt_count", 0)
    existing_ticket_id = state.get("ticket_id")

    if existing_ticket_id:
        ticket_id = existing_ticket_id
        detail = f"Reused existing ticket {ticket_id} (no duplicate filed)."
    else:
        ticket_id = f"MOCK-{state.get('app_name', 'app').upper()}-1"
        detail = f"Filed new mock ticket {ticket_id}."

    status = "ticket_ready"
    history = log_node("ticket_agent", status, attempt_count, detail=detail)

    return {
        "ticket_id": ticket_id,
        "status": status,
        "execution_history": [history],
    }


# --------------------------------------------------------------------------
# Real mode
# --------------------------------------------------------------------------

def _run_real(state: PipelineState) -> dict:
    app_config: AppConfig = state["app_config"]
    attempt_count = state.get("attempt_count", 0)
    qa_finding = state.get("qa_finding") or {}
    rca_finding = state.get("rca_finding") or {}
    existing_ticket_id = state.get("ticket_id")
    dry_run = bool(state.get("dry_run", False))
    file_ticket_for_infrastructure = bool(state.get("file_ticket_for_infrastructure", False))

    print(f"\n[ticket_agent] Preparing ticket for '{app_config.name}' ({app_config.platform})...")

    skip_reason = _skip_reason(qa_finding.get("failure_type"), file_ticket_for_infrastructure)
    if skip_reason:
        return _finalize_skip(skip_reason, existing_ticket_id, attempt_count)

    jira_issue_input = bool(state.get("jira_issue_input"))
    if jira_issue_input and attempt_count == 0:
        reason = (
            f"Pipeline started from existing Jira issue {existing_ticket_id} - "
            "skipping duplicate issue creation."
        )
        return _finalize_skip(reason, existing_ticket_id, attempt_count)

    if jira_issue_input:
        is_retry = attempt_count > 0 and bool(existing_ticket_id)
    else:
        is_retry = bool(existing_ticket_id)
    severity = _derive_severity(qa_finding)
    title = _build_title(app_config, qa_finding, severity)
    body = (
        _build_followup_comment(qa_finding, rca_finding, attempt_count)
        if is_retry
        else _build_issue_body(app_config, state, qa_finding, rca_finding, severity, attempt_count)
    )
    attachments = _gather_attachments(qa_finding)

    if dry_run:
        return _finalize_dry_run(title, body, attachments, existing_ticket_id, attempt_count, is_retry)

    # Docile mode: never post retry comments; keep ticket_id and the rendered body locally.
    if is_retry:
        return _finalize_comment_suppressed(title, body, attachments, existing_ticket_id, attempt_count)

    cwd = app_config.clone_path if app_config.clone_path.is_dir() else Path.cwd()

    if uses_jira_integration(app_config):
        return _publish_jira(
            app_config, title, body, attachments, existing_ticket_id, attempt_count, state
        )

    auth_status = check_auth(app_config.repo, cwd)
    if not auth_status.authenticated:
        return _finalize_failure(
            f"GitHub CLI is not authenticated: {auth_status.error}", title, body, attachments, existing_ticket_id, attempt_count
        )
    if auth_status.can_write is False:
        return _finalize_failure(
            f"GitHub token lacks write access to '{app_config.repo}' ({auth_status.detail}).",
            title, body, attachments, existing_ticket_id, attempt_count,
        )

    result = create_issue(app_config.repo, title, body, cwd=cwd, labels=_derive_labels(qa_finding), attachments=attachments)

    if not result.success:
        return _finalize_failure(result.error or "gh command failed.", title, body, attachments, existing_ticket_id, attempt_count)

    ticket_id = str(result.number) if result.number is not None else existing_ticket_id
    ticket_url = result.url or state.get("ticket_url")
    status = "ticket_ready"
    detail = f"Filed new GitHub issue {app_config.repo}#{ticket_id} ({ticket_url})."
    history = log_node("ticket_agent", status, attempt_count, detail=detail)

    return {
        "ticket_id": ticket_id,
        "ticket_url": ticket_url,
        "status": status,
        "last_error": None,
        "ticket_finding": {
            "repo": app_config.repo,
            "issue_number": result.number,
            "issue_url": ticket_url,
            "created": True,
            "dry_run": False,
            "title": title,
            "body": body,
        },
        "execution_history": [history],
    }


def _publish_jira(
    app_config: AppConfig,
    title: str,
    body: str,
    attachments: list[Path],
    existing_ticket_id: Optional[str],
    attempt_count: int,
    state: PipelineState,
) -> dict:
    jira = app_config.integrations.jira
    assert jira is not None
    cwd = app_config.clone_path if app_config.clone_path.is_dir() else Path.cwd()
    auth_status = jira_api.check_auth(jira.base_url, jira.project_key, cwd=cwd)
    if not auth_status.authenticated:
        return _finalize_failure(
            f"Jira is not authenticated: {auth_status.error}",
            title,
            body,
            attachments,
            existing_ticket_id,
            attempt_count,
        )
    if auth_status.can_write is False:
        return _finalize_failure(
            f"Jira token lacks access to project '{jira.project_key}' ({auth_status.detail}).",
            title,
            body,
            attachments,
            existing_ticket_id,
            attempt_count,
        )

    # Retries are handled before _publish_jira (comment APIs are disabled).
    result = jira_api.create_issue(
        jira.base_url,
        jira.project_key,
        title,
        body,
        issue_type=jira.issue_type,
        cwd=cwd,
        attachments=attachments,
    )

    if not result.success:
        return _finalize_failure(
            result.error or "Jira API call failed.",
            title,
            body,
            attachments,
            existing_ticket_id,
            attempt_count,
        )

    ticket_id = result.key or existing_ticket_id
    ticket_url = result.url or state.get("ticket_url")
    status = "ticket_ready"
    detail = f"Filed new Jira issue {ticket_id} ({ticket_url})."
    history = log_node("ticket_agent", status, attempt_count, detail=detail)
    return {
        "ticket_id": ticket_id,
        "ticket_url": ticket_url,
        "status": status,
        "last_error": None,
        "ticket_finding": {
            "tracker": "jira",
            "project_key": jira.project_key,
            "issue_key": ticket_id,
            "issue_url": ticket_url,
            "created": True,
            "dry_run": False,
            "title": title,
            "body": body,
        },
        "execution_history": [history],
    }


def _skip_reason(failure_type: Optional[str], file_ticket_for_infrastructure: bool) -> Optional[str]:
    if failure_type is None:
        return "No QA finding was available - nothing to file a ticket for."
    if failure_type == "none":
        return "QA passed - no defect to file."
    if failure_type == "unverified":
        return "QA result was unverified (no acceptance criteria) - no confirmed defect to file a ticket for."
    if failure_type == "infrastructure" and not file_ticket_for_infrastructure:
        return (
            "QA failure was classified as infrastructure, not an application defect - skipping ticket "
            "creation (set state['file_ticket_for_infrastructure']=True to file one anyway)."
        )
    return None


def _finalize_skip(reason: str, existing_ticket_id: Optional[str], attempt_count: int) -> dict:
    status = "ticket_skipped"
    history = log_node("ticket_agent", status, attempt_count, detail=reason)
    return {
        "ticket_id": existing_ticket_id,
        "status": status,
        "ticket_finding": {"skipped": True, "reason": reason},
        "execution_history": [history],
    }


def _finalize_comment_suppressed(
    title: str,
    body: str,
    attachments: list[Path],
    existing_ticket_id: Optional[str],
    attempt_count: int,
) -> dict:
    """Reuse the existing ticket without posting a follow-up comment (docile mode)."""
    reason = (
        f"Reused existing ticket {existing_ticket_id} without posting a comment "
        "(ticket_agent comments are disabled)."
    )
    status = "ticket_skipped"
    history = log_node("ticket_agent", status, attempt_count, detail=reason)
    return {
        "ticket_id": existing_ticket_id,
        "status": status,
        "ticket_finding": {
            "skipped": True,
            "reason": reason,
            "comment_suppressed": True,
            "title": title,
            "body": body,
            "attachments": [str(p) for p in attachments],
        },
        "execution_history": [history],
    }


def _finalize_dry_run(
    title: str, body: str, attachments: list[Path], existing_ticket_id: Optional[str], attempt_count: int, is_retry: bool
) -> dict:
    if is_retry:
        action = "reuse the existing issue without commenting (comments disabled)"
    else:
        action = "create a new issue"
    status = "ticket_dry_run"
    detail = f"Dry run: would {action} (not published)."
    history = log_node("ticket_agent", status, attempt_count, detail=detail)
    return {
        "ticket_id": existing_ticket_id,
        "status": status,
        "ticket_finding": {
            "dry_run": True,
            "action": action,
            "title": title,
            "body": body,
            "attachments": [str(p) for p in attachments],
        },
        "execution_history": [history],
    }


def _finalize_failure(
    error: str, title: str, body: str, attachments: list[Path], existing_ticket_id: Optional[str], attempt_count: int
) -> dict:
    status = "ticket_failed"
    history = log_node("ticket_agent", status, attempt_count, detail=error)
    return {
        "ticket_id": existing_ticket_id,
        "status": status,
        "last_error": error,
        # The rendered report is preserved here even though publishing
        # failed, so nothing gathered by qa_agent/rca_agent is lost - a
        # retry or a human can still use title/body directly.
        "ticket_finding": {
            "error": error,
            "title": title,
            "body": body,
            "attachments": [str(p) for p in attachments],
        },
        "execution_history": [history],
    }


# --------------------------------------------------------------------------
# Report construction
# --------------------------------------------------------------------------

def _derive_severity(qa_finding: dict[str, Any]) -> str:
    """Simple, deterministic severity heuristic - never invented, just rule-based on failure_type."""
    failure_type = qa_finding.get("failure_type")
    if failure_type == "functional":
        return "high"
    if failure_type == "visual":
        return "low"
    return "medium"


def _derive_labels(qa_finding: dict[str, Any]) -> list[str]:
    labels = ["qa-pipeline", f"severity:{_derive_severity(qa_finding)}"]
    failure_type = qa_finding.get("failure_type")
    if failure_type:
        labels.append(f"type:{failure_type}")
    return labels


def _gather_attachments(qa_finding: dict[str, Any]) -> list[Path]:
    """Screenshots/JUnit reports/video the QA run actually produced - skips anything no longer on disk."""
    paths = []
    for raw_path in qa_finding.get("evidence_paths") or []:
        path = Path(raw_path)
        if path.is_file():
            paths.append(path)
    return paths


def _build_title(app_config: AppConfig, qa_finding: dict[str, Any], severity: str) -> str:
    flow_name = qa_finding.get("flow_name", "flow")
    description = str(qa_finding.get("description") or "QA failure")
    short_description = description if len(description) <= 80 else description[:77] + "..."
    return f"[{severity.upper()}] {app_config.name}: {flow_name} - {short_description}"


def _build_issue_body(
    app_config: AppConfig,
    state: PipelineState,
    qa_finding: dict[str, Any],
    rca_finding: dict[str, Any],
    severity: str,
    attempt_count: int,
) -> str:
    app_version = state.get("app_version") or "unknown (not provided)"

    repro_steps = qa_finding.get("reproduction_steps") or []
    repro_section = "\n".join(f"{i}. {step}" for i, step in enumerate(repro_steps, start=1)) or "Not provided."

    lines = [
        "## Summary",
        str(qa_finding.get("description") or "No description provided."),
        "",
        f"**Severity:** {severity}",
        f"**Platform:** {app_config.platform}",
        f"**App version:** {app_version}",
        "",
        "## Reproduction Steps",
        repro_section,
        "",
        "## Expected vs Actual Behavior",
        f"**Expected:** {qa_finding.get('expected_behavior', 'Not provided.')}",
        f"**Actual:** {qa_finding.get('actual_behavior', 'Not provided.')}",
        "",
        "## Maestro Flow",
        f"**Flow:** {qa_finding.get('flow_name', 'unknown')}",
        f"**Failed assertion / finding:** {qa_finding.get('description', 'Not provided.')}",
        "",
        "## Root Cause Analysis",
    ]

    target_app = rca_finding.get("target_app")
    if target_app:
        lines += ["", f"**Fix target repository:** `{target_app}`"]

    if rca_finding:
        suspected_files = rca_finding.get("suspected_files") or []
        suspected_methods = rca_finding.get("suspected_methods") or []
        lines += [
            f"**Hypothesis:** {rca_finding.get('root_cause_hypothesis', 'Not available.')}",
            "**Suspected files:** " + (", ".join(f"`{f}`" for f in suspected_files) or "none identified"),
            "**Suspected methods:** " + (", ".join(f"`{m}`" for m in suspected_methods) or "none identified"),
            f"**Suggested fix:** {rca_finding.get('suggested_fix') or 'Not provided.'}",
        ]
    else:
        lines.append("Not available - RCA did not run or produced no result.")

    missing_information = rca_finding.get("missing_information") or []
    missing_lines = [f"- {item}" for item in missing_information] or ["- None noted."]

    lines += [
        "",
        "## Confidence",
        f"**QA confidence:** {qa_finding.get('confidence', 'unknown')}",
        f"**RCA confidence:** {rca_finding.get('confidence', 'unknown')}",
        "",
        "## Unresolved Questions",
        *missing_lines,
        "",
        "---",
        f"*Filed automatically by the QA pipeline (attempt #{attempt_count}).*",
    ]

    return "\n".join(lines)


def _build_followup_comment(qa_finding: dict[str, Any], rca_finding: dict[str, Any], attempt_count: int) -> str:
    missing_information = rca_finding.get("missing_information") or []
    missing_lines = [f"- {item}" for item in missing_information] or ["- None noted."]

    lines = [
        f"## Retry update - attempt #{attempt_count}",
        "",
        f"**QA result:** {qa_finding.get('description', 'Not provided.')}",
        f"**Actual behavior:** {qa_finding.get('actual_behavior', 'Not provided.')}",
        "",
        f"**RCA hypothesis:** {rca_finding.get('root_cause_hypothesis', 'Not available.')}",
        f"**RCA confidence:** {rca_finding.get('confidence', 'unknown')}",
        "",
        "**Unresolved questions:**",
        *missing_lines,
    ]
    return "\n".join(lines)
