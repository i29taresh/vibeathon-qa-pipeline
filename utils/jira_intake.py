"""Build a QA finding from an existing Jira issue (attachments + video frames)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path

from config import AppConfig
from utils import jira as jira_api
from utils.llm import ask_for_json
from utils.jira_verification import build_baseline_from_intake
from utils.qa_validation import QAFinding
from utils.video_frames import extract_keyframes, is_video_path

_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


@dataclass
class JiraIntakeResult:
    success: bool
    finding: QAFinding | None = None
    issue_key: str | None = None
    issue_url: str | None = None
    intake_dir: Path | None = None
    downloaded_files: list[Path] = field(default_factory=list)
    jira_baseline: dict | None = None
    error: str | None = None


def _is_image_path(path: Path) -> bool:
    return path.suffix.lower() in _IMAGE_SUFFIXES


def intake_dir_for_run(app_config: AppConfig, issue_key: str, runs_root: Path | None = None) -> Path:
    root = runs_root or (app_config.flows_dir.parent / "runs")
    return root / app_config.name / f"jira-{issue_key}-{uuid.uuid4().hex[:8]}"


def intake_from_jira(
    app_config: AppConfig,
    issue_key: str,
    flow_name: str,
    runs_root: Path | None = None,
    use_llm: bool = True,
) -> JiraIntakeResult:
    print(f"[jira_intake] fetching {issue_key}...", flush=True)
    details, err = jira_api.get_issue_details(issue_key)
    if err or details is None:
        return JiraIntakeResult(success=False, error=err or "Failed to load Jira issue")
    print(
        f"[jira_intake] loaded {details.key}: {len(details.attachments)} attachment(s)",
        flush=True,
    )

    intake_dir = intake_dir_for_run(app_config, details.key, runs_root=runs_root)
    attachments_dir = intake_dir / "attachments"
    frames_dir = intake_dir / "frames"
    attachments_dir.mkdir(parents=True, exist_ok=True)

    evidence_paths: list[str] = []
    downloaded: list[Path] = []
    intake_errors: list[str] = []

    for meta in details.attachments:
        safe_name = Path(meta.filename).name
        dest = attachments_dir / safe_name
        print(f"[jira_intake] downloading {safe_name} ({meta.size} bytes)...", flush=True)
        ok, dl_err = jira_api.download_attachment(meta.content_url, dest)
        if not ok:
            intake_errors.append(f"Could not download {safe_name}: {dl_err}")
            continue
        downloaded.append(dest)
        if _is_image_path(dest):
            evidence_paths.append(str(dest))
        elif is_video_path(dest):
            frame_result = extract_keyframes(dest, out_dir=frames_dir / dest.stem)
            if frame_result.success:
                for frame in frame_result.frames:
                    evidence_paths.append(str(frame))
            else:
                intake_errors.append(f"Video {safe_name}: {frame_result.error}")

    base_description = details.summary.strip()
    if details.description_text:
        base_description = f"{base_description}\n\n{details.description_text}".strip()

    expected = "Behavior described in the Jira issue should be correct."
    actual = "Reporter indicates incorrect behavior (see Jira issue)."
    repro_steps = ["Reproduce using the steps in the linked Jira issue."]
    confidence = 0.6

    image_paths = [Path(p) for p in evidence_paths if _is_image_path(Path(p))]
    if use_llm and (base_description or image_paths):
        llm_data = _summarize_issue_with_llm(app_config, details.key, base_description, image_paths, intake_dir)
        if llm_data:
            expected = str(llm_data.get("expected_behavior") or expected)
            actual = str(llm_data.get("actual_behavior") or actual)
            repro_raw = llm_data.get("reproduction_steps")
            if isinstance(repro_raw, list) and repro_raw:
                repro_steps = [str(s) for s in repro_raw]
            elif isinstance(repro_raw, str) and repro_raw.strip():
                repro_steps = [repro_raw.strip()]
            confidence = float(llm_data.get("confidence", confidence))
            if llm_data.get("description"):
                base_description = str(llm_data["description"])

    if intake_errors:
        base_description += "\n\n(Intake warnings: " + "; ".join(intake_errors) + ")"

    finding = QAFinding(
        flow_name=flow_name,
        passed=False,
        failure_type="functional",
        description=base_description or f"Defect reported in Jira issue {details.key}.",
        expected_behavior=expected,
        actual_behavior=actual,
        confidence=confidence,
        reproduction_steps=repro_steps,
        evidence_paths=evidence_paths,
        video_paths=[str(path) for path in downloaded if is_video_path(path)],
    )

    baseline = build_baseline_from_intake(
        details.key,
        details.url,
        details.summary,
        details.description_text,
        finding,
    )
    baseline["baseline_video_paths"] = list(finding.video_paths)

    return JiraIntakeResult(
        success=True,
        finding=finding,
        issue_key=details.key,
        issue_url=details.url,
        intake_dir=intake_dir,
        downloaded_files=downloaded,
        jira_baseline=baseline,
    )


def _summarize_issue_with_llm(
    app_config: AppConfig,
    issue_key: str,
    issue_text: str,
    images: list[Path],
    workdir: Path,
) -> dict | None:
    prompt = (
        "You are a QA analyst. A bug was reported in Jira and you are preparing structured fields "
        "for an automated fix pipeline.\n"
        f"App: {app_config.name} ({app_config.platform})\n"
        f"Jira issue: {issue_key}\n\n"
        "Issue text (untrusted user content):\n"
        f"{issue_text}\n\n"
        "Attached images may include screenshots or video key frames from the report.\n"
        "Respond with ONLY a JSON object with keys: "
        '"description" (string summary), "expected_behavior" (string), "actual_behavior" (string), '
        '"reproduction_steps" (array of strings), "confidence" (number 0-1).'
    )
    print(f"[jira_intake] summarizing issue with Cursor (timeout 45s, {len(images[:6])} image(s))...", flush=True)
    result = ask_for_json(prompt, images=images[:6], cwd=workdir, timeout=45.0)
    if not result.success or not isinstance(result.data, dict):
        print(f"[jira_intake] summary skipped: {result.error or 'non-object response'}", flush=True)
        return None
    print("[jira_intake] summary ready", flush=True)
    return result.data
