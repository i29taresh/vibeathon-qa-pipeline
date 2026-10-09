"""Unit tests for utils.gitlab (HTTP mocked)."""

from __future__ import annotations

import json
from unittest.mock import patch

from utils.gitlab import check_auth, clone_url, create_merge_request


def test_clone_url_embeds_oauth2_token():
    url = clone_url("gitlab.example.com", "group/project", token="glpat-secret")
    assert url.startswith("https://oauth2:glpat-secret@gitlab.example.com/")
    assert url.endswith("/group/project.git")


@patch("utils.gitlab._request")
def test_check_auth_ok(mock_request):
    mock_request.return_value = (
        200,
        json.dumps({"permissions": {"project_access": {"access_level": 40}}}),
    )
    import os

    os.environ["GITLAB_TOKEN"] = "glpat-testtoken1234567890"
    status = check_auth("gitlab.example.com", "group/project")
    assert status.authenticated
    assert status.can_write is True


@patch("utils.gitlab._request")
def test_create_mr_success(mock_request):
    mock_request.return_value = (
        201,
        json.dumps({"iid": 3, "web_url": "https://gitlab.example.com/g/p/-/merge_requests/3"}),
    )
    import os

    os.environ["GITLAB_TOKEN"] = "glpat-testtoken1234567890"
    result = create_merge_request(
        "gitlab.example.com",
        "group/project",
        "title",
        "body",
        source_branch="ai-fix/B2B-1",
        target_branch="main",
    )
    assert result.success
    assert result.iid == 3
