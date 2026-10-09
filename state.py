"""Shared pipeline state used by every LangGraph node.

PipelineState flows through the graph defined in graph.py. It is a
TypedDict, which is just a plain dict at runtime - nodes may stash extra
ad-hoc keys beyond the ones declared here when needed; LangGraph does not
reject unknown keys.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, Optional, TypedDict

from config import AppConfig


class ExecutionRecord(TypedDict):
    """One entry in `execution_history`: a single node's run."""

    node: str
    status: str
    attempt_count: int
    detail: str


class PipelineState(TypedDict, total=False):
    app_name: str
    app_config: AppConfig
    project_graph: dict[str, AppConfig]
    flow_name: str
    # When set, the pipeline starts from jira_intake_agent instead of qa_agent.
    jira_issue_key: Optional[str]
    # True after jira_intake_agent: ticket_agent reuses the issue instead of creating one.
    jira_issue_input: bool
    # Original Jira report snapshot for post-fix verification in retest_agent.
    jira_baseline: dict[str, Any]
    status: str
    passed: bool
    ticket_id: Optional[str]
    screenshots: list[str]
    last_error: Optional[str]
    root_cause: Optional[str]
    suspected_files: list[str]
    attempt_count: int
    max_attempts: int
    # "mock" (default) drives qa_agent from mock_scenarios.py; "real" drives
    # it from utils/platform.py, utils/maestro.py, and utils/llm.py instead.
    mode: str
    mock_scenario: Optional[str]
    # Structured output of a real qa_agent run (see nodes/qa_agent.py's
    # QAFinding) - absent/unset in mock mode.
    qa_finding: dict[str, Any]
    # Structured output of a real rca_agent run (see nodes/rca_agent.py's
    # RCAFinding) - absent/unset in mock mode.
    rca_finding: dict[str, Any]
    # The GitHub issue's URL, persisted alongside ticket_id once a real
    # ticket_agent run files or finds one - absent/unset in mock mode.
    ticket_url: Optional[str]
    # Structured record of what ticket_agent did (created/reused/skipped/
    # dry-run/failed) and the rendered title/body - see nodes/ticket_agent.py.
    ticket_finding: dict[str, Any]
    # True = ticket_agent renders the report but never calls `gh`.
    dry_run: bool
    # True = file a ticket even for an infrastructure-classified QA failure
    # (default False: infra failures are not application defects).
    file_ticket_for_infrastructure: bool
    # Structured record of what dev_agent did (branch, files changed, risk
    # flags, build/test results) - see nodes/dev_agent.py's dev_finding dict.
    dev_finding: dict[str, Any]
    # True = dev_agent may proceed even when a change touches a denylisted
    # path (deletion, CI/workflow config, secrets-looking file). Default
    # False: such changes halt with status="dev_requires_approval" instead.
    approve_destructive_changes: bool
    # Structured record of what retest_agent did (primary + regression
    # results, build/install results) - see nodes/retest_agent.py.
    retest_finding: dict[str, Any]
    # The opened Pull Request's URL/number, persisted once a real
    # merge_step run creates (or finds an existing) PR - absent/unset in
    # mock mode. merge_step never merges - these just record what it opened.
    pr_url: Optional[str]
    pr_number: Optional[int]
    # Structured record of what merge_step did (branch, base branch,
    # title/body, or why it was blocked) - see nodes/merge_step.py.
    merge_finding: dict[str, Any]
    # True = merge_step may push a branch whose dev_finding carries risk
    # flags (deletion, CI/workflow config, secrets-looking file). Default
    # False: such branches are blocked from being pushed, mirroring
    # approve_destructive_changes but for the more consequential, visible
    # act of pushing to the remote.
    approve_push: bool
    # Whether merge_step opens the PR as a draft. Default True (set by
    # merge_step itself via .get() - not required here).
    draft_pr: bool
    # Optional wall-clock budget (seconds) for the whole pipeline run, read
    # by graph.run_pipeline (also overridable via that function's own
    # `timeout` argument). None = no pipeline-level timeout.
    timeout_seconds: Optional[float]
    # True = allow a real-mode run even when the bootstrap ready marker is
    # missing (CLI --force / dashboard checkbox). Default False.
    force_unbootstrapped: bool
    # Absolute path to a generated Maestro repro YAML (Jira POC path).
    maestro_flow_path: Optional[str]
    # After-video frame verifier result (retest hard gate).
    verifier_finding: dict[str, Any]
    after_video_paths: list[str]
    before_video_paths: list[str]
    # Local finalize (commit, no push/MR) result for Jira POC path.
    finalize_finding: dict[str, Any]
    run_history_id: Optional[str]
    maven_local_version: Optional[str]
    payzy_shared_pin_original: Optional[str]
    attempt_patch_paths: list[str]
    # Each node returns a single-item list here; LangGraph appends it to the
    # running history via operator.add instead of overwriting it.
    execution_history: Annotated[list[ExecutionRecord], operator.add]


VALID_MODES = ("mock", "real")


def create_pipeline_state(
    app_name: str,
    app_config: AppConfig,
    *,
    mock_scenario: Optional[str] = None,
    mode: str = "mock",
    flow_name: str = "default_flow",
    jira_issue_raw: Optional[str] = None,
    max_attempts: int = 3,
    dry_run: bool = False,
    timeout_seconds: Optional[float] = None,
    project_graph: Optional[dict[str, AppConfig]] = None,
    force_unbootstrapped: bool = False,
) -> PipelineState:
    """Build pipeline state for CLI/dashboard (parses Jira URL/key, tolerates older initial_state)."""
    from utils.jira import normalize_issue_input

    jira_issue_key: Optional[str] = None
    if jira_issue_raw and str(jira_issue_raw).strip():
        jira_issue_key = normalize_issue_input(str(jira_issue_raw))
        if not jira_issue_key:
            raise ValueError(
                f"Could not parse a Jira issue key from {jira_issue_raw!r}. "
                "Use KEY-123 or a browse URL like https://your.atlassian.net/browse/KEY-123."
            )

    kwargs = {
        "app_name": app_name,
        "app_config": app_config,
        "mock_scenario": mock_scenario,
        "mode": mode,
        "flow_name": flow_name,
        "max_attempts": max_attempts,
        "dry_run": dry_run,
        "timeout_seconds": timeout_seconds,
        "project_graph": project_graph,
        "force_unbootstrapped": force_unbootstrapped,
    }
    try:
        state = initial_state(**kwargs, jira_issue_key=jira_issue_key)
    except TypeError:
        kwargs.pop("force_unbootstrapped", None)
        state = initial_state(**kwargs)
        if jira_issue_key:
            state["jira_issue_key"] = jira_issue_key
        state["force_unbootstrapped"] = force_unbootstrapped
    return state


def initial_state(
    app_name: str,
    app_config: AppConfig,
    mock_scenario: Optional[str] = None,
    mode: str = "mock",
    flow_name: str = "default_flow",
    jira_issue_key: Optional[str] = None,
    max_attempts: int = 3,
    dry_run: bool = False,
    timeout_seconds: Optional[float] = None,
    project_graph: Optional[dict[str, AppConfig]] = None,
    force_unbootstrapped: bool = False,
) -> PipelineState:
    """Build the starting PipelineState for one pipeline run.

    `mode` must be "mock" or "real" (explicit - no other value is silently
    accepted) - "mock" drives every node from mock_scenarios.py; "real"
    drives the actual QA/RCA/Ticket/Dev/Retest/PR components.
    """
    if mode not in VALID_MODES:
        raise ValueError(f"mode must be one of {VALID_MODES}, got {mode!r}")

    graph = project_graph if project_graph is not None else {app_name: app_config}

    return {
        "app_name": app_name,
        "app_config": app_config,
        "project_graph": graph,
        "flow_name": flow_name,
        "jira_issue_key": jira_issue_key,
        "force_unbootstrapped": force_unbootstrapped,
        "jira_issue_input": False,
        "status": "pending",
        "passed": False,
        "ticket_id": None,
        "ticket_url": None,
        "screenshots": [],
        "last_error": None,
        "root_cause": None,
        "suspected_files": [],
        "attempt_count": 0,
        "max_attempts": max_attempts,
        "mode": mode,
        "mock_scenario": mock_scenario,
        "dry_run": dry_run,
        "file_ticket_for_infrastructure": False,
        "timeout_seconds": timeout_seconds,
        "execution_history": [],
    }


def log_node(node_name: str, status: str, attempt_count: int, detail: str = "") -> ExecutionRecord:
    """Print standard observability output for a node and build its history record."""
    print(f"Agent: {node_name}")
    print(f"Status: {status}")
    print(f"Attempt count: {attempt_count}")
    if detail:
        print(f"Detail: {detail}")
    return {
        "node": node_name,
        "status": status,
        "attempt_count": attempt_count,
        "detail": detail,
    }
