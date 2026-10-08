"""Unit tests for utils.llm.ask_for_json - with a fake Anthropic client, no network calls."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import anthropic

from utils.llm import ask_for_json, get_client


@dataclass
class _FakeBlock:
    type: str
    text: str


@dataclass
class _FakeResponse:
    content: list


class _FakeMessages:
    def __init__(self, response=None, exc=None):
        self._response = response
        self._exc = exc
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self._exc is not None:
            raise self._exc
        return self._response


@dataclass
class _FakeClient:
    messages: _FakeMessages = field(default_factory=_FakeMessages)

    @classmethod
    def with_response(cls, text: str) -> "_FakeClient":
        return cls(messages=_FakeMessages(response=_FakeResponse(content=[_FakeBlock(type="text", text=text)])))

    @classmethod
    def with_error(cls, exc: Exception) -> "_FakeClient":
        return cls(messages=_FakeMessages(exc=exc))


def test_ask_for_json_success():
    client = _FakeClient.with_response('{"root_cause": "x"}')

    result = ask_for_json("analyze this", client=client)

    assert result.success
    assert result.data == {"root_cause": "x"}


def test_ask_for_json_malformed_json():
    client = _FakeClient.with_response("not json at all")

    result = ask_for_json("analyze this", client=client)

    assert not result.success
    assert "Malformed JSON" in result.error


def test_ask_for_json_non_object_json():
    client = _FakeClient.with_response("[1, 2, 3]")

    result = ask_for_json("analyze this", client=client)

    assert not result.success
    assert "not an object" in result.error


def test_ask_for_json_timeout():
    client = _FakeClient.with_error(anthropic.APITimeoutError(request=None))

    result = ask_for_json("analyze this", client=client)

    assert not result.success
    assert "timed out" in result.error


def test_ask_for_json_api_error():
    client = _FakeClient.with_error(anthropic.AnthropicError("boom"))

    result = ask_for_json("analyze this", client=client)

    assert not result.success
    assert "LLM API error" in result.error


def test_ask_for_json_missing_image(tmp_path):
    client = _FakeClient.with_response('{"ok": true}')

    result = ask_for_json("analyze this", images=[tmp_path / "missing.png"], client=client)

    assert not result.success
    assert "Image not found" in result.error
    assert client.messages.calls == []


def test_ask_for_json_includes_image_block(tmp_path):
    image_path = tmp_path / "screenshot.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\nfakepngdata")
    client = _FakeClient.with_response('{"ok": true}')

    result = ask_for_json("what is wrong?", images=[image_path], client=client)

    assert result.success
    sent_content = client.messages.calls[0]["messages"][0]["content"]
    assert sent_content[0]["type"] == "image"
    assert sent_content[0]["source"]["media_type"] == "image/png"
    assert sent_content[-1]["type"] == "text"


def test_prompt_text_is_redacted_before_sending(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-abcdefghijklmnop")
    client = _FakeClient.with_response('{"ok": true}')

    ask_for_json("here is my key sk-ant-abcdefghijklmnop please use it", client=client)

    sent_text = client.messages.calls[0]["messages"][0]["content"][-1]["text"]
    assert "sk-ant-abcdefghijklmnop" not in sent_text


def test_get_client_is_cached_per_api_key():
    client_a = get_client(api_key="test-key-a")
    client_b = get_client(api_key="test-key-a")
    client_c = get_client(api_key="test-key-b")

    assert client_a is client_b
    assert client_a is not client_c
