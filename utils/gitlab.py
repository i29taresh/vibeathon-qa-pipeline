"""GitLab merge request operations via the GitLab REST API (stdlib HTTP only).

Credentials from environment:
  GITLAB_TOKEN (required)
  GITLAB_HOST (optional default host when not taken from app config)
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from utils.secrets import redact

_PROJECT_PATH_RE = __import__("re").compile(r"^[\w.-]+(/[\w.-]+)+$")


class GitLabError(Exception):
    """Invalid input or API failure surfaced to callers."""


@dataclass
class MergeRequestResult:
    success: bool
    iid: int | None = None
    url: str | None = None
    raw_body: str = ""
    error: str | None = None


@dataclass
class AuthStatus:
    authenticated: bool
    can_write: bool | None = None
    detail: str = ""
    error: str | None = None


def _token() -> str:
    token = (
        os.environ.get("GITLAB_TOKEN")
        or os.environ.get("GITLAB_API_ACCESSTOKEN")
        or os.environ.get("GITLAB_PRIVATE_TOKEN")
        or ""
    )
    if not token:
        raise GitLabError("GITLAB_TOKEN is not set in the session.")
    return token


def _api_base(host: str) -> str:
    host = host.strip().rstrip("/")
    if host.startswith("http://") or host.startswith("https://"):
        return f"{host}/api/v4"
    return f"https://{host}/api/v4"


def _request(
    method: str,
    api_base: str,
    path: str,
    token: str,
    payload: dict | None = None,
    timeout: float = 30.0,
) -> tuple[int, str]:
    url = f"{api_base}{path}"
    data = None
    headers = {"PRIVATE-TOKEN": token, "Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as exc:
        raise GitLabError(redact(str(exc.reason))) from exc


def _encode_project(project_path: str) -> str:
    if not _PROJECT_PATH_RE.match(project_path):
        raise GitLabError(f"project_path must be a nested path, got {project_path!r}")
    return urllib.parse.quote(project_path, safe="")


def format_attachments(attachments: Sequence[Path] | None) -> str:
    if not attachments:
        return ""
    lines = ["", "---", "**Evidence:**"]
    for raw_path in attachments:
        path = Path(raw_path)
        if not path.is_file():
            raise GitLabError(f"Attachment not found: {path}")
        lines.append(f"- `{path.name}` (local path: `{path}`)")
    return "\n".join(lines)


def check_auth(host: str, project_path: str, cwd: Path | None = None, timeout: float = 15.0) -> AuthStatus:
    del cwd
    try:
        token = _token()
    except GitLabError as exc:
        return AuthStatus(authenticated=False, error=str(exc))

    api_base = _api_base(host)
    try:
        encoded = _encode_project(project_path)
    except GitLabError as exc:
        return AuthStatus(authenticated=False, error=str(exc))

    status, body = _request("GET", api_base, f"/projects/{encoded}", token, timeout=timeout)
    if status != 200:
        return AuthStatus(authenticated=False, error=redact(body) or f"HTTP {status}")

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return AuthStatus(authenticated=False, error="Invalid JSON from GitLab")

    perms = data.get("permissions") or {}
    project_access = perms.get("project_access") or {}
    group_access = perms.get("group_access") or {}
    level = max(project_access.get("access_level") or 0, group_access.get("access_level") or 0)
    can_write = level >= 30  # Developer
    detail = f"access_level={level}" if level else "permissions unknown"
    return AuthStatus(authenticated=True, can_write=can_write if level else None, detail=detail)


def get_merge_request_for_branch(
    host: str,
    project_path: str,
    source_branch: str,
    cwd: Path | None = None,
    timeout: float = 30.0,
) -> MergeRequestResult:
    del cwd
    try:
        token = _token()
        encoded = _encode_project(project_path)
    except GitLabError as exc:
        return MergeRequestResult(success=False, error=str(exc))

    api_base = _api_base(host)
    query = urllib.parse.urlencode({"source_branch": source_branch, "state": "opened"})
    status, body = _request(
        "GET", api_base, f"/projects/{encoded}/merge_requests?{query}", token, timeout=timeout
    )
    if status != 200:
        return MergeRequestResult(success=False, raw_body=redact(body), error=redact(body) or f"HTTP {status}")

    try:
        items = json.loads(body)
    except json.JSONDecodeError:
        return MergeRequestResult(success=False, error="Invalid JSON from GitLab")

    if not items:
        return MergeRequestResult(success=False, error="No open merge request for branch")

    mr = items[0]
    return MergeRequestResult(
        success=True,
        iid=mr.get("iid"),
        url=mr.get("web_url"),
        raw_body=body,
    )


def create_merge_request(
    host: str,
    project_path: str,
    title: str,
    description: str,
    source_branch: str,
    target_branch: str,
    cwd: Path | None = None,
    draft: bool = True,
    timeout: float = 30.0,
) -> MergeRequestResult:
    del cwd
    try:
        token = _token()
        encoded = _encode_project(project_path)
    except GitLabError as exc:
        return MergeRequestResult(success=False, error=str(exc))

    api_base = _api_base(host)
    payload = {
        "title": title,
        "description": description,
        "source_branch": source_branch,
        "target_branch": target_branch,
        "remove_source_branch": False,
    }
    if draft:
        payload["title"] = f"Draft: {title}"

    status, body = _request(
        "POST",
        api_base,
        f"/projects/{encoded}/merge_requests",
        token,
        payload=payload,
        timeout=timeout,
    )
    if status not in (200, 201):
        return MergeRequestResult(success=False, raw_body=redact(body), error=redact(body) or f"HTTP {status}")

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return MergeRequestResult(success=False, error="Invalid JSON from GitLab")

    return MergeRequestResult(
        success=True,
        iid=data.get("iid"),
        url=data.get("web_url"),
        raw_body=body,
    )
