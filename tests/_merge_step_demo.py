"""Standalone demo: render a merge_step PR body in dry-run mode.

Not a pytest test - run with: python tests/_merge_step_demo.py

Uses dry_run=True, which never calls `git push` or `gh pr create` - but
dry-run still runs the real validation (branch-drift check, clean-tree
check) against an actual git repo, so this builds a small disposable one
with a baseline commit on `main` and a matching fix commit on `ai-fix/42`,
exactly mirroring what dev_agent would have produced.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from config import load_app_config
from nodes.merge_step import merge_step
from state import initial_state


def _run_git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def main() -> None:
    tmp_dir = Path(tempfile.mkdtemp(prefix="merge_step_demo_"))
    try:
        _run_demo(tmp_dir)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _run_demo(repo_dir: Path) -> None:
    (repo_dir / "app").mkdir(parents=True)
    login_activity = repo_dir / "app" / "LoginActivity.kt"
    login_activity.write_text("class LoginActivity {\n    loginButton!!.setEnabled(false)\n}\n")

    _run_git(["init", "-b", "main"], repo_dir)
    _run_git(["config", "user.email", "demo@example.com"], repo_dir)
    _run_git(["config", "user.name", "Demo"], repo_dir)
    _run_git(["add", "-A"], repo_dir)
    _run_git(["commit", "-m", "Initial commit with a planted bug"], repo_dir)

    _run_git(["checkout", "-b", "ai-fix/42"], repo_dir)
    login_activity.write_text("class LoginActivity {\n    loginButton?.setEnabled(false)\n}\n")
    (repo_dir / "app" / "LoginActivityTest.kt").write_text("class LoginActivityTest {\n    fun test() {}\n}\n")
    _run_git(["add", "-A"], repo_dir)
    _run_git(["commit", "-m", "ai-fix: attempt #1 for ai-fix/42"], repo_dir)

    app_config = load_app_config("sample_android")
    app_config.clone_path = repo_dir

    state = initial_state(app_name=app_config.name, app_config=app_config, mode="real", dry_run=True)
    state["ticket_id"] = "42"
    state["passed"] = True
    state["status"] = "retest_passed"
    state["qa_finding"] = {
        "flow_name": "login.yaml",
        "description": 'Maestro assertion(s) failed: Element "LoginButton" not clickable - app crashed.',
        "evidence_paths": ["/tmp/qa_run/before_crash.png"],
    }
    state["rca_finding"] = {
        "root_cause_hypothesis": (
            "LoginActivity.onLoginButtonClick dereferences loginButton with !! before the view "
            "has been bound, throwing a NullPointerException."
        ),
        "suspected_files": ["app/LoginActivity.kt"],
        "missing_information": [],
    }
    state["dev_finding"] = {
        "branch": "ai-fix/42",
        "base_branch": "main",
        "files_changed": ["M app/LoginActivity.kt", "?? app/LoginActivityTest.kt"],
        "requires_approval": False,
        "risk_flags": [],
        "claude_summary": "Null-checked loginButton before calling setEnabled, and added a regression test.",
        "diff_summary": " LoginActivity.kt     | 2 +-\n LoginActivityTest.kt | 3 +++\n 2 files changed, 4 insertions(+), 1 deletion(-)",
        "build": {"success": True, "artifact_path": "app-debug.apk"},
        "tests": {"ran": True, "success": True, "summary": "Tests passed."},
    }
    state["retest_finding"] = {
        "primary_result": {
            "flow_name": "login.yaml",
            "passed": True,
            "evidence_paths": ["/tmp/retest_run/after_fix.png"],
        },
        "regression_results": [],
        "original_defect_resolved": True,
        "new_regressions": [],
        "build": {"success": True, "artifact_path": "app-debug.apk"},
        "install": {"success": True, "device_id": "emulator-5554"},
    }

    update = merge_step(state)

    print("\n================ MERGE STEP DRY RUN (real repo, no push, no gh call) ================\n")
    print(f"status: {update['status']}")
    print(f"\nTitle:\n{update['merge_finding']['title']}\n")
    print("Body:")
    print(update["merge_finding"]["body"])


if __name__ == "__main__":
    main()
