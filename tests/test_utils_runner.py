"""Unit tests for utils.runner.run_command - all with subprocess.run mocked out."""

from __future__ import annotations

import subprocess

import pytest

from utils.runner import RunnerError, run_command


def test_run_command_success(monkeypatch, tmp_path):
    def fake_run(argv, **kwargs):
        assert kwargs["shell"] is False
        assert argv == ["echo", "hi"]
        return subprocess.CompletedProcess(argv, 0, stdout="hi\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = run_command(["echo", "hi"], cwd=tmp_path)

    assert result.ok
    assert result.returncode == 0
    assert result.stdout == "hi\n"


def test_run_command_nonzero_exit(monkeypatch, tmp_path):
    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="boom")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = run_command(["false"], cwd=tmp_path)

    assert not result.ok
    assert result.returncode == 1
    assert result.stderr == "boom"


def test_run_command_timeout(monkeypatch, tmp_path):
    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=1, output="partial", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = run_command(["sleep", "100"], cwd=tmp_path, timeout=1)

    assert result.timed_out
    assert not result.ok


def test_run_command_missing_binary(monkeypatch, tmp_path):
    def fake_run(argv, **kwargs):
        raise FileNotFoundError()

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RunnerError):
        run_command(["definitely-not-a-real-binary"], cwd=tmp_path)


def test_run_command_rejects_shell_string(tmp_path):
    with pytest.raises(RunnerError):
        run_command("echo hi", cwd=tmp_path)  # type: ignore[arg-type]


def test_run_command_rejects_empty_argv(tmp_path):
    with pytest.raises(RunnerError):
        run_command([], cwd=tmp_path)


def test_run_command_requires_existing_cwd(tmp_path):
    with pytest.raises(RunnerError):
        run_command(["echo", "hi"], cwd=tmp_path / "does_not_exist")


def test_run_command_never_uses_shell_true(monkeypatch, tmp_path):
    captured = {}

    def fake_run(argv, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    run_command(["echo", "hi"], cwd=tmp_path)

    assert captured["shell"] is False
