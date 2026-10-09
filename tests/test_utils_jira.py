"""Unit tests for utils.jira (HTTP mocked)."""

from __future__ import annotations

import json
from unittest.mock import patch

from utils.jira import check_auth, create_issue, parse_issue_key


def test_parse_issue_key():
    assert parse_issue_key("Fixes B2B-42 please") == "B2B-42"


@patch("utils.jira._request")
def test_check_auth_ok(mock_request):
    mock_request.side_effect = [
        (200, json.dumps({"accountId": "1"})),
        (200, json.dumps({"key": "B2B"})),
    ]
    import os

    os.environ["JIRA_BASE_URL"] = "https://example.atlassian.net"
    os.environ["JIRA_EMAIL"] = "user@example.com"
    os.environ["JIRA_API_TOKEN"] = "token"
    status = check_auth("https://example.atlassian.net", "B2B")
    assert status.authenticated
    assert status.can_write is True


@patch("utils.jira._request")
def test_create_issue_success(mock_request):
    mock_request.return_value = (201, json.dumps({"key": "B2B-9", "self": "x"}))
    import os

    os.environ["JIRA_BASE_URL"] = "https://example.atlassian.net"
    os.environ["JIRA_EMAIL"] = "user@example.com"
    os.environ["JIRA_API_TOKEN"] = "token"
    result = create_issue(
        "https://example.atlassian.net",
        "B2B",
        "title",
        "body",
    )
    assert result.success
    assert result.key == "B2B-9"
