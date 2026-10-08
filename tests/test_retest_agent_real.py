"""Unit tests for nodes.retest_agent's "real" mode.

utils.platform.{build_app,install_app,select_device} and
utils.qa_validation.{select_device,run_flow,ask_for_json} (the dependencies
run_qa_check itself calls) are all mocked - no real adb/xcrun/maestro
binary and no real LLM call happens anywhere in this file.
"""

from __future__ import annotations

from pathlib import Path

import nodes.retest_agent as retest_agent_mod
import utils.qa_validation as qa_validation_mod
from config import load_app_config
from nodes.retest_agent import retest_agent
from state import initial_state
from utils.maestro import MaestroResult
from utils.platform import PlatformResult

PASS_JUNIT_XML = """<?xml version="1.0"?>
<testsuite name="login" tests="1" failures="0">
  <testcase name="launch and log in" classname="MaestroFlow"/>
</testsuite>
"""

FAIL_JUNIT_XML = """<?xml version="1.0"?>
<testsuite name="login" tests="1" failures="1">
  <testcase name="launch and log in" classname="MaestroFlow">
    <failure message="Element not found: Login button">Element not found: Login button</failure>
  </testcase>
</testsuite>
"""


def _make_config(tmp_path: Path, app_name: str = "sample_android"):
    cfg = load_app_config(app_name)
    cfg.clone_path = tmp_path / "clone"
    cfg.flows_dir = tmp_path / "flows"
    cfg.reference_dir = tmp_path / "references"
    cfg.clone_path.mkdir(parents=True)
    cfg.flows_dir.mkdir(parents=True)
    cfg.reference_dir.mkdir(parents=True)
    return cfg


def _write_flow(cfg, flow_name="login.yaml"):
    (cfg.flows_dir / flow_name).write_text("appId: com.example\n---\n- launchApp\n")


def _write_criteria(cfg, flow_name="login.yaml", text="The login button must be visible and tappable."):
    flow_stem = Path(flow_name).stem
    ref_dir = cfg.reference_dir / flow_stem
    ref_dir.mkdir(parents=True, exist_ok=True)
    (ref_dir / "acceptance_criteria.md").write_text(text)


def _valid_dev_finding(**overrides):
    finding = {
        "branch": "ai-fix/42",
        "base_branch": "main",
        "files_changed": ["M app/LoginActivity.kt"],
        "requires_approval": False,
        "build": {"success": True, "artifact_path": "app-debug.apk"},
    }
    finding.update(overrides)
    return finding


def _real_state(app_config, dev_finding=None, attempt_count=1, max_attempts=3, flow_name="login.yaml"):
    state = initial_state(app_name=app_config.name, app_config=app_config, mode="real", flow_name=flow_name)
    state["attempt_count"] = attempt_count
    state["max_attempts"] = max_attempts
    state["dev_finding"] = dev_finding if dev_finding is not None else _valid_dev_finding()
    return state


def _patch_platform(monkeypatch, build_success=True, build_error=None, device_id="emulator-5554", device_success=True, install_success=True, install_error=None):
    monkeypatch.setattr(
        retest_agent_mod,
        "build_app",
        lambda app_config: PlatformResult(
            success=build_success, error=build_error, artifact_path=Path("/tmp/app-debug.apk") if build_success else None
        ),
    )
    monkeypatch.setattr(
        retest_agent_mod,
        "select_device",
        lambda app_config: PlatformResult(success=device_success, details={"device_id": device_id} if device_success else {}, error=None if device_success else "no device"),
    )
    monkeypatch.setattr(
        retest_agent_mod,
        "install_app",
        lambda app_config, device_id, artifact_path: PlatformResult(success=install_success, error=install_error),
    )


def _patch_device_for_qa_check(monkeypatch, device_id="emulator-5554"):
    monkeypatch.setattr(
        qa_validation_mod, "select_device", lambda app_config, timeout=30.0: PlatformResult(success=True, details={"device_id": device_id})
    )


def _patch_maestro_sequence(monkeypatch, results: list[MaestroResult]):
    sequence = list(results)

    def fake_run_flow(app_config, flow_name, device_id, runs_root=None, timeout=600.0):
        return sequence.pop(0)

    monkeypatch.setattr(qa_validation_mod, "run_flow", fake_run_flow)


def _patch_llm_no_visual_mismatch(monkeypatch):
    from utils.llm import LLMResult

    monkeypatch.setattr(qa_validation_mod, "ask_for_json", lambda prompt, images=None, **k: LLMResult(success=True, data={"visual_mismatch": False}))


# --------------------------------------------------------------------------
# Pass
# --------------------------------------------------------------------------

def test_retest_passes_when_fix_resolves_original_defect(tmp_path, monkeypatch):
    cfg = _make_config(tmp_path)
    _write_flow(cfg)
    _write_criteria(cfg)
    _patch_platform(monkeypatch)
    _patch_device_for_qa_check(monkeypatch)
    report_path = tmp_path / "report.xml"
    report_path.write_text(PASS_JUNIT_XML)
    _patch_maestro_sequence(monkeypatch, [MaestroResult(success=True, exit_code=0, report_path=report_path)])
    _patch_llm_no_visual_mismatch(monkeypatch)

    update = retest_agent(_real_state(cfg))

    assert update["passed"] is True
    assert update["status"] == "retest_passed"
    finding = update["retest_finding"]
    assert finding["original_defect_resolved"] is True
    assert finding["new_regressions"] == []
    assert finding["build"]["success"] is True
    assert finding["install"]["success"] is True


# --------------------------------------------------------------------------
# Fail: original defect still present
# --------------------------------------------------------------------------

def test_retest_fails_when_original_defect_persists(tmp_path, monkeypatch):
    cfg = _make_config(tmp_path)
    _write_flow(cfg)
    _write_criteria(cfg)
    _patch_platform(monkeypatch)
    _patch_device_for_qa_check(monkeypatch)
    report_path = tmp_path / "report.xml"
    report_path.write_text(FAIL_JUNIT_XML)
    _patch_maestro_sequence(monkeypatch, [MaestroResult(success=False, exit_code=1, report_path=report_path)])
    _patch_llm_no_visual_mismatch(monkeypatch)

    update = retest_agent(_real_state(cfg, attempt_count=1, max_attempts=3))

    assert update["passed"] is False
    assert update["status"] == "retest_failed"
    assert "Login button" in update["last_error"]
    # The fresh failure becomes the next RCA pass's input.
    assert update["qa_finding"]["failure_type"] == "functional"
    assert update["retest_finding"]["original_defect_resolved"] is False


# --------------------------------------------------------------------------
# New regression detected despite original flow passing
# --------------------------------------------------------------------------

def test_retest_detects_new_regression_in_configured_flow(tmp_path, monkeypatch):
    cfg = _make_config(tmp_path)
    cfg.regression_flows = ["smoke_test.yaml"]
    _write_flow(cfg, "login.yaml")
    _write_criteria(cfg, "login.yaml")
    _write_flow(cfg, "smoke_test.yaml")
    _write_criteria(cfg, "smoke_test.yaml")
    _patch_platform(monkeypatch)
    _patch_device_for_qa_check(monkeypatch)

    pass_report = tmp_path / "pass_report.xml"
    pass_report.write_text(PASS_JUNIT_XML)
    fail_report = tmp_path / "fail_report.xml"
    fail_report.write_text(FAIL_JUNIT_XML)
    # login.yaml (primary) passes, smoke_test.yaml (regression) fails.
    _patch_maestro_sequence(
        monkeypatch,
        [
            MaestroResult(success=True, exit_code=0, report_path=pass_report),
            MaestroResult(success=False, exit_code=1, report_path=fail_report),
        ],
    )
    _patch_llm_no_visual_mismatch(monkeypatch)

    update = retest_agent(_real_state(cfg))

    assert update["passed"] is False
    assert update["status"] == "retest_failed"
    finding = update["retest_finding"]
    assert finding["original_defect_resolved"] is True
    assert finding["new_regressions"] == ["smoke_test.yaml"]
    # RCA should now focus on the regression, not the (already-fixed) original flow.
    assert update["qa_finding"]["flow_name"] == "smoke_test.yaml"


def test_retest_runs_all_configured_regression_flows(tmp_path, monkeypatch):
    cfg = _make_config(tmp_path)
    cfg.regression_flows = ["smoke_test.yaml", "checkout_flow.yaml"]
    for name in ("login.yaml", "smoke_test.yaml", "checkout_flow.yaml"):
        _write_flow(cfg, name)
        _write_criteria(cfg, name)
    _patch_platform(monkeypatch)
    _patch_device_for_qa_check(monkeypatch)

    pass_report = tmp_path / "pass_report.xml"
    pass_report.write_text(PASS_JUNIT_XML)
    calls = []

    def fake_run_flow(app_config, flow_name, device_id, runs_root=None, timeout=600.0):
        calls.append(flow_name)
        return MaestroResult(success=True, exit_code=0, report_path=pass_report)

    monkeypatch.setattr(qa_validation_mod, "run_flow", fake_run_flow)
    _patch_llm_no_visual_mismatch(monkeypatch)

    update = retest_agent(_real_state(cfg))

    assert calls == ["login.yaml", "smoke_test.yaml", "checkout_flow.yaml"]
    assert update["passed"] is True
    assert len(update["retest_finding"]["regression_results"]) == 2


# --------------------------------------------------------------------------
# Build/infrastructure errors - never conflated with a defect
# --------------------------------------------------------------------------

def test_retest_build_failure_is_infrastructure_not_a_defect(tmp_path, monkeypatch):
    cfg = _make_config(tmp_path)
    _write_flow(cfg)
    _write_criteria(cfg)
    _patch_platform(monkeypatch, build_success=False, build_error="Compilation failed: unresolved reference")

    update = retest_agent(_real_state(cfg, attempt_count=1, max_attempts=3))

    assert update["passed"] is False
    assert update["status"] == "retest_failed"
    assert "Compilation failed" in update["last_error"]
    assert update["qa_finding"]["failure_type"] == "infrastructure"
    assert update["retest_finding"]["original_defect_resolved"] is False


def test_retest_device_unavailable_is_infrastructure(tmp_path, monkeypatch):
    cfg = _make_config(tmp_path)
    _write_flow(cfg)
    _write_criteria(cfg)
    _patch_platform(monkeypatch, device_success=False)

    update = retest_agent(_real_state(cfg))

    assert update["qa_finding"]["failure_type"] == "infrastructure"
    assert "device" in update["last_error"].lower()


def test_retest_install_failure_is_infrastructure(tmp_path, monkeypatch):
    cfg = _make_config(tmp_path)
    _write_flow(cfg)
    _write_criteria(cfg)
    _patch_platform(monkeypatch, install_success=False, install_error="INSTALL_FAILED_INSUFFICIENT_STORAGE")

    update = retest_agent(_real_state(cfg))

    assert update["qa_finding"]["failure_type"] == "infrastructure"
    assert "INSTALL_FAILED" in update["last_error"]


def test_retest_rejects_invalid_patch_without_building(tmp_path, monkeypatch):
    cfg = _make_config(tmp_path)
    build_calls = []
    monkeypatch.setattr(retest_agent_mod, "build_app", lambda app_config: build_calls.append(1))

    update = retest_agent(_real_state(cfg, dev_finding={"error": "Claude Code invocation failed: timed out"}))

    assert update["passed"] is False
    assert "did not produce a valid patch" in update["last_error"]
    assert update["qa_finding"]["failure_type"] == "infrastructure"
    assert build_calls == []


def test_retest_rejects_patch_that_never_built(tmp_path):
    cfg = _make_config(tmp_path)

    update = retest_agent(_real_state(cfg, dev_finding={"build": {"success": False, "error": "boom"}}))

    assert update["passed"] is False
    assert "did not build successfully" in update["last_error"]


def test_retest_rejects_missing_dev_finding(tmp_path):
    cfg = _make_config(tmp_path)

    update = retest_agent(_real_state(cfg, dev_finding={}))

    assert update["passed"] is False
    assert "No dev_finding" in update["last_error"]


# --------------------------------------------------------------------------
# Retry limit escalation
# --------------------------------------------------------------------------

def test_retest_failure_within_budget_is_retest_failed(tmp_path, monkeypatch):
    cfg = _make_config(tmp_path)
    _write_flow(cfg)
    _write_criteria(cfg)
    _patch_platform(monkeypatch, build_success=False, build_error="boom")

    update = retest_agent(_real_state(cfg, attempt_count=2, max_attempts=3))

    assert update["status"] == "retest_failed"


def test_retest_failure_at_retry_limit_escalates_to_human_review(tmp_path, monkeypatch):
    cfg = _make_config(tmp_path)
    _write_flow(cfg)
    _write_criteria(cfg)
    _patch_platform(monkeypatch, build_success=False, build_error="boom")

    update = retest_agent(_real_state(cfg, attempt_count=3, max_attempts=3))

    assert update["status"] == "needs_human_review"


def test_retest_never_increments_attempt_count_itself(tmp_path, monkeypatch):
    cfg = _make_config(tmp_path)
    _write_flow(cfg)
    _write_criteria(cfg)
    _patch_platform(monkeypatch)
    _patch_device_for_qa_check(monkeypatch)
    report_path = tmp_path / "report.xml"
    report_path.write_text(PASS_JUNIT_XML)
    _patch_maestro_sequence(monkeypatch, [MaestroResult(success=True, exit_code=0, report_path=report_path)])
    _patch_llm_no_visual_mismatch(monkeypatch)

    update = retest_agent(_real_state(cfg, attempt_count=1))

    assert "attempt_count" not in update  # retest_agent leaves attempt_count alone; dev_agent owns the increment


# --------------------------------------------------------------------------
# Never touches GitHub - structurally impossible to duplicate a ticket
# --------------------------------------------------------------------------

def test_retest_module_has_no_github_dependency():
    import nodes.retest_agent as module

    assert not hasattr(module, "create_issue")
    assert not hasattr(module, "comment_on_issue")
    assert not hasattr(module, "get_issue")


# --------------------------------------------------------------------------
# iOS parity
# --------------------------------------------------------------------------

def test_retest_works_for_ios_config(tmp_path, monkeypatch):
    cfg = _make_config(tmp_path, app_name="sample_ios")
    _write_flow(cfg)
    _write_criteria(cfg)
    _patch_platform(monkeypatch, device_id="ABC-123")
    _patch_device_for_qa_check(monkeypatch, device_id="ABC-123")
    report_path = tmp_path / "report.xml"
    report_path.write_text(PASS_JUNIT_XML)
    _patch_maestro_sequence(monkeypatch, [MaestroResult(success=True, exit_code=0, report_path=report_path)])
    _patch_llm_no_visual_mismatch(monkeypatch)

    update = retest_agent(_real_state(cfg))

    assert update["passed"] is True


# --------------------------------------------------------------------------
# Mock mode untouched
# --------------------------------------------------------------------------

def test_mock_mode_is_unchanged(tmp_path):
    cfg = _make_config(tmp_path)
    state = initial_state(app_name=cfg.name, app_config=cfg, mock_scenario="fix_success")
    state["attempt_count"] = 1

    update = retest_agent(state)

    assert update["status"] == "retest_passed"
    assert "retest_finding" not in update
