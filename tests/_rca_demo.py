"""Standalone demo: run the real rca_agent against synthetic failing evidence.

Not a pytest test - a manual demonstration script. Run with:
    python tests/_rca_demo.py

Mocks only the device/git/LLM boundary (same seam the unit tests mock); the
repository search and file-reading tools run for real against a temp repo
this script creates, with one planted bug and one "hallucination trap" file
that the fake LLM will incorrectly name - showing that the agent's
evidence-grounding step drops it.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

from config import load_app_config
from nodes.rca_agent import rca_agent
from state import initial_state
from utils.llm import LLMResult
from utils.platform import PlatformResult
from utils.runner import CommandResult


def main() -> None:
    tmp_dir = Path(tempfile.mkdtemp(prefix="rca_demo_"))
    try:
        _run_demo(tmp_dir)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _run_demo(tmp_dir: Path) -> None:
    app_config = load_app_config("sample_android")
    app_config.clone_path = tmp_dir / "clone"
    app_config.clone_path.mkdir(parents=True)
    (app_config.clone_path / ".git").mkdir()

    login_activity = app_config.clone_path / "app/src/main/java/com/example/LoginActivity.kt"
    login_activity.parent.mkdir(parents=True)
    login_activity.write_text(
        "class LoginActivity {\n"
        "    private var loginButton: Button? = null\n"
        "\n"
        "    fun onLoginButtonClick() {\n"
        "        loginButton!!.setEnabled(false)  // crashes if the view hasn't bound yet\n"
        "    }\n"
        "}\n"
    )
    # A second, unrelated file the fake LLM will (incorrectly) name - the
    # agent must drop this from its final suspected_files/methods.
    (app_config.clone_path / "app/src/main/java/com/example/Analytics.kt").write_text(
        "class Analytics {\n    fun track(event: String) {}\n}\n"
    )

    qa_finding = {
        "flow_name": "login.yaml",
        "passed": False,
        "failure_type": "functional",
        "description": 'Maestro assertion(s) failed: Element "LoginButton" not clickable - app crashed.',
        "expected_behavior": "Tapping the login button navigates to the home screen.",
        "actual_behavior": "App crashed with a NullPointerException when LoginButton was tapped.",
        "reproduction_steps": ["launch and log in"],
        "evidence_paths": [],
        "confidence": 1.0,
    }

    synthetic_device_logs = (
        "E/AndroidRuntime: FATAL EXCEPTION: main\n"
        "E/AndroidRuntime: java.lang.NullPointerException\n"
        "E/AndroidRuntime:     at com.example.LoginActivity.onLoginButtonClick(LoginActivity.kt:5)\n"
    )

    synthetic_llm_response = {
        "root_cause_hypothesis": (
            "LoginActivity.onLoginButtonClick dereferences loginButton with !! before the view "
            "has been bound, throwing a NullPointerException."
        ),
        "suspected_files": [
            "app/src/main/java/com/example/LoginActivity.kt",
            "app/src/main/java/com/example/UnrelatedHallucinatedFile.kt",  # not in evidence - must be dropped
        ],
        "suspected_methods": ["onLoginButtonClick", "someHallucinatedMethod"],  # second must be dropped
        "supporting_evidence": [
            "Device log: java.lang.NullPointerException at LoginActivity.kt:5",
            "Source: loginButton!!.setEnabled(false)",
        ],
        "confidence": 0.85,
        "suggested_fix": "Null-check loginButton (or use a safe call) before calling setEnabled.",
        "missing_information": [],
    }

    state = initial_state(app_name=app_config.name, app_config=app_config, mode="real")
    state["qa_finding"] = qa_finding

    with (
        patch("nodes.rca_agent.select_device", return_value=PlatformResult(success=True, details={"device_id": "emulator-5554"})),
        patch("nodes.rca_agent.capture_logs", return_value=PlatformResult(success=True, logs=synthetic_device_logs)),
        patch("nodes.rca_agent.run_command", side_effect=_fake_git_command),
        patch("nodes.rca_agent.ask_for_json", return_value=LLMResult(success=True, data=synthetic_llm_response)),
    ):
        update = rca_agent(state)

    print("\n================ RCA AGENT OUTPUT (synthetic evidence demo) ================\n")
    print(json.dumps(update["rca_finding"], indent=2))
    print("\nNote: 'UnrelatedHallucinatedFile.kt' and 'someHallucinatedMethod' were in the")
    print("fake LLM's raw response above but are ABSENT from suspected_files/suspected_methods")
    print("below - they were never found in the gathered evidence, so they were dropped and")
    print("recorded in missing_information instead of being trusted.")


def _fake_git_command(argv, cwd, timeout=120.0, env=None):
    if "--name-only" in argv:
        return CommandResult(argv=argv, returncode=0, stdout="app/src/main/java/com/example/LoginActivity.kt\n", stderr="")
    return CommandResult(
        argv=argv,
        returncode=0,
        stdout=(
            "diff --git a/app/src/main/java/com/example/LoginActivity.kt b/app/src/main/java/com/example/LoginActivity.kt\n"
            "+        loginButton!!.setEnabled(false)  // crashes if the view hasn't bound yet\n"
        ),
        stderr="",
    )


if __name__ == "__main__":
    main()
