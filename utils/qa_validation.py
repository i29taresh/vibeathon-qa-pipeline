"""Shared QA validation logic: run one Maestro flow and judge pass/fail.

This is the single implementation of "run flow X, compare the result
against its acceptance criteria, decide pass/fail/visual/infrastructure" -
both nodes/qa_agent.py (the initial run) and nodes/retest_agent.py
(re-running the same flow, plus any regression flows, after a fix) call
`run_qa_check` instead of each re-implementing this pipeline. Neither node
duplicates flow validation, reference loading, JUnit parsing, or the LLM
visual-comparison step - they live here exactly once.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from config import AppConfig
from utils.llm import ask_for_json
from utils.maestro import MaestroResult, run_flow
from utils.paths import PathSecurityError, ensure_within
from utils.platform import select_device

# failure_type values: "none" | "functional" | "visual" | "infrastructure" | "unverified"
_CRITERIA_FILENAMES = (
    "acceptance_criteria.md",
    "acceptance_criteria.txt",
    "criteria.md",
    "criteria.txt",
)


@dataclass
class QAFinding:
    """The structured result of checking one flow."""

    flow_name: str
    passed: bool
    failure_type: str
    description: str
    expected_behavior: str
    actual_behavior: str
    confidence: float
    reproduction_steps: list[str] = field(default_factory=list)
    evidence_paths: list[str] = field(default_factory=list)


@dataclass
class StepResult:
    name: str
    passed: bool
    message: Optional[str] = None


@dataclass
class ReferenceMaterials:
    criteria_text: Optional[str]
    criteria_path: Optional[Path]
    expected_screenshots: list[Path]


def run_qa_check(app_config: AppConfig, flow_name: str) -> QAFinding:
    """Validate, select a device, run `flow_name`, and judge the result.

    This is the whole QA validation routine in one call: flow-exists check,
    acceptance-criteria loading, device selection, running the flow via
    Maestro, parsing the JUnit report, and (when criteria + screenshots are
    available) an LLM visual-regression check layered on top of the
    deterministic functional result. See `build_finding` for the pass/fail
    rules this enforces.
    """
    finding = validate_flow_exists(app_config, flow_name)

    references: Optional[ReferenceMaterials] = None
    if finding is None:
        references = load_reference_materials(app_config, flow_name)
        if not references.criteria_text:
            finding = unverified_finding(flow_name, app_config.reference_dir / Path(flow_name).stem)

    device_id: Optional[str] = None
    if finding is None:
        device_id, infra_finding = select_device_or_finding(app_config, flow_name)
        finding = infra_finding

    if finding is None:
        maestro_result = run_flow(app_config, flow_name, device_id)
        if maestro_result.exit_code is None:
            finding = QAFinding(
                flow_name=flow_name,
                passed=False,
                failure_type="infrastructure",
                description=f"Maestro could not be run: {maestro_result.error}",
                expected_behavior="N/A",
                actual_behavior="N/A",
                confidence=1.0,
                evidence_paths=[str(p) for p in maestro_result.screenshots],
            )
        else:
            step_results = parse_junit_report(maestro_result.report_path)
            functional_passed = maestro_result.success and not any(not s.passed for s in step_results)
            llm_data = analyze_with_llm(app_config, flow_name, references, maestro_result, functional_passed)
            finding = build_finding(flow_name, maestro_result, step_results, llm_data)

    return finding


def validate_flow_exists(app_config: AppConfig, flow_name: str) -> Optional[QAFinding]:
    try:
        flow_path = ensure_within(app_config.flows_dir / flow_name, app_config.flows_dir)
    except PathSecurityError as exc:
        return infrastructure_finding(flow_name, f"Flow path is invalid: {exc}")

    if not flow_path.is_file():
        return infrastructure_finding(flow_name, f"Flow file not found: {flow_path}")

    return None


def select_device_or_finding(app_config: AppConfig, flow_name: str) -> tuple[Optional[str], Optional[QAFinding]]:
    device_result = select_device(app_config)
    if not device_result.success:
        finding = infrastructure_finding(
            flow_name, f"Could not select a device for '{app_config.name}': {device_result.error}"
        )
        return None, finding
    return device_result.details.get("device_id"), None


def infrastructure_finding(flow_name: str, description: str) -> QAFinding:
    return QAFinding(
        flow_name=flow_name,
        passed=False,
        failure_type="infrastructure",
        description=description,
        expected_behavior="N/A - infrastructure failure, not an app behavior.",
        actual_behavior="N/A - infrastructure failure, not an app behavior.",
        confidence=1.0,
    )


def unverified_finding(flow_name: str, flow_reference_dir: Path) -> QAFinding:
    return QAFinding(
        flow_name=flow_name,
        passed=False,
        failure_type="unverified",
        description=(
            f"No acceptance criteria found under '{flow_reference_dir}'. "
            "Skipping verification rather than inventing expected behavior."
        ),
        expected_behavior="Unknown - no acceptance criteria provided.",
        actual_behavior="Not evaluated.",
        confidence=0.0,
    )


def load_reference_materials(app_config: AppConfig, flow_name: str) -> ReferenceMaterials:
    flow_stem = Path(flow_name).stem
    flow_reference_dir = app_config.reference_dir / flow_stem

    criteria_text: Optional[str] = None
    criteria_path: Optional[Path] = None
    for candidate_name in _CRITERIA_FILENAMES:
        candidate = flow_reference_dir / candidate_name
        try:
            resolved = ensure_within(candidate, app_config.reference_dir)
        except PathSecurityError:
            continue
        if resolved.is_file():
            criteria_path = resolved
            criteria_text = resolved.read_text().strip() or None
            break

    expected_screenshots: list[Path] = []
    screenshots_dir = flow_reference_dir / "screenshots"
    if screenshots_dir.is_dir():
        expected_screenshots = sorted(screenshots_dir.glob("*.png"))

    return ReferenceMaterials(
        criteria_text=criteria_text,
        criteria_path=criteria_path,
        expected_screenshots=expected_screenshots,
    )


def parse_junit_report(report_path: Optional[Path]) -> list[StepResult]:
    """Pull per-step pass/fail out of Maestro's JUnit report (functional ground truth)."""
    if not report_path or not report_path.is_file():
        return []

    try:
        tree = ET.parse(report_path)
    except ET.ParseError:
        return []

    steps: list[StepResult] = []
    for testcase in tree.getroot().iter("testcase"):
        name = testcase.get("name", "step")
        failure_node = testcase.find("failure")
        error_node = testcase.find("error")
        node = failure_node if failure_node is not None else error_node
        if node is not None:
            message = node.get("message") or (node.text or "").strip() or "Assertion failed"
            steps.append(StepResult(name=name, passed=False, message=message))
        else:
            steps.append(StepResult(name=name, passed=True))
    return steps


def analyze_with_llm(
    app_config: AppConfig,
    flow_name: str,
    references: ReferenceMaterials,
    maestro_result: MaestroResult,
    functional_passed: bool,
) -> Optional[dict[str, Any]]:
    """Ask the LLM to compare actual screenshots against acceptance criteria.

    This never decides functional pass/fail - it only ever contributes a
    `visual_mismatch` verdict plus descriptive text, layered on top of
    Maestro's own (deterministic) result.
    """
    if not references.criteria_text or not maestro_result.screenshots:
        return None

    prompt = (
        "You are a QA analyst comparing an app's actual behavior against written acceptance criteria.\n"
        f"App: {app_config.name} ({app_config.platform})\n"
        f"Flow: {flow_name}\n\n"
        f"Acceptance criteria:\n{references.criteria_text}\n\n"
        f"The automated test assertions {'passed' if functional_passed else 'FAILED'} for this run.\n"
        "Compare the attached actual screenshot(s) against the acceptance criteria (and any reference "
        "screenshots also attached) and look for a *visual* regression only - do not judge functional "
        "correctness from the screenshots alone. Respond with ONLY a JSON object with these keys: "
        '"visual_mismatch" (boolean), "description" (string), "expected_behavior" (string), '
        '"actual_behavior" (string), "confidence" (number from 0 to 1).'
    )

    images = [*references.expected_screenshots, *maestro_result.screenshots]
    result = ask_for_json(prompt, images=images)
    if not result.success or not isinstance(result.data, dict):
        return None
    return result.data


def build_finding(
    flow_name: str,
    maestro_result: MaestroResult,
    step_results: list[StepResult],
    llm_data: Optional[dict[str, Any]],
) -> QAFinding:
    functional_failures = [s for s in step_results if not s.passed]
    functional_passed = maestro_result.success and not functional_failures

    evidence_paths = [str(p) for p in maestro_result.screenshots]
    if maestro_result.report_path:
        evidence_paths.append(str(maestro_result.report_path))

    # Rule: a flow is never "passed" if Maestro's own assertions failed,
    # regardless of anything the LLM says.
    if not functional_passed:
        failure_messages = "; ".join(s.message or s.name for s in functional_failures)
        failure_messages = failure_messages or maestro_result.error or "Maestro reported a non-zero exit code."
        return QAFinding(
            flow_name=flow_name,
            passed=False,
            failure_type="functional",
            description=f"Maestro assertion(s) failed: {failure_messages}",
            reproduction_steps=[s.name for s in step_results] or ["Run the configured Maestro flow."],
            expected_behavior=(llm_data or {}).get(
                "expected_behavior", "Flow should complete with all assertions passing."
            ),
            actual_behavior=(llm_data or {}).get("actual_behavior", failure_messages),
            evidence_paths=evidence_paths,
            confidence=1.0,  # Maestro's exit code/assertions are ground truth, not inferred.
        )

    # Maestro passed functionally - a visual-only regression is still a distinct failure.
    if llm_data and llm_data.get("visual_mismatch"):
        return QAFinding(
            flow_name=flow_name,
            passed=False,
            failure_type="visual",
            description=llm_data.get("description", "Visual difference detected versus the reference screenshots."),
            reproduction_steps=[s.name for s in step_results],
            expected_behavior=llm_data.get("expected_behavior", "UI should match the reference screenshots."),
            actual_behavior=llm_data.get("actual_behavior", "UI differs from the reference screenshots."),
            evidence_paths=evidence_paths,
            confidence=float(llm_data.get("confidence", 0.5)),
        )

    return QAFinding(
        flow_name=flow_name,
        passed=True,
        failure_type="none",
        description="All Maestro assertions passed; no visual regression detected.",
        expected_behavior="Flow completes with all assertions passing.",
        actual_behavior="Flow completed with all assertions passing.",
        evidence_paths=evidence_paths,
        confidence=1.0,
    )
