"""Unit tests for nodes.qa_agent's "real" mode.

qa_agent delegates to utils.qa_validation.run_qa_check (shared with
retest_agent), so utils.platform.select_device, utils.maestro.run_flow, and
utils.llm.ask_for_json are mocked at the utils.qa_validation module level -
no real adb/xcrun/maestro binary and no real LLM call happens anywhere in
this file.
"""

from __future__ import annotations

from pathlib import Path

import utils.qa_validation as qa_validation_mod
from config import load_app_config
from nodes.qa_agent import qa_agent
from state import initial_state
from utils.llm import LLMResult
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


def _real_state(app_config, flow_name="login.yaml"):
    return initial_state(app_name=app_config.name, app_config=app_config, mode="real", flow_name=flow_name)


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


def _patch_device(monkeypatch, device_id="emulator-5554", success=True, error=None):
    def fake_select_device(app_config, timeout=30.0):
        if success:
            return PlatformResult(success=True, details={"device_id": device_id})
        return PlatformResult(success=False, error=error or "No device available")

    monkeypatch.setattr(qa_validation_mod, "select_device", fake_select_device)


def _patch_maestro(monkeypatch, result: MaestroResult):
    def fake_run_flow(app_config, flow_name, device_id, runs_root=None, timeout=600.0):
        return result

    monkeypatch.setattr(qa_validation_mod, "run_flow", fake_run_flow)


def _patch_llm(monkeypatch, data=None, success=True):
    def fake_ask_for_json(prompt, images=None, **kwargs):
        if success:
            return LLMResult(success=True, data=data or {"visual_mismatch": False})
        return LLMResult(success=False, error="LLM unavailable")

    monkeypatch.setattr(qa_validation_mod, "ask_for_json", fake_ask_for_json)


# --------------------------------------------------------------------------
# Pass
# --------------------------------------------------------------------------

def test_real_qa_agent_pass(tmp_path, monkeypatch):
    cfg = _make_config(tmp_path)
    _write_flow(cfg)
    _write_criteria(cfg)
    _patch_device(monkeypatch)

    report_path = tmp_path / "report.xml"
    report_path.write_text(PASS_JUNIT_XML)
    screenshot = tmp_path / "final.png"
    screenshot.write_bytes(b"fake-png")
    _patch_maestro(
        monkeypatch,
        MaestroResult(success=True, exit_code=0, run_dir=tmp_path, report_path=report_path, screenshots=[screenshot]),
    )
    _patch_llm(monkeypatch, data={"visual_mismatch": False})

    update = qa_agent(_real_state(cfg))

    assert update["passed"] is True
    assert update["status"] == "no_bugs_found"
    finding = update["qa_finding"]
    assert finding["failure_type"] == "none"
    assert finding["confidence"] == 1.0
    assert str(screenshot) in finding["evidence_paths"]
    assert str(report_path) in finding["evidence_paths"]


# --------------------------------------------------------------------------
# Fail (functional)
# --------------------------------------------------------------------------

def test_real_qa_agent_functional_failure(tmp_path, monkeypatch):
    cfg = _make_config(tmp_path)
    _write_flow(cfg)
    _write_criteria(cfg)
    _patch_device(monkeypatch)

    report_path = tmp_path / "report.xml"
    report_path.write_text(FAIL_JUNIT_XML)
    screenshot = tmp_path / "failure.png"
    screenshot.write_bytes(b"fake-png")
    _patch_maestro(
        monkeypatch,
        MaestroResult(success=False, exit_code=1, run_dir=tmp_path, report_path=report_path, screenshots=[screenshot]),
    )
    # Even if the LLM claims a visual mismatch too, functional failure must win.
    _patch_llm(monkeypatch, data={"visual_mismatch": True, "confidence": 0.9})

    update = qa_agent(_real_state(cfg))

    assert update["passed"] is False
    assert update["status"] == "bug_detected"
    finding = update["qa_finding"]
    assert finding["failure_type"] == "functional"
    assert finding["confidence"] == 1.0
    assert "Login button" in finding["description"]


def test_real_qa_agent_never_passes_on_nonzero_exit_without_junit(tmp_path, monkeypatch):
    cfg = _make_config(tmp_path)
    _write_flow(cfg)
    _write_criteria(cfg)
    _patch_device(monkeypatch)
    _patch_maestro(monkeypatch, MaestroResult(success=False, exit_code=1, error="maestro exited with 1"))
    _patch_llm(monkeypatch)

    update = qa_agent(_real_state(cfg))

    assert update["passed"] is False
    assert update["qa_finding"]["failure_type"] == "functional"


# --------------------------------------------------------------------------
# Visual-only regression (functional pass, visual fail)
# --------------------------------------------------------------------------

def test_real_qa_agent_visual_regression_when_functionally_passing(tmp_path, monkeypatch):
    cfg = _make_config(tmp_path)
    _write_flow(cfg)
    _write_criteria(cfg)
    _patch_device(monkeypatch)

    report_path = tmp_path / "report.xml"
    report_path.write_text(PASS_JUNIT_XML)
    screenshot = tmp_path / "final.png"
    screenshot.write_bytes(b"fake-png")
    _patch_maestro(
        monkeypatch,
        MaestroResult(success=True, exit_code=0, run_dir=tmp_path, report_path=report_path, screenshots=[screenshot]),
    )
    _patch_llm(
        monkeypatch,
        data={
            "visual_mismatch": True,
            "description": "Login button is the wrong color.",
            "confidence": 0.7,
        },
    )

    update = qa_agent(_real_state(cfg))

    assert update["passed"] is False
    finding = update["qa_finding"]
    assert finding["failure_type"] == "visual"
    assert finding["confidence"] == 0.7
    assert "wrong color" in finding["description"]


# --------------------------------------------------------------------------
# Missing references
# --------------------------------------------------------------------------

def test_real_qa_agent_missing_acceptance_criteria(tmp_path, monkeypatch):
    cfg = _make_config(tmp_path)
    _write_flow(cfg)
    # No acceptance criteria written.

    device_calls = []
    maestro_calls = []
    monkeypatch.setattr(
        qa_validation_mod, "select_device", lambda *a, **k: device_calls.append(1) or PlatformResult(success=True, details={"device_id": "x"})
    )
    monkeypatch.setattr(
        qa_validation_mod, "run_flow", lambda *a, **k: maestro_calls.append(1) or MaestroResult(success=True, exit_code=0)
    )

    update = qa_agent(_real_state(cfg))

    assert update["passed"] is False
    finding = update["qa_finding"]
    assert finding["failure_type"] == "unverified"
    assert finding["confidence"] == 0.0
    assert "No acceptance criteria" in finding["description"]
    # Missing references short-circuits before touching a device or running Maestro.
    assert device_calls == []
    assert maestro_calls == []


def test_real_qa_agent_blank_criteria_file_is_treated_as_missing(tmp_path, monkeypatch):
    cfg = _make_config(tmp_path)
    _write_flow(cfg)
    _write_criteria(cfg, text="   \n   ")

    update = qa_agent(_real_state(cfg))

    assert update["qa_finding"]["failure_type"] == "unverified"


# --------------------------------------------------------------------------
# Infrastructure errors
# --------------------------------------------------------------------------

def test_real_qa_agent_flow_file_missing_is_infrastructure(tmp_path, monkeypatch):
    cfg = _make_config(tmp_path)
    # Flow file is never written.

    update = qa_agent(_real_state(cfg, flow_name="missing.yaml"))

    assert update["passed"] is False
    finding = update["qa_finding"]
    assert finding["failure_type"] == "infrastructure"
    assert "not found" in finding["description"]


def test_real_qa_agent_flow_path_traversal_is_infrastructure(tmp_path, monkeypatch):
    cfg = _make_config(tmp_path)
    _write_criteria(cfg)

    update = qa_agent(_real_state(cfg, flow_name="../../etc/passwd"))

    assert update["passed"] is False
    assert update["qa_finding"]["failure_type"] == "infrastructure"


def test_real_qa_agent_device_unavailable_is_infrastructure(tmp_path, monkeypatch):
    cfg = _make_config(tmp_path)
    _write_flow(cfg)
    _write_criteria(cfg)
    _patch_device(monkeypatch, success=False, error="emulator 'pixel_7_api_34' is not booted")

    update = qa_agent(_real_state(cfg))

    assert update["passed"] is False
    finding = update["qa_finding"]
    assert finding["failure_type"] == "infrastructure"
    assert "not booted" in finding["description"]
    assert finding["confidence"] == 1.0


def test_real_qa_agent_maestro_binary_missing_is_infrastructure(tmp_path, monkeypatch):
    cfg = _make_config(tmp_path)
    _write_flow(cfg)
    _write_criteria(cfg)
    _patch_device(monkeypatch)
    _patch_maestro(monkeypatch, MaestroResult(success=False, exit_code=None, error="Command not found: maestro"))

    update = qa_agent(_real_state(cfg))

    assert update["passed"] is False
    finding = update["qa_finding"]
    assert finding["failure_type"] == "infrastructure"
    assert "Command not found" in finding["description"]


# --------------------------------------------------------------------------
# iOS runs through the same code path
# --------------------------------------------------------------------------

def test_real_qa_agent_works_for_ios_config(tmp_path, monkeypatch):
    cfg = _make_config(tmp_path, app_name="sample_ios")
    _write_flow(cfg)
    _write_criteria(cfg)
    _patch_device(monkeypatch, device_id="ABC-123")

    report_path = tmp_path / "report.xml"
    report_path.write_text(PASS_JUNIT_XML)
    _patch_maestro(monkeypatch, MaestroResult(success=True, exit_code=0, report_path=report_path))
    _patch_llm(monkeypatch, data={"visual_mismatch": False})

    update = qa_agent(_real_state(cfg))

    assert update["passed"] is True
    assert update["qa_finding"]["failure_type"] == "none"
