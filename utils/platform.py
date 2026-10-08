"""Android/iOS device, build, and install operations.

This is the "single dispatch point per concern" the project's platform
strategy calls for: `select_device`, `capture_logs`, `build_app`, and
`install_app` are each one function with one interface, used identically by
callers regardless of platform. All of the android-vs-ios branching lives
inside these four functions - node code and the rest of utils/ never
branches on `app_config.platform` itself.

Everything here goes through utils.runner.run_command (argv lists, no
shell). `build_command`/`install_command` come from the app's own
apps/<name>.yaml - operator-authored config, not untrusted input - so
running them is expected; what we still guard against is any *path*
derived from them (artifact_path, workspace, clone_path) ending up outside
the app's own clone_path.
"""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from config import AppConfig
from utils.paths import PathSecurityError, ensure_within
from utils.runner import RunnerError, run_command
from utils.secrets import redact


class PlatformError(Exception):
    """Raised for invalid configuration or an out-of-bounds path."""


@dataclass
class PlatformResult:
    """Structured outcome for every operation in this module."""

    success: bool
    logs: str = ""
    error: str | None = None
    artifact_path: Path | None = None
    details: dict[str, Any] = field(default_factory=dict)


def _result_from_command(result, *, artifact_path: Path | None = None, details: dict | None = None) -> PlatformResult:
    logs = redact(result.stdout + result.stderr)
    if result.ok:
        return PlatformResult(success=True, logs=logs, artifact_path=artifact_path, details=details or {})
    error = redact(result.stderr.strip() or f"Command exited with code {result.returncode}")
    return PlatformResult(success=False, logs=logs, error=error, details=details or {})


# --------------------------------------------------------------------------
# Device selection
# --------------------------------------------------------------------------

def select_device(app_config: AppConfig, timeout: float = 30.0) -> PlatformResult:
    """Find the device configured for this app (its AVD or simulator).

    Only *detects* an already-running match - it does not attempt to boot
    a full Android emulator (too slow/stateful for a bounded-timeout call).
    For iOS it will boot the configured simulator if it exists but isn't
    currently booted, since `xcrun simctl boot` is fast and synchronous.
    """
    try:
        if app_config.platform == "android":
            return _select_android_device(app_config, timeout)
        return _select_ios_device(app_config, timeout)
    except RunnerError as exc:
        return PlatformResult(success=False, error=str(exc))


def _select_android_device(app_config: AppConfig, timeout: float) -> PlatformResult:
    cwd = app_config.clone_path if app_config.clone_path.is_dir() else Path.cwd()
    devices_result = run_command(["adb", "devices"], cwd=cwd, timeout=timeout)
    if not devices_result.ok:
        return _result_from_command(devices_result)

    serials = []
    for line in devices_result.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            serials.append(parts[0])

    target_avd = app_config.device.avd_name
    for serial in serials:
        avd_result = run_command(["adb", "-s", serial, "emu", "avd", "name"], cwd=cwd, timeout=timeout)
        if not avd_result.ok:
            continue
        avd_name = avd_result.stdout.splitlines()[0].strip() if avd_result.stdout.strip() else ""
        if avd_name == target_avd:
            return PlatformResult(
                success=True,
                logs=redact(devices_result.stdout),
                details={"device_id": serial, "avd_name": avd_name},
            )

    return PlatformResult(
        success=False,
        logs=redact(devices_result.stdout),
        error=(
            f"Configured emulator '{target_avd}' is not currently running. "
            f"Boot it first (e.g. `emulator -avd {target_avd}`); this function "
            f"only detects/selects an already-running emulator."
        ),
    )


def _select_ios_device(app_config: AppConfig, timeout: float) -> PlatformResult:
    cwd = app_config.clone_path if app_config.clone_path.is_dir() else Path.cwd()
    list_result = run_command(["xcrun", "simctl", "list", "devices", "--json"], cwd=cwd, timeout=timeout)
    if not list_result.ok:
        return _result_from_command(list_result)

    try:
        devices_by_runtime = json.loads(list_result.stdout).get("devices", {})
    except json.JSONDecodeError as exc:
        return PlatformResult(success=False, error=f"Malformed simctl output: {exc}")

    target_name = app_config.device.simulator_name
    candidate = None
    for runtime, devices in devices_by_runtime.items():
        for device in devices:
            if device.get("name") == target_name:
                if device.get("state") == "Booted":
                    return PlatformResult(
                        success=True,
                        details={"device_id": device["udid"], "name": target_name, "runtime": runtime},
                    )
                candidate = device

    if candidate is None:
        return PlatformResult(
            success=False,
            error=f"No simulator named '{target_name}' exists. Create it first with `xcrun simctl create`.",
        )

    boot_result = run_command(["xcrun", "simctl", "boot", candidate["udid"]], cwd=cwd, timeout=timeout)
    if not boot_result.ok:
        return _result_from_command(boot_result)

    return PlatformResult(success=True, details={"device_id": candidate["udid"], "name": target_name})


# --------------------------------------------------------------------------
# Log capture
# --------------------------------------------------------------------------

def capture_logs(app_config: AppConfig, device_id: str, timeout: float = 60.0) -> PlatformResult:
    """Dump the current device log buffer for `device_id`."""
    cwd = app_config.clone_path if app_config.clone_path.is_dir() else Path.cwd()

    if app_config.platform == "android":
        argv = ["adb", "-s", device_id, "logcat", "-d"]
    else:
        argv = ["xcrun", "simctl", "spawn", device_id, "log", "show", "--style", "compact", "--last", "2m"]

    try:
        result = run_command(argv, cwd=cwd, timeout=timeout)
    except RunnerError as exc:
        return PlatformResult(success=False, error=str(exc))

    return _result_from_command(result)


# --------------------------------------------------------------------------
# Build and install
# --------------------------------------------------------------------------

def build_app(app_config: AppConfig, timeout: float = 1800.0) -> PlatformResult:
    """Run the app's configured build command, confined to its clone_path.

    `app_config.build.build_command` already encodes whatever Gradle
    module/variant or Xcode workspace/project/scheme/destination this app
    needs (set in apps/<name>.yaml) - this function does not hardcode or
    guess any of that, it just runs the command.
    """
    try:
        cwd = ensure_within(app_config.clone_path, app_config.clone_path)
    except PathSecurityError as exc:
        return PlatformResult(success=False, error=str(exc))

    try:
        artifact_path = ensure_within(app_config.clone_path / app_config.build.artifact_path, app_config.clone_path)
    except PathSecurityError as exc:
        return PlatformResult(success=False, error=str(exc))

    argv = shlex.split(app_config.build.build_command)
    try:
        result = run_command(argv, cwd=cwd, timeout=timeout)
    except RunnerError as exc:
        return PlatformResult(success=False, error=str(exc))

    details = {"artifact_exists": artifact_path.exists()}
    return _result_from_command(result, artifact_path=artifact_path if result.ok else None, details=details)


def install_app(app_config: AppConfig, device_id: str, artifact_path: Path, timeout: float = 300.0) -> PlatformResult:
    """Install a previously built artifact onto `device_id`.

    `artifact_path` must resolve inside `app_config.clone_path`.
    """
    try:
        artifact_path = ensure_within(Path(artifact_path), app_config.clone_path)
    except PathSecurityError as exc:
        return PlatformResult(success=False, error=str(exc))

    command = app_config.build.install_command.format(artifact_path=str(artifact_path))
    argv = shlex.split(command)

    if app_config.platform == "android":
        if argv and argv[0] == "adb" and "-s" not in argv:
            argv = [argv[0], "-s", device_id, *argv[1:]]
    else:
        argv = [device_id if token == "booted" else token for token in argv]

    try:
        result = run_command(argv, cwd=app_config.clone_path, timeout=timeout)
    except RunnerError as exc:
        return PlatformResult(success=False, error=str(exc))

    return _result_from_command(result, artifact_path=artifact_path if result.ok else None)
