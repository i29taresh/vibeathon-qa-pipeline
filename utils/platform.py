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
import os
import shlex
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from config import AppConfig
from utils.paths import PathSecurityError, ensure_within
from utils.runner import RunnerError, run_command, start_background, stop_background
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
            result = _select_android_device(app_config, timeout)
            if result.success:
                device_id = str(result.details.get("device_id") or "")
                if device_id:
                    unlock_android_device(device_id, cwd=_android_cwd(app_config))
            return result
        return _select_ios_device(app_config, timeout)
    except RunnerError as exc:
        return PlatformResult(success=False, error=str(exc))


def _android_cwd(app_config: AppConfig) -> Path:
    return app_config.clone_path if app_config.clone_path.is_dir() else Path.cwd()


def unlock_android_device(device_id: str, cwd: Path | None = None, pin: str | None = None) -> None:
    """Best-effort wake + PIN unlock (POC default PIN is 1111).

    Only types the PIN when keyguard is actually showing — otherwise `input
    text` lands in whatever app is in the foreground (e.g. Chrome).

    Set ``EMULATOR_PIN`` to override. Failures are ignored.
    """
    pin = pin if pin is not None else os.environ.get("EMULATOR_PIN", "1111")
    work = cwd if cwd is not None else Path.cwd()
    try:
        run_command(["adb", "-s", device_id, "shell", "input", "keyevent", "224"], cwd=work, timeout=10.0)
        policy = run_command(
            ["adb", "-s", device_id, "shell", "dumpsys", "window", "policy"],
            cwd=work,
            timeout=15.0,
        )
        blob = (policy.stdout or "").lower()
        locked = (
            "iskeyguardshowing=true" in blob.replace(" ", "")
            or "iskeyguardlocked=true" in blob.replace(" ", "")
            or "mshowinglockscreen=true" in blob.replace(" ", "")
            or "minputrestricted=true" in blob.replace(" ", "")
        )
        if not locked:
            # Still dismiss an idle keyguard if present (no PIN typed).
            run_command(["adb", "-s", device_id, "shell", "wm", "dismiss-keyguard"], cwd=work, timeout=10.0)
            return
        # Swipe up to reveal PIN pad, then enter PIN.
        run_command(
            ["adb", "-s", device_id, "shell", "input", "swipe", "300", "1400", "300", "400", "300"],
            cwd=work,
            timeout=10.0,
        )
        if pin:
            run_command(["adb", "-s", device_id, "shell", "input", "text", pin], cwd=work, timeout=10.0)
            run_command(["adb", "-s", device_id, "shell", "input", "keyevent", "66"], cwd=work, timeout=10.0)
    except RunnerError:
        return


def _select_android_device(app_config: AppConfig, timeout: float) -> PlatformResult:
    cwd = _android_cwd(app_config)
    devices_result = run_command(["adb", "devices"], cwd=cwd, timeout=timeout)
    if not devices_result.ok:
        return _result_from_command(devices_result)

    serials = []
    for line in devices_result.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            serials.append(parts[0])

    if not serials:
        target_avd = app_config.device.avd_name
        return PlatformResult(
            success=False,
            logs=redact(devices_result.stdout),
            error=(
                f"No Android device/emulator is running (adb devices empty). "
                f"Boot one first, e.g. `emulator -avd {target_avd}` "
                f"(or any available AVD). This function only selects an already-running emulator."
            ),
        )

    target_avd = (app_config.device.avd_name or "").strip()
    target_norm = target_avd.lower().replace("-", "_")
    for serial in serials:
        avd_result = run_command(["adb", "-s", serial, "emu", "avd", "name"], cwd=cwd, timeout=timeout)
        if not avd_result.ok:
            continue
        avd_name = avd_result.stdout.splitlines()[0].strip() if avd_result.stdout.strip() else ""
        avd_norm = avd_name.lower().replace("-", "_")
        if avd_name == target_avd or (target_norm and target_norm in avd_norm):
            return PlatformResult(
                success=True,
                logs=redact(devices_result.stdout),
                details={"device_id": serial, "avd_name": avd_name},
            )

    # POC fallback: use the first booted emulator when the configured AVD name
    # does not match (Studio AVD ids often differ from apps/*.yaml).
    serial = serials[0]
    avd_result = run_command(["adb", "-s", serial, "emu", "avd", "name"], cwd=cwd, timeout=timeout)
    avd_name = ""
    if avd_result.ok and avd_result.stdout.strip():
        avd_name = avd_result.stdout.splitlines()[0].strip()
    return PlatformResult(
        success=True,
        logs=redact(devices_result.stdout),
        details={
            "device_id": serial,
            "avd_name": avd_name or "unknown",
            "avd_fallback": True,
            "configured_avd": target_avd,
        },
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

def capture_logs(
    app_config: AppConfig,
    device_id: str,
    timeout: float = 20.0,
    max_lines: int = 400,
) -> PlatformResult:
    """Dump a bounded slice of the device log buffer for `device_id`."""
    cwd = app_config.clone_path if app_config.clone_path.is_dir() else Path.cwd()

    if app_config.platform == "android":
        argv = ["adb", "-s", device_id, "logcat", "-d", "-t", str(max_lines)]
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


# --------------------------------------------------------------------------
# Screen recording (best-effort; a failure here never fails the QA run)
# --------------------------------------------------------------------------

@dataclass
class ScreenRecording:
    """A screen recording started on a device and not yet pulled locally."""

    process: Any
    dest: Path
    platform: str
    device_id: str
    cwd: Path
    remote_path: str | None = None


def start_screen_recording(app_config: AppConfig, device_id: str, dest: Path) -> ScreenRecording | None:
    """Begin a device screen recording into `dest`. Returns None if it cannot start."""
    cwd = app_config.clone_path if app_config.clone_path.is_dir() else Path.cwd()
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if app_config.platform == "android":
            remote = f"/sdcard/ox-{dest.stem}.mp4"
            process = start_background(
                ["adb", "-s", device_id, "shell", "screenrecord", "--time-limit", "180", remote],
                cwd,
            )
            return ScreenRecording(
                process=process, dest=dest, platform="android", device_id=device_id, cwd=cwd, remote_path=remote
            )
        process = start_background(
            ["xcrun", "simctl", "io", device_id, "recordVideo", "--codec", "h264", str(dest)],
            cwd,
        )
        return ScreenRecording(process=process, dest=dest, platform="ios", device_id=device_id, cwd=cwd)
    except (OSError, RunnerError):
        return None


def stop_screen_recording(recording: ScreenRecording | None) -> Path | None:
    """Stop a recording and return the local file when it has actual bytes.

    Android ``screenrecord`` only writes the MP4 moov atom after a clean
    SIGINT on the *remote* process. Killing the local ``adb`` session alone
    often leaves a truncated file (ffmpeg: ``moov atom not found``).
    """
    if recording is None:
        return None
    if recording.platform == "android" and recording.remote_path:
        try:
            # Prefer signaling screenrecord on-device so it finalizes the file.
            run_command(
                [
                    "adb",
                    "-s",
                    recording.device_id,
                    "shell",
                    "pkill",
                    "-SIGINT",
                    "screenrecord",
                ],
                cwd=recording.cwd,
                timeout=15.0,
            )
        except RunnerError:
            pass
        # Give the recorder a moment to flush the moov atom.
        time.sleep(2.0)
        try:
            recording.process.wait(timeout=5.0)
        except Exception:
            stop_background(recording.process, timeout=5.0)
        try:
            run_command(
                ["adb", "-s", recording.device_id, "pull", recording.remote_path, str(recording.dest)],
                cwd=recording.cwd,
                timeout=60.0,
            )
            run_command(
                ["adb", "-s", recording.device_id, "shell", "rm", "-f", recording.remote_path],
                cwd=recording.cwd,
                timeout=15.0,
            )
        except RunnerError:
            return None
    else:
        stop_background(recording.process)
    if recording.dest.is_file() and recording.dest.stat().st_size > 0:
        return recording.dest
    return None
