"""LangGraph wiring for the QA pipeline.

    START -> qa_agent  OR  (when state["jira_issue_key"] is set) -> jira_intake_agent -> rca_agent
               |-- passes                                  --> END (status=no_bugs_found, set by qa_agent)
               |-- confirmed defect (functional/visual)     --> rca_agent -> ticket_agent -> dev_agent -> retest_agent
               '-- unverified/infrastructure (real mode)    --> END, blocked - no ticket filed

    retest_agent (Maestro + Jira baseline verification when jira_issue_input) -->
               |-- passes                                   --> merge_step -> END (opens a PR, never merges)
               |-- infrastructure failure (real mode)        --> END, blocked
               |-- confirmed failure, attempts remaining     --> rca_agent (loop)
               '-- confirmed failure, attempts exhausted     --> END (status=needs_human_review)

ticket_agent reuses the existing ticket_id and dev_agent reuses the existing
ai-fix/<ticket_id> branch on every pass through the loop, so retries never
file a duplicate issue or recreate a fix branch.

Both modes (`state["mode"]` - "mock" or "real", validated by
state.initial_state) run through this exact same graph shape; only the
routing decisions below need to know the difference, since each node
dispatches to its own mock/real implementation internally. In "mock" mode
`qa_finding`/`retest`-related fields are simply absent, so the
infrastructure/unverified routing branches never trigger and behavior is
unchanged from Phase 3.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, Optional

ProgressCallback = Callable[["PipelineState", float], None]

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from nodes.dev_agent import dev_agent
from nodes.jira_intake_agent import jira_intake_agent
from nodes.merge_step import merge_step
from nodes.qa_agent import qa_agent
from nodes.rca_agent import rca_agent
from nodes.retest_agent import retest_agent
from nodes.ticket_agent import ticket_agent
from config import collect_clone_paths
from state import PipelineState

# This file's own directory is the orchestration repo's root - the one
# thing app_config.clone_path must never be (or contain, or be contained
# by), since real-mode nodes build/edit/commit/push inside clone_path.
ORCHESTRATOR_ROOT = Path(__file__).resolve().parent

# Failure types that mean "not a confirmed, fixable application defect" -
# qa_agent/rca_agent/retest_agent already classify findings this way
# (see utils/qa_validation.py); the graph just needs to stop looping on them.
_NON_DEFECT_FAILURE_TYPES = ("infrastructure", "unverified")


class PipelineSafetyError(Exception):
    """Raised when running the pipeline would be unsafe to even start."""


def ensure_all_sources_isolated(clone_paths: list[Path]) -> None:
    for path in clone_paths:
        ensure_source_isolated(path)


def ensure_source_isolated(clone_path: Path) -> None:
    """Refuse to proceed if `clone_path` overlaps this orchestrator's own repository.

    Real-mode nodes build, edit, commit, and push inside `clone_path` - it
    must be an isolated checkout of the *target* application, never this
    codebase (or an ancestor/descendant of it), regardless of what a
    particular apps/<name>.yaml happens to say.
    """
    resolved = Path(clone_path).resolve()
    if resolved == ORCHESTRATOR_ROOT or ORCHESTRATOR_ROOT in resolved.parents or resolved in ORCHESTRATOR_ROOT.parents:
        raise PipelineSafetyError(
            f"app_config.clone_path ({resolved}) overlaps this orchestrator's own repository "
            f"({ORCHESTRATOR_ROOT}) - refusing to run. Point clone_path at an isolated checkout "
            "of the target application instead."
        )


def _route_from_start(state: PipelineState) -> str:
    if state.get("jira_issue_key"):
        return "jira_intake"
    return "qa_agent"


def _route_after_qa(state: PipelineState) -> str:
    if state.get("passed"):
        return "end"

    qa_finding = state.get("qa_finding")
    if qa_finding and qa_finding.get("failure_type") in _NON_DEFECT_FAILURE_TYPES:
        return "blocked"

    return "rca_agent"


def _route_after_retest(state: PipelineState) -> str:
    if state.get("passed"):
        return "merge"

    # An infrastructure failure isn't something another code-fix attempt
    # can resolve - stop regardless of attempts remaining, rather than
    # burning the retry budget on a problem that isn't in the code.
    qa_finding = state.get("qa_finding")
    if qa_finding and qa_finding.get("failure_type") == "infrastructure":
        return "blocked"

    if state.get("status") == "needs_human_review":
        return "stop"

    attempt_count = state.get("attempt_count", 0)
    max_attempts = state.get("max_attempts", 3)
    if attempt_count >= max_attempts:
        return "stop"

    return "retry"


def build_graph() -> CompiledStateGraph:
    """Construct and compile the QA pipeline graph."""
    graph = StateGraph(PipelineState)

    graph.add_node("qa_agent", qa_agent)
    graph.add_node("jira_intake_agent", jira_intake_agent)
    graph.add_node("rca_agent", rca_agent)
    graph.add_node("ticket_agent", ticket_agent)
    graph.add_node("dev_agent", dev_agent)
    graph.add_node("retest_agent", retest_agent)
    graph.add_node("merge_step", merge_step)

    graph.add_conditional_edges(
        START, _route_from_start, {"jira_intake": "jira_intake_agent", "qa_agent": "qa_agent"}
    )
    graph.add_edge("jira_intake_agent", "rca_agent")
    graph.add_conditional_edges(
        "qa_agent", _route_after_qa, {"end": END, "blocked": END, "rca_agent": "rca_agent"}
    )
    graph.add_edge("rca_agent", "ticket_agent")
    graph.add_edge("ticket_agent", "dev_agent")
    graph.add_edge("dev_agent", "retest_agent")
    graph.add_conditional_edges(
        "retest_agent",
        _route_after_retest,
        {"merge": "merge_step", "blocked": END, "stop": END, "retry": "rca_agent"},
    )
    graph.add_edge("merge_step", END)

    return graph.compile()


def run_pipeline(
    state: PipelineState,
    timeout: Optional[float] = None,
    on_progress: ProgressCallback | None = None,
) -> PipelineState:
    """Run the compiled graph to completion and return the final state.

    `timeout` (seconds) bounds total wall-clock execution, falling back to
    `state["timeout_seconds"]` when not given explicitly, and to "no
    timeout" when neither is set. If the budget is exceeded, the most
    recently completed node's full state is returned as-is except for
    `status`/`last_error` - execution_history, findings, and evidence
    gathered up to that point are preserved rather than discarded.
    """
    if state.get("mode") == "real":
        project_graph = state.get("project_graph")
        if project_graph:
            ensure_all_sources_isolated(collect_clone_paths(project_graph))
        else:
            app_config = state.get("app_config")
            if app_config is not None:
                ensure_source_isolated(app_config.clone_path)

    pipeline = build_graph()

    timeout_value = timeout if timeout is not None else state.get("timeout_seconds")
    started = time.monotonic()
    deadline = started + timeout_value if timeout_value is not None else None
    last_state: PipelineState = state

    for snapshot in pipeline.stream(state, stream_mode="values"):
        last_state = snapshot
        if on_progress:
            on_progress(last_state, time.monotonic() - started)
        if deadline is not None and time.monotonic() > deadline:
            timed_out = dict(last_state)
            timed_out["status"] = "pipeline_timeout"
            timed_out["last_error"] = f"Pipeline exceeded the configured timeout of {timeout_value}s."
            return timed_out

    return last_state
