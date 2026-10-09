"""Unit tests for utils.cursor_agent JSON extraction."""

from utils.cursor_agent import extract_json_object


def test_extract_json_object_raw():
    data, err = extract_json_object('{"ok": true}')
    assert err is None
    assert data == {"ok": True}


def test_extract_json_object_fenced():
    data, err = extract_json_object('Here you go:\n```json\n{"a": 1}\n```')
    assert err is None
    assert data == {"a": 1}
