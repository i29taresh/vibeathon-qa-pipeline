"""Safe local integration demo for the real dev_agent.

Not a pytest test - run with: python tests/_dev_agent_demo.py

Unlike tests/test_dev_agent_real.py (which mocks utils.runner.run_command
entirely), this demo exercises the *real* git plumbing end-to-end: a real
throwaway git repo is created in a temp directory, and only the `claude`
binary is swapped out (via PATH) for a tiny local fake script that makes a
deterministic, known edit - standing in for what Claude Code would do,
with zero network access and zero real application touched. build_command/
test_command are trivial shell no-ops, so no real build tool runs either.

This proves the branch-create/commit/diff/build/test wiring in
nodes/dev_agent.py works against real `git`, not just against mocks.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import load_app_config  # noqa: E402
from nodes.dev_agent import dev_agent  # noqa: E402
from state import initial_state  # noqa: E402

FAKE_CLAUDE_SCRIPT = """#!/usr/bin/env python3
import json
import os
import sys

# Deterministic stand-in for Claude Code: fixes the known bug in
# LoginActivity.kt and adds a trivial regression test file. Never reads
# the network, never touches anything outside the current directory.
buggy_file = os.path.join(os.getcwd(), "app", "LoginActivity.kt")
with open(buggy_file) as f:
    content = f.read()
content = content.replace("loginButton!!.setEnabled(false)", "loginButton?.setEnabled(false)")
with open(buggy_file, "w") as f:
    f.write(content)

test_file = os.path.join(os.getcwd(), "app", "LoginActivityTest.kt")
with open(test_file, "w") as f:
    f.write("class LoginActivityTest {\\n    fun testLoginButtonNullSafe() {}\\n}\\n")

print(json.dumps({"result": "Fixed null-pointer crash in onLoginButtonClick and added a regression test."}))
"""


def main() -> None:
    tmp_dir = Path(tempfile.mkdtemp(prefix="dev_agent_demo_"))
    fake_bin_dir = tmp_dir / "fakebin"
    repo_dir = tmp_dir / "repo"
    try:
        _run_demo(repo_dir, fake_bin_dir)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _run_git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _run_demo(repo_dir: Path, fake_bin_dir: Path) -> None:
    # --- Set up a disposable local git repo with one planted bug ---
    repo_dir.mkdir(parents=True)
    (repo_dir / "app").mkdir()
    (repo_dir / "app" / "LoginActivity.kt").write_text(
        "class LoginActivity {\n"
        "    private var loginButton: Button? = null\n"
        "\n"
        "    fun onLoginButtonClick() {\n"
        "        loginButton!!.setEnabled(false)\n"
        "    }\n"
        "}\n"
    )
    _run_git(["init", "-b", "main"], repo_dir)
    _run_git(["config", "user.email", "demo@example.com"], repo_dir)
    _run_git(["config", "user.name", "Demo"], repo_dir)
    _run_git(["add", "-A"], repo_dir)
    _run_git(["commit", "-m", "Initial commit with a planted bug"], repo_dir)

    # --- Install a fake `claude` binary on PATH, ahead of any real one ---
    fake_bin_dir.mkdir(parents=True)
    fake_claude = fake_bin_dir / "claude"
    fake_claude.write_text(FAKE_CLAUDE_SCRIPT)
    fake_claude.chmod(fake_claude.stat().st_mode | stat.S_IEXEC)
    os.environ["PATH"] = f"{fake_bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"

    # --- Build an AppConfig pointed at the demo repo, with trivial build/test commands ---
    app_config = load_app_config("sample_android")
    app_config.clone_path = repo_dir
    app_config.build.build_command = "true"  # no real Gradle/Xcode invoked
    app_config.build.test_command = "true"
    app_config.build.artifact_path = "app/LoginActivity.kt"  # just needs to resolve inside clone_path

    state = initial_state(app_name=app_config.name, app_config=app_config, mode="real")
    state["ticket_id"] = "1"
    state["qa_finding"] = {
        "flow_name": "login.yaml",
        "failure_type": "functional",
        "description": 'Maestro assertion(s) failed: Element "LoginButton" not clickable - app crashed.',
        "expected_behavior": "Tapping the login button navigates to the home screen.",
        "actual_behavior": "App crashed with a NullPointerException when LoginButton was tapped.",
        "reproduction_steps": ["Launch the app", "Tap LoginButton"],
    }
    state["rca_finding"] = {
        "root_cause_hypothesis": "onLoginButtonClick dereferences loginButton with !! before it is bound.",
        "suspected_files": ["app/LoginActivity.kt"],
        "suspected_methods": ["onLoginButtonClick"],
        "confidence": 0.85,
        "missing_information": [],
    }

    update = dev_agent(state)

    print("\n================ DEV AGENT INTEGRATION DEMO (real git, fake claude) ================\n")
    print(json.dumps(update["dev_finding"], indent=2))

    fixed_content = (repo_dir / "app" / "LoginActivity.kt").read_text()
    test_file_exists = (repo_dir / "app" / "LoginActivityTest.kt").is_file()
    log_result = subprocess.run(["git", "log", "--oneline", "ai-fix/1"], cwd=repo_dir, capture_output=True, text=True)

    print("\n--- Verification against the real repo on disk ---")
    print(f"status: {update['status']}")
    print(f"Bug actually fixed in working tree: {'loginButton!!' not in fixed_content}")
    print(f"Regression test file actually created: {test_file_exists}")
    print(f"Commits on ai-fix/1 branch:\n{log_result.stdout}")


if __name__ == "__main__":
    main()
