"""Run a single Maestro flow and capture its artifacts.

Each call creates its own unique run directory under `runs_root` so
parallel/repeated runs never clobber each other's JUnit report or
screenshots. The flow file itself must live inside the app's configured
`flows_dir` - flow names are treated as untrusted-ish input (they end up in
a filesystem path), so they're resolved and checked before use.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from config import AppConfig
from utils.paths import PathSecurityError, ensure_within
from utils.runner import RunnerError, run_command
from utils.secrets import redact


class MaestroError(Exception):
    """Raised when the flow file can't be found/validated before running."""


@dataclass
class MaestroResult:
    success: bool
    exit_code: int | None = None
    run_dir: Path | None = None
    report_path: Path | None = None
    screenshots: list[Path] = field(default_factory=list)
    videos: list[Path] = field(default_factory=list)
    logs: str = ""
    error: str | None = None


_VIDEO_SUFFIXES = {".mp4", ".mov", ".webm", ".mkv", ".m4v"}


def _resolve_flow_path(
    app_config: AppConfig,
    flow_name: str,
    flow_path: Path | str | None = None,
) -> Path:
    """Resolve a flow under flows_dir, or an absolute path under an allowed root.

    Absolute / generated flows (e.g. ``local/runs/<TICKET>/repro.yaml``) must
    stay inside the orchestrator repo's ``local/`` tree or the app's ``flows_dir``.
    """
    if flow_path is not None:
        candidate = Path(flow_path).resolve()
        orchestrator_root = Path(__file__).resolve().parent.parent
        allowed_roots = [
            app_config.flows_dir.resolve(),
            (orchestrator_root / "local").resolve(),
        ]
        last_err: Exception | None = None
        for root in allowed_roots:
            try:
                return ensure_within(candidate, root)
            except PathSecurityError as exc:
                last_err = exc
                continue
        raise PathSecurityError(str(last_err) if last_err else f"Flow path not allowed: {candidate}")
    return ensure_within(app_config.flows_dir / flow_name, app_config.flows_dir)


def run_flow(
    app_config: AppConfig,
    flow_name: str,
    device_id: str,
    runs_root: Path | None = None,
    timeout: float = 600.0,
    record_screen: bool = False,
    flow_path: Path | str | None = None,
) -> MaestroResult:
    """Execute a Maestro flow on `device_id`.

    By default runs ``flows_dir/<flow_name>``. When ``flow_path`` is set (POC
    generated repro YAML), that absolute path is used instead if it stays
    inside ``flows_dir`` or the orchestrator ``local/`` directory.
    """
    try:
        flow_path = _resolve_flow_path(app_config, flow_name, flow_path=flow_path)
    except PathSecurityError as exc:
        return MaestroResult(success=False, error=str(exc))

    if not flow_path.is_file():
        return MaestroResult(success=False, error=f"Flow file not found: {flow_path}")

    run_dir = _make_run_dir(app_config, flow_path, runs_root)
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return MaestroResult(success=False, error=f"Could not create run directory {run_dir}: {exc}")

    report_path = run_dir / "report.xml"
    argv = [
        "maestro",
        "--device", device_id,
        "test", str(flow_path),
        "--format", "junit",
        "--output", str(report_path),
        "--debug-output", str(run_dir),
    ]

    should_record = record_screen or os.environ.get("QA_RECORD_SCREEN") == "1"
    recording = _start_recording(app_config, device_id, run_dir) if should_record else None
    command_error: str | None = None
    try:
        result = run_command(argv, cwd=app_config.clone_path, timeout=timeout)
    except RunnerError as exc:
        result = None
        command_error = str(exc)
    _stop_recording(recording)

    screenshots = sorted(run_dir.glob("*.png"))
    videos = _collect_videos(run_dir)
    if command_error is not None or result is None:
        return MaestroResult(success=False, run_dir=run_dir, screenshots=screenshots, videos=videos, error=command_error)

    logs = redact(result.stdout + result.stderr)

    if result.timed_out:
        return MaestroResult(
            success=False,
            exit_code=result.returncode,
            run_dir=run_dir,
            screenshots=screenshots,
            videos=videos,
            logs=logs,
            error=f"Maestro flow timed out after {timeout}s",
        )

    return MaestroResult(
        success=result.ok,
        exit_code=result.returncode,
        run_dir=run_dir,
        report_path=report_path if report_path.is_file() else None,
        screenshots=screenshots,
        videos=videos,
        logs=logs,
        error=None if result.ok else redact(result.stderr.strip() or f"maestro exited with {result.returncode}"),
    )


def _collect_videos(run_dir: Path) -> list[Path]:
    if not run_dir.is_dir():
        return []
    return sorted(path for path in run_dir.rglob("*") if path.is_file() and path.suffix.lower() in _VIDEO_SUFFIXES)


def _start_recording(app_config: AppConfig, device_id: str, run_dir: Path):
    from utils.platform import start_screen_recording

    return start_screen_recording(app_config, device_id, run_dir / "screenrecord.mp4")


def _stop_recording(recording) -> None:
    if recording is None:
        return
    from utils.platform import stop_screen_recording

    stop_screen_recording(recording)


def _make_run_dir(app_config: AppConfig, flow_path: Path, runs_root: Path | None) -> Path:
    root = runs_root or (app_config.flows_dir.parent / "runs")
    unique = f"{flow_path.stem}-{uuid.uuid4().hex[:8]}"
    return root / app_config.name / unique
