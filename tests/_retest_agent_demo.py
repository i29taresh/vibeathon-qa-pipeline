"""Standalone demo: run the real retest_agent against synthetic evidence.

Not a pytest test - run with: python tests/_retest_agent_demo.py

Mocks only the device/build/maestro/LLM boundary (same seam the unit tests
mock) - no real adb/xcrun/maestro/gh binary and no real LLM call happens
here, and no real application is touched.

Demonstrates two runs back to back: a retest that still fails (original
defect persists) followed by a retest that passes (fix confirmed) - the
same shape as the retry_success mock scenario, but through the real
validation path.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from config import load_app_config
from nodes.retest_agent import retest_agent
from state import initial_state
from utils.llm import LLMResult
from utils.maestro import MaestroResult
from utils.platform import PlatformResult

FAIL_JUNIT_XML = """<?xml version="1.0"?>
<testsuite name="login" tests="1" failures="1">
  <testcase name="launch and log in" classname="MaestroFlow">
    <failure message="Element not found: LoginButton">Element not found: LoginButton</failure>
  </testcase>
</testsuite>
"""

PASS_JUNIT_XML = """<?xml version="1.0"?>
<testsuite name="login" tests="1" failures="0">
  <testcase name="launch and log in" classname="MaestroFlow"/>
</testsuite>
"""


def main() -> None:
    app_config = load_app_config("sample_android")
    app_config.flows_dir = Path("/tmp/retest_agent_demo/flows")
    app_config.reference_dir = Path("/tmp/retest_agent_demo/references")
    app_config.flows_dir.mkdir(parents=True, exist_ok=True)
    (app_config.flows_dir / "login.yaml").write_text("appId: com.example\n---\n- launchApp\n")
    ref_dir = app_config.reference_dir / "login"
    ref_dir.mkdir(parents=True, exist_ok=True)
    (ref_dir / "acceptance_criteria.md").write_text("Tapping LoginButton must navigate to the home screen.")

    dev_finding = {
        "branch": "ai-fix/42",
        "base_branch": "main",
        "files_changed": ["M app/LoginActivity.kt"],
        "requires_approval": False,
        "build": {"success": True, "artifact_path": "app-debug.apk"},
    }

    print("================ RETEST AGENT DEMO: attempt #1 (fix incomplete) ================\n")
    _run_once(app_config, dev_finding, attempt_count=1, report_xml=FAIL_JUNIT_XML, build_ok=True)

    print("\n================ RETEST AGENT DEMO: attempt #2 (fix confirmed) ================\n")
    _run_once(app_config, dev_finding, attempt_count=2, report_xml=PASS_JUNIT_XML, build_ok=True)


def _run_once(app_config, dev_finding, attempt_count, report_xml, build_ok) -> None:
    report_path = Path("/tmp/retest_agent_demo/report.xml")
    report_path.write_text(report_xml)

    state = initial_state(app_name=app_config.name, app_config=app_config, mode="real", flow_name="login.yaml")
    state["attempt_count"] = attempt_count
    state["max_attempts"] = 3
    state["dev_finding"] = dev_finding

    with (
        patch("nodes.retest_agent.build_app", return_value=PlatformResult(success=build_ok, artifact_path=report_path)),
        patch("nodes.retest_agent.select_device", return_value=PlatformResult(success=True, details={"device_id": "emulator-5554"})),
        patch("nodes.retest_agent.install_app", return_value=PlatformResult(success=True)),
        patch("utils.qa_validation.select_device", return_value=PlatformResult(success=True, details={"device_id": "emulator-5554"})),
        patch("utils.qa_validation.run_flow", return_value=MaestroResult(success="failures=\"0\"" in report_xml, exit_code=0 if "failures=\"0\"" in report_xml else 1, report_path=report_path)),
        patch("utils.qa_validation.ask_for_json", return_value=LLMResult(success=True, data={"visual_mismatch": False})),
    ):
        update = retest_agent(state)

    print(f"status: {update['status']}")
    print(f"passed: {update['passed']}")
    print(json.dumps(update["retest_finding"], indent=2, default=str))


if __name__ == "__main__":
    main()
