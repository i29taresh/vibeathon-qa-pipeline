"""Unit tests for the Phase 3 mock LangGraph pipeline (graph.py + nodes/).

These tests exercise the mock agents only - no LLM, Maestro, GitHub, or real
build tooling is involved anywhere here. Each mock scenario is deterministic
(see mock_scenarios.py), so these assertions are exact, not probabilistic.
"""

from __future__ import annotations

import pytest

from config import load_app_config
from graph import run_pipeline
from state import PipelineState, initial_state


def _run(app_name: str, scenario: str) -> PipelineState:
    app_config = load_app_config(app_name)
    state = initial_state(app_name=app_name, app_config=app_config, mock_scenario=scenario)
    return run_pipeline(state)


def _nodes(final_state: PipelineState) -> list[str]:
    return [record["node"] for record in final_state["execution_history"]]


def test_initial_qa_success_terminates_immediately():
    final_state = _run("sample_android", "initial_pass")

    assert final_state["status"] == "no_bugs_found"
    assert final_state["passed"] is True
    assert final_state["ticket_id"] is None
    assert _nodes(final_state) == ["qa_agent"]


def test_detected_bug_reaches_rca_ticket_dev_retest():
    final_state = _run("sample_android", "fix_success")

    nodes = _nodes(final_state)
    assert "rca_agent" in nodes
    assert "ticket_agent" in nodes
    assert "dev_agent" in nodes
    assert "retest_agent" in nodes


def test_successful_fix_reaches_merge_step():
    final_state = _run("sample_android", "fix_success")

    assert final_state["status"] == "ready_for_review"
    assert final_state["passed"] is True
    assert _nodes(final_state)[-1] == "merge_step"


def test_failed_retest_loops_back_to_rca_then_succeeds():
    final_state = _run("sample_android", "retry_success")

    nodes = _nodes(final_state)
    assert nodes.count("rca_agent") == 2
    assert nodes.count("retest_agent") == 2
    assert final_state["attempt_count"] == 2
    assert final_state["status"] == "ready_for_review"


def test_no_duplicate_tickets_on_retries():
    final_state = _run("sample_android", "retry_success")

    ticket_records = [r for r in final_state["execution_history"] if r["node"] == "ticket_agent"]
    assert len(ticket_records) == 2
    assert "Filed new" in ticket_records[0]["detail"]
    assert "Reused" in ticket_records[1]["detail"]
    assert final_state["ticket_id"] == "MOCK-SAMPLE_ANDROID-1"


def test_retry_limit_is_respected():
    final_state = _run("sample_android", "always_fail")

    assert final_state["status"] == "needs_human_review"
    assert final_state["passed"] is False
    assert final_state["attempt_count"] == final_state["max_attempts"] == 3
    nodes = _nodes(final_state)
    assert nodes.count("retest_agent") == 3
    assert "merge_step" not in nodes


def test_app_config_is_propagated_to_final_state():
    app_config = load_app_config("sample_android")
    state = initial_state(app_name="sample_android", app_config=app_config, mock_scenario="fix_success")

    final_state = run_pipeline(state)

    assert final_state["app_name"] == "sample_android"
    assert final_state["app_config"].name == app_config.name
    assert final_state["app_config"].platform == app_config.platform
    assert final_state["app_config"].app_id == app_config.app_id


@pytest.mark.parametrize("app_name", ["sample_android", "sample_ios"])
@pytest.mark.parametrize(
    "scenario,expected_status",
    [
        ("initial_pass", "no_bugs_found"),
        ("fix_success", "ready_for_review"),
        ("retry_success", "ready_for_review"),
        ("always_fail", "needs_human_review"),
    ],
)
def test_orchestrator_is_platform_agnostic(app_name, scenario, expected_status):
    """Same graph.py / nodes code, no branching, for both Android and iOS configs."""
    final_state = _run(app_name, scenario)

    assert final_state["status"] == expected_status
    assert final_state["app_config"].platform in ("android", "ios")
