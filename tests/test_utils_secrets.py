"""Unit tests for utils.secrets.redact."""

from __future__ import annotations

from utils.secrets import redact


def test_redacts_known_env_secret(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-abcdefghijklmnop")
    text = "Authorization: Bearer sk-ant-abcdefghijklmnop"

    result = redact(text)

    assert "sk-ant-abcdefghijklmnop" not in result
    assert "***REDACTED***" in result


def test_redacts_github_token_pattern_without_env_var(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    text = "token=ghp_1234567890abcdef in the logs"

    result = redact(text)

    assert "ghp_1234567890abcdef" not in result
    assert "***REDACTED***" in result


def test_redact_handles_empty_and_none():
    assert redact("") == ""
    assert redact(None) == ""


def test_redact_leaves_normal_text_untouched():
    text = "Build succeeded in 12s"
    assert redact(text) == text
