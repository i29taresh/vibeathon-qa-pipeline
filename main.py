"""CLI entry point: loads a validated app config and runs the QA pipeline.

Supports two explicit execution modes (`--mode mock|real`, default mock):
- mock: every node reads its outcome from mock_scenarios.py (Phase 3).
- real: every node invokes its actual QA/RCA/Ticket/Dev/Retest/PR
  implementation (Phase 4). See CLAUDE.md for the full pipeline design.

`--preflight` runs a read-only diagnostic (config, auth, dependencies,
device availability, test command, reference assets) instead of the
pipeline - see `_run_preflight` below. It never builds, installs, edits, or
touches a real application.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from config import AppConfig, ConfigError, load_app_config
from graph import PipelineSafetyError, ensure_source_isolated, run_pipeline
from mock_scenarios import SCENARIOS
from state import VALID_MODES, initial_state


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the QA pipeline for a configured app.")
    parser.add_argument("--app", required=True, help="App name, e.g. 'sample_android'")
    parser.add_argument(
        "--mode",
        choices=VALID_MODES,
        default="mock",
        help="Execution mode: 'mock' (default, Phase 3 scenarios) or 'real' (Phase 4 agents).",
    )
    parser.add_argument(
        "--mock-scenario",
        choices=sorted(SCENARIOS),
        default="fix_success",
        help="Deterministic mock outcome to simulate - only used with --mode mock (default: fix_success).",
    )
    parser.add_argument(
        "--flow",
        default="default_flow",
        help="Flow filename to run - only used with --mode real (default: default_flow).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Real mode only: render ticket/PR content without ever calling gh or git push.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="Optional wall-clock budget in seconds for the whole pipeline run.",
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=3,
        help="Retry budget for the dev/retest loop (default: 3).",
    )
    parser.add_argument(
        "--preflight",
        action="store_true",
        help="Run a read-only real-mode diagnostic (config/auth/dependencies/device/tests/references) and exit.",
    )
    args = parser.parse_args(argv)

    try:
        app_config = load_app_config(args.app)
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 1

    if args.preflight:
        return _run_preflight(app_config)

    if args.mode == "real":
        try:
            ensure_source_isolated(app_config.clone_path)
        except PipelineSafetyError as exc:
            print(f"Safety error: {exc}", file=sys.stderr)
            return 1

    state = initial_state(
        app_name=args.app,
        app_config=app_config,
        mode=args.mode,
        mock_scenario=args.mock_scenario,
        flow_name=args.flow,
        max_attempts=args.max_attempts,
        dry_run=args.dry_run,
        timeout_seconds=args.timeout,
    )

    banner = f"=== Running pipeline for '{args.app}' ({app_config.platform}) | mode={args.mode}"
    banner += f" | scenario={args.mock_scenario} ===" if args.mode == "mock" else f" | flow={args.flow} ==="
    print(banner)
    if args.dry_run:
        print("(dry run: no GitHub issue/PR will be published, no branch will be pushed)")

    final_state = run_pipeline(state, timeout=args.timeout)

    _print_summary(final_state)
    return 0


def _print_summary(final_state: dict) -> None:
    print("\n=== Final result ===")
    print(f"status:        {final_state.get('status')}")
    print(f"passed:        {final_state.get('passed')}")
    print(f"ticket_id:     {final_state.get('ticket_id')}")
    if final_state.get("ticket_url"):
        print(f"ticket_url:    {final_state.get('ticket_url')}")
    if final_state.get("pr_url"):
        print(f"pr_url:        {final_state.get('pr_url')}")
    print(f"attempt_count: {final_state.get('attempt_count')}")

    qa_finding = final_state.get("qa_finding")
    if qa_finding:
        print(f"failure_type:  {qa_finding.get('failure_type')}")

    if final_state.get("last_error"):
        print(f"last_error:    {final_state['last_error']}")

    print("\n=== Execution history ===")
    for i, record in enumerate(final_state.get("execution_history", []), start=1):
        print(
            f"{i}. {record['node']:<13} status={record['status']:<20} "
            f"attempt={record['attempt_count']}  {record['detail']}"
        )


# --------------------------------------------------------------------------
# Preflight (real mode only; never builds, installs, edits, or pushes)
# --------------------------------------------------------------------------

_REQUIRED_BINARIES = ("gh", "claude", "maestro")
_CRITERIA_FILENAMES = ("acceptance_criteria.md", "acceptance_criteria.txt", "criteria.md", "criteria.txt")


def _run_preflight(app_config: AppConfig) -> int:
    from utils.github import check_auth
    from utils.platform import select_device

    print(f"=== Preflight check for '{app_config.name}' ({app_config.platform}) ===\n")

    checks: list[dict] = []

    try:
        ensure_source_isolated(app_config.clone_path)
        checks.append(_check("Source isolation", True, True, f"clone_path is outside this orchestrator's repo"))
    except PipelineSafetyError as exc:
        checks.append(_check("Source isolation", True, False, str(exc)))

    platform_binary = "adb" if app_config.platform == "android" else "xcrun"
    for binary in (*_REQUIRED_BINARIES, platform_binary):
        found = shutil.which(binary) is not None
        checks.append(_check(f"Dependency: {binary}", True, found, "found on PATH" if found else "NOT FOUND on PATH"))

    cwd = app_config.clone_path if app_config.clone_path.is_dir() else Path.cwd()
    auth = check_auth(app_config.repo, cwd)
    checks.append(_check("GitHub authentication", True, auth.authenticated, auth.error or "authenticated"))
    if auth.authenticated:
        write_ok = auth.can_write is not False
        detail = auth.detail or ("permission undetermined" if auth.can_write is None else "")
        checks.append(_check(f"GitHub write access to '{app_config.repo}'", True, write_ok, detail))

    device_result = select_device(app_config)
    detail = device_result.error if not device_result.success else f"device_id={device_result.details.get('device_id')}"
    checks.append(_check("Device availability", False, device_result.success, detail or ""))

    test_command = getattr(app_config.build, "test_command", None)
    checks.append(
        _check(
            "Automated test command",
            False,
            bool(test_command),
            test_command or "not configured - dev_agent will report tests as skipped",
        )
    )

    checks.extend(_check_reference_assets(app_config))

    _print_preflight_report(checks)
    return 0 if all(c["passed"] for c in checks if c["critical"]) else 1


def _check(name: str, critical: bool, passed: bool, detail: str) -> dict:
    return {"name": name, "critical": critical, "passed": passed, "detail": detail}


def _check_reference_assets(app_config: AppConfig) -> list[dict]:
    if not app_config.flows_dir.is_dir():
        return [_check("Reference assets", False, False, f"flows_dir does not exist: {app_config.flows_dir}")]

    flow_files = sorted(p.name for p in app_config.flows_dir.glob("*.yaml"))
    flow_files += sorted(name for name in app_config.regression_flows if name not in flow_files)
    if not flow_files:
        return [_check("Reference assets", False, False, f"No flow files found under {app_config.flows_dir}")]

    results = []
    for flow_file in flow_files:
        flow_stem = Path(flow_file).stem
        ref_dir = app_config.reference_dir / flow_stem
        has_criteria = any((ref_dir / name).is_file() for name in _CRITERIA_FILENAMES)
        detail = "acceptance criteria found" if has_criteria else f"no acceptance criteria under {ref_dir}"
        results.append(_check(f"Reference assets: {flow_file}", False, has_criteria, detail))
    return results


def _print_preflight_report(checks: list[dict]) -> None:
    for check in checks:
        marker = "PASS" if check["passed"] else ("FAIL" if check["critical"] else "WARN")
        print(f"[{marker}] {check['name']}: {check['detail']}")

    critical_failures = [c for c in checks if c["critical"] and not c["passed"]]
    warnings = [c for c in checks if not c["critical"] and not c["passed"]]

    print()
    if critical_failures:
        print(f"{len(critical_failures)} critical check(s) failed - real mode is not ready to run.")
    elif warnings:
        print(f"All critical checks passed; {len(warnings)} advisory warning(s) - review before running real mode.")
    else:
        print("All checks passed - real mode is ready to run.")


if __name__ == "__main__":
    raise SystemExit(main())
