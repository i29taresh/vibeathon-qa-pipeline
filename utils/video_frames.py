"""Extract key frames from videos using the analyze-video-frames skill script."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from utils.runner import RunnerError, run_command

_VIDEO_SUFFIXES = {".mp4", ".mov", ".webm", ".mkv", ".m4v"}
_DEFAULT_SKILL_SCRIPT = Path.home() / ".cursor/skills/analyze-video-frames/scripts/extract_keyframes.sh"
_FRAME_PATH_RE = re.compile(r"^(/[^\s]+\.png)\s*$", re.MULTILINE)


@dataclass
class FrameExtractResult:
    success: bool
    frames: list[Path]
    output_dir: Path | None = None
    error: str | None = None


def is_video_path(path: Path) -> bool:
    return path.suffix.lower() in _VIDEO_SUFFIXES


def skill_script_path() -> Path:
    override = os.environ.get("VIDEO_FRAMES_SCRIPT")
    if override:
        return Path(override).expanduser()
    return _DEFAULT_SKILL_SCRIPT


def extract_keyframes(
    video_path: Path,
    out_dir: Path | None = None,
    max_frames: int = 12,
    interval_seconds: float = 1.0,
    timeout: float = 120.0,
) -> FrameExtractResult:
    """Run the Cursor skill extract_keyframes.sh and return sorted PNG paths."""
    video = Path(video_path).resolve()
    if not video.is_file():
        return FrameExtractResult(success=False, error=f"Video not found: {video}")

    script = skill_script_path()
    if not script.is_file():
        return FrameExtractResult(
            success=False,
            error=f"Frame extraction script not found: {script} (install analyze-video-frames skill or set VIDEO_FRAMES_SCRIPT)",
        )

    argv = [
        "bash",
        str(script),
        str(video),
        "--max-frames",
        str(max_frames),
        "--interval",
        str(interval_seconds),
    ]
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        argv.extend(["--out", str(out_dir.resolve())])

    try:
        result = run_command(argv, cwd=video.parent, timeout=timeout)
    except RunnerError as exc:
        return FrameExtractResult(success=False, error=str(exc))

    if not result.ok:
        message = (result.stderr or result.stdout or "").strip() or "ffmpeg frame extraction failed"
        return FrameExtractResult(success=False, error=message)

    frames = _parse_frame_paths_from_script_output(result.stdout, out_dir)
    if not frames and out_dir and out_dir.is_dir():
        frames = sorted(out_dir.glob("frame_*.png"))

    if not frames:
        return FrameExtractResult(success=False, error="No frames extracted from video")

    resolved_out = frames[0].parent
    return FrameExtractResult(success=True, frames=frames, output_dir=resolved_out)


def _parse_frame_paths_from_script_output(stdout: str, out_dir: Path | None) -> list[Path]:
    paths: list[Path] = []
    in_frames_section = False
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped == "frames:":
            in_frames_section = True
            continue
        if in_frames_section and stripped.endswith(".png"):
            path = Path(stripped)
            if path.is_file():
                paths.append(path)
    if not paths:
        for match in _FRAME_PATH_RE.finditer(stdout):
            path = Path(match.group(1))
            if path.is_file():
                paths.append(path)
    if paths:
        return sorted(paths)
    if out_dir and out_dir.is_dir():
        return sorted(out_dir.glob("frame_*.png")) or sorted(out_dir.glob("*.png"))
    return []
