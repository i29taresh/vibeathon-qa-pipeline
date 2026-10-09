"""Retest agent: re-verifies a fix the Dev Agent produced.

Two modes, selected by `state["mode"]` (default "mock", set by
state.initial_state) - same pattern as the other real agents:

- "mock" - the Phase 3 placeholder, unchanged.
- "real" - the workflow below.

This node deliberately does not re-implement QA validation: re-running the
originally failing flow (and any configured regression flows) goes through
`utils.qa_validation.run_qa_check` - the exact same function qa_agent uses
for the initial run - so "compare behavior against the same acceptance
criteria used by the initial QA Agent" is automatic (same flow_name, same
reference files) rather than a second implementation that could drift from
the first.

attempt_count note: dev_agent (Phase 4 Step 5) already increments
attempt_count once it has genuinely attempted a fix - that's the
established "one increment per dev+retest cycle" contract the mock pipeline
and graph.py's 3-attempt budget are built on. This node reads attempt_count
(to decide retest_failed vs needs_human_review) but does not increment it
again, which would silently double-count attempts against max_attempts.

This node never touches GitHub - it has no import of utils/github.py at
all - so it is structurally incapable of filing a duplicate ticket; it only
ever updates `state["qa_finding"]` so the next rca_agent pass (if any) sees
the freshest failure instead of the original one.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Optional

from config import AppConfig
from mock_scenarios import retest_passes
from state import PipelineState, log_node
from utils.platform import build_app, install_app, select_device
from utils.jira_verification import jira_failure_finding, verify_against_jira_baseline
from utils.qa_validation import QAFinding, infrastructure_finding, run_qa_check


def retest_agent(state: PipelineState) -> dict:
    if state.get("mode", "mock") == "mock":
        return _run_mock(state)
    return _run_real(state)


# --------------------------------------------------------------------------
# Mock mode (Phase 3 - unchanged)
# --------------------------------------------------------------------------

def _run_mock(state: PipelineState) -> dict:
    print("\n[retest_agent] Re-running QA flow (mock)...")

    scenario = state.get("mock_scenario", "fix_success")
    attempt_count = state.get("attempt_count", 0)
    max_attempts = state.get("max_attempts", 3)
    passed = retest_passes(scenario, attempt_count)

    if passed:
        status = "retest_passed"
        last_error = None
        detail = f"Mock retest passed on attempt {attempt_count}."
    elif attempt_count >= max_attempts:
        status = "needs_human_review"
        last_error = f"Mock retest failed on attempt {attempt_count}; retry limit reached."
        detail = last_error
    else:
        status = "retest_failed"
        last_error = f"Mock retest failed on attempt {attempt_count}."
        detail = last_error

    history = log_node("retest_agent", status, attempt_count, detail=detail)

    return {
        "passed": passed,
        "status": status,
        "last_error": last_error,
        "execution_history": [history],
    }


# --------------------------------------------------------------------------
# Real mode
# --------------------------------------------------------------------------

def _run_real(state: PipelineState) -> dict:
    app_config: AppConfig = state["app_config"]
    flow_name = state.get("flow_name", "default_flow")
    dev_finding = state.get("dev_finding") or {}
    attempt_count = state.get("attempt_count", 0)
    max_attempts = state.get("max_attempts", 3)

    print(f"\n[retest_agent] Verifying fix for '{app_config.name}' ({app_config.platform})...")

    # Step 1: verify the Dev Agent actually produced a valid, buildable patch.
    patch_error = _validate_patch(dev_finding)
    if patch_error:
        finding = infrastructure_finding(flow_name, patch_error)
        return _finalize_failure(finding, [], attempt_count, max_attempts, {})

    # Step 2: build and install the patched app (fresh - not reusing dev_agent's build).
    build_result = build_app(app_config)
    if not build_result.success:
        finding = infrastructure_finding(flow_name, f"Build failed during retest: {build_result.error}")
        return _finalize_failure(finding, [], attempt_count, max_attempts, {"build": {"success": False, "error": build_result.error}})

    device_result = select_device(app_config)
    if not device_result.success:
        finding = infrastructure_finding(flow_name, f"Could not select a device for retest: {device_result.error}")
        return _finalize_failure(finding, [], attempt_count, max_attempts, {})
    device_id = device_result.details.get("device_id")

    artifact_path = build_result.artifact_path or (app_config.clone_path / app_config.build.artifact_path)
    install_result = install_app(app_config, device_id, artifact_path)
    if not install_result.success:
        finding = infrastructure_finding(flow_name, f"Could not install the patched app for retest: {install_result.error}")
        return _finalize_failure(finding, [], attempt_count, max_attempts, {})

    build_install_info = {
        "build": {"success": True, "artifact_path": str(artifact_path)},
        "install": {"success": True, "device_id": device_id},
    }

    # Steps 3-6: re-run the originally failing flow, then any configured
    # regression flows, through the exact same QA validation routine
    # qa_agent uses - never a second, drifted implementation.
    primary_finding = run_qa_check(app_config, flow_name)
    regression_findings = [run_qa_check(app_config, name) for name in app_config.regression_flows]
    failed_regressions = [f for f in regression_findings if not f.passed]

    jira_verification_info: dict[str, Any] | None = None
    jira_result = None
    jira_baseline = state.get("jira_baseline")
    if state.get("jira_issue_input") and jira_baseline:
        jira_result = verify_against_jira_baseline(
            app_config,
            jira_baseline,
            primary_finding.evidence_paths,
            flow_name,
        )
        jira_verification_info = {
            "passed": jira_result.passed,
            "issue_resolved": jira_result.issue_resolved,
            "description": jira_result.description,
            "expected_behavior": jira_result.expected_behavior,
            "actual_behavior": jira_result.actual_behavior,
            "confidence": jira_result.confidence,
            "error": jira_result.error,
        }
        print(
            f"[retest_agent] Jira verification for {jira_baseline.get('issue_key')}: "
            f"{'resolved' if jira_result.passed else 'not resolved'} "
            f"(confidence={jira_result.confidence:.2f})."
        )

    maestro_ok = primary_finding.passed and not failed_regressions
    jira_ok = jira_verification_info is None or bool(jira_verification_info.get("passed"))

    # Steps 7-8: Maestro + (when applicable) Jira report must both be satisfied.
    if maestro_ok and jira_ok:
        return _finalize_success(
            primary_finding,
            regression_findings,
            attempt_count,
            build_install_info,
            jira_verification=jira_verification_info,
        )

    # Something is still failing. Priority: Maestro primary, regression, then Jira-only failure.
    if not primary_finding.passed:
        focus_finding = primary_finding
    elif failed_regressions:
        focus_finding = failed_regressions[0]
    elif jira_result is not None and not jira_ok:
        focus_finding = jira_failure_finding(flow_name, jira_baseline, jira_result)
        # Merge retest screenshots into evidence for RCA
        focus_finding.evidence_paths = list(dict.fromkeys(primary_finding.evidence_paths + focus_finding.evidence_paths))
    else:
        focus_finding = primary_finding

    return _finalize_failure(
        focus_finding,
        regression_findings,
        attempt_count,
        max_attempts,
        build_install_info,
        primary_finding=primary_finding,
        jira_verification=jira_verification_info,
    )


def _validate_patch(dev_finding: dict[str, Any]) -> Optional[str]:
    if not dev_finding:
        return "No dev_finding in state - the Dev Agent has not produced a patch to retest."
    if dev_finding.get("error"):
        return f"Dev Agent did not produce a valid patch: {dev_finding['error']}"
    build_info = dev_finding.get("build")
    if not build_info or not build_info.get("success"):
        return "Dev Agent's patch did not build successfully - nothing to retest."
    return None


def _finalize_success(
    primary_finding: QAFinding,
    regression_findings: list[QAFinding],
    attempt_count: int,
    build_install_info: dict[str, Any],
    jira_verification: dict[str, Any] | None = None,
) -> dict:
    evidence_paths = list(primary_finding.evidence_paths)
    for finding in regression_findings:
        evidence_paths.extend(finding.evidence_paths)

    retest_finding = {
        "primary_result": asdict(primary_finding),
        "regression_results": [asdict(f) for f in regression_findings],
        "original_defect_resolved": True,
        "new_regressions": [],
        "jira_verification": jira_verification,
        **build_install_info,
    }

    status = "retest_passed"
    jira_note = ""
    if jira_verification:
        jira_note = " Jira report verification passed."
    detail = (
        f"Retest passed: original defect resolved; {len(regression_findings)} regression flow(s) all passed."
        f"{jira_note}"
    )
    history = log_node("retest_agent", status, attempt_count, detail=detail)

    return {
        "passed": True,
        "status": status,
        "last_error": None,
        "screenshots": evidence_paths,
        "retest_finding": retest_finding,
        "after_video_paths": list(primary_finding.video_paths),
        "execution_history": [history],
    }


def _finalize_failure(
    focus_finding: QAFinding,
    regression_findings: list[QAFinding],
    attempt_count: int,
    max_attempts: int,
    build_install_info: dict[str, Any],
    primary_finding: Optional[QAFinding] = None,
    jira_verification: dict[str, Any] | None = None,
) -> dict:
    evidence_paths = list(focus_finding.evidence_paths)
    for finding in regression_findings:
        if finding is not focus_finding:
            evidence_paths.extend(finding.evidence_paths)

    retest_finding = {
        "primary_result": asdict(primary_finding) if primary_finding else None,
        "regression_results": [asdict(f) for f in regression_findings],
        "original_defect_resolved": bool(primary_finding and primary_finding.passed),
        "new_regressions": [f.flow_name for f in regression_findings if not f.passed],
        "jira_verification": jira_verification,
        **build_install_info,
    }

    # Never conflate an infrastructure failure with a confirmed app defect:
    # failure_type (functional/visual/infrastructure/unverified) on the
    # finding itself - not the top-level status - carries that distinction,
    # so graph.py's routing vocabulary (retest_failed/needs_human_review)
    # stays the same regardless of *why* the retest failed.
    status = "needs_human_review" if attempt_count >= max_attempts else "retest_failed"
    history = log_node("retest_agent", status, attempt_count, detail=focus_finding.description)

    return {
        "passed": False,
        "status": status,
        "last_error": focus_finding.description,
        "screenshots": evidence_paths,
        # So the next rca_agent pass analyzes the freshest failure, not the
        # one that started this whole attempt - this is the "keep
        # failed-fix evidence available for the next RCA attempt" rule.
        "qa_finding": asdict(focus_finding),
        "retest_finding": retest_finding,
        "after_video_paths": list(primary_finding.video_paths) if primary_finding else [],
        "execution_history": [history],
    }
