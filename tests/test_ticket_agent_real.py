"""Unit tests for nodes.ticket_agent's "real" mode.

utils.github.{check_auth,create_issue,comment_on_issue} are all mocked at the
nodes.ticket_agent module level - no real `gh` binary and no real GitHub API
call happens anywhere in this file, so no test here ever creates a real issue.
"""

from __future__ import annotations

from pathlib import Path

import nodes.ticket_agent as ticket_agent_mod
from config import load_app_config
from nodes.ticket_agent import ticket_agent
from state import initial_state
from utils.github import AuthStatus, IssueResult


def _functional_qa_finding(**overrides):
    finding = {
        "flow_name": "login.yaml",
        "passed": False,
        "failure_type": "functional",
        "description": 'Maestro assertion(s) failed: Element not found: "LoginButton"',
        "expected_behavior": "Tapping LoginButton should navigate to the home screen.",
        "actual_behavior": 'Element not found: "LoginButton"',
        "reproduction_steps": ["launch and log in"],
        "evidence_paths": [],
        "confidence": 1.0,
    }
    finding.update(overrides)
    return finding


def _rca_finding(**overrides):
    finding = {
        "root_cause_hypothesis": "LoginButton click handler throws before navigation completes.",
        "suspected_files": ["app/LoginActivity.kt"],
        "suspected_methods": ["onLoginButtonClick"],
        "supporting_evidence": ["NullPointerException at LoginActivity.kt:42"],
        "confidence": 0.8,
        "suggested_fix": "Null-check the view before binding the click listener.",
        "missing_information": [],
    }
    finding.update(overrides)
    return finding


def _real_state(app_config, qa_finding=None, rca_finding=None, ticket_id=None, **state_overrides):
    state = initial_state(app_name=app_config.name, app_config=app_config, mode="real")
    if qa_finding is not None:
        state["qa_finding"] = qa_finding
    if rca_finding is not None:
        state["rca_finding"] = rca_finding
    if ticket_id is not None:
        state["ticket_id"] = ticket_id
    state.update(state_overrides)
    return state


def _make_config(tmp_path: Path, app_name: str = "sample_android"):
    cfg = load_app_config(app_name)
    cfg.clone_path = tmp_path / "clone"
    cfg.clone_path.mkdir(parents=True)
    return cfg


def _patch_auth(monkeypatch, authenticated=True, can_write=True, error=None):
    def fake_check_auth(repo, cwd, timeout=15.0):
        return AuthStatus(authenticated=authenticated, can_write=can_write, error=error)

    monkeypatch.setattr(ticket_agent_mod, "check_auth", fake_check_auth)


# --------------------------------------------------------------------------
# New issue creation
# --------------------------------------------------------------------------

def test_creates_new_issue_for_confirmed_defect(tmp_path, monkeypatch):
    cfg = _make_config(tmp_path)
    _patch_auth(monkeypatch)

    calls = []

    def fake_create_issue(repo, title, body, cwd, labels=None, attachments=None, timeout=30.0):
        calls.append({"repo": repo, "title": title, "body": body, "labels": labels})
        return IssueResult(success=True, number=7, url="https://github.com/acme/app/issues/7")

    monkeypatch.setattr(ticket_agent_mod, "create_issue", fake_create_issue)

    state = _real_state(cfg, qa_finding=_functional_qa_finding(), rca_finding=_rca_finding())
    update = ticket_agent(state)

    assert update["status"] == "ticket_ready"
    assert update["ticket_id"] == "7"
    assert update["ticket_url"] == "https://github.com/acme/app/issues/7"
    assert len(calls) == 1
    assert calls[0]["repo"] == cfg.repo
    assert "LoginButton" in calls[0]["title"]
    body = calls[0]["body"]
    assert "## Summary" in body
    assert "## Reproduction Steps" in body
    assert "## Expected vs Actual Behavior" in body
    assert "## Maestro Flow" in body
    assert "## Root Cause Analysis" in body
    assert "app/LoginActivity.kt" in body
    assert "onLoginButtonClick" in body
    assert "## Confidence" in body
    assert "## Unresolved Questions" in body
    assert calls[0]["labels"] == ["qa-pipeline", "severity:high", "type:functional"]


def test_uses_app_config_repo_never_hardcoded(tmp_path, monkeypatch):
    cfg = _make_config(tmp_path, app_name="sample_ios")
    _patch_auth(monkeypatch)
    seen_repos = []

    def fake_create_issue(repo, title, body, cwd, labels=None, attachments=None, timeout=30.0):
        seen_repos.append(repo)
        return IssueResult(success=True, number=1, url="https://github.com/x/y/issues/1")

    monkeypatch.setattr(ticket_agent_mod, "create_issue", fake_create_issue)

    ticket_agent(_real_state(cfg, qa_finding=_functional_qa_finding(), rca_finding=_rca_finding()))

    assert seen_repos == [cfg.repo]
    assert cfg.repo == "your-org/sample-ios-app"


def test_includes_available_screenshot_attachments(tmp_path, monkeypatch):
    cfg = _make_config(tmp_path)
    _patch_auth(monkeypatch)
    shot = tmp_path / "failure.png"
    shot.write_bytes(b"fake-png")
    missing = tmp_path / "gone.png"  # referenced but no longer on disk

    captured = {}

    def fake_create_issue(repo, title, body, cwd, labels=None, attachments=None, timeout=30.0):
        captured["attachments"] = attachments
        return IssueResult(success=True, number=1, url="https://github.com/acme/app/issues/1")

    monkeypatch.setattr(ticket_agent_mod, "create_issue", fake_create_issue)

    qa_finding = _functional_qa_finding(evidence_paths=[str(shot), str(missing)])
    ticket_agent(_real_state(cfg, qa_finding=qa_finding, rca_finding=_rca_finding()))

    assert captured["attachments"] == [shot]


# --------------------------------------------------------------------------
# Retries: comment instead of duplicate
# --------------------------------------------------------------------------

def test_retry_comments_on_existing_issue_instead_of_creating(tmp_path, monkeypatch):
    cfg = _make_config(tmp_path)
    _patch_auth(monkeypatch)

    create_calls = []
    comment_calls = []

    monkeypatch.setattr(
        ticket_agent_mod,
        "create_issue",
        lambda *a, **k: create_calls.append(1) or IssueResult(success=True, number=99, url="x"),
    )

    def fake_comment_on_issue(repo, issue_number, body, cwd, attachments=None, timeout=30.0):
        comment_calls.append({"issue_number": issue_number, "body": body})
        return IssueResult(success=True, number=issue_number, url="https://github.com/acme/app/issues/42")

    monkeypatch.setattr(ticket_agent_mod, "comment_on_issue", fake_comment_on_issue)

    state = _real_state(
        cfg,
        qa_finding=_functional_qa_finding(),
        rca_finding=_rca_finding(confidence=0.4),
        ticket_id="42",
    )
    update = ticket_agent(state)

    assert update["ticket_id"] == "42"
    assert create_calls == []
    assert len(comment_calls) == 1
    assert comment_calls[0]["issue_number"] == 42
    assert "Retry update" in comment_calls[0]["body"]
    assert "0.4" in comment_calls[0]["body"]


def test_retry_with_non_numeric_ticket_id_fails_without_losing_findings(tmp_path, monkeypatch):
    cfg = _make_config(tmp_path)
    _patch_auth(monkeypatch)

    state = _real_state(cfg, qa_finding=_functional_qa_finding(), rca_finding=_rca_finding(), ticket_id="MOCK-APP-1")
    update = ticket_agent(state)

    assert update["status"] == "ticket_failed"
    assert "not a valid GitHub issue number" in update["last_error"]
    assert "## Retry update" in update["ticket_finding"]["body"]


# --------------------------------------------------------------------------
# Authentication / permission failures
# --------------------------------------------------------------------------

def test_not_authenticated_fails_without_losing_findings(tmp_path, monkeypatch):
    cfg = _make_config(tmp_path)
    _patch_auth(monkeypatch, authenticated=False, can_write=None, error="not logged in")
    create_calls = []
    monkeypatch.setattr(ticket_agent_mod, "create_issue", lambda *a, **k: create_calls.append(1))

    state = _real_state(cfg, qa_finding=_functional_qa_finding(), rca_finding=_rca_finding())
    update = ticket_agent(state)

    assert update["status"] == "ticket_failed"
    assert "not authenticated" in update["last_error"]
    assert create_calls == []
    # The composed report survives the failure so nothing is lost.
    assert "LoginButton" in update["ticket_finding"]["title"]
    assert "## Root Cause Analysis" in update["ticket_finding"]["body"]


def test_read_only_permission_fails_without_losing_findings(tmp_path, monkeypatch):
    cfg = _make_config(tmp_path)
    _patch_auth(monkeypatch, authenticated=True, can_write=False)
    create_calls = []
    monkeypatch.setattr(ticket_agent_mod, "create_issue", lambda *a, **k: create_calls.append(1))

    update = ticket_agent(_real_state(cfg, qa_finding=_functional_qa_finding(), rca_finding=_rca_finding()))

    assert update["status"] == "ticket_failed"
    assert "lacks write access" in update["last_error"]
    assert create_calls == []
    assert update["ticket_finding"]["body"]


def test_gh_command_failure_fails_without_losing_findings(tmp_path, monkeypatch):
    cfg = _make_config(tmp_path)
    _patch_auth(monkeypatch)
    monkeypatch.setattr(
        ticket_agent_mod, "create_issue", lambda *a, **k: IssueResult(success=False, error="rate limited")
    )

    update = ticket_agent(_real_state(cfg, qa_finding=_functional_qa_finding(), rca_finding=_rca_finding()))

    assert update["status"] == "ticket_failed"
    assert "rate limited" in update["last_error"]
    assert "## Summary" in update["ticket_finding"]["body"]


# --------------------------------------------------------------------------
# Dry run
# --------------------------------------------------------------------------

def test_dry_run_renders_report_without_calling_gh(tmp_path, monkeypatch):
    cfg = _make_config(tmp_path)

    auth_calls = []
    create_calls = []
    monkeypatch.setattr(ticket_agent_mod, "check_auth", lambda *a, **k: auth_calls.append(1))
    monkeypatch.setattr(ticket_agent_mod, "create_issue", lambda *a, **k: create_calls.append(1))

    state = _real_state(cfg, qa_finding=_functional_qa_finding(), rca_finding=_rca_finding(), dry_run=True)
    update = ticket_agent(state)

    assert update["status"] == "ticket_dry_run"
    assert update["ticket_finding"]["dry_run"] is True
    assert "## Summary" in update["ticket_finding"]["body"]
    assert auth_calls == []
    assert create_calls == []
    # Nothing was published, so ticket_id stays whatever it was before (None).
    assert update["ticket_id"] is None


def test_dry_run_on_retry_shows_comment_not_full_report(tmp_path, monkeypatch):
    cfg = _make_config(tmp_path)
    comment_calls = []
    monkeypatch.setattr(ticket_agent_mod, "comment_on_issue", lambda *a, **k: comment_calls.append(1))

    state = _real_state(
        cfg, qa_finding=_functional_qa_finding(), rca_finding=_rca_finding(), ticket_id="42", dry_run=True
    )
    update = ticket_agent(state)

    assert update["status"] == "ticket_dry_run"
    assert "Retry update" in update["ticket_finding"]["body"]
    assert comment_calls == []
    assert update["ticket_id"] == "42"


# --------------------------------------------------------------------------
# Skipping: no confirmed defect
# --------------------------------------------------------------------------

def test_skips_when_qa_passed(tmp_path):
    cfg = _make_config(tmp_path)
    qa_finding = _functional_qa_finding(passed=True, failure_type="none")

    update = ticket_agent(_real_state(cfg, qa_finding=qa_finding))

    assert update["status"] == "ticket_skipped"
    assert update["ticket_finding"]["skipped"] is True


def test_skips_when_unverified(tmp_path):
    cfg = _make_config(tmp_path)
    qa_finding = {"flow_name": "login.yaml", "failure_type": "unverified", "description": "no criteria"}

    update = ticket_agent(_real_state(cfg, qa_finding=qa_finding))

    assert update["status"] == "ticket_skipped"


def test_skips_infrastructure_failures_by_default(tmp_path, monkeypatch):
    cfg = _make_config(tmp_path)
    create_calls = []
    monkeypatch.setattr(ticket_agent_mod, "create_issue", lambda *a, **k: create_calls.append(1))

    qa_finding = {"flow_name": "login.yaml", "failure_type": "infrastructure", "description": "emulator not booted"}
    update = ticket_agent(_real_state(cfg, qa_finding=qa_finding))

    assert update["status"] == "ticket_skipped"
    assert "infrastructure" in update["ticket_finding"]["reason"]
    assert create_calls == []


def test_files_infrastructure_ticket_when_explicitly_opted_in(tmp_path, monkeypatch):
    cfg = _make_config(tmp_path)
    _patch_auth(monkeypatch)
    monkeypatch.setattr(
        ticket_agent_mod, "create_issue", lambda *a, **k: IssueResult(success=True, number=5, url="https://x/5")
    )

    qa_finding = {"flow_name": "login.yaml", "failure_type": "infrastructure", "description": "emulator not booted"}
    state = _real_state(cfg, qa_finding=qa_finding, file_ticket_for_infrastructure=True)
    update = ticket_agent(state)

    assert update["status"] == "ticket_ready"
    assert update["ticket_id"] == "5"


def test_skips_when_no_qa_finding_present(tmp_path):
    cfg = _make_config(tmp_path)

    update = ticket_agent(_real_state(cfg))

    assert update["status"] == "ticket_skipped"


# --------------------------------------------------------------------------
# Mock mode untouched
# --------------------------------------------------------------------------

def test_mock_mode_is_unchanged(tmp_path):
    cfg = _make_config(tmp_path)
    state = initial_state(app_name=cfg.name, app_config=cfg, mock_scenario="fix_success")

    update = ticket_agent(state)

    assert update["ticket_id"].startswith("MOCK-")
    assert "ticket_finding" not in update
