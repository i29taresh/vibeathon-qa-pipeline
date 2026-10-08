"""Unit tests for nodes.dev_agent's "real" mode.

utils.runner.run_command (used for all git calls, the Claude Code CLI
invocation, and the test command) and utils.github.get_issue /
utils.platform.build_app are all mocked at the nodes.dev_agent module level
- no real `claude`, `git`, `gh`, or build-tool binary is ever invoked here,
and no real application is ever modified.
"""

from __future__ import annotations

from pathlib import Path

import nodes.dev_agent as dev_agent_mod
from config import load_app_config
from nodes.dev_agent import dev_agent
from state import initial_state
from utils.github import IssueResult
from utils.platform import PlatformResult
from utils.runner import CommandResult


def _qa_finding(**overrides):
    finding = {
        "flow_name": "login.yaml",
        "failure_type": "functional",
        "description": 'Maestro assertion(s) failed: Element not found: "LoginButton"',
        "expected_behavior": "Tapping LoginButton navigates to the home screen.",
        "actual_behavior": 'Element not found: "LoginButton"',
        "reproduction_steps": ["launch and log in"],
    }
    finding.update(overrides)
    return finding


def _rca_finding(**overrides):
    finding = {
        "root_cause_hypothesis": "LoginButton click handler throws before navigation completes.",
        "suspected_files": ["app/LoginActivity.kt"],
        "suspected_methods": ["onLoginButtonClick"],
        "confidence": 0.8,
        "missing_information": [],
    }
    finding.update(overrides)
    return finding


def _real_state(app_config, ticket_id="42", **overrides):
    state = initial_state(app_name=app_config.name, app_config=app_config, mode="real")
    state["ticket_id"] = ticket_id
    state["qa_finding"] = _qa_finding()
    state["rca_finding"] = _rca_finding()
    state.update(overrides)
    return state


def _make_repo(tmp_path: Path, app_name: str = "sample_android"):
    cfg = load_app_config(app_name)
    cfg.clone_path = tmp_path / "clone"
    cfg.clone_path.mkdir(parents=True)
    (cfg.clone_path / ".git").mkdir()
    return cfg


class _GitScript:
    """Scripts fake `git`/`claude`/test-command responses for run_command.

    Branch state is tracked in-memory so _ensure_fix_branch's checkout/
    create logic behaves like a real repo without touching one.
    """

    def __init__(self, current_branch="main", existing_branches=("main",), claude_response=None, claude_ok=True):
        self.current_branch = current_branch
        self.branches = set(existing_branches)
        self.claude_response = claude_response if claude_response is not None else {"result": "Fixed the bug."}
        self.claude_ok = claude_ok
        self.committed = False
        self.claude_invoked = False
        self.calls: list[list[str]] = []
        self.porcelain_status = " M app/LoginActivity.kt\n"

    def __call__(self, argv, cwd, timeout=120.0, env=None):
        self.calls.append(list(argv))

        if argv[0] == "claude":
            self.claude_invoked = True
            if not self.claude_ok:
                return CommandResult(argv=argv, returncode=1, stdout="", stderr="claude: not authenticated")
            import json as _json

            return CommandResult(argv=argv, returncode=0, stdout=_json.dumps(self.claude_response), stderr="")

        if argv[:2] == ["git", "rev-parse"] and "--abbrev-ref" in argv:
            return CommandResult(argv=argv, returncode=0, stdout=self.current_branch + "\n", stderr="")

        if argv[:2] == ["git", "rev-parse"] and "--verify" in argv:
            branch = argv[-1]
            ok = branch in self.branches
            return CommandResult(argv=argv, returncode=0 if ok else 1, stdout="", stderr="" if ok else "not found")

        if argv[:2] == ["git", "status"]:
            # Before Claude Code has run, the tree is clean; after it runs
            # (and until we commit), it shows whatever this test configured.
            if self.committed or not self.claude_invoked:
                status = ""
            else:
                status = self.porcelain_status
            return CommandResult(argv=argv, returncode=0, stdout=status, stderr="")

        if argv[:2] == ["git", "checkout"] and "-b" in argv:
            new_branch = argv[argv.index("-b") + 1]
            self.branches.add(new_branch)
            self.current_branch = new_branch
            return CommandResult(argv=argv, returncode=0, stdout="", stderr="")

        if argv[:2] == ["git", "checkout"]:
            self.current_branch = argv[2]
            return CommandResult(argv=argv, returncode=0, stdout="", stderr="")

        if argv[:2] == ["git", "add"]:
            return CommandResult(argv=argv, returncode=0, stdout="", stderr="")

        if argv[:2] == ["git", "commit"]:
            self.committed = True
            return CommandResult(argv=argv, returncode=0, stdout="", stderr="")

        if argv[:2] == ["git", "diff"]:
            return CommandResult(argv=argv, returncode=0, stdout=" app/LoginActivity.kt | 2 +-\n", stderr="")

        return CommandResult(argv=argv, returncode=0, stdout="", stderr="")


def _patch_issue_and_build(monkeypatch, build_success=True, build_error=None):
    monkeypatch.setattr(
        dev_agent_mod, "get_issue", lambda repo, number, cwd: IssueResult(success=False, error="not fetched in this test")
    )
    monkeypatch.setattr(
        dev_agent_mod,
        "build_app",
        lambda app_config: PlatformResult(success=build_success, error=build_error, artifact_path=Path("app-debug.apk")),
    )


# --------------------------------------------------------------------------
# Happy path
# --------------------------------------------------------------------------

def test_applies_fix_builds_and_runs_tests(tmp_path, monkeypatch):
    cfg = _make_repo(tmp_path)
    cfg.build.test_command = "./gradlew test"
    script = _GitScript(claude_response={"result": "Added a null check and a regression test."})

    def fake_run_command(argv, cwd, timeout=120.0, env=None):
        if argv[:1] == ["./gradlew"] or (argv and "gradlew" in argv[0]):
            return CommandResult(argv=argv, returncode=0, stdout="BUILD SUCCESSFUL", stderr="")
        return script(argv, cwd, timeout, env)

    monkeypatch.setattr(dev_agent_mod, "run_command", fake_run_command)
    _patch_issue_and_build(monkeypatch)

    update = dev_agent(_real_state(cfg))

    assert update["status"] == "fix_applied"
    assert update["attempt_count"] == 1
    finding = update["dev_finding"]
    assert finding["branch"] == "ai-fix/42"
    assert finding["base_branch"] == "main"
    assert finding["files_changed"] == ["M app/LoginActivity.kt"]
    assert finding["requires_approval"] is False
    assert "Added a null check" in finding["claude_summary"]
    assert finding["build"]["success"] is True
    assert finding["tests"]["ran"] is True
    assert finding["tests"]["success"] is True


def test_reuses_existing_fix_branch_on_retry(tmp_path, monkeypatch):
    cfg = _make_repo(tmp_path)
    script = _GitScript(current_branch="ai-fix/42", existing_branches=("main", "ai-fix/42"))
    monkeypatch.setattr(dev_agent_mod, "run_command", script)
    _patch_issue_and_build(monkeypatch)

    update = dev_agent(_real_state(cfg, attempt_count=1))

    checkout_calls = [c for c in script.calls if c[:2] == ["git", "checkout"]]
    assert checkout_calls == []  # already on the branch - no checkout needed
    assert update["status"] == "fix_applied"
    assert update["attempt_count"] == 2
    assert update["dev_finding"]["branch"] == "ai-fix/42"


def test_never_passes_dangerous_permission_bypass(tmp_path, monkeypatch):
    cfg = _make_repo(tmp_path)
    script = _GitScript()
    monkeypatch.setattr(dev_agent_mod, "run_command", script)
    _patch_issue_and_build(monkeypatch)

    dev_agent(_real_state(cfg))

    claude_calls = [c for c in script.calls if c[0] == "claude"]
    assert len(claude_calls) == 1
    argv = claude_calls[0]
    assert "--dangerously-skip-permissions" not in argv
    assert "--add-dir" not in argv
    assert "--permission-mode" in argv
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"
    assert "--allowedTools" in argv
    allowed = argv[argv.index("--allowedTools") + 1]
    assert "Bash" not in allowed


def test_invokes_claude_with_cwd_scoped_to_clone_path(tmp_path, monkeypatch):
    cfg = _make_repo(tmp_path)
    script = _GitScript()
    captured_cwd = {}

    def fake_run_command(argv, cwd, timeout=120.0, env=None):
        if argv and argv[0] == "claude":
            captured_cwd["cwd"] = cwd
        return script(argv, cwd, timeout, env)

    monkeypatch.setattr(dev_agent_mod, "run_command", fake_run_command)
    _patch_issue_and_build(monkeypatch)

    dev_agent(_real_state(cfg))

    assert captured_cwd["cwd"] == cfg.clone_path


# --------------------------------------------------------------------------
# No test_command configured
# --------------------------------------------------------------------------

def test_skips_tests_when_no_test_command_configured(tmp_path, monkeypatch):
    cfg = _make_repo(tmp_path)  # sample_android has no test_command by default unless we set one
    cfg.build.test_command = None
    script = _GitScript()
    monkeypatch.setattr(dev_agent_mod, "run_command", script)
    _patch_issue_and_build(monkeypatch)

    update = dev_agent(_real_state(cfg))

    assert update["dev_finding"]["tests"] == {
        "ran": False,
        "success": None,
        "summary": "No test_command configured for this app - skipped.",
    }


# --------------------------------------------------------------------------
# Build failure
# --------------------------------------------------------------------------

def test_build_failure_is_structured_and_distinct_from_dev_failed(tmp_path, monkeypatch):
    cfg = _make_repo(tmp_path)
    script = _GitScript()
    monkeypatch.setattr(dev_agent_mod, "run_command", script)
    _patch_issue_and_build(monkeypatch, build_success=False, build_error="Compilation failed: unresolved reference")

    update = dev_agent(_real_state(cfg))

    assert update["status"] == "dev_build_failed"
    assert "Compilation failed" in update["last_error"]
    assert update["dev_finding"]["build"]["success"] is False
    # A commit still happened before the build attempt, since the diff itself wasn't risky.
    assert script.committed is True


# --------------------------------------------------------------------------
# Destructive-change approval gate
# --------------------------------------------------------------------------

def test_requires_approval_for_deleted_file(tmp_path, monkeypatch):
    cfg = _make_repo(tmp_path)
    script = _GitScript()
    script.porcelain_status = " D app/OldHelper.kt\n"
    monkeypatch.setattr(dev_agent_mod, "run_command", script)
    _patch_issue_and_build(monkeypatch)

    update = dev_agent(_real_state(cfg))

    assert update["status"] == "dev_requires_approval"
    assert update["attempt_count"] == 1  # still counts as a genuine attempt
    finding = update["dev_finding"]
    assert finding["requires_approval"] is True
    assert any("Deleted file" in flag for flag in finding["risk_flags"])
    assert script.committed is False  # nothing committed while awaiting approval


def test_requires_approval_for_ci_workflow_change(tmp_path, monkeypatch):
    cfg = _make_repo(tmp_path)
    script = _GitScript()
    script.porcelain_status = " M .github/workflows/ci.yml\n"
    monkeypatch.setattr(dev_agent_mod, "run_command", script)
    _patch_issue_and_build(monkeypatch)

    update = dev_agent(_real_state(cfg))

    assert update["status"] == "dev_requires_approval"
    assert any("CI/workflow" in flag for flag in update["dev_finding"]["risk_flags"])


def test_requires_approval_for_secret_looking_file(tmp_path, monkeypatch):
    cfg = _make_repo(tmp_path)
    script = _GitScript()
    script.porcelain_status = " M .env.production\n"
    monkeypatch.setattr(dev_agent_mod, "run_command", script)
    _patch_issue_and_build(monkeypatch)

    update = dev_agent(_real_state(cfg))

    assert update["status"] == "dev_requires_approval"
    assert any("secret" in flag.lower() for flag in update["dev_finding"]["risk_flags"])


def test_proceeds_past_risky_change_when_explicitly_approved(tmp_path, monkeypatch):
    cfg = _make_repo(tmp_path)
    script = _GitScript()
    script.porcelain_status = " D app/OldHelper.kt\n"
    monkeypatch.setattr(dev_agent_mod, "run_command", script)
    _patch_issue_and_build(monkeypatch)

    update = dev_agent(_real_state(cfg, approve_destructive_changes=True))

    assert update["status"] == "fix_applied"
    assert update["dev_finding"]["requires_approval"] is True  # still flagged for the record
    assert script.committed is True


# --------------------------------------------------------------------------
# Claude Code / no-op / precondition failures
# --------------------------------------------------------------------------

def test_claude_code_failure_is_reported_without_crashing(tmp_path, monkeypatch):
    cfg = _make_repo(tmp_path)
    script = _GitScript(claude_ok=False)
    monkeypatch.setattr(dev_agent_mod, "run_command", script)
    _patch_issue_and_build(monkeypatch)

    update = dev_agent(_real_state(cfg))

    assert update["status"] == "dev_failed"
    assert "not authenticated" in update["last_error"]
    assert update["attempt_count"] == 1


def test_no_changes_from_claude_is_reported_distinctly(tmp_path, monkeypatch):
    cfg = _make_repo(tmp_path)
    script = _GitScript()
    script.porcelain_status = ""  # nothing changed
    monkeypatch.setattr(dev_agent_mod, "run_command", script)
    _patch_issue_and_build(monkeypatch)

    update = dev_agent(_real_state(cfg))

    assert update["status"] == "dev_no_changes"
    assert update["dev_finding"]["files_changed"] == []


def test_missing_ticket_id_blocks_without_incrementing_attempt_count(tmp_path):
    cfg = _make_repo(tmp_path)

    update = dev_agent(_real_state(cfg, ticket_id=None, attempt_count=1))

    assert update["status"] == "dev_blocked"
    assert "No ticket_id" in update["last_error"]
    assert "attempt_count" not in update


def test_missing_git_repo_blocks_without_incrementing_attempt_count(tmp_path):
    cfg = load_app_config("sample_android")
    cfg.clone_path = tmp_path / "not_a_repo"
    cfg.clone_path.mkdir(parents=True)  # exists, but no .git

    update = dev_agent(_real_state(cfg, attempt_count=1))

    assert update["status"] == "dev_blocked"
    assert "No git repository" in update["last_error"]
    assert "attempt_count" not in update


def test_no_base_branch_blocks_cleanly(tmp_path, monkeypatch):
    cfg = _make_repo(tmp_path)
    script = _GitScript(existing_branches=())  # neither main nor master exists
    monkeypatch.setattr(dev_agent_mod, "run_command", script)

    update = dev_agent(_real_state(cfg))

    assert update["status"] == "dev_blocked"
    assert "base branch" in update["last_error"]


# --------------------------------------------------------------------------
# iOS parity
# --------------------------------------------------------------------------

def test_works_for_ios_config(tmp_path, monkeypatch):
    cfg = _make_repo(tmp_path, app_name="sample_ios")
    script = _GitScript()
    monkeypatch.setattr(dev_agent_mod, "run_command", script)
    _patch_issue_and_build(monkeypatch)

    update = dev_agent(_real_state(cfg))

    assert update["status"] == "fix_applied"
    assert update["dev_finding"]["branch"] == "ai-fix/42"


# --------------------------------------------------------------------------
# Mock mode untouched
# --------------------------------------------------------------------------

def test_mock_mode_is_unchanged(tmp_path):
    cfg = _make_repo(tmp_path)
    state = initial_state(app_name=cfg.name, app_config=cfg, mock_scenario="fix_success")

    update = dev_agent(state)

    assert update["status"] == "fix_applied"
    assert update["attempt_count"] == 1
    assert "dev_finding" not in update
