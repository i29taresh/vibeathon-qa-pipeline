"""End-to-end real-mode integration tests, run through the actual compiled graph.

Unlike the per-node real-mode test files, this exercises the whole pipeline
via graph.run_pipeline() for both Android and iOS: real LangGraph routing,
a real disposable git repo (with a real local bare "origin" remote so
dev_agent/merge_step's git branch/commit/push machinery is genuinely
exercised), and real local file/text operations (rca_agent's repository
search, JUnit parsing). Only genuinely external integrations are mocked:
device/emulator access, Maestro, the LLM, the Claude Code CLI, and GitHub -
no real adb/xcrun/maestro/claude/gh binary and no real network call happens
anywhere in this file, and no real network remote is ever pushed to.

Scenario: QA fails once, the first fix attempt still fails retest, the
second fix attempt passes - the same shape as the `retry_success` mock
scenario, proving requirement #7 (no duplicate ticket, no recreated fix
branch across that retry) through the real graph, not just at the unit level.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import nodes.dev_agent as dev_agent_mod
import nodes.merge_step as merge_step_mod
import nodes.rca_agent as rca_agent_mod
import nodes.retest_agent as retest_agent_mod
import nodes.ticket_agent as ticket_agent_mod
import utils.qa_validation as qa_validation_mod
from config import load_app_config
from graph import run_pipeline
from state import initial_state
from utils.github import AuthStatus, IssueResult, PullRequestResult
from utils.llm import LLMResult
from utils.maestro import MaestroResult
from utils.platform import PlatformResult
from utils.runner import CommandResult, run_command as real_run_command

FAIL_JUNIT = """<?xml version="1.0"?>
<testsuite name="login" tests="1" failures="1">
  <testcase name="launch and log in" classname="MaestroFlow">
    <failure message="Element not found: LoginButton">Element not found: LoginButton</failure>
  </testcase>
</testsuite>
"""

PASS_JUNIT = """<?xml version="1.0"?>
<testsuite name="login" tests="1" failures="0">
  <testcase name="launch and log in" classname="MaestroFlow"/>
</testsuite>
"""

BUGGY_FILE = (
    "class LoginActivity {\n"
    "    // Handles LoginButton taps\n"
    "    fun onLoginButtonClick() {\n"
    "        loginButton!!.setEnabled(false)\n"
    "    }\n"
    "}\n"
)


def _run_git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _setup_repo(tmp_path: Path) -> tuple[Path, Path]:
    clone_dir = tmp_path / "clone"
    bare_dir = tmp_path / "bare.git"
    (clone_dir / "app").mkdir(parents=True)
    (clone_dir / "app" / "LoginActivity.kt").write_text(BUGGY_FILE)

    subprocess.run(["git", "init", "--bare", str(bare_dir)], check=True, capture_output=True, text=True)

    _run_git(["init", "-b", "main"], clone_dir)
    _run_git(["config", "user.email", "demo@example.com"], clone_dir)
    _run_git(["config", "user.name", "Demo"], clone_dir)
    _run_git(["remote", "add", "origin", str(bare_dir)], clone_dir)
    _run_git(["add", "-A"], clone_dir)
    _run_git(["commit", "-m", "Initial commit with a planted bug"], clone_dir)

    return clone_dir, bare_dir


def _setup_app_config(tmp_path: Path, app_name: str, clone_dir: Path):
    app_config = load_app_config(app_name)
    app_config.clone_path = clone_dir
    app_config.flows_dir = tmp_path / "flows"
    app_config.reference_dir = tmp_path / "references"
    app_config.build.test_command = None
    app_config.flows_dir.mkdir(parents=True)
    (app_config.flows_dir / "login.yaml").write_text("appId: com.example\n---\n- launchApp\n")
    ref_dir = app_config.reference_dir / "login"
    ref_dir.mkdir(parents=True)
    (ref_dir / "acceptance_criteria.md").write_text("Tapping LoginButton must navigate to the home screen.")
    return app_config


def _make_fake_claude(repo_dir: Path):
    call_count = {"n": 0}

    def apply_fix() -> None:
        call_count["n"] += 1
        n = call_count["n"]
        (repo_dir / "app" / "LoginActivity.kt").write_text(
            "class LoginActivity {\n"
            "    // Handles LoginButton taps\n"
            "    fun onLoginButtonClick() {\n"
            "        loginButton?.setEnabled(false)  // fixed: null-safe LoginButton access\n"
            f"        // fix attempt #{n}\n"
            "    }\n"
            "}\n"
        )
        (repo_dir / "app" / "LoginActivityTest.kt").write_text(
            f"class LoginActivityTest {{\n    fun testLoginButtonNullSafe_{n}() {{}}\n}}\n"
        )

    return call_count, apply_fix


def _patch_all_external(monkeypatch, repo_dir: Path, maestro_results: list[MaestroResult]):
    # --- Device / Maestro / LLM, used by utils.qa_validation.run_qa_check
    #     (shared by both qa_agent and retest_agent) ---
    monkeypatch.setattr(
        qa_validation_mod, "select_device", lambda app_config, timeout=30.0: PlatformResult(success=True, details={"device_id": "emulator-5554"})
    )
    sequence = list(maestro_results)
    monkeypatch.setattr(
        qa_validation_mod, "run_flow", lambda app_config, flow_name, device_id, runs_root=None, timeout=600.0: sequence.pop(0)
    )
    monkeypatch.setattr(qa_validation_mod, "ask_for_json", lambda prompt, images=None, **k: LLMResult(success=True, data={"visual_mismatch": False}))

    # --- rca_agent's own device-log capture and LLM call ---
    monkeypatch.setattr(rca_agent_mod, "select_device", lambda app_config, timeout=30.0: PlatformResult(success=True, details={"device_id": "emulator-5554"}))
    monkeypatch.setattr(
        rca_agent_mod,
        "capture_logs",
        lambda app_config, device_id, timeout=60.0: PlatformResult(success=True, logs="E/AndroidRuntime: NullPointerException touching LoginButton\n"),
    )
    monkeypatch.setattr(
        rca_agent_mod,
        "ask_for_json",
        lambda prompt, images=None, **k: LLMResult(
            success=True,
            data={
                "root_cause_hypothesis": "onLoginButtonClick dereferences loginButton with !! before it is bound.",
                "suspected_files": ["app/LoginActivity.kt"],
                "suspected_methods": ["onLoginButtonClick"],
                "supporting_evidence": ["NullPointerException touching LoginButton"],
                "confidence": 0.8,
                "suggested_fix": "Use the safe-call operator instead of !!.",
                "missing_information": [],
            },
        ),
    )

    # --- ticket_agent: mocked GitHub issue create/comment ---
    monkeypatch.setattr(ticket_agent_mod, "check_auth", lambda repo, cwd: AuthStatus(authenticated=True, can_write=True))
    create_issue_calls: list[dict] = []
    comment_calls: list[dict] = []

    def fake_create_issue(repo, title, body, cwd, labels=None, attachments=None, timeout=30.0):
        create_issue_calls.append({"title": title})
        return IssueResult(success=True, number=99, url="https://github.com/acme/app/issues/99")

    def fake_comment_on_issue(repo, issue_number, body, cwd, attachments=None, timeout=30.0):
        comment_calls.append({"issue_number": issue_number})
        return IssueResult(success=True, number=issue_number, url="https://github.com/acme/app/issues/99")

    monkeypatch.setattr(ticket_agent_mod, "create_issue", fake_create_issue)
    monkeypatch.setattr(ticket_agent_mod, "comment_on_issue", fake_comment_on_issue)

    # --- dev_agent: mocked issue fetch + build; claude faked, git left real ---
    monkeypatch.setattr(dev_agent_mod, "get_issue", lambda repo, number, cwd: IssueResult(success=False, error="not fetched in this test"))
    monkeypatch.setattr(dev_agent_mod, "build_app", lambda app_config: PlatformResult(success=True, artifact_path=repo_dir / "app-debug.apk"))

    call_count, apply_fix = _make_fake_claude(repo_dir)
    checkout_calls: list[list[str]] = []

    def fake_dev_run_command(argv, cwd, timeout=120.0, env=None):
        if argv and argv[0] == "claude":
            apply_fix()
            return CommandResult(
                argv=argv, returncode=0,
                stdout=json.dumps({"result": f"Fixed LoginButton null-pointer crash (attempt #{call_count['n']})."}),
                stderr="",
            )
        if argv[:2] == ["git", "checkout"]:
            checkout_calls.append(list(argv))
        return real_run_command(argv, cwd=cwd, timeout=timeout, env=env)

    monkeypatch.setattr(dev_agent_mod, "run_command", fake_dev_run_command)

    # --- retest_agent: mocked build/install/device (maestro/LLM already covered above) ---
    monkeypatch.setattr(retest_agent_mod, "build_app", lambda app_config: PlatformResult(success=True, artifact_path=repo_dir / "app-debug.apk"))
    monkeypatch.setattr(retest_agent_mod, "select_device", lambda app_config: PlatformResult(success=True, details={"device_id": "emulator-5554"}))
    monkeypatch.setattr(retest_agent_mod, "install_app", lambda app_config, device_id, artifact_path: PlatformResult(success=True))

    # --- merge_step: mocked GitHub auth/PR lookup/create; git (diff/status/remote/push) left real ---
    monkeypatch.setattr(merge_step_mod, "check_auth", lambda repo, cwd: AuthStatus(authenticated=True, can_write=True))
    monkeypatch.setattr(merge_step_mod, "get_pull_request_for_branch", lambda repo, branch, cwd: PullRequestResult(success=False, error="no pull requests found"))
    create_pr_calls: list[dict] = []

    def fake_create_pull_request(repo, title, body, head, base, cwd, draft=True, timeout=30.0):
        create_pr_calls.append({"head": head, "base": base})
        return PullRequestResult(success=True, number=5, url="https://github.com/acme/app/pull/5")

    monkeypatch.setattr(merge_step_mod, "create_pull_request", fake_create_pull_request)

    return {
        "create_issue_calls": create_issue_calls,
        "comment_calls": comment_calls,
        "create_pr_calls": create_pr_calls,
        "checkout_calls": checkout_calls,
        "claude_calls": call_count,
    }


@pytest.mark.parametrize("app_name", ["sample_android", "sample_ios"])
def test_full_pipeline_fails_once_then_succeeds(tmp_path, monkeypatch, app_name):
    clone_dir, _bare_dir = _setup_repo(tmp_path)
    app_config = _setup_app_config(tmp_path, app_name, clone_dir)

    maestro_sequence = [
        MaestroResult(success=False, exit_code=1, report_path=_write_report(tmp_path, "r1.xml", FAIL_JUNIT)),  # initial qa_agent
        MaestroResult(success=False, exit_code=1, report_path=_write_report(tmp_path, "r2.xml", FAIL_JUNIT)),  # retest attempt 1
        MaestroResult(success=True, exit_code=0, report_path=_write_report(tmp_path, "r3.xml", PASS_JUNIT)),  # retest attempt 2
    ]
    spies = _patch_all_external(monkeypatch, clone_dir, maestro_sequence)

    state = initial_state(app_name=app_name, app_config=app_config, mode="real", flow_name="login.yaml")
    final_state = run_pipeline(state)

    assert final_state["status"] == "ready_for_review"
    assert final_state["passed"] is True
    assert final_state["pr_url"] == "https://github.com/acme/app/pull/5"
    assert final_state["ticket_id"] == "99"
    assert final_state["attempt_count"] == 2

    nodes_run = [r["node"] for r in final_state["execution_history"]]
    assert nodes_run == [
        "qa_agent", "rca_agent", "ticket_agent", "dev_agent", "retest_agent",
        "rca_agent", "ticket_agent", "dev_agent", "retest_agent",
        "merge_step",
    ]

    # Requirement #7: no duplicate ticket, no recreated fix branch across the retry.
    assert len(spies["create_issue_calls"]) == 1
    assert len(spies["comment_calls"]) == 1
    assert spies["checkout_calls"] == [["git", "checkout", "-b", "ai-fix/99", "main"]]  # only ever created once
    assert len(spies["create_pr_calls"]) == 1
    assert spies["create_pr_calls"][0]["head"] == "ai-fix/99"
    assert spies["create_pr_calls"][0]["base"] == "main"
    assert spies["claude_calls"]["n"] == 2  # exactly one Claude Code invocation per attempt

    # requirement #8: evidence/findings survived the whole run.
    assert final_state["rca_finding"]["root_cause_hypothesis"]
    assert final_state["dev_finding"]["branch"] == "ai-fix/99"
    assert final_state["retest_finding"]["original_defect_resolved"] is True

    # Branch actually exists on the local "origin" remote - a real push happened.
    log_result = subprocess.run(
        ["git", "branch", "-r"], cwd=clone_dir, check=True, capture_output=True, text=True
    )
    assert "origin/ai-fix/99" in log_result.stdout


def _write_report(tmp_path: Path, name: str, content: str) -> Path:
    path = tmp_path / name
    path.write_text(content)
    return path


def test_qa_pass_terminates_immediately_without_ticket_or_dev_work(tmp_path, monkeypatch):
    clone_dir, _bare_dir = _setup_repo(tmp_path)
    app_config = _setup_app_config(tmp_path, "sample_android", clone_dir)

    spies = _patch_all_external(monkeypatch, clone_dir, [MaestroResult(success=True, exit_code=0, report_path=_write_report(tmp_path, "pass.xml", PASS_JUNIT))])

    state = initial_state(app_name="sample_android", app_config=app_config, mode="real", flow_name="login.yaml")
    final_state = run_pipeline(state)

    assert final_state["status"] == "no_bugs_found"
    assert final_state["passed"] is True
    assert [r["node"] for r in final_state["execution_history"]] == ["qa_agent"]
    assert spies["create_issue_calls"] == []
    assert spies["claude_calls"]["n"] == 0


def test_unverified_qa_result_is_blocked_without_ticket(tmp_path, monkeypatch):
    clone_dir, _bare_dir = _setup_repo(tmp_path)
    app_config = _setup_app_config(tmp_path, "sample_android", clone_dir)
    # Remove the acceptance criteria that _setup_app_config wrote, so QA can't verify anything.
    (app_config.reference_dir / "login" / "acceptance_criteria.md").unlink()

    spies = _patch_all_external(monkeypatch, clone_dir, [])

    state = initial_state(app_name="sample_android", app_config=app_config, mode="real", flow_name="login.yaml")
    final_state = run_pipeline(state)

    assert final_state["qa_finding"]["failure_type"] == "unverified"
    assert final_state["passed"] is False
    assert [r["node"] for r in final_state["execution_history"]] == ["qa_agent"]
    assert spies["create_issue_calls"] == []
