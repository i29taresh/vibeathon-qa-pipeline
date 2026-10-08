"""Merge step: opens a Pull Request for a validated fix - never merges it.

Despite the historical node name, this step creates a PR for human review;
it never auto-merges, and never pushes directly to `main`/`master`.

Two modes, selected by `state["mode"]` (default "mock", set by
state.initial_state) - same pattern as the other real agents:

- "mock" - the Phase 3 placeholder, unchanged.
- "real" - the workflow below, against `app_config.repo` (never a
  hardcoded repository).

Safety model:
- A long list of preconditions (retest passed, build/tests passed, a real
  patch with changed files exists, the branch is an `ai-fix/*` branch) must
  all hold before this node touches git or GitHub at all - any failure
  blocks with `status="merge_blocked"` and a specific reason, never a
  generic error.
- Before pushing, the branch's actual committed diff (`git diff
  base...HEAD`) is compared against what dev_agent reported changing - any
  extra file blocks the PR, since the branch must contain only the
  intended changes.
- This node never commits unknown content itself: if the working tree is
  dirty at merge time, that's unexpected (dev_agent should already have
  committed everything) and blocks rather than silently folding unvalidated
  changes into the PR.
- `git push` never gets `--force`, and this node refuses outright to push
  anything named `main`/`master`.
- If the branch carries risk flags from dev_agent (deletion, CI/workflow
  config, a secrets-looking file), pushing requires a *second*, explicit
  `state["approve_push"]=True` - approving the local commit
  (`approve_destructive_changes`) and approving pushing it to a shared
  remote are kept as separate gates.
- Duplicate PRs are avoided two ways: a fast in-state check
  (`state["pr_url"]` already set) and a check against GitHub itself
  (`utils.github.get_pull_request_for_branch`) before creating one.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from config import AppConfig
from state import PipelineState, log_node
from utils.github import check_auth, create_pull_request, get_pull_request_for_branch
from utils.runner import CommandResult, RunnerError, run_command
from utils.secrets import redact

_BASE_BRANCH_NAMES = ("main", "master")


def merge_step(state: PipelineState) -> dict:
    if state.get("mode", "mock") == "mock":
        return _run_mock(state)
    return _run_real(state)


# --------------------------------------------------------------------------
# Mock mode (Phase 3 - unchanged)
# --------------------------------------------------------------------------

def _run_mock(state: PipelineState) -> dict:
    print("\n[merge_step] Preparing PR for review (mock)...")

    attempt_count = state.get("attempt_count", 0)
    status = "ready_for_review"
    detail = f"Mock merge step: would open a PR for ticket {state.get('ticket_id')} (not auto-merged)."
    history = log_node("merge_step", status, attempt_count, detail=detail)

    return {
        "status": status,
        "execution_history": [history],
    }


# --------------------------------------------------------------------------
# Real mode
# --------------------------------------------------------------------------

def _run_real(state: PipelineState) -> dict:
    app_config: AppConfig = state["app_config"]
    attempt_count = state.get("attempt_count", 0)
    dev_finding = state.get("dev_finding") or {}
    retest_finding = state.get("retest_finding") or {}
    rca_finding = state.get("rca_finding") or {}
    qa_finding = state.get("qa_finding") or {}
    ticket_id = state.get("ticket_id")

    print(f"\n[merge_step] Preparing PR for '{app_config.name}' ({app_config.platform})...")

    existing_pr_url = state.get("pr_url")
    if existing_pr_url:
        return _finalize_existing(existing_pr_url, state.get("pr_number"), attempt_count)

    precondition_error = _check_preconditions(state, dev_finding, retest_finding)
    if precondition_error:
        return _finalize_blocked(precondition_error, attempt_count)

    branch = dev_finding["branch"]
    base_branch = dev_finding["base_branch"]
    root = app_config.clone_path

    drift_error = _check_for_branch_drift(root, base_branch, dev_finding)
    if drift_error:
        return _finalize_blocked(drift_error, attempt_count)

    dirty_error = _check_clean_tree(root)
    if dirty_error:
        return _finalize_blocked(dirty_error, attempt_count)

    auth_status = check_auth(app_config.repo, root)
    if not auth_status.authenticated:
        return _finalize_blocked(f"GitHub CLI is not authenticated: {auth_status.error}", attempt_count)
    if auth_status.can_write is False:
        return _finalize_blocked(
            f"GitHub token lacks write access to '{app_config.repo}' ({auth_status.detail}).", attempt_count
        )

    # Dedup via GitHub itself, in case a PR already exists for this branch
    # from outside this process/state (e.g. a prior run that crashed after
    # creating the PR but before saving pr_url).
    existing = get_pull_request_for_branch(app_config.repo, branch, root)
    if existing.success and existing.url:
        return _finalize_existing(existing.url, existing.number, attempt_count)

    title, body = _build_pr_content(qa_finding, rca_finding, dev_finding, retest_finding, ticket_id)
    dry_run = bool(state.get("dry_run", False))

    if dry_run:
        return _finalize_dry_run(title, body, branch, base_branch, attempt_count)

    risk_flags = dev_finding.get("risk_flags") or []
    if risk_flags and not state.get("approve_push", False):
        reason = (
            "Branch contains changes flagged for approval at commit time ("
            + "; ".join(risk_flags)
            + "); pushing requires state['approve_push']=True."
        )
        return _finalize_blocked(reason, attempt_count)

    push_error = _push_branch(root, branch)
    if push_error:
        return _finalize_blocked(push_error, attempt_count)

    draft = bool(state.get("draft_pr", True))
    result = create_pull_request(app_config.repo, title, body, head=branch, base=base_branch, cwd=root, draft=draft)
    if not result.success:
        return _finalize_blocked(f"Could not create pull request: {result.error}", attempt_count)

    status = "ready_for_review"
    detail = f"Opened PR {result.url} for ticket {ticket_id} (not merged)."
    history = log_node("merge_step", status, attempt_count, detail=detail)

    return {
        "status": status,
        "last_error": None,
        "pr_url": result.url,
        "pr_number": result.number,
        "merge_finding": {
            "pr_url": result.url,
            "pr_number": result.number,
            "branch": branch,
            "base_branch": base_branch,
            "title": title,
            "body": body,
            "draft": draft,
        },
        "execution_history": [history],
    }


def _finalize_existing(pr_url: str, pr_number: Optional[int], attempt_count: int) -> dict:
    status = "ready_for_review"
    detail = f"Pull request already exists: {pr_url} (not creating a duplicate)."
    history = log_node("merge_step", status, attempt_count, detail=detail)
    return {
        "status": status,
        "last_error": None,
        "pr_url": pr_url,
        "pr_number": pr_number,
        "merge_finding": {"pr_url": pr_url, "pr_number": pr_number, "duplicate_prevented": True},
        "execution_history": [history],
    }


def _finalize_blocked(reason: str, attempt_count: int) -> dict:
    status = "merge_blocked"
    history = log_node("merge_step", status, attempt_count, detail=reason)
    return {
        "status": status,
        "last_error": reason,
        "merge_finding": {"error": reason},
        "execution_history": [history],
    }


def _finalize_dry_run(title: str, body: str, branch: str, base_branch: str, attempt_count: int) -> dict:
    status = "merge_dry_run"
    detail = f"Dry run: would push '{branch}' and open a PR against '{base_branch}' (not published)."
    history = log_node("merge_step", status, attempt_count, detail=detail)
    return {
        "status": status,
        "merge_finding": {
            "dry_run": True,
            "title": title,
            "body": body,
            "branch": branch,
            "base_branch": base_branch,
        },
        "execution_history": [history],
    }


# --------------------------------------------------------------------------
# Preconditions
# --------------------------------------------------------------------------

def _check_preconditions(state: PipelineState, dev_finding: dict[str, Any], retest_finding: dict[str, Any]) -> Optional[str]:
    if not state.get("passed") or state.get("status") != "retest_passed":
        return "Retest Agent has not passed - refusing to open a PR."

    if not dev_finding or dev_finding.get("error"):
        return "No valid patch from Dev Agent - refusing to open a PR."

    if not dev_finding.get("files_changed"):
        return "Dev Agent recorded no changed files - nothing to open a PR for."

    build_info = dev_finding.get("build") or {}
    if not build_info.get("success"):
        return "Dev Agent's build did not succeed - refusing to open a PR."

    tests_info = dev_finding.get("tests") or {}
    if tests_info.get("ran") and not tests_info.get("success"):
        return "Dev Agent's automated tests failed - refusing to open a PR."

    retest_build_info = retest_finding.get("build") or {}
    if not retest_build_info.get("success"):
        return "Retest Agent's build did not succeed - refusing to open a PR."

    if not retest_finding.get("original_defect_resolved"):
        return "Retest Agent did not confirm the original defect was resolved - refusing to open a PR."

    if retest_finding.get("new_regressions"):
        regressions = ", ".join(retest_finding["new_regressions"])
        return f"Retest Agent detected new regression(s) ({regressions}) - refusing to open a PR."

    branch = dev_finding.get("branch")
    if not branch or not branch.startswith("ai-fix/"):
        return f"Working branch '{branch}' is not an approved ai-fix/* branch - refusing to open a PR."

    return None


# --------------------------------------------------------------------------
# Git helpers (all read-only or scoped to pushing the fix branch itself)
# --------------------------------------------------------------------------

def _run_git(argv: list[str], root: Path, timeout: float = 30.0) -> CommandResult:
    try:
        return run_command(argv, cwd=root, timeout=timeout)
    except RunnerError as exc:
        return CommandResult(argv=argv, returncode=-1, stdout="", stderr=str(exc))


def _check_for_branch_drift(root: Path, base_branch: str, dev_finding: dict[str, Any]) -> Optional[str]:
    """Confirm the committed diff matches what dev_agent reported changing."""
    expected_files = {
        entry.split(" ", 1)[1] for entry in dev_finding.get("files_changed", []) if " " in entry
    }

    result = _run_git(["git", "diff", "--name-only", f"{base_branch}...HEAD"], root)
    if not result.ok:
        return f"Could not inspect the fix branch's diff: {redact(result.stderr.strip())}"

    actual_files = {line.strip() for line in result.stdout.splitlines() if line.strip()}
    unexpected = actual_files - expected_files
    if unexpected:
        return (
            "The fix branch contains changes beyond what Dev Agent reported ("
            + ", ".join(sorted(unexpected))
            + ") - refusing to open a PR until this is reviewed."
        )
    return None


def _check_clean_tree(root: Path) -> Optional[str]:
    result = _run_git(["git", "status", "--porcelain"], root)
    if not result.ok:
        return f"Could not check the working tree: {redact(result.stderr.strip())}"
    if result.stdout.strip():
        return "Working tree has uncommitted changes at merge time - refusing to commit unvalidated content."
    return None


def _push_branch(root: Path, branch: str) -> Optional[str]:
    if branch in _BASE_BRANCH_NAMES:
        return f"Refusing to push directly to '{branch}' - this must be a dedicated ai-fix/* branch."

    remote_check = _run_git(["git", "remote", "get-url", "origin"], root, timeout=15.0)
    if not remote_check.ok:
        return f"No 'origin' remote configured - cannot push: {redact(remote_check.stderr.strip())}"

    push_result = _run_git(["git", "push", "origin", branch], root, timeout=120.0)
    if not push_result.ok:
        return f"Could not push branch '{branch}': {redact(push_result.stderr.strip())}"
    return None


# --------------------------------------------------------------------------
# PR content
# --------------------------------------------------------------------------

def _build_pr_content(
    qa_finding: dict[str, Any],
    rca_finding: dict[str, Any],
    dev_finding: dict[str, Any],
    retest_finding: dict[str, Any],
    ticket_id: Optional[str],
) -> tuple[str, str]:
    flow_name = qa_finding.get("flow_name", "unknown")
    raw_title = f"Fix: {qa_finding.get('description') or flow_name}"
    title = raw_title if len(raw_title) <= 80 else raw_title[:77] + "..."

    lines: list[str] = []
    if ticket_id:
        lines += [f"Fixes #{ticket_id}", ""]

    lines += [
        "## Root Cause",
        str(rca_finding.get("root_cause_hypothesis") or "Not available."),
        "",
        "## Fix Summary",
        str(dev_finding.get("claude_summary") or "Not available."),
    ]
    diff_summary = dev_finding.get("diff_summary")
    if diff_summary:
        lines += ["", "```", str(diff_summary).strip(), "```"]

    lines += ["", "## Tests Executed"]
    tests_info = dev_finding.get("tests") or {}
    if tests_info.get("ran"):
        lines.append(f"- Dev Agent test run: {tests_info.get('summary', 'ran')}")
    else:
        lines.append("- Dev Agent: no automated test command configured for this app.")

    primary_result = retest_finding.get("primary_result") or {}
    lines.append(f"- Retest of `{primary_result.get('flow_name', flow_name)}`: passed")
    for regression in retest_finding.get("regression_results") or []:
        outcome = "passed" if regression.get("passed") else "FAILED"
        lines.append(f"- Regression flow `{regression.get('flow_name')}`: {outcome}")

    lines += ["", "## Before / After Evidence"]
    before_evidence = qa_finding.get("evidence_paths") or []
    after_evidence = primary_result.get("evidence_paths") or []
    before_text = ", ".join(f"`{p}`" for p in before_evidence) or "none recorded"
    after_text = ", ".join(f"`{p}`" for p in after_evidence) or "none recorded"
    lines.append(f"**Before (original failure):** {before_text}")
    lines.append(f"**After (retest):** {after_text}")

    missing_information = rca_finding.get("missing_information") or []
    risk_flags = dev_finding.get("risk_flags") or []
    limitation_lines = [f"- {item}" for item in missing_information]
    limitation_lines += [f"- Flagged at commit time (approved): {flag}" for flag in risk_flags]
    lines += ["", "## Remaining Limitations"]
    lines += limitation_lines or ["- None noted."]

    lines += ["", "---", "*Opened automatically by the QA pipeline. Review required before merging.*"]

    return title, "\n".join(lines)
