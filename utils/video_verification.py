"""Hard-gate verifier: after-video frames must show the Jira issue resolved."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from utils.llm import ask_for_json
from utils.secrets import redact
from utils.video_frames import extract_keyframes


@dataclass
class VerifierFinding:
    passed: bool
    issue_resolved: bool
    comments: str
    confidence: float
    frame_paths: list[str] = field(default_factory=list)
    video_path: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def verify_after_video(
    jira_baseline: dict[str, Any],
    video_paths: list[str],
    *,
    frames_out: Path | None = None,
    cwd: Path | None = None,
    max_frames: int = 12,
    timeout: float = 120.0,
) -> VerifierFinding:
    """Extract frames from after-video and ask whether the original issue is resolved."""
    videos = [Path(p) for p in video_paths if Path(p).is_file()]
    if not videos:
        return VerifierFinding(
            passed=False,
            issue_resolved=False,
            comments="No after-video available for verification.",
            confidence=0.0,
            error="missing_after_video",
        )

    video = videos[0]
    out_dir = frames_out or (video.parent / "verifier_frames")
    extract = extract_keyframes(video, out_dir=out_dir, max_frames=max_frames)
    if not extract.success or not extract.frames:
        return VerifierFinding(
            passed=False,
            issue_resolved=False,
            comments=extract.error or "Could not extract frames from after-video.",
            confidence=0.0,
            video_path=str(video),
            error="frame_extract_failed",
        )

    issue_key = str(jira_baseline.get("issue_key") or "unknown")
    repro = jira_baseline.get("reproduction_steps") or []
    repro_text = "\n".join(f"- {s}" for s in repro) if repro else "(none)"
    prompt = (
        "You are a QA verifier. An automated fix was applied and Maestro re-ran the flow. "
        "These images are key frames from the AFTER screen recording.\n"
        f"Jira issue: {issue_key}\n"
        f"Summary: {redact(str(jira_baseline.get('summary') or ''))}\n"
        f"Expected: {redact(str(jira_baseline.get('expected_behavior') or ''))}\n"
        f"Original actual (bug): {redact(str(jira_baseline.get('actual_behavior') or ''))}\n"
        f"Reproduction steps:\n{redact(repro_text)}\n\n"
        "Decide if the AFTER video shows the issue is resolved.\n"
        "Respond with ONLY JSON: "
        '{"issue_resolved": true/false, "comments": "string", "confidence": 0.0-1.0}.'
    )
    result = ask_for_json(prompt, images=extract.frames[:max_frames], cwd=cwd, timeout=timeout)
    if not result.success or not isinstance(result.data, dict):
        return VerifierFinding(
            passed=False,
            issue_resolved=False,
            comments=result.error or "Verifier LLM failed.",
            confidence=0.0,
            frame_paths=[str(p) for p in extract.frames],
            video_path=str(video),
            error="verifier_llm_failed",
        )

    resolved = bool(result.data.get("issue_resolved"))
    comments = str(result.data.get("comments") or "")
    try:
        confidence = float(result.data.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0

    return VerifierFinding(
        passed=resolved,
        issue_resolved=resolved,
        comments=comments,
        confidence=confidence,
        frame_paths=[str(p) for p in extract.frames],
        video_path=str(video),
    )
