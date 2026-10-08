"""Thin, reusable wrapper around the Anthropic SDK.

One cached client, text + image input, and strict JSON-only output. Every
failure mode (timeout, API error, non-JSON reply) is reported back as a
LLMResult with success=False rather than raised, so a caller can branch on
`result.success` without wrapping every call in its own try/except.

Prompt text is redacted before it's sent and before any of it is echoed
into a log line or error message - upstream text (RCA input, issue bodies,
log excerpts) is untrusted and may accidentally contain a token.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anthropic

from utils.secrets import redact

DEFAULT_MODEL = "claude-sonnet-5"
DEFAULT_TIMEOUT = 60.0
DEFAULT_MAX_TOKENS = 1024

_IMAGE_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}

_client_cache: dict[str, anthropic.Anthropic] = {}


class LLMError(Exception):
    """Raised only for programmer errors (e.g. an unreadable image path), not API failures."""


@dataclass
class LLMResult:
    success: bool
    data: dict[str, Any] | None = None
    raw_text: str | None = None
    error: str | None = None


def get_client(api_key: str | None = None, timeout: float = DEFAULT_TIMEOUT) -> anthropic.Anthropic:
    """Create (and cache) a reusable Anthropic client.

    Reads ANTHROPIC_API_KEY from the environment when `api_key` is omitted
    (the SDK's own default behavior) - this function never logs or returns
    the key itself.
    """
    cache_key = api_key or "env"
    if cache_key not in _client_cache:
        _client_cache[cache_key] = anthropic.Anthropic(api_key=api_key, timeout=timeout)
    return _client_cache[cache_key]


def _image_block(image_path: Path) -> dict[str, Any]:
    path = Path(image_path)
    if not path.is_file():
        raise LLMError(f"Image not found: {path}")

    media_type = _IMAGE_MEDIA_TYPES.get(path.suffix.lower())
    if media_type is None:
        raise LLMError(f"Unsupported image type '{path.suffix}' for {path}")

    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": media_type, "data": data},
    }


def ask_for_json(
    prompt: str,
    images: list[Path] | None = None,
    model: str = DEFAULT_MODEL,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    client: anthropic.Anthropic | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> LLMResult:
    """Send a prompt (optionally with images) and parse the reply as a JSON object."""
    try:
        content: list[dict[str, Any]] = [_image_block(p) for p in (images or [])]
    except LLMError as exc:
        return LLMResult(success=False, error=str(exc))

    content.append({"type": "text", "text": redact(prompt)})

    active_client = client or get_client(timeout=timeout)

    try:
        response = active_client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": content}],
        )
    except anthropic.APITimeoutError as exc:
        return LLMResult(success=False, error=f"LLM request timed out: {redact(str(exc))}")
    except anthropic.AnthropicError as exc:
        return LLMResult(success=False, error=f"LLM API error: {redact(str(exc))}")

    raw_text = "".join(
        block.text for block in response.content if getattr(block, "type", None) == "text"
    )

    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        return LLMResult(success=False, raw_text=redact(raw_text), error=f"Malformed JSON response: {exc}")

    if not isinstance(data, dict):
        return LLMResult(success=False, raw_text=redact(raw_text), error="Response JSON was not an object")

    return LLMResult(success=True, data=data, raw_text=redact(raw_text))
