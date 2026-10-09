"""Secret redaction helpers.

Used anywhere we might log, store, or forward subprocess output, CLI
arguments, or LLM prompt text: tool output can easily contain an API key or
token that the host process has in its environment (or that a screenshot /
log file accidentally captured), and none of that should be echoed back out
to a log line, a GitHub issue body, or an LLM prompt.
"""

from __future__ import annotations

import os
import re

_SECRET_ENV_VARS = (
    "CURSOR_API_KEY",
    "ANTHROPIC_API_KEY",
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "OPENAI_API_KEY",
    "GITLAB_TOKEN",
    "GITLAB_PRIVATE_TOKEN",
    "GITLAB_API_ACCESSTOKEN",
    "JIRA_API_TOKEN",
)

# Common token *shapes*, redacted even if they didn't come from one of the
# env vars above (e.g. a token pasted into a log line by some other tool).
_SECRET_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9\-_]{10,}"),
    re.compile(r"ghp_[A-Za-z0-9]{10,}"),
    re.compile(r"gho_[A-Za-z0-9]{10,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{10,}"),
    re.compile(r"glpat-[A-Za-z0-9\-_]{10,}"),
]

REDACTED = "***REDACTED***"


def known_secret_values() -> list[str]:
    """Current values of well-known secret env vars, longest first.

    Longest-first ordering avoids a short secret's value accidentally
    matching as a substring and leaving part of a longer one exposed.
    """
    values = {os.environ[name] for name in _SECRET_ENV_VARS if os.environ.get(name)}
    return sorted(values, key=len, reverse=True)


def redact(text: str | None) -> str:
    """Replace known secret env var values and common token shapes with a placeholder."""
    if not text:
        return text or ""

    redacted = text
    for value in known_secret_values():
        redacted = redacted.replace(value, REDACTED)
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub(REDACTED, redacted)
    return redacted
