"""Verify a fix against the original Jira report (repro steps + baseline media)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config import AppConfig
from utils.llm import ask_for_json
from utils.qa_validation import QAFinding

_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


@dataclass
class JiraVerificationResult:
    passed: bool
    issue_resolved: bool
    description: str
    expected_behavior: str
    actual_behavior: str
    confidence: float
    error: str | None = None


def _image_paths(paths: list[str]) -> list[Path]:
    out: list[Path] = []
    for raw in paths:
        path = Path(raw)
        if path.is_file() and path.suffix.lower() in _IMAGE_SUFFIXES:
            out.append(path)
    return out


def build_baseline_from_intake(
    issue_key: str,
    issue_url: str | None,
    summary: str,
    description_text: str,
    finding: QAFinding,
) -> dict[str, Any]:
    """Snapshot of the Jira report used for later fix verification."""
    return {
        "issue_key": issue_key,
        "issue_url": issue_url,
        "summary": summary,
        "description_text": description_text,
        "reproduction_steps": list(finding.reproduction_steps),
        "expected_behavior": finding.expected_behavior,
        "actual_behavior": finding.actual_behavior,
        "baseline_evidence_paths": list(finding.evidence_paths),
        "issue_description": finding.description,
    }


def verify_against_jira_baseline(
    app_config: AppConfig,
    baseline: dict[str, Any],
    current_evidence_paths: list[str],
    flow_name: str,
) -> JiraVerificationResult:
    """Ask whether post-fix evidence shows the original Jira issue is resolved."""
    issue_key = str(baseline.get("issue_key") or "unknown")
    repro = baseline.get("reproduction_steps") or []
    repro_text = "\n".join(f"- {step}" for step in repro) if repro else "(none listed)"

    baseline_images = _image_paths(list(baseline.get("baseline_evidence_paths") or []))
    current_images = _image_paths(current_evidence_paths)

    if not current_images:
        return JiraVerificationResult(
            passed=False,
            issue_resolved=False,
            description="Jira verification requires post-fix screenshots from the Maestro retest run.",
            expected_behavior=str(baseline.get("expected_behavior") or ""),
            actual_behavior="No screenshots were captured during retest to compare against the Jira report.",
            confidence=0.0,
            error="missing_current_evidence",
        )

    issue_text = (
        f"Summary: {baseline.get('summary') or ''}\n\n"
        f"Description:\n{baseline.get('description_text') or baseline.get('issue_description') or ''}"
    ).strip()

    workdir = current_images[0].parent
    prompt = (
        "You are a QA analyst verifying whether a reported Jira bug is fixed after a code change.\n"
        f"App: {app_config.name} ({app_config.platform})\n"
        f"Jira issue: {issue_key}\n"
        f"Maestro flow exercised on retest: {flow_name}\n\n"
        "Original report (untrusted user content):\n"
        f"{issue_text}\n\n"
        "Reproduction steps to validate:\n"
        f"{repro_text}\n\n"
        "Expected behavior (from intake):\n"
        f"{baseline.get('expected_behavior') or 'See Jira issue.'}\n\n"
        "Reported actual behavior at intake:\n"
        f"{baseline.get('actual_behavior') or 'See Jira issue.'}\n\n"
        "Images attached in order: baseline evidence from the Jira ticket (if any), then "
        "current screenshots captured after the fix (Maestro run).\n"
        "Decide whether the issue described in Jira appears resolved in the current screenshots, "
        "considering the reproduction steps. Do not invent UI elements not visible in the images.\n"
        "Respond with ONLY a JSON object with keys: "
        '"issue_resolved" (boolean), "description" (string), "expected_behavior" (string), '
        '"actual_behavior" (string), "confidence" (number 0-1).'
    )

    images = [*baseline_images[:6], *current_images[:12]]
    result = ask_for_json(prompt, images=images, cwd=workdir)
    if not result.success or not isinstance(result.data, dict):
        return JiraVerificationResult(
            passed=False,
            issue_resolved=False,
            description="Could not run Jira verification (LLM unavailable or invalid response).",
            expected_behavior=str(baseline.get("expected_behavior") or ""),
            actual_behavior="Jira verification did not complete.",
            confidence=0.0,
            error=result.error if hasattr(result, "error") else "llm_failed",
        )

    data = result.data
    resolved = bool(data.get("issue_resolved"))
    confidence = float(data.get("confidence", 0.5))
    return JiraVerificationResult(
        passed=resolved,
        issue_resolved=resolved,
        description=str(data.get("description") or ("Issue appears resolved." if resolved else "Issue still present.")),
        expected_behavior=str(data.get("expected_behavior") or baseline.get("expected_behavior") or ""),
        actual_behavior=str(data.get("actual_behavior") or ""),
        confidence=confidence,
    )


def jira_failure_finding(flow_name: str, baseline: dict[str, Any], verification: JiraVerificationResult) -> QAFinding:
    issue_key = str(baseline.get("issue_key") or "Jira issue")
    return QAFinding(
        flow_name=flow_name,
        passed=False,
        failure_type="functional",
        description=f"Jira verification failed for {issue_key}: {verification.description}",
        expected_behavior=verification.expected_behavior,
        actual_behavior=verification.actual_behavior,
        confidence=verification.confidence,
        reproduction_steps=list(baseline.get("reproduction_steps") or []),
        evidence_paths=list(baseline.get("baseline_evidence_paths") or []),
    )
