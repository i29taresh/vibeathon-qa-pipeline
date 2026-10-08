"""Unit tests for graph.py's Phase 4 additions: routing, source isolation, and
the pipeline-level timeout - independent of the heavier full-pipeline
integration tests in test_graph_real_integration.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from config import load_app_config
from graph import PipelineSafetyError, _route_after_qa, _route_after_retest, ensure_source_isolated, run_pipeline
from state import initial_state


# --------------------------------------------------------------------------
# _route_after_qa
# --------------------------------------------------------------------------

def test_route_after_qa_passed_goes_to_end():
    assert _route_after_qa({"passed": True}) == "end"


def test_route_after_qa_mock_failure_goes_to_rca(tmp_path):
    # Mock mode never sets qa_finding - falls back to the Phase 3 behavior.
    assert _route_after_qa({"passed": False}) == "rca_agent"


def test_route_after_qa_confirmed_functional_defect_goes_to_rca():
    state = {"passed": False, "qa_finding": {"failure_type": "functional"}}
    assert _route_after_qa(state) == "rca_agent"


def test_route_after_qa_confirmed_visual_defect_goes_to_rca():
    state = {"passed": False, "qa_finding": {"failure_type": "visual"}}
    assert _route_after_qa(state) == "rca_agent"


@pytest.mark.parametrize("failure_type", ["infrastructure", "unverified"])
def test_route_after_qa_non_defect_failure_is_blocked(failure_type):
    state = {"passed": False, "qa_finding": {"failure_type": failure_type}}
    assert _route_after_qa(state) == "blocked"


# --------------------------------------------------------------------------
# _route_after_retest
# --------------------------------------------------------------------------

def test_route_after_retest_passed_goes_to_merge():
    assert _route_after_retest({"passed": True}) == "merge"


def test_route_after_retest_mock_failure_within_budget_retries():
    assert _route_after_retest({"passed": False, "status": "retest_failed"}) == "retry"


def test_route_after_retest_mock_failure_at_budget_stops():
    assert _route_after_retest({"passed": False, "status": "needs_human_review"}) == "stop"


def test_route_after_retest_infrastructure_failure_is_blocked_even_with_retries_left():
    state = {"passed": False, "status": "retest_failed", "qa_finding": {"failure_type": "infrastructure"}}
    assert _route_after_retest(state) == "blocked"


def test_route_after_retest_functional_failure_within_budget_retries():
    state = {"passed": False, "status": "retest_failed", "qa_finding": {"failure_type": "functional"}}
    assert _route_after_retest(state) == "retry"


# --------------------------------------------------------------------------
# ensure_source_isolated
# --------------------------------------------------------------------------

def test_ensure_source_isolated_allows_disjoint_path(tmp_path):
    ensure_source_isolated(tmp_path)  # must not raise


def test_ensure_source_isolated_rejects_orchestrator_root_itself():
    orchestrator_root = Path(__file__).resolve().parent.parent
    with pytest.raises(PipelineSafetyError):
        ensure_source_isolated(orchestrator_root)


def test_ensure_source_isolated_rejects_path_inside_orchestrator_repo():
    inside = Path(__file__).resolve().parent.parent / "some_subdir"
    with pytest.raises(PipelineSafetyError):
        ensure_source_isolated(inside)


def test_run_pipeline_refuses_real_mode_with_unsafe_clone_path(tmp_path):
    app_config = load_app_config("sample_android")
    app_config.clone_path = Path(__file__).resolve().parent.parent  # the orchestrator repo itself

    state = initial_state(app_name="sample_android", app_config=app_config, mode="real")

    with pytest.raises(PipelineSafetyError):
        run_pipeline(state)


def test_run_pipeline_does_not_check_isolation_in_mock_mode(tmp_path):
    app_config = load_app_config("sample_android")
    app_config.clone_path = Path(__file__).resolve().parent.parent  # would be unsafe in real mode

    state = initial_state(app_name="sample_android", app_config=app_config, mock_scenario="initial_pass")

    final_state = run_pipeline(state)  # must not raise - mock mode never touches clone_path

    assert final_state["status"] == "no_bugs_found"


# --------------------------------------------------------------------------
# Pipeline-level timeout
# --------------------------------------------------------------------------

def test_run_pipeline_without_timeout_runs_to_completion():
    app_config = load_app_config("sample_android")
    state = initial_state(app_name="sample_android", app_config=app_config, mock_scenario="fix_success")

    final_state = run_pipeline(state)

    assert final_state["status"] == "ready_for_review"


def test_run_pipeline_timeout_preserves_partial_state_instead_of_fabricating_one():
    app_config = load_app_config("sample_android")
    state = initial_state(app_name="sample_android", app_config=app_config, mock_scenario="always_fail")

    final_state = run_pipeline(state, timeout=0.0)

    assert final_state["status"] == "pipeline_timeout"
    assert "exceeded the configured timeout" in final_state["last_error"]
    # stream_mode="values" yields the pristine input state as its first
    # item, so a timeout of exactly 0.0 may fire before any node even
    # completes - the point is that whatever was gathered is preserved
    # verbatim (not fabricated), not that a specific amount of progress was made.
    assert final_state["app_name"] == "sample_android"
    assert isinstance(final_state["execution_history"], list)


def test_run_pipeline_timeout_from_state_field():
    app_config = load_app_config("sample_android")
    state = initial_state(
        app_name="sample_android", app_config=app_config, mock_scenario="always_fail", timeout_seconds=0.0
    )

    final_state = run_pipeline(state)  # no explicit `timeout` arg - must fall back to state["timeout_seconds"]

    assert final_state["status"] == "pipeline_timeout"
