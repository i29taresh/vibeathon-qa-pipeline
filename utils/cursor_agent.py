"""Cursor SDK agent invocations (local runtime, CURSOR_API_KEY)."""

from __future__ import annotations

import os
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from utils.secrets import redact

try:
    from cursor_sdk import Agent, AgentOptions, CursorAgentError, LocalAgentOptions
except ImportError:  # pragma: no cover - tested via mocks when package absent
    Agent = None  # type: ignore[misc, assignment]
    AgentOptions = None  # type: ignore[misc, assignment]
    CursorAgentError = Exception  # type: ignore[misc, assignment]
    LocalAgentOptions = None  # type: ignore[misc, assignment]

DEFAULT_CURSOR_MODEL = "composer-2.5"
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)


def cursor_api_key() -> str | None:
    return os.environ.get("CURSOR_API_KEY") or None


def default_model() -> str:
    return os.environ.get("CURSOR_MODEL", DEFAULT_CURSOR_MODEL)


def extract_json_object(text: str) -> tuple[dict | None, str | None]:
    """Parse a JSON object from agent output (raw JSON or fenced code block)."""
    raw = (text or "").strip()
    if not raw:
        return None, "Empty response"

    candidates = [raw]
    for match in _JSON_FENCE_RE.finditer(raw):
        candidates.append(match.group(1).strip())

    import json

    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data, None
        return None, "Response JSON was not an object"

    return None, "Malformed JSON response"


def run_agent_prompt(
    prompt: str,
    cwd: Path,
    model: str | None = None,
    timeout: float = 120.0,
    tools: list[str] | None = None,
    dirs: list[Path] | None = None,
) -> tuple[bool, str, str | None]:
    """One-shot local Cursor agent. Returns (success, result_text, error_message).

    The SDK call itself has no deadline, so this wrapper enforces `timeout`.
    A timed-out run may keep working in the background; callers should proceed
    without its result.
    """
    api_key = cursor_api_key()
    if not api_key:
        return False, "", "CURSOR_API_KEY is not set"

    if Agent is None or AgentOptions is None or LocalAgentOptions is None:
        return False, "", "cursor-sdk is not installed (pip install cursor-sdk)"

    safe_prompt = redact(prompt)
    resolved = str(Path(cwd).resolve())

    extra_dirs = [str(Path(d).resolve()) for d in (dirs or [])]
    extra_dirs = [d for d in extra_dirs if d != resolved]

    def _call():
        options = AgentOptions(
            api_key=api_key,
            model=model or default_model(),
            local=LocalAgentOptions(cwd=resolved, dirs=extra_dirs or None),
            tools=tools,
        )
        return Agent.prompt(safe_prompt, options)

    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(_call)
    try:
        result = future.result(timeout=timeout)
    except TimeoutError:
        pool.shutdown(wait=False, cancel_futures=True)
        return False, "", f"Cursor agent timed out after {timeout:.0f}s"
    except CursorAgentError as exc:
        pool.shutdown(wait=False, cancel_futures=True)
        return False, "", redact(str(exc))
    except Exception as exc:  # noqa: BLE001
        pool.shutdown(wait=False, cancel_futures=True)
        return False, "", redact(str(exc))
    else:
        pool.shutdown(wait=False, cancel_futures=True)

    if getattr(result, "status", None) == "error":
        return False, result.result or "", f"Cursor agent run failed ({result.id})"

    return True, result.result or "", None
