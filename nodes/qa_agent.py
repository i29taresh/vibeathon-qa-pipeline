"""QA agent: runs the initial test flow for the configured app and reports findings.

Two modes, selected by `state["mode"]` (default "mock", set by
state.initial_state):

- "mock" - the Phase 3 placeholder: looks up a deterministic pass/fail
  outcome from mock_scenarios.py. Unchanged from Phase 3 so existing mock
  tests keep passing untouched.
- "real" - delegates to utils.qa_validation.run_qa_check, the shared
  validation routine also used by nodes/retest_agent.py to re-run the same
  flow after a fix - this node does not re-implement any of that logic.

Scope note: "the target app exists" is already guaranteed by Phase 2's
config.load_app_config validation before this node ever runs (it can't
produce an AppConfig with a missing app_id/repo/etc). What can still fail
independently at QA-run time is the specific flow file and the device to
run it on, both of which utils.qa_validation.run_qa_check validates.
"""

from __future__ import annotations

from dataclasses import asdict

from config import AppConfig
from mock_scenarios import initial_qa_passes
from state import PipelineState, log_node
from utils.qa_validation import run_qa_check


def qa_agent(state: PipelineState) -> dict:
    if state.get("mode", "mock") == "mock":
        return _run_mock(state)
    return _run_real(state)


# --------------------------------------------------------------------------
# Mock mode (Phase 3 - unchanged)
# --------------------------------------------------------------------------

def _run_mock(state: PipelineState) -> dict:
    print("\n[qa_agent] Running initial QA flow (mock)...")

    scenario = state.get("mock_scenario", "fix_success")
    attempt_count = state.get("attempt_count", 0)
    passed = initial_qa_passes(scenario)

    status = "no_bugs_found" if passed else "bug_detected"
    detail = "Mock QA passed." if passed else "Mock QA failed: simulated UI assertion mismatch."
    history = log_node("qa_agent", status, attempt_count, detail=detail)

    return {
        "passed": passed,
        "status": status,
        "last_error": None if passed else detail,
        "screenshots": [] if passed else ["mock_failure_screenshot.png"],
        "execution_history": [history],
    }


# --------------------------------------------------------------------------
# Real mode
# --------------------------------------------------------------------------

def _run_real(state: PipelineState) -> dict:
    app_config: AppConfig = state["app_config"]
    flow_name = state.get("flow_name", "default_flow")
    attempt_count = state.get("attempt_count", 0)

    print(f"\n[qa_agent] Running real QA flow '{flow_name}' for '{app_config.name}' ({app_config.platform})...")

    finding = run_qa_check(app_config, flow_name)

    status = "no_bugs_found" if finding.passed else "bug_detected"
    history = log_node("qa_agent", status, attempt_count, detail=finding.description)

    return {
        "passed": finding.passed,
        "status": status,
        "last_error": None if finding.passed else finding.description,
        "screenshots": finding.evidence_paths,
        "qa_finding": asdict(finding),
        "before_video_paths": list(finding.video_paths),
        "execution_history": [history],
    }
