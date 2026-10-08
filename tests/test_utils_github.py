"""Unit tests for utils.github - with utils.github.run_command mocked out.

No real `gh` binary and no real GitHub API call is ever invoked here.
"""

from __future__ import annotations

import utils.github as github_mod
from utils.github import (
    check_auth,
    create_issue,
    create_pull_request,
    format_attachments,
    get_pull_request_for_branch,
    parse_issue_reference,
    parse_pr_reference,
    update_issue,
)
from utils.runner import CommandResult


def _result(returncode=0, stdout="", stderr=""):
    return CommandResult(argv=[], returncode=returncode, stdout=stdout, stderr=stderr)


class _FakeRunner:
    def __init__(self, response):
        self._response = response
        self.calls: list[list[str]] = []

    def __call__(self, argv, cwd, timeout=30.0, env=None):
        self.calls.append(list(argv))
        return self._response


class _SequenceRunner:
    """Replays one response per call, in order - for multi-step flows like check_auth."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[list[str]] = []

    def __call__(self, argv, cwd, timeout=30.0, env=None):
        self.calls.append(list(argv))
        return self._responses.pop(0)


def test_parse_issue_reference_from_url():
    assert parse_issue_reference("https://github.com/acme/app/issues/42") == 42


def test_parse_issue_reference_from_hash():
    assert parse_issue_reference("fixes #7 for real") == 7


def test_parse_issue_reference_no_match():
    assert parse_issue_reference("nothing to see here") is None


def test_parse_pr_reference_from_url():
    assert parse_pr_reference("https://github.com/acme/app/pull/99") == 99


def test_format_attachments_lists_each_file(tmp_path):
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"fake")

    body = format_attachments([shot])

    assert "shot.png" in body
    assert str(shot) in body


def test_format_attachments_rejects_missing_file(tmp_path):
    from utils.github import GitHubError

    missing = tmp_path / "missing.png"
    import pytest

    with pytest.raises(GitHubError):
        format_attachments([missing])


def test_create_issue_rejects_bad_repo_format(tmp_path):
    result = create_issue("not-a-repo", "title", "body", cwd=tmp_path)

    assert not result.success
    assert "owner/repository" in result.error


def test_create_issue_success(monkeypatch, tmp_path):
    fake = _FakeRunner(_result(stdout="https://github.com/acme/app/issues/5\n"))
    monkeypatch.setattr(github_mod, "run_command", fake)

    result = create_issue("acme/app", "Bug found", "Something broke", cwd=tmp_path)

    assert result.success
    assert result.number == 5
    assert result.url == "https://github.com/acme/app/issues/5"
    assert fake.calls[0][:3] == ["gh", "issue", "create"]


def test_create_issue_includes_attachments_in_body(monkeypatch, tmp_path):
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"fake")
    fake = _FakeRunner(_result(stdout="https://github.com/acme/app/issues/5\n"))
    monkeypatch.setattr(github_mod, "run_command", fake)

    create_issue("acme/app", "Bug found", "Something broke", cwd=tmp_path, attachments=[shot])

    body_index = fake.calls[0].index("--body") + 1
    assert "shot.png" in fake.calls[0][body_index]


def test_create_issue_rejects_missing_attachment(tmp_path):
    missing = tmp_path / "missing.png"

    result = create_issue("acme/app", "Bug found", "Something broke", cwd=tmp_path, attachments=[missing])

    assert not result.success
    assert "Attachment not found" in result.error


def test_create_issue_command_failure(monkeypatch, tmp_path):
    fake = _FakeRunner(_result(returncode=1, stderr="authentication required"))
    monkeypatch.setattr(github_mod, "run_command", fake)

    result = create_issue("acme/app", "Bug found", "Something broke", cwd=tmp_path)

    assert not result.success
    assert "authentication required" in result.error


def test_update_issue_closes_and_edits(monkeypatch, tmp_path):
    fake = _FakeRunner(_result(returncode=0, stdout=""))
    monkeypatch.setattr(github_mod, "run_command", fake)

    result = update_issue("acme/app", 5, cwd=tmp_path, body="updated", state="closed")

    assert result.success
    assert fake.calls[0][:3] == ["gh", "issue", "close"]
    assert fake.calls[1][:3] == ["gh", "issue", "edit"]


def test_create_pull_request_success(monkeypatch, tmp_path):
    fake = _FakeRunner(_result(stdout="https://github.com/acme/app/pull/12\n"))
    monkeypatch.setattr(github_mod, "run_command", fake)

    result = create_pull_request(
        "acme/app", "Fix bug", "Fixes #5", head="fix-branch", base="main", cwd=tmp_path
    )

    assert result.success
    assert result.number == 12
    assert "--draft" in fake.calls[0]


def test_create_pull_request_rejects_bad_repo_format(tmp_path):
    result = create_pull_request("bad repo", "t", "b", head="h", base="main", cwd=tmp_path)

    assert not result.success


def test_check_auth_success_with_write_access(monkeypatch, tmp_path):
    fake = _SequenceRunner(
        [
            _result(stdout="Logged in to github.com as someone\n"),
            _result(stdout='{"viewerPermission": "WRITE"}\n'),
        ]
    )
    monkeypatch.setattr(github_mod, "run_command", fake)

    status = check_auth("acme/app", cwd=tmp_path)

    assert status.authenticated
    assert status.can_write is True


def test_check_auth_not_authenticated(monkeypatch, tmp_path):
    fake = _FakeRunner(_result(returncode=1, stderr="You are not logged into any GitHub hosts."))
    monkeypatch.setattr(github_mod, "run_command", fake)

    status = check_auth("acme/app", cwd=tmp_path)

    assert not status.authenticated
    assert "not logged into" in status.error


def test_check_auth_read_only_permission(monkeypatch, tmp_path):
    fake = _SequenceRunner(
        [
            _result(stdout="Logged in to github.com as someone\n"),
            _result(stdout='{"viewerPermission": "READ"}\n'),
        ]
    )
    monkeypatch.setattr(github_mod, "run_command", fake)

    status = check_auth("acme/app", cwd=tmp_path)

    assert status.authenticated
    assert status.can_write is False


def test_check_auth_rejects_bad_repo_format(tmp_path):
    status = check_auth("not-a-repo", cwd=tmp_path)

    assert not status.authenticated
    assert "owner/repository" in status.error


def test_get_pull_request_for_branch_found(monkeypatch, tmp_path):
    fake = _FakeRunner(_result(stdout='{"number": 7, "url": "https://github.com/acme/app/pull/7", "state": "OPEN"}\n'))
    monkeypatch.setattr(github_mod, "run_command", fake)

    result = get_pull_request_for_branch("acme/app", "ai-fix/42", cwd=tmp_path)

    assert result.success
    assert result.number == 7
    assert result.url == "https://github.com/acme/app/pull/7"
    assert fake.calls[0][:3] == ["gh", "pr", "view"]


def test_get_pull_request_for_branch_not_found(monkeypatch, tmp_path):
    fake = _FakeRunner(_result(returncode=1, stderr="no pull requests found for branch \"ai-fix/42\""))
    monkeypatch.setattr(github_mod, "run_command", fake)

    result = get_pull_request_for_branch("acme/app", "ai-fix/42", cwd=tmp_path)

    assert not result.success
    assert "no pull requests found" in result.error
