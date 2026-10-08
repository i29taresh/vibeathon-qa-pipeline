"""Unit tests for utils.platform - all with utils.platform.run_command mocked out.

No real adb/xcrun/gradlew/xcodebuild is ever invoked here.
"""

from __future__ import annotations

import json

import utils.platform as platform_mod
from config import load_app_config
from utils.platform import build_app, capture_logs, install_app, select_device
from utils.runner import CommandResult


def _android_config():
    return load_app_config("sample_android")


def _ios_config():
    return load_app_config("sample_ios")


def _result(returncode=0, stdout="", stderr=""):
    return CommandResult(argv=[], returncode=returncode, stdout=stdout, stderr=stderr)


class _FakeRunner:
    """Replaces utils.platform.run_command with a scripted sequence of results."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[list[str]] = []

    def __call__(self, argv, cwd, timeout=120.0, env=None):
        self.calls.append(list(argv))
        assert self._responses, f"No more fake responses queued for call: {argv}"
        return self._responses.pop(0)


def test_select_android_device_found(monkeypatch):
    cfg = _android_config()
    fake = _FakeRunner(
        [
            _result(stdout="List of devices attached\nemulator-5554\tdevice\n"),
            _result(stdout="pixel_7_api_34\nOK\n"),
        ]
    )
    monkeypatch.setattr(platform_mod, "run_command", fake)

    result = select_device(cfg)

    assert result.success
    assert result.details["device_id"] == "emulator-5554"


def test_select_android_device_not_running(monkeypatch):
    cfg = _android_config()
    fake = _FakeRunner([_result(stdout="List of devices attached\n")])
    monkeypatch.setattr(platform_mod, "run_command", fake)

    result = select_device(cfg)

    assert not result.success
    assert "not currently running" in result.error


def test_select_ios_device_already_booted(monkeypatch):
    cfg = _ios_config()
    payload = {"devices": {"iOS 17.5": [{"name": "iPhone 15", "udid": "ABC-123", "state": "Booted"}]}}
    fake = _FakeRunner([_result(stdout=json.dumps(payload))])
    monkeypatch.setattr(platform_mod, "run_command", fake)

    result = select_device(cfg)

    assert result.success
    assert result.details["device_id"] == "ABC-123"


def test_select_ios_device_boots_if_not_running(monkeypatch):
    cfg = _ios_config()
    payload = {"devices": {"iOS 17.5": [{"name": "iPhone 15", "udid": "ABC-123", "state": "Shutdown"}]}}
    fake = _FakeRunner([_result(stdout=json.dumps(payload)), _result(returncode=0)])
    monkeypatch.setattr(platform_mod, "run_command", fake)

    result = select_device(cfg)

    assert result.success
    assert result.details["device_id"] == "ABC-123"
    assert fake.calls[1][:3] == ["xcrun", "simctl", "boot"]


def test_select_ios_device_unknown_name(monkeypatch):
    cfg = _ios_config()
    payload = {"devices": {"iOS 17.5": [{"name": "iPad Pro", "udid": "XYZ", "state": "Shutdown"}]}}
    fake = _FakeRunner([_result(stdout=json.dumps(payload))])
    monkeypatch.setattr(platform_mod, "run_command", fake)

    result = select_device(cfg)

    assert not result.success
    assert "No simulator named" in result.error


def test_capture_logs_android(monkeypatch):
    cfg = _android_config()
    fake = _FakeRunner([_result(stdout="log line 1\nlog line 2\n")])
    monkeypatch.setattr(platform_mod, "run_command", fake)

    result = capture_logs(cfg, "emulator-5554")

    assert result.success
    assert "log line 1" in result.logs
    assert fake.calls[0][:3] == ["adb", "-s", "emulator-5554"]


def test_capture_logs_ios(monkeypatch):
    cfg = _ios_config()
    fake = _FakeRunner([_result(stdout="ios log line\n")])
    monkeypatch.setattr(platform_mod, "run_command", fake)

    result = capture_logs(cfg, "ABC-123")

    assert result.success
    assert fake.calls[0][:3] == ["xcrun", "simctl", "spawn"]


def test_build_app_success(monkeypatch, tmp_path):
    cfg = _android_config()
    cfg.clone_path = tmp_path
    fake = _FakeRunner([_result(stdout="BUILD SUCCESSFUL")])
    monkeypatch.setattr(platform_mod, "run_command", fake)

    result = build_app(cfg)

    assert result.success
    assert result.artifact_path == (tmp_path / cfg.build.artifact_path).resolve()
    assert fake.calls[0] == cfg.build.build_command.split()


def test_build_app_reports_failure(monkeypatch, tmp_path):
    cfg = _android_config()
    cfg.clone_path = tmp_path
    fake = _FakeRunner([_result(returncode=1, stderr="Task failed")])
    monkeypatch.setattr(platform_mod, "run_command", fake)

    result = build_app(cfg)

    assert not result.success
    assert "Task failed" in result.error
    assert result.artifact_path is None


def test_build_app_rejects_artifact_path_escaping_clone_path(monkeypatch, tmp_path):
    cfg = _android_config()
    cfg.clone_path = tmp_path
    cfg.build.artifact_path = "../outside.apk"

    def fail_if_called(argv, cwd, timeout=120.0, env=None):
        raise AssertionError("run_command must not be called when the artifact path escapes clone_path")

    monkeypatch.setattr(platform_mod, "run_command", fail_if_called)

    result = build_app(cfg)

    assert not result.success
    assert "outside the allowed directory" in result.error


def test_install_app_targets_specific_android_device(monkeypatch, tmp_path):
    cfg = _android_config()
    cfg.clone_path = tmp_path
    artifact = tmp_path / "app-debug.apk"
    artifact.write_text("fake apk")
    fake = _FakeRunner([_result(returncode=0)])
    monkeypatch.setattr(platform_mod, "run_command", fake)

    result = install_app(cfg, "emulator-5554", artifact)

    assert result.success
    assert fake.calls[0][:3] == ["adb", "-s", "emulator-5554"]


def test_install_app_targets_specific_ios_simulator(monkeypatch, tmp_path):
    cfg = _ios_config()
    cfg.clone_path = tmp_path
    artifact = tmp_path / "SampleIOSApp.app"
    artifact.write_text("fake bundle")
    fake = _FakeRunner([_result(returncode=0)])
    monkeypatch.setattr(platform_mod, "run_command", fake)

    result = install_app(cfg, "ABC-123", artifact)

    assert result.success
    assert "ABC-123" in fake.calls[0]
    assert "booted" not in fake.calls[0]


def test_install_app_rejects_artifact_outside_clone_path(tmp_path):
    cfg = _android_config()
    cfg.clone_path = tmp_path
    outside_artifact = tmp_path.parent / "outside.apk"

    result = install_app(cfg, "emulator-5554", outside_artifact)

    assert not result.success
    assert "outside the allowed directory" in result.error
