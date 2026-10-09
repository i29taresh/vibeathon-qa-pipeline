"""Jira issue operations via the Jira REST API (stdlib HTTP only).

Credentials from environment (set by the dashboard session or the shell):
  JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN
"""

from __future__ import annotations

import base64
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence

from utils.secrets import redact

_ISSUE_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")


class JiraError(Exception):
    """Invalid input or API failure surfaced to callers."""


@dataclass
class IssueResult:
    success: bool
    key: str | None = None
    url: str | None = None
    raw_body: str = ""
    error: str | None = None


@dataclass
class AuthStatus:
    authenticated: bool
    can_write: bool | None = None
    detail: str = ""
    error: str | None = None


@dataclass
class JiraAttachmentMeta:
    id: str
    filename: str
    mime_type: str
    content_url: str
    size: int


@dataclass
class IssueDetails:
    key: str
    url: str
    summary: str
    description_text: str
    attachments: list[JiraAttachmentMeta]


def _credentials() -> tuple[str, str, str]:
    base_url = (os.environ.get("JIRA_BASE_URL") or "").rstrip("/")
    email = os.environ.get("JIRA_EMAIL") or ""
    token = os.environ.get("JIRA_API_TOKEN") or ""
    if not base_url or not email or not token:
        raise JiraError(
            "Jira credentials missing. Set JIRA_BASE_URL, JIRA_EMAIL, and JIRA_API_TOKEN in the session."
        )
    return base_url, email, token


def _auth_header(email: str, token: str) -> str:
    raw = f"{email}:{token}".encode()
    return "Basic " + base64.b64encode(raw).decode("ascii")


def _request(
    method: str,
    path: str,
    base_url: str,
    email: str,
    token: str,
    payload: dict[str, Any] | None = None,
    timeout: float = 30.0,
) -> tuple[int, str]:
    url = f"{base_url}{path}"
    data = None
    headers = {
        "Authorization": _auth_header(email, token),
        "Accept": "application/json",
    }
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return resp.status, body
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return exc.code, body
    except urllib.error.URLError as exc:
        raise JiraError(redact(str(exc.reason))) from exc


def parse_issue_key(text: str) -> str | None:
    match = _ISSUE_KEY_RE.search(text or "")
    return match.group(1) if match else None


def normalize_issue_input(text: str) -> str | None:
    """Accept issue key or Jira browse URL; return canonical KEY-123 or None."""
    raw = (text or "").strip()
    if not raw:
        return None
    key = parse_issue_key(raw)
    if key:
        return key
    compact = raw.upper().replace(" ", "")
    if _ISSUE_KEY_RE.fullmatch(compact):
        return compact
    return None


def format_attachments(attachments: Sequence[Path] | None) -> str:
    if not attachments:
        return ""
    lines = ["", "---", "**Evidence:**"]
    for raw_path in attachments:
        path = Path(raw_path)
        if not path.is_file():
            raise JiraError(f"Attachment not found: {path}")
        lines.append(f"- `{path.name}` (local path: `{path}`)")
    return "\n".join(lines)


def check_auth(base_url: str, project_key: str, cwd: Path | None = None, timeout: float = 15.0) -> AuthStatus:
    """Verify credentials and that the Jira project exists."""
    del cwd  # Jira REST does not use cwd; kept for API symmetry with github.check_auth
    try:
        resolved_base, email, token = _credentials()
    except JiraError as exc:
        return AuthStatus(authenticated=False, error=str(exc))

    if base_url.rstrip("/") != resolved_base:
        return AuthStatus(
            authenticated=False,
            error=f"JIRA_BASE_URL ({resolved_base}) does not match configured base_url ({base_url})",
        )

    status, body = _request("GET", "/rest/api/3/myself", resolved_base, email, token, timeout=timeout)
    if status != 200:
        return AuthStatus(authenticated=False, error=redact(body) or f"HTTP {status}")

    encoded_key = urllib.parse.quote(project_key, safe="")
    status, body = _request(
        "GET", f"/rest/api/3/project/{encoded_key}", resolved_base, email, token, timeout=timeout
    )
    if status != 200:
        return AuthStatus(
            authenticated=True,
            can_write=False,
            detail=redact(body) or f"Cannot access project {project_key}",
            error=f"Project {project_key} not accessible (HTTP {status})",
        )
    return AuthStatus(authenticated=True, can_write=True, detail=f"project {project_key} accessible")


def _adf_paragraph(text: str) -> dict[str, Any]:
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": text}],
            }
        ],
    }


def create_issue(
    base_url: str,
    project_key: str,
    summary: str,
    description_markdown: str,
    issue_type: str = "Bug",
    cwd: Path | None = None,
    attachments: Sequence[Path] | None = None,
    timeout: float = 30.0,
) -> IssueResult:
    del cwd
    try:
        resolved_base, email, token = _credentials()
    except JiraError as exc:
        return IssueResult(success=False, error=str(exc))

    body_text = description_markdown
    if attachments:
        body_text += format_attachments(attachments)

    payload = {
        "fields": {
            "project": {"key": project_key},
            "summary": summary,
            "issuetype": {"name": issue_type},
            "description": _adf_paragraph(body_text),
        }
    }
    status, raw = _request(
        "POST", "/rest/api/3/issue", resolved_base, email, token, payload=payload, timeout=timeout
    )
    if status not in (200, 201):
        return IssueResult(success=False, raw_body=redact(raw), error=redact(raw) or f"HTTP {status}")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return IssueResult(success=False, raw_body=redact(raw), error="Invalid JSON from Jira")

    key = data.get("key")
    issue_url = f"{resolved_base}/browse/{key}" if key else None
    return IssueResult(success=True, key=key, url=issue_url, raw_body=raw)


def comment_on_issue(
    issue_key: str,
    body_markdown: str,
    cwd: Path | None = None,
    attachments: Sequence[Path] | None = None,
    timeout: float = 30.0,
) -> IssueResult:
    del cwd
    try:
        resolved_base, email, token = _credentials()
    except JiraError as exc:
        return IssueResult(success=False, error=str(exc))

    text = body_markdown
    if attachments:
        text += format_attachments(attachments)

    payload = {"body": _adf_paragraph(text)}
    encoded = urllib.parse.quote(issue_key, safe="")
    status, raw = _request(
        "POST",
        f"/rest/api/3/issue/{encoded}/comment",
        resolved_base,
        email,
        token,
        payload=payload,
        timeout=timeout,
    )
    if status not in (200, 201):
        return IssueResult(success=False, raw_body=redact(raw), error=redact(raw) or f"HTTP {status}")

    return IssueResult(success=True, key=issue_key, url=f"{resolved_base}/browse/{issue_key}", raw_body=raw)


def adf_to_plain_text(node: Any) -> str:
    """Best-effort plain text from Jira Cloud ADF description JSON."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if not isinstance(node, dict):
        return ""

    node_type = node.get("type")
    if node_type == "text":
        return str(node.get("text", ""))

    children = node.get("content") or []
    child_text = "".join(adf_to_plain_text(child) for child in children)
    if node_type in ("paragraph", "heading", "listItem"):
        return child_text + "\n"
    if node_type in ("bulletList", "orderedList"):
        return child_text
    return child_text


def get_issue_details(issue_key: str, cwd: Path | None = None, timeout: float = 30.0) -> tuple[IssueDetails | None, str | None]:
    """Fetch summary, description, and attachment metadata for an issue."""
    del cwd
    try:
        resolved_base, email, token = _credentials()
    except JiraError as exc:
        return None, str(exc)

    encoded = urllib.parse.quote(issue_key, safe="")
    fields = urllib.parse.quote("summary,description,attachment", safe="")
    status, raw = _request(
        "GET",
        f"/rest/api/3/issue/{encoded}?fields={fields}",
        resolved_base,
        email,
        token,
        timeout=timeout,
    )
    if status != 200:
        return None, redact(raw) or f"HTTP {status}"

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None, "Invalid JSON from Jira"

    fields_data = data.get("fields") or {}
    summary = str(fields_data.get("summary") or "")
    description_text = adf_to_plain_text(fields_data.get("description")).strip()

    attachments: list[JiraAttachmentMeta] = []
    for item in fields_data.get("attachment") or []:
        if not isinstance(item, dict):
            continue
        content_url = str(item.get("content") or "")
        filename = str(item.get("filename") or "attachment")
        if not content_url:
            continue
        attachments.append(
            JiraAttachmentMeta(
                id=str(item.get("id") or filename),
                filename=filename,
                mime_type=str(item.get("mimeType") or ""),
                content_url=content_url,
                size=int(item.get("size") or 0),
            )
        )

    key = str(data.get("key") or issue_key)
    return (
        IssueDetails(
            key=key,
            url=f"{resolved_base}/browse/{key}",
            summary=summary,
            description_text=description_text,
            attachments=attachments,
        ),
        None,
    )


def download_attachment(content_url: str, dest_path: Path, timeout: float = 120.0) -> tuple[bool, str | None]:
    """Download one Jira attachment (authenticated) to dest_path."""
    try:
        resolved_base, email, token = _credentials()
    except JiraError as exc:
        return False, str(exc)

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    headers = {"Authorization": _auth_header(email, token)}
    req = urllib.request.Request(content_url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return False, redact(body) or f"HTTP {exc.code}"
    except urllib.error.URLError as exc:
        return False, redact(str(exc.reason))

    dest_path.write_bytes(data)
    return True, None


def get_issue(issue_key: str, cwd: Path | None = None, timeout: float = 30.0) -> IssueResult:
    del cwd
    try:
        resolved_base, email, token = _credentials()
    except JiraError as exc:
        return IssueResult(success=False, error=str(exc))

    encoded = urllib.parse.quote(issue_key, safe="")
    status, raw = _request(
        "GET", f"/rest/api/3/issue/{encoded}", resolved_base, email, token, timeout=timeout
    )
    if status != 200:
        return IssueResult(success=False, raw_body=redact(raw), error=redact(raw) or f"HTTP {status}")
    return IssueResult(success=True, key=issue_key, url=f"{resolved_base}/browse/{issue_key}", raw_body=raw)
