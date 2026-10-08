"""Unit tests for nodes.merge_step's "real" mode.

utils.github.{check_auth,create_pull_request,get_pull_request_for_branch}
and utils.runner.run_command (all git calls) are mocked at the
nodes.merge_step module level - no real `git`/`gh` binary and no real PR is
ever created here.
"""

from __future__ import annotations

from pathlib import Path

import nodes.merge_step as merge_step_mod
from config import load_app_config
from nodes.merge_step import merge_step
from state import initial_state
from utils.github import AuthStatus, PullRequestResult
from utils.runner import CommandResult


def _qa_finding(**overrides):
    finding = {
        "flow_name": "login.yaml",
        "description": 'Maestro assertion(s) failed: Element not found: "LoginButton"',
        "evidence_paths": ["before_screenshot.png"],
    }
    finding.update(overrides)
    return finding


def _rca_finding(**overrides):
    finding = {
        "root_cause_hypothesis": "LoginButton click handler throws before navigation completes.",
        "suspected_files": ["app/LoginActivity.kt"],
        "missing_information": [],
    }
    finding.update(overrides)
    return finding


def _dev_finding(**overrides):
    finding = {
        "branch": "ai-fix/42",
        "base_branch": "main",
        "files_changed": ["M app/LoginActivity.kt"],
        "requires_approval": False,
        "risk_flags": [],
        "claude_summary": "Added a null check and a regression test.",
        "diff_summary": " app/LoginActivity.kt | 2 +-",
        "build": {"success": True, "artifact_path": "app-debug.apk"},
        "tests": {"ran": True, "success": True, "summary": "Tests passed."},
    }
    finding.update(overrides)
    return finding


def _retest_finding(**overrides):
    finding = {
        "primary_result": {"flow_name": "login.yaml", "passed": True, "evidence_paths": ["after_screenshot.png"]},
        "regression_results": [],
        "original_defect_resolved": True,
        "new_regressions": [],
        "build": {"success": True, "artifact_path": "app-debug.apk"},
        "install": {"success": True, "device_id": "emulator-5554"},
    }
    finding.update(overrides)
    return finding


def _make_config(tmp_path: Path, app_name: str = "sample_android"):
    cfg = load_app_config(app_name)
    cfg.clone_path = tmp_path / "clone"
    cfg.clone_path.mkdir(parents=True)
    return cfg


def _passing_state(app_config, ticket_id="42", **overrides):
    state = initial_state(app_name=app_config.name, app_config=app_config, mode="real")
    state["ticket_id"] = ticket_id
    state["passed"] = True
    state["status"] = "retest_passed"
    state["qa_finding"] = _qa_finding()
    state["rca_finding"] = _rca_finding()
    state["dev_finding"] = _dev_finding()
    state["retest_finding"] = _retest_finding()
    state.update(overrides)
    return state


def _result(returncode=0, stdout="", stderr=""):
    return CommandResult(argv=[], returncode=returncode, stdout=stdout, stderr=stderr)


class _GitDouble:
    """Fakes the handful of git subcommands merge_step uses, plus push tracking."""

    def __init__(self, dirty=False, extra_diff_files=(), remote_ok=True, push_ok=True):
        self.dirty = dirty
        self.extra_diff_files = list(extra_diff_files)
        self.remote_ok = remote_ok
        self.push_ok = push_ok
        self.push_calls: list[list[str]] = []
        self.calls: list[list[str]] = []

    def __call__(self, argv, cwd, timeout=120.0, env=None):
        self.calls.append(list(argv))

        if argv[:2] == ["git", "diff"]:
            files = ["app/LoginActivity.kt", *self.extra_diff_files]
            return _result(stdout="\n".join(files) + "\n")

        if argv[:2] == ["git", "status"]:
            return _result(stdout=" M somefile.kt\n" if self.dirty else "")

        if argv[:3] == ["git", "remote", "get-url"]:
            return _result(returncode=0 if self.remote_ok else 1, stderr="" if self.remote_ok else "No such remote")

        if argv[:2] == ["git", "push"]:
            self.push_calls.append(list(argv))
            return _result(returncode=0 if self.push_ok else 1, stderr="" if self.push_ok else "failed to push")

        return _result()


def _patch_auth(monkeypatch, authenticated=True, can_write=True, error=None):
    monkeypatch.setattr(
        merge_step_mod, "check_auth", lambda repo, cwd: AuthStatus(authenticated=authenticated, can_write=can_write, error=error)
    )


def _patch_no_existing_pr(monkeypatch):
    monkeypatch.setattr(
        merge_step_mod, "get_pull_request_for_branch", lambda repo, branch, cwd: PullRequestResult(success=False, error="no pull requests found")
    )


# --------------------------------------------------------------------------
# PR creation (happy path)
# --------------------------------------------------------------------------

def test_creates_pr_with_full_content(tmp_path, monkeypatch):
    cfg = _make_config(tmp_path)
    git = _GitDouble()
    monkeypatch.setattr(merge_step_mod, "run_command", git)
    _patch_auth(monkeypatch)
    _patch_no_existing_pr(monkeypatch)

    captured = {}

    def fake_create_pr(repo, title, body, head, base, cwd, draft=True, timeout=30.0):
        captured.update(repo=repo, title=title, body=body, head=head, base=base, draft=draft)
        return PullRequestResult(success=True, number=9, url="https://github.com/acme/app/pull/9")

    monkeypatch.setattr(merge_step_mod, "create_pull_request", fake_create_pr)

    update = merge_step(_passing_state(cfg))

    assert update["status"] == "ready_for_review"
    assert update["pr_url"] == "https://github.com/acme/app/pull/9"
    assert update["pr_number"] == 9
    assert captured["repo"] == cfg.repo
    assert captured["head"] == "ai-fix/42"
    assert captured["base"] == "main"
    assert captured["draft"] is True
    body = captured["body"]
    assert "Fixes #42" in body
    assert "## Root Cause" in body
    assert "## Fix Summary" in body
    assert "## Tests Executed" in body
    assert "## Before / After Evidence" in body
    assert "before_screenshot.png" in body
    assert "after_screenshot.png" in body
    assert "## Remaining Limitations" in body
    # Branch was actually pushed, without --force.
    assert git.push_calls == [["git", "push", "origin", "ai-fix/42"]]
    assert all("--force" not in c and "-f" not in c for c in git.push_calls)


def test_never_pushes_to_main_or_master(tmp_path, monkeypatch):
    cfg = _make_config(tmp_path)
    git = _GitDouble()
    monkeypatch.setattr(merge_step_mod, "run_command", git)
    _patch_auth(monkeypatch)
    _patch_no_existing_pr(monkeypatch)
    monkeypatch.setattr(merge_step_mod, "create_pull_request", lambda *a, **k: PullRequestResult(success=True, number=1, url="x"))

    state = _passing_state(cfg, dev_finding=_dev_finding(branch="main"))
    update = merge_step(state)

    assert update["status"] == "merge_blocked"
    assert "not an approved ai-fix/* branch" in update["last_error"]
    assert git.push_calls == []


def test_respects_non_draft_override(tmp_path, monkeypatch):
    cfg = _make_config(tmp_path)
    monkeypatch.setattr(merge_step_mod, "run_command", _GitDouble())
    _patch_auth(monkeypatch)
    _patch_no_existing_pr(monkeypatch)
    captured = {}
    monkeypatch.setattr(
        merge_step_mod,
        "create_pull_request",
        lambda repo, title, body, head, base, cwd, draft=True, timeout=30.0: (captured.update(draft=draft), PullRequestResult(success=True, number=1, url="x"))[1],
    )

    merge_step(_passing_state(cfg, draft_pr=False))

    assert captured["draft"] is False


# --------------------------------------------------------------------------
# Duplicate PR handling
# --------------------------------------------------------------------------

def test_skips_creation_when_pr_url_already_in_state(tmp_path, monkeypatch):
    cfg = _make_config(tmp_path)
    create_calls = []
    monkeypatch.setattr(merge_step_mod, "create_pull_request", lambda *a, **k: create_calls.append(1))
    auth_calls = []
    monkeypatch.setattr(merge_step_mod, "check_auth", lambda *a, **k: auth_calls.append(1))

    state = _passing_state(cfg, pr_url="https://github.com/acme/app/pull/5", pr_number=5)
    update = merge_step(state)

    assert update["status"] == "ready_for_review"
    assert update["pr_url"] == "https://github.com/acme/app/pull/5"
    assert update["merge_finding"]["duplicate_prevented"] is True
    assert create_calls == []
    assert auth_calls == []  # fast in-state dedup skips everything else


def test_skips_creation_when_github_already_has_a_pr_for_branch(tmp_path, monkeypatch):
    cfg = _make_config(tmp_path)
    monkeypatch.setattr(merge_step_mod, "run_command", _GitDouble())
    _patch_auth(monkeypatch)
    monkeypatch.setattr(
        merge_step_mod,
        "get_pull_request_for_branch",
        lambda repo, branch, cwd: PullRequestResult(success=True, number=3, url="https://github.com/acme/app/pull/3"),
    )
    create_calls = []
    monkeypatch.setattr(merge_step_mod, "create_pull_request", lambda *a, **k: create_calls.append(1))

    update = merge_step(_passing_state(cfg))

    assert update["status"] == "ready_for_review"
    assert update["pr_url"] == "https://github.com/acme/app/pull/3"
    assert create_calls == []


# --------------------------------------------------------------------------
# Failed validation / preconditions
# --------------------------------------------------------------------------

def test_blocks_when_retest_did_not_pass(tmp_path):
    cfg = _make_config(tmp_path)

    update = merge_step(_passing_state(cfg, passed=False, status="retest_failed"))

    assert update["status"] == "merge_blocked"
    assert "Retest Agent has not passed" in update["last_error"]


def test_blocks_when_dev_build_failed(tmp_path):
    cfg = _make_config(tmp_path)

    update = merge_step(_passing_state(cfg, dev_finding=_dev_finding(build={"success": False, "error": "boom"})))

    assert update["status"] == "merge_blocked"
    assert "Dev Agent's build did not succeed" in update["last_error"]


def test_blocks_when_dev_tests_failed(tmp_path):
    cfg = _make_config(tmp_path)

    update = merge_step(
        _passing_state(cfg, dev_finding=_dev_finding(tests={"ran": True, "success": False, "summary": "Tests failed."}))
    )

    assert update["status"] == "merge_blocked"
    assert "automated tests failed" in update["last_error"]


def test_blocks_when_original_defect_not_resolved(tmp_path):
    cfg = _make_config(tmp_path)

    update = merge_step(_passing_state(cfg, retest_finding=_retest_finding(original_defect_resolved=False)))

    assert update["status"] == "merge_blocked"
    assert "did not confirm the original defect" in update["last_error"]


def test_blocks_when_new_regression_present(tmp_path):
    cfg = _make_config(tmp_path)

    update = merge_step(_passing_state(cfg, retest_finding=_retest_finding(new_regressions=["smoke_test.yaml"])))

    assert update["status"] == "merge_blocked"
    assert "new regression" in update["last_error"].lower()


def test_blocks_when_no_files_changed(tmp_path):
    cfg = _make_config(tmp_path)

    update = merge_step(_passing_state(cfg, dev_finding=_dev_finding(files_changed=[])))

    assert update["status"] == "merge_blocked"
    assert "no changed files" in update["last_error"]


def test_blocks_on_unexpected_branch_drift(tmp_path, monkeypatch):
    cfg = _make_config(tmp_path)
    git = _GitDouble(extra_diff_files=["unexpected_file.kt"])
    monkeypatch.setattr(merge_step_mod, "run_command", git)
    _patch_auth(monkeypatch)
    _patch_no_existing_pr(monkeypatch)
    create_calls = []
    monkeypatch.setattr(merge_step_mod, "create_pull_request", lambda *a, **k: create_calls.append(1))

    update = merge_step(_passing_state(cfg))

    assert update["status"] == "merge_blocked"
    assert "unexpected_file.kt" in update["last_error"]
    assert create_calls == []


def test_blocks_on_dirty_working_tree(tmp_path, monkeypatch):
    cfg = _make_config(tmp_path)
    git = _GitDouble(dirty=True)
    monkeypatch.setattr(merge_step_mod, "run_command", git)
    _patch_auth(monkeypatch)

    update = merge_step(_passing_state(cfg))

    assert update["status"] == "merge_blocked"
    assert "uncommitted changes" in update["last_error"]


# --------------------------------------------------------------------------
# Authentication / permission errors
# --------------------------------------------------------------------------

def test_reports_authentication_error_explicitly(tmp_path, monkeypatch):
    cfg = _make_config(tmp_path)
    monkeypatch.setattr(merge_step_mod, "run_command", _GitDouble())
    _patch_auth(monkeypatch, authenticated=False, can_write=None, error="not logged in")
    create_calls = []
    monkeypatch.setattr(merge_step_mod, "create_pull_request", lambda *a, **k: create_calls.append(1))

    update = merge_step(_passing_state(cfg))

    assert update["status"] == "merge_blocked"
    assert "not authenticated" in update["last_error"]
    assert create_calls == []


def test_reports_permission_error_explicitly(tmp_path, monkeypatch):
    cfg = _make_config(tmp_path)
    monkeypatch.setattr(merge_step_mod, "run_command", _GitDouble())
    _patch_auth(monkeypatch, authenticated=True, can_write=False)
    create_calls = []
    monkeypatch.setattr(merge_step_mod, "create_pull_request", lambda *a, **k: create_calls.append(1))

    update = merge_step(_passing_state(cfg))

    assert update["status"] == "merge_blocked"
    assert "lacks write access" in update["last_error"]
    assert create_calls == []


def test_push_failure_is_reported(tmp_path, monkeypatch):
    cfg = _make_config(tmp_path)
    git = _GitDouble(push_ok=False)
    monkeypatch.setattr(merge_step_mod, "run_command", git)
    _patch_auth(monkeypatch)
    _patch_no_existing_pr(monkeypatch)
    create_calls = []
    monkeypatch.setattr(merge_step_mod, "create_pull_request", lambda *a, **k: create_calls.append(1))

    update = merge_step(_passing_state(cfg))

    assert update["status"] == "merge_blocked"
    assert "Could not push branch" in update["last_error"]
    assert create_calls == []


def test_no_remote_configured_is_reported(tmp_path, monkeypatch):
    cfg = _make_config(tmp_path)
    git = _GitDouble(remote_ok=False)
    monkeypatch.setattr(merge_step_mod, "run_command", git)
    _patch_auth(monkeypatch)
    _patch_no_existing_pr(monkeypatch)

    update = merge_step(_passing_state(cfg))

    assert update["status"] == "merge_blocked"
    assert "No 'origin' remote" in update["last_error"]


# --------------------------------------------------------------------------
# Risk-flagged branches require a second, explicit approval to push
# --------------------------------------------------------------------------

def test_blocks_push_of_risky_branch_without_explicit_approval(tmp_path, monkeypatch):
    cfg = _make_config(tmp_path)
    git = _GitDouble()
    monkeypatch.setattr(merge_step_mod, "run_command", git)
    _patch_auth(monkeypatch)
    _patch_no_existing_pr(monkeypatch)

    state = _passing_state(cfg, dev_finding=_dev_finding(risk_flags=["Deleted file: app/Old.kt"]))
    update = merge_step(state)

    assert update["status"] == "merge_blocked"
    assert "approve_push" in update["last_error"]
    assert git.push_calls == []


def test_allows_push_of_risky_branch_with_explicit_approval(tmp_path, monkeypatch):
    cfg = _make_config(tmp_path)
    git = _GitDouble()
    monkeypatch.setattr(merge_step_mod, "run_command", git)
    _patch_auth(monkeypatch)
    _patch_no_existing_pr(monkeypatch)
    monkeypatch.setattr(merge_step_mod, "create_pull_request", lambda *a, **k: PullRequestResult(success=True, number=1, url="x"))

    state = _passing_state(cfg, dev_finding=_dev_finding(risk_flags=["Deleted file: app/Old.kt"]), approve_push=True)
    update = merge_step(state)

    assert update["status"] == "ready_for_review"
    assert git.push_calls == [["git", "push", "origin", "ai-fix/42"]]


# --------------------------------------------------------------------------
# Dry run
# --------------------------------------------------------------------------

def test_dry_run_never_pushes_or_creates_pr(tmp_path, monkeypatch):
    cfg = _make_config(tmp_path)
    git = _GitDouble()
    monkeypatch.setattr(merge_step_mod, "run_command", git)
    _patch_auth(monkeypatch)
    _patch_no_existing_pr(monkeypatch)
    create_calls = []
    monkeypatch.setattr(merge_step_mod, "create_pull_request", lambda *a, **k: create_calls.append(1))

    update = merge_step(_passing_state(cfg, dry_run=True))

    assert update["status"] == "merge_dry_run"
    assert "## Root Cause" in update["merge_finding"]["body"]
    assert git.push_calls == []
    assert create_calls == []


# --------------------------------------------------------------------------
# iOS parity
# --------------------------------------------------------------------------

def test_works_for_ios_config(tmp_path, monkeypatch):
    cfg = _make_config(tmp_path, app_name="sample_ios")
    monkeypatch.setattr(merge_step_mod, "run_command", _GitDouble())
    _patch_auth(monkeypatch)
    _patch_no_existing_pr(monkeypatch)
    monkeypatch.setattr(merge_step_mod, "create_pull_request", lambda *a, **k: PullRequestResult(success=True, number=1, url="https://x/1"))

    update = merge_step(_passing_state(cfg))

    assert update["status"] == "ready_for_review"


# --------------------------------------------------------------------------
# Mock mode untouched
# --------------------------------------------------------------------------

def test_mock_mode_is_unchanged(tmp_path):
    cfg = _make_config(tmp_path)
    state = initial_state(app_name=cfg.name, app_config=cfg, mock_scenario="fix_success")

    update = merge_step(state)

    assert update["status"] == "ready_for_review"
    assert "merge_finding" not in update
