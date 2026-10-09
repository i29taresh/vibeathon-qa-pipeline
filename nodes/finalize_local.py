"""Finalize a successful Jira POC run: local commit + maven cleanup + history (no push/MR)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from config import AppConfig
from state import PipelineState, log_node
from utils import jira as jira_api
from utils.git_workspace import commit_paths, current_branch
from utils.maven_local import cleanup_maven_local
from utils.project_context import resolve_fix_app_config
from utils.run_history import save_run


def finalize_local(state: PipelineState) -> dict:
    if state.get("mode", "mock") == "mock":
        return _run_mock(state)
    return _run_real(state)


def _run_mock(state: PipelineState) -> dict:
    attempt_count = state.get("attempt_count", 0)
    history = log_node("finalize_local", "ready_for_review", attempt_count, detail="Mock local finalize.")
    return {
        "status": "ready_for_review",
        "passed": True,
        "finalize_finding": {"mock": True, "committed": True},
        "merge_finding": {"mock": True, "committed": True},
        "execution_history": [history],
    }


def _run_real(state: PipelineState) -> dict:
    attempt_count = state.get("attempt_count", 0)
    verifier = state.get("verifier_finding") or {}
    retest = state.get("retest_finding") or {}
    dev_finding = state.get("dev_finding") or {}

    # Prefer explicit verifier_finding; fall back to retest embedding
    issue_resolved = verifier.get("issue_resolved")
    if issue_resolved is None:
        jv = retest.get("jira_verification") or retest.get("verifier") or {}
        issue_resolved = jv.get("issue_resolved") or jv.get("passed")
    verifier_ok = bool(verifier.get("passed") or issue_resolved)

    if not state.get("passed") or state.get("status") != "retest_passed":
        return _blocked(state, "Retest has not passed; refusing to finalize.")
    if not verifier_ok:
        return _blocked(state, "Verifier did not confirm issue_resolved; refusing to commit.")

    files_changed = dev_finding.get("files_changed") or []
    paths = []
    for entry in files_changed:
        parts = str(entry).split(" ", 1)
        paths.append(parts[1].strip() if len(parts) == 2 else str(entry).strip())
    paths = [p for p in paths if p]
    if not paths:
        return _blocked(state, "dev_finding has no files_changed to commit.")

    branch = str(dev_finding.get("branch") or "")
    if not branch.startswith("ai-fix/"):
        return _blocked(state, f"Expected ai-fix/* branch, got {branch!r}.")

    _, fix_config = resolve_fix_app_config(state)
    root: Path = fix_config.clone_path
    on_branch = current_branch(root)
    if on_branch != branch:
        return _blocked(state, f"Checkout is on {on_branch!r}, expected {branch!r}.")

    ticket = state.get("ticket_id") or state.get("jira_issue_key") or "ticket"
    message = f"fix: {ticket} automated POC fix (local only)"
    ok, sha, err = commit_paths(root, message, paths)
    if not ok:
        return _blocked(state, f"git commit failed: {err}")

    # Maven cleanup
    project_graph = state.get("project_graph") or {}
    b2b = project_graph.get("b2b_android") or project_graph.get("b2b-android")
    if b2b is None and isinstance(state.get("app_config"), AppConfig):
        # primary may be b2b
        if getattr(state["app_config"], "name", "").startswith("b2b"):
            b2b = state["app_config"]
    cleanup_info = cleanup_maven_local(
        state.get("maven_local_version"),
        b2b if isinstance(b2b, AppConfig) else None,
        state.get("payzy_shared_pin_original"),
    )

    recordings = [
        Path(p)
        for p in (state.get("after_video_paths") or [])
        if p and Path(p).is_file()
    ]

    finalize_finding: dict[str, Any] = {
        "committed": True,
        "branch": branch,
        "commit_sha": sha,
        "files_changed": paths,
        "maven_cleanup": cleanup_info,
        "recording_paths": [str(p) for p in recordings],
        "pushed": False,
        "mr_created": False,
    }

    # Jira comment: verifier passed → attach recording paths + mention fix branch.
    jira_key = str(state.get("jira_issue_key") or state.get("ticket_id") or "")
    if jira_key and state.get("jira_issue_input"):
        body = (
            f"Automated retest verifier: issue resolved.\n\n"
            f"Fix branch: `{branch}`\n"
            f"Local commit: `{sha}`\n"
        )
        if recordings:
            body += "\nAfter-fix screen recording(s):\n"
            for rec in recordings:
                body += f"- `{rec}`\n"
        try:
            comment = jira_api.comment_on_issue(jira_key, body, attachments=recordings or None)
            finalize_finding["jira_comment"] = {
                "success": comment.success,
                "error": comment.error,
                "issue_key": jira_key,
            }
        except Exception as exc:  # noqa: BLE001 — never fail finalize on comment
            finalize_finding["jira_comment"] = {"success": False, "error": str(exc)}

    # Persist history
    run_state = dict(state)
    run_state["finalize_finding"] = finalize_finding
    run_state["merge_finding"] = finalize_finding
    try:
        run_dir = save_run(run_state)
        run_history_id = run_dir.name
        finalize_finding["run_dir"] = str(run_dir)
    except OSError as exc:
        run_history_id = None
        finalize_finding["history_error"] = str(exc)

    detail = f"Committed {sha} on {branch}; recording attached to Jira comment; maven cleaned; no push/MR."
    history = log_node("finalize_local", "ready_for_review", attempt_count, detail=detail)
    out = {
        "status": "ready_for_review",
        "passed": True,
        "pr_url": None,
        "pr_number": None,
        "finalize_finding": finalize_finding,
        "merge_finding": finalize_finding,
        "maven_local_version": None,
        "payzy_shared_pin_original": None,
        "execution_history": [history],
    }
    if run_history_id:
        out["run_history_id"] = run_history_id
    return out


def _blocked(state: PipelineState, reason: str) -> dict:
    attempt_count = state.get("attempt_count", 0)
    history = log_node("finalize_local", "merge_blocked", attempt_count, detail=reason)
    return {
        "status": "merge_blocked",
        "last_error": reason,
        "finalize_finding": {"error": reason, "committed": False},
        "merge_finding": {"error": reason, "committed": False},
        "execution_history": [history],
    }
