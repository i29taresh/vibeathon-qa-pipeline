"""Retest agent: re-verifies a fix the Dev Agent produced.

Two modes, selected by `state["mode"]` (default "mock", set by
state.initial_state) - same pattern as the other real agents:

- "mock" - the Phase 3 placeholder, unchanged.
- "real" - the workflow below.

For the Jira lean POC path: mavenLocal publish/pin when payzyshared changed,
Maestro on the generated flow with screenrecord, after-video frame verifier
hard gate, patch+reset+maven cleanup on fail.
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional

from config import AppConfig
from mock_scenarios import retest_passes
from state import PipelineState, log_node
from utils.git_workspace import reset_hard, write_patch
from utils.jira_verification import jira_failure_finding, verify_against_jira_baseline
from utils.maven_local import cleanup_maven_local, pin_b2b_payzy_shared, publish_payzyshared
from utils.platform import build_app, install_app, select_device
from utils.project_context import resolve_fix_app_config
from utils.qa_validation import QAFinding, infrastructure_finding, run_qa_check
from utils.video_verification import verify_after_video

_ORCHESTRATOR_ROOT = Path(__file__).resolve().parent.parent


def retest_agent(state: PipelineState) -> dict:
    if state.get("mode", "mock") == "mock":
        return _run_mock(state)
    return _run_real(state)


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


def _run_real(state: PipelineState) -> dict:
    app_config: AppConfig = state["app_config"]
    flow_name = state.get("flow_name", "default_flow")
    maestro_flow_path = state.get("maestro_flow_path")
    dev_finding = state.get("dev_finding") or {}
    attempt_count = state.get("attempt_count", 0)
    max_attempts = state.get("max_attempts", 3)
    jira_poc = bool(state.get("jira_issue_input"))

    print(f"\n[retest_agent] Verifying fix for '{app_config.name}' ({app_config.platform})...")

    patch_error = _validate_patch(dev_finding)
    if patch_error:
        finding = infrastructure_finding(flow_name, patch_error)
        return _finalize_failure(finding, [], attempt_count, max_attempts, {})

    maven_info: dict[str, Any] = {}
    maven_version = state.get("maven_local_version")
    original_pin = state.get("payzy_shared_pin_original")

    if jira_poc and _needs_maven_publish(state, dev_finding):
        maven_result = _publish_and_pin(state)
        maven_info = maven_result
        if not maven_result.get("success"):
            finding = infrastructure_finding(
                flow_name, f"Maven local publish/pin failed: {maven_result.get('error')}"
            )
            return _finalize_failure(finding, [], attempt_count, max_attempts, {"maven": maven_result})
        maven_version = maven_result.get("version")
        original_pin = maven_result.get("original_pin")

    build_result = build_app(app_config)
    if not build_result.success:
        finding = infrastructure_finding(flow_name, f"Build failed during retest: {build_result.error}")
        return _fail_with_cleanup(
            state,
            finding,
            [],
            attempt_count,
            max_attempts,
            {"build": {"success": False, "error": build_result.error}, "maven": maven_info},
            maven_version,
            original_pin,
        )

    device_result = select_device(app_config)
    if not device_result.success:
        finding = infrastructure_finding(flow_name, f"Could not select a device for retest: {device_result.error}")
        return _fail_with_cleanup(
            state, finding, [], attempt_count, max_attempts, {"maven": maven_info}, maven_version, original_pin
        )
    device_id = device_result.details.get("device_id")

    artifact_path = build_result.artifact_path or (app_config.clone_path / app_config.build.artifact_path)
    install_result = install_app(app_config, device_id, artifact_path)
    if not install_result.success:
        finding = infrastructure_finding(flow_name, f"Could not install the patched app for retest: {install_result.error}")
        return _fail_with_cleanup(
            state, finding, [], attempt_count, max_attempts, {"maven": maven_info}, maven_version, original_pin
        )

    build_install_info = {
        "build": {"success": True, "artifact_path": str(artifact_path)},
        "install": {"success": True, "device_id": device_id},
        "maven": maven_info,
    }

    if maestro_flow_path:
        primary_finding = run_qa_check(
            app_config, flow_name, flow_path=maestro_flow_path, skip_acceptance_criteria=True
        )
        regression_findings: list[QAFinding] = []
    else:
        primary_finding = run_qa_check(app_config, flow_name)
        regression_findings = [run_qa_check(app_config, name) for name in app_config.regression_flows]
    failed_regressions = [f for f in regression_findings if not f.passed]

    after_videos = list(primary_finding.video_paths)
    verifier_finding: dict[str, Any] | None = None
    jira_verification_info: dict[str, Any] | None = None
    jira_result = None
    jira_baseline = state.get("jira_baseline")

    if jira_poc and jira_baseline:
        # Hard gate: after-video frame verifier (preferred) or screenshot baseline verifier.
        if after_videos:
            frames_out = _ORCHESTRATOR_ROOT / "local" / "runs" / str(
                state.get("ticket_id") or "UNKNOWN"
            ) / f"attempt-{attempt_count}-frames"
            vf = verify_after_video(jira_baseline, after_videos, frames_out=frames_out)
            verifier_finding = vf.to_dict()
            jira_verification_info = {
                "passed": vf.passed,
                "issue_resolved": vf.issue_resolved,
                "description": vf.comments,
                "confidence": vf.confidence,
                "error": vf.error,
                "frame_paths": vf.frame_paths,
                "video_path": vf.video_path,
            }
            print(
                f"[retest_agent] Verifier for {jira_baseline.get('issue_key')}: "
                f"{'resolved' if vf.passed else 'not resolved'} "
                f"(confidence={vf.confidence:.2f})."
            )
        else:
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
            verifier_finding = {
                "passed": jira_result.passed,
                "issue_resolved": jira_result.issue_resolved,
                "comments": jira_result.description,
                "confidence": jira_result.confidence,
                "frame_paths": [],
                "video_path": None,
                "error": jira_result.error or "missing_after_video",
            }

    maestro_ok = primary_finding.passed and not failed_regressions
    verifier_ok = True
    if jira_poc:
        verifier_ok = bool(jira_verification_info and jira_verification_info.get("passed"))

    # Jira lean POC: after-video verifier is the hard pass/fail gate (not Maestro
    # asserts alone). A Maestro assert may fail while the defect is still open;
    # when the verifier says resolved, the run passes and finalize attaches the
    # recording + fix branch.
    if jira_poc:
        verifier_error = (jira_verification_info or {}).get("error") or (
            (verifier_finding or {}).get("error")
        )
        infra_blocker = (
            primary_finding.failure_type == "infrastructure"
            or verifier_error
            in ("missing_after_video", "frame_extract_failed", "verifier_llm_failed")
        )
        if infra_blocker and not verifier_ok:
            focus = primary_finding
            if primary_finding.failure_type != "infrastructure":
                focus = infrastructure_finding(
                    flow_name,
                    f"Retest could not verify the fix ({verifier_error or primary_finding.description}).",
                )
                focus.evidence_paths = list(
                    dict.fromkeys(primary_finding.evidence_paths + focus.evidence_paths)
                )
                focus.video_paths = after_videos
            return _fail_with_cleanup(
                state,
                focus,
                regression_findings,
                attempt_count,
                max_attempts,
                build_install_info,
                maven_version,
                original_pin,
                primary_finding=primary_finding,
                jira_verification=jira_verification_info,
                verifier_finding=verifier_finding,
                after_videos=after_videos,
                mark_failed_when_exhausted=True,
            )
        if verifier_ok:
            branch = str((state.get("dev_finding") or {}).get("branch") or "")
            out = _finalize_success(
                primary_finding,
                regression_findings,
                attempt_count,
                build_install_info,
                jira_verification=jira_verification_info,
                fix_branch=branch,
            )
            out["after_video_paths"] = after_videos
            if verifier_finding is not None:
                out["verifier_finding"] = verifier_finding
            if maven_version:
                out["maven_local_version"] = maven_version
                out["payzy_shared_pin_original"] = original_pin
            return out
        # Verifier: not fixed → RCA retry (up to max_attempts), then status=failed.
        if jira_result is not None:
            focus_finding = jira_failure_finding(flow_name, jira_baseline, jira_result)
        else:
            focus_finding = QAFinding(
                flow_name=flow_name,
                passed=False,
                failure_type="functional",
                description=str(
                    (verifier_finding or {}).get("comments")
                    or "Verifier did not confirm the Jira issue is resolved."
                ),
                expected_behavior=str((jira_baseline or {}).get("expected_behavior") or ""),
                actual_behavior="After-video verification: issue not resolved.",
                confidence=float((verifier_finding or {}).get("confidence") or 0.0),
                evidence_paths=list((verifier_finding or {}).get("frame_paths") or []),
                video_paths=after_videos,
            )
        focus_finding.evidence_paths = list(
            dict.fromkeys(primary_finding.evidence_paths + focus_finding.evidence_paths)
        )
        return _fail_with_cleanup(
            state,
            focus_finding,
            regression_findings,
            attempt_count,
            max_attempts,
            build_install_info,
            maven_version,
            original_pin,
            primary_finding=primary_finding,
            jira_verification=jira_verification_info,
            verifier_finding=verifier_finding,
            after_videos=after_videos,
            mark_failed_when_exhausted=True,
        )

    if maestro_ok:
        out = _finalize_success(
            primary_finding,
            regression_findings,
            attempt_count,
            build_install_info,
            jira_verification=jira_verification_info,
        )
        out["after_video_paths"] = after_videos
        if verifier_finding is not None:
            out["verifier_finding"] = verifier_finding
        if maven_version:
            out["maven_local_version"] = maven_version
            out["payzy_shared_pin_original"] = original_pin
        return out

    if not primary_finding.passed:
        focus_finding = primary_finding
    elif failed_regressions:
        focus_finding = failed_regressions[0]
    else:
        focus_finding = primary_finding

    return _fail_with_cleanup(
        state,
        focus_finding,
        regression_findings,
        attempt_count,
        max_attempts,
        build_install_info,
        maven_version,
        original_pin,
        primary_finding=primary_finding,
        jira_verification=jira_verification_info,
        verifier_finding=verifier_finding,
        after_videos=after_videos,
    )


def _needs_maven_publish(state: PipelineState, dev_finding: dict[str, Any]) -> bool:
    if (dev_finding.get("target_app") or "") in ("payzyshared", "payzy-shared"):
        return True
    for entry in dev_finding.get("files_changed") or []:
        path = str(entry).split(" ", 1)[-1].lower()
        if "payzy" in path and "shared" in path:
            return True
    # Also if fix target is payzyshared via project graph
    try:
        stem, _ = resolve_fix_app_config(state)
        return stem in ("payzyshared", "payzy-shared")
    except Exception:
        return False


def _publish_and_pin(state: PipelineState) -> dict[str, Any]:
    project_graph = state.get("project_graph") or {}
    payzy = project_graph.get("payzyshared") or project_graph.get("payzy-shared")
    b2b = project_graph.get("b2b_android") or project_graph.get("b2b-android") or state.get("app_config")
    ticket = str(state.get("ticket_id") or state.get("jira_issue_key") or "TICKET")
    if payzy is None:
        return {"success": False, "error": "payzyshared not in project_graph"}
    if b2b is None:
        return {"success": False, "error": "b2b_android not available for pin"}
    pub = publish_payzyshared(payzy, ticket)
    if not pub.success:
        return {"success": False, "error": pub.error, "version": pub.version}
    pin = pin_b2b_payzy_shared(b2b, pub.version or "")
    if not pin.success:
        return {"success": False, "error": pin.error, "version": pub.version}
    return {
        "success": True,
        "version": pub.version,
        "original_pin": pin.original_pin,
    }


def _fail_with_cleanup(
    state: PipelineState,
    focus_finding: QAFinding,
    regression_findings: list[QAFinding],
    attempt_count: int,
    max_attempts: int,
    build_install_info: dict[str, Any],
    maven_version: str | None,
    original_pin: str | None,
    *,
    primary_finding: Optional[QAFinding] = None,
    jira_verification: dict[str, Any] | None = None,
    verifier_finding: dict[str, Any] | None = None,
    after_videos: list[str] | None = None,
    mark_failed_when_exhausted: bool = False,
) -> dict:
    patch_paths: list[str] = list(state.get("attempt_patch_paths") or [])
    if state.get("jira_issue_input"):
        ticket = str(state.get("ticket_id") or "UNKNOWN")
        run_dir = _ORCHESTRATOR_ROOT / "local" / "runs" / ticket
        patch_file = run_dir / f"attempt-{attempt_count}.patch"
        try:
            _, fix_config = resolve_fix_app_config(state)
            if write_patch(fix_config.clone_path, patch_file):
                patch_paths.append(str(patch_file))
            reset_hard(fix_config.clone_path)
        except Exception as exc:
            build_install_info = {**build_install_info, "cleanup_error": str(exc)}

        project_graph = state.get("project_graph") or {}
        b2b = project_graph.get("b2b_android") or project_graph.get("b2b-android") or state.get("app_config")
        if maven_version or original_pin:
            build_install_info["maven_cleanup"] = cleanup_maven_local(
                maven_version,
                b2b if isinstance(b2b, AppConfig) else None,
                original_pin,
            )

    out = _finalize_failure(
        focus_finding,
        regression_findings,
        attempt_count,
        max_attempts,
        build_install_info,
        primary_finding=primary_finding,
        jira_verification=jira_verification,
        mark_failed_when_exhausted=mark_failed_when_exhausted,
    )
    out["attempt_patch_paths"] = patch_paths
    out["after_video_paths"] = after_videos or []
    if verifier_finding is not None:
        out["verifier_finding"] = verifier_finding
    # Clear maven pin state after cleanup
    out["maven_local_version"] = None
    out["payzy_shared_pin_original"] = None
    return out


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
    fix_branch: str = "",
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
        "fix_branch": fix_branch or None,
        "recording_paths": list(primary_finding.video_paths),
        **build_install_info,
    }

    status = "retest_passed"
    jira_note = ""
    if jira_verification:
        jira_note = " Verifier passed."
    branch_note = f" Branch: {fix_branch}." if fix_branch else ""
    detail = (
        f"Retest passed: original defect resolved; {len(regression_findings)} regression flow(s) all passed."
        f"{jira_note}{branch_note}"
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
    mark_failed_when_exhausted: bool = False,
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

    if attempt_count >= max_attempts:
        # Jira POC: mark failed after verifier rejects max_attempts times.
        status = "failed" if mark_failed_when_exhausted else "needs_human_review"
    else:
        status = "retest_failed"
    history = log_node("retest_agent", status, attempt_count, detail=focus_finding.description)

    return {
        "passed": False,
        "status": status,
        "last_error": focus_finding.description,
        "screenshots": evidence_paths,
        "qa_finding": asdict(focus_finding),
        "retest_finding": retest_finding,
        "after_video_paths": list(primary_finding.video_paths) if primary_finding else [],
        "execution_history": [history],
    }
