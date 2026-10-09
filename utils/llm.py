"""LLM helpers for structured JSON analysis via the Cursor SDK.

Uses CURSOR_API_KEY and a local Cursor agent (see utils.cursor_agent).
Failures return LLMResult(success=False) instead of raising.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from utils.cursor_agent import extract_json_object, run_agent_prompt
from utils.secrets import redact

DEFAULT_MODEL = "composer-2.5"
DEFAULT_TIMEOUT = 60.0
DEFAULT_MAX_TOKENS = 1024

_client_cache: dict[str, str] = {}


class LLMError(Exception):
    """Raised only for programmer errors (e.g. an unreadable image path), not API failures."""


@dataclass
class LLMResult:
    success: bool
    data: dict[str, Any] | None = None
    raw_text: str | None = None
    error: str | None = None


def get_client(api_key: str | None = None, timeout: float = DEFAULT_TIMEOUT) -> str:
    """Compatibility shim for tests: returns a cache key (Cursor uses CURSOR_API_KEY in env)."""
    del timeout
    cache_key = api_key or "env"
    if cache_key not in _client_cache:
        _client_cache[cache_key] = cache_key
    return _client_cache[cache_key]


def _image_paths_block(images: list[Path]) -> str:
    lines = []
    for raw in images:
        path = Path(raw)
        if not path.is_file():
            raise LLMError(f"Image not found: {path}")
        lines.append(str(path.resolve()))
    return "Screenshot file paths to read with your file tools:\n" + "\n".join(lines)


def ask_for_json(
    prompt: str,
    images: list[Path] | None = None,
    model: str = DEFAULT_MODEL,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    client: Any | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    cwd: Path | None = None,
    tools: list[str] | None = None,
    dirs: list[Path] | None = None,
) -> LLMResult:
    """Send a prompt (optionally referencing image paths) and parse JSON from the reply.

    `tools`/`dirs` let a caller hand the agent real search tools over one or
    more repositories instead of only the text in `prompt`.
    """
    del max_tokens
    workdir = cwd or Path.cwd()

    try:
        image_block = _image_paths_block(images or []) if images else ""
    except LLMError as exc:
        return LLMResult(success=False, error=str(exc))

    if client is not None and hasattr(client, "messages"):
        return _ask_via_legacy_test_client(prompt, images, client)

    full_prompt = redact(prompt)
    if image_block:
        full_prompt = f"{image_block}\n\n{full_prompt}"
    full_prompt += "\n\nRespond with ONLY a single JSON object. No markdown fences or commentary."

    ok, raw_text, err = run_agent_prompt(
        full_prompt,
        workdir,
        model=model,
        timeout=timeout,
        tools=tools or ["read"],
        dirs=dirs,
    )
    if not ok:
        return LLMResult(success=False, error=err or "Cursor agent failed")

    data, parse_err = extract_json_object(raw_text)
    if parse_err:
        return LLMResult(success=False, raw_text=redact(raw_text), error=parse_err)

    return LLMResult(success=True, data=data, raw_text=redact(raw_text))


def _ask_via_legacy_test_client(
    prompt: str,
    images: list[Path] | None,
    client: Any,
) -> LLMResult:
    """Support unit tests that inject a fake Anthropic-style client."""
    try:
        content: list[dict[str, Any]] = []
        for path in images or []:
            content.append({"type": "image", "source": {"media_type": "image/png", "data": "x"}})
        content.append({"type": "text", "text": redact(prompt)})
        response = client.messages.create(
            model="test",
            max_tokens=1024,
            messages=[{"role": "user", "content": content}],
        )
    except Exception as exc:
        name = type(exc).__name__
        if "Timeout" in name:
            return LLMResult(success=False, error=f"LLM request timed out: {exc}")
        return LLMResult(success=False, error=f"LLM API error: {exc}")

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
