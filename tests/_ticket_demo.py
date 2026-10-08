"""Standalone demo: render a ticket_agent bug report in dry-run mode.

Not a pytest test - run with: python tests/_ticket_demo.py
Uses dry_run=True, which never calls `gh` at all - this is the safe way to
preview a report locally without any risk of hitting a real GitHub repo.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from config import load_app_config
from nodes.ticket_agent import ticket_agent
from state import initial_state


def main() -> None:
    tmp_dir = Path(tempfile.mkdtemp(prefix="ticket_demo_"))
    try:
        _run_demo(tmp_dir)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _run_demo(tmp_dir: Path) -> None:
    app_config = load_app_config("sample_android")
    app_config.clone_path = tmp_dir

    qa_finding = {
        "flow_name": "login.yaml",
        "passed": False,
        "failure_type": "functional",
        "description": 'Maestro assertion(s) failed: Element "LoginButton" not clickable - app crashed.',
        "expected_behavior": "Tapping the login button navigates to the home screen.",
        "actual_behavior": "App crashed with a NullPointerException when LoginButton was tapped.",
        "reproduction_steps": ["Launch the app", "Enter credentials", "Tap LoginButton"],
        "evidence_paths": [],
        "confidence": 1.0,
    }
    rca_finding = {
        "root_cause_hypothesis": (
            "LoginActivity.onLoginButtonClick dereferences loginButton with !! before the view "
            "has been bound, throwing a NullPointerException."
        ),
        "suspected_files": ["app/src/main/java/com/example/LoginActivity.kt"],
        "suspected_methods": ["onLoginButtonClick"],
        "supporting_evidence": ["Device log: NullPointerException at LoginActivity.kt:5"],
        "confidence": 0.85,
        "suggested_fix": "Null-check loginButton (or use a safe call) before calling setEnabled.",
        "missing_information": [],
    }

    state = initial_state(app_name=app_config.name, app_config=app_config, mode="real", dry_run=True)
    state["qa_finding"] = qa_finding
    state["rca_finding"] = rca_finding

    update = ticket_agent(state)

    print("\n================ TICKET AGENT DRY RUN (no gh call made) ================\n")
    print(f"Title:\n{update['ticket_finding']['title']}\n")
    print("Body:")
    print(update["ticket_finding"]["body"])


if __name__ == "__main__":
    main()
