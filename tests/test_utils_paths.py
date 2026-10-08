"""Unit tests for utils.paths.ensure_within."""

from __future__ import annotations

import pytest

from utils.paths import PathSecurityError, ensure_within


def test_ensure_within_allows_nested_path(tmp_path):
    nested = tmp_path / "a" / "b.txt"
    nested.parent.mkdir(parents=True)
    nested.write_text("x")

    assert ensure_within(nested, tmp_path) == nested.resolve()


def test_ensure_within_allows_root_itself(tmp_path):
    assert ensure_within(tmp_path, tmp_path) == tmp_path.resolve()


def test_ensure_within_rejects_path_outside_root(tmp_path):
    outside = tmp_path.parent / "outside.txt"

    with pytest.raises(PathSecurityError):
        ensure_within(outside, tmp_path)


def test_ensure_within_rejects_dot_dot_escape(tmp_path):
    escape = tmp_path / ".." / "escape.txt"

    with pytest.raises(PathSecurityError):
        ensure_within(escape, tmp_path)
