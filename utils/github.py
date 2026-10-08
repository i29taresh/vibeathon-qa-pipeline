"""GitHub Issue/PR operations via the GitHub CLI (`gh`).

Every call goes through utils.runner.run_command with an explicit argv
list - `gh` does its own authentication (via `gh auth login`, done once
outside this process; no token handling happens here).

Screenshot/video evidence has no first-class upload endpoint in the GitHub
REST API or `gh` CLI (binary issue attachments are a web-UI-only, drag-and-
drop feature) - so "attachment support" here means each evidence file is
validated to exist and referenced by name/path in the issue or PR body.
Swapping that for a real upload (e.g. committing to an `evidence` branch
and linking the raw URL) is a later, pluggable step.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from utils.runner import CommandResult, RunnerError, run_command
from utils.secrets import redact

_ISSUE_URL_RE = re.compile(r"github\.com/[^/\s]+/[^/\s]+/issues/(\d+)")
_PR_URL_RE = re.compile(r"github\.com/[^/\s]+/[^/\s]+/pull/(\d+)")
_REFERENCE_RE = re.compile(r"#(\d+)\b")
_REPO_RE = re.compile(r"^[\w.-]+/[\w.-]+$")


class GitHubError(Exception):
    """Raised for invalid input: bad repo format or a missing attachment file."""


@dataclass
class IssueResult:
    success: bool
    number: int | None = None
    url: str | None = None
    raw_stdout: str = ""
    error: str | None = None


@dataclass
class PullRequestResult:
    success: bool
    number: int | None = None
    url: str | None = None
    raw_stdout: str = ""
    error: str | None = None


@dataclass
class AuthStatus:
    authenticated: bool
    can_write: bool | None = None  # None = undetermined (not a hard "no")
    detail: str = ""
    error: str | None = None


def _validate_repo(repo: str) -> None:
    if not _REPO_RE.match(repo):
        raise GitHubError(f"repo must look like 'owner/repository', got {repo!r}")


def format_attachments(attachments: Sequence[Path] | None) -> str:
    """Build a Markdown "Evidence" section referencing each attachment by path.

    Raises GitHubError if any attachment path doesn't exist - we never want
    to silently reference evidence that isn't actually there.
    """
    if not attachments:
        return ""

    lines = ["", "---", "**Evidence:**"]
    for raw_path in attachments:
        path = Path(raw_path)
        if not path.is_file():
            raise GitHubError(f"Attachment not found: {path}")
        lines.append(f"- `{path.name}` (local path: `{path}`)")
    return "\n".join(lines)


def parse_issue_reference(text: str) -> int | None:
    """Extract an issue number from a GitHub issue URL or a '#123' reference."""
    match = _ISSUE_URL_RE.search(text) or _REFERENCE_RE.search(text)
    return int(match.group(1)) if match else None


def parse_pr_reference(text: str) -> int | None:
    """Extract a PR number from a GitHub pull-request URL."""
    match = _PR_URL_RE.search(text)
    return int(match.group(1)) if match else None


def check_auth(repo: str, cwd: Path, timeout: float = 15.0) -> AuthStatus:
    """Confirm `gh` is authenticated and (best-effort) that it can write to `repo`.

    `can_write=False` is a hard signal (the caller shouldn't try to file
    anything); `can_write=None` means we couldn't determine it (e.g. the
    permission probe itself failed for an unrelated reason) and callers
    should let the actual create/comment call be the final word instead of
    blocking on this alone.
    """
    try:
        _validate_repo(repo)
    except GitHubError as exc:
        return AuthStatus(authenticated=False, error=str(exc))

    try:
        auth_result = run_command(["gh", "auth", "status"], cwd=cwd, timeout=timeout)
    except RunnerError as exc:
        return AuthStatus(authenticated=False, error=str(exc))

    if not auth_result.ok:
        message = redact((auth_result.stderr or auth_result.stdout or "").strip()) or "gh is not authenticated"
        return AuthStatus(authenticated=False, error=message)

    try:
        perm_result = run_command(["gh", "repo", "view", repo, "--json", "viewerPermission"], cwd=cwd, timeout=timeout)
    except RunnerError as exc:
        return AuthStatus(authenticated=True, can_write=None, error=str(exc))

    if not perm_result.ok:
        return AuthStatus(authenticated=True, can_write=None, error=redact(perm_result.stderr.strip()))

    try:
        data = json.loads(perm_result.stdout)
    except json.JSONDecodeError as exc:
        return AuthStatus(authenticated=True, can_write=None, error=f"Malformed gh JSON: {exc}")

    permission = data.get("viewerPermission", "")
    can_write = permission in ("WRITE", "ADMIN", "MAINTAIN")
    return AuthStatus(authenticated=True, can_write=can_write, detail=f"viewerPermission={permission or 'unknown'}")


def create_issue(
    repo: str,
    title: str,
    body: str,
    cwd: Path,
    labels: Sequence[str] | None = None,
    attachments: Sequence[Path] | None = None,
    timeout: float = 30.0,
) -> IssueResult:
    try:
        _validate_repo(repo)
        full_body = body + format_attachments(attachments)
    except GitHubError as exc:
        return IssueResult(success=False, error=str(exc))

    argv = ["gh", "issue", "create", "--repo", repo, "--title", title, "--body", full_body]
    for label in labels or []:
        argv += ["--label", label]

    try:
        result = run_command(argv, cwd=cwd, timeout=timeout)
    except RunnerError as exc:
        return IssueResult(success=False, error=str(exc))

    return _issue_result_from(result)


def get_issue(repo: str, issue_number: int, cwd: Path, timeout: float = 30.0) -> IssueResult:
    try:
        _validate_repo(repo)
    except GitHubError as exc:
        return IssueResult(success=False, error=str(exc))

    argv = ["gh", "issue", "view", str(issue_number), "--repo", repo, "--json", "number,url,title,body,state"]
    try:
        result = run_command(argv, cwd=cwd, timeout=timeout)
    except RunnerError as exc:
        return IssueResult(success=False, error=str(exc))

    if not result.ok:
        return IssueResult(success=False, error=redact(result.stderr.strip()))

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        return IssueResult(success=False, raw_stdout=redact(result.stdout), error=f"Malformed gh JSON: {exc}")

    return IssueResult(success=True, number=data.get("number"), url=data.get("url"), raw_stdout=redact(result.stdout))


def comment_on_issue(
    repo: str,
    issue_number: int,
    body: str,
    cwd: Path,
    attachments: Sequence[Path] | None = None,
    timeout: float = 30.0,
) -> IssueResult:
    try:
        _validate_repo(repo)
        full_body = body + format_attachments(attachments)
    except GitHubError as exc:
        return IssueResult(success=False, number=issue_number, error=str(exc))

    argv = ["gh", "issue", "comment", str(issue_number), "--repo", repo, "--body", full_body]
    try:
        result = run_command(argv, cwd=cwd, timeout=timeout)
    except RunnerError as exc:
        return IssueResult(success=False, number=issue_number, error=str(exc))

    return _issue_result_from(result, fallback_number=issue_number)


def update_issue(
    repo: str,
    issue_number: int,
    cwd: Path,
    title: str | None = None,
    body: str | None = None,
    add_labels: Sequence[str] | None = None,
    state: str | None = None,
    timeout: float = 30.0,
) -> IssueResult:
    try:
        _validate_repo(repo)
    except GitHubError as exc:
        return IssueResult(success=False, number=issue_number, error=str(exc))

    if state == "closed":
        try:
            result = run_command(["gh", "issue", "close", str(issue_number), "--repo", repo], cwd=cwd, timeout=timeout)
        except RunnerError as exc:
            return IssueResult(success=False, number=issue_number, error=str(exc))
        if not result.ok:
            return _issue_result_from(result, fallback_number=issue_number)
    elif state == "open":
        try:
            result = run_command(["gh", "issue", "reopen", str(issue_number), "--repo", repo], cwd=cwd, timeout=timeout)
        except RunnerError as exc:
            return IssueResult(success=False, number=issue_number, error=str(exc))
        if not result.ok:
            return _issue_result_from(result, fallback_number=issue_number)

    argv = ["gh", "issue", "edit", str(issue_number), "--repo", repo]
    if title is not None:
        argv += ["--title", title]
    if body is not None:
        argv += ["--body", body]
    for label in add_labels or []:
        argv += ["--add-label", label]

    if len(argv) == 4:
        # Nothing left to edit beyond the state change already applied above.
        return IssueResult(success=True, number=issue_number)

    try:
        result = run_command(argv, cwd=cwd, timeout=timeout)
    except RunnerError as exc:
        return IssueResult(success=False, number=issue_number, error=str(exc))

    return _issue_result_from(result, fallback_number=issue_number)


def create_pull_request(
    repo: str,
    title: str,
    body: str,
    head: str,
    base: str,
    cwd: Path,
    draft: bool = True,
    timeout: float = 30.0,
) -> PullRequestResult:
    try:
        _validate_repo(repo)
    except GitHubError as exc:
        return PullRequestResult(success=False, error=str(exc))

    argv = [
        "gh", "pr", "create",
        "--repo", repo,
        "--title", title,
        "--body", body,
        "--head", head,
        "--base", base,
    ]
    if draft:
        argv.append("--draft")

    try:
        result = run_command(argv, cwd=cwd, timeout=timeout)
    except RunnerError as exc:
        return PullRequestResult(success=False, error=str(exc))

    return _pr_result_from(result)


def get_pull_request_for_branch(repo: str, head_branch: str, cwd: Path, timeout: float = 30.0) -> PullRequestResult:
    """Look up an existing PR for `head_branch`, if any.

    `success=False` here most commonly just means "no PR exists yet for
    this branch" (`gh pr view` exits non-zero in that case) - callers that
    want to avoid creating a duplicate PR should treat that as "safe to
    create one," not as a hard error; `.error` is still populated for
    visibility into which case occurred.
    """
    try:
        _validate_repo(repo)
    except GitHubError as exc:
        return PullRequestResult(success=False, error=str(exc))

    argv = ["gh", "pr", "view", head_branch, "--repo", repo, "--json", "number,url,state"]
    try:
        result = run_command(argv, cwd=cwd, timeout=timeout)
    except RunnerError as exc:
        return PullRequestResult(success=False, error=str(exc))

    if not result.ok:
        return PullRequestResult(success=False, error=redact((result.stderr or result.stdout).strip()))

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        return PullRequestResult(success=False, error=f"Malformed gh JSON: {exc}")

    return PullRequestResult(success=True, number=data.get("number"), url=data.get("url"), raw_stdout=redact(result.stdout))


def _issue_result_from(result: CommandResult, fallback_number: int | None = None) -> IssueResult:
    if not result.ok:
        return IssueResult(success=False, number=fallback_number, error=redact(result.stderr.strip()))

    stdout = result.stdout.strip()
    url = stdout.splitlines()[-1] if stdout else None
    number = (parse_issue_reference(url) if url else None) or fallback_number
    return IssueResult(success=True, number=number, url=url, raw_stdout=redact(result.stdout))


def _pr_result_from(result: CommandResult) -> PullRequestResult:
    if not result.ok:
        return PullRequestResult(success=False, error=redact(result.stderr.strip()))

    stdout = result.stdout.strip()
    url = stdout.splitlines()[-1] if stdout else None
    number = parse_pr_reference(url) if url else None
    return PullRequestResult(success=True, number=number, url=url, raw_stdout=redact(result.stdout))
