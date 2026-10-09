"""CLI entry point: loads a validated app config and runs the QA pipeline.

Supports two explicit execution modes (`--mode mock|real`, default mock):
- mock: every node reads its outcome from mock_scenarios.py (Phase 3).
- real: every node invokes its actual QA/RCA/Ticket/Dev/Retest/PR
  implementation (Phase 4). See CLAUDE.md for the full pipeline design.

`--preflight` runs a read-only diagnostic (config, auth, dependencies,
device availability, test command, reference assets) instead of the
pipeline - see `_run_preflight` below. It never builds, installs, edits, or
touches a real application.

`--bootstrap` clones missing repos, warms each configured build, writes a
repo map and ready marker under `local/bootstrap/`. Real mode refuses to
start without that marker unless `--force` is set. `--probe-search` times
two semSearch-only agent runs and writes nothing.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

from config import AppConfig, ConfigError, load_app_config, load_project_graph
from graph import PipelineSafetyError, ensure_all_sources_isolated, ensure_source_isolated, run_pipeline
from mock_scenarios import SCENARIOS
from state import VALID_MODES, create_pipeline_state


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
        "--jira-issue",
        default=None,
        metavar="KEY",
        help="Real mode: start from an existing Jira issue (downloads attachments/video frames) instead of Maestro QA.",
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
    parser.add_argument(
        "--bootstrap",
        action="store_true",
        help="Clone missing repos, warm builds, write repo maps and a ready marker, then exit.",
    )
    parser.add_argument(
        "--skip-build",
        action="store_true",
        help="With --bootstrap: skip the build warm-up step.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Real mode: allow a run even when the bootstrap ready marker is missing.",
    )
    parser.add_argument(
        "--probe-search",
        action="store_true",
        help="Time two semSearch-only Cursor agent runs against the primary checkout and exit.",
    )
    args = parser.parse_args(argv)

    try:
        app_config = load_app_config(args.app)
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 1

    if args.preflight:
        return _run_preflight(app_config, app_stem=args.app)

    if args.bootstrap:
        from utils.bootstrap import bootstrap_project

        result = bootstrap_project(args.app, skip_build=args.skip_build)
        if not result.success:
            print(f"Bootstrap failed: {result.error}", file=sys.stderr)
            return 1
        print(f"Bootstrap complete. Marker: {result.marker_path}")
        return 0

    if args.probe_search:
        return _run_probe_search(app_config, app_stem=args.app)

    if args.jira_issue and args.mode != "real":
        print("Config error: --jira-issue requires --mode real.", file=sys.stderr)
        return 1

    project_graph = None
    if args.mode == "real":
        try:
            project_graph = load_project_graph(args.app)
            ensure_all_sources_isolated([c.clone_path for c in project_graph.values()])
        except (ConfigError, PipelineSafetyError) as exc:
            print(f"Safety error: {exc}", file=sys.stderr)
            return 1

    state = create_pipeline_state(
        app_name=args.app,
        app_config=app_config,
        mode=args.mode,
        mock_scenario=args.mock_scenario,
        flow_name=args.flow,
        jira_issue_raw=args.jira_issue,
        max_attempts=args.max_attempts,
        dry_run=args.dry_run,
        timeout_seconds=args.timeout,
        project_graph=project_graph,
        force_unbootstrapped=args.force,
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

_REQUIRED_BINARIES = ("gh", "maestro")
_CRITERIA_FILENAMES = ("acceptance_criteria.md", "acceptance_criteria.txt", "criteria.md", "criteria.txt")


def _run_preflight(app_config: AppConfig, app_stem: str | None = None) -> int:
    from config import uses_gitlab_integration, uses_jira_integration
    from utils import gitlab as gitlab_api
    from utils import jira as jira_api
    from utils.github import check_auth
    from utils.platform import select_device

    print(f"=== Preflight check for '{app_config.name}' ({app_config.platform}) ===\n")

    checks: list[dict] = []

    project_graph = None
    if app_stem:
        try:
            project_graph = load_project_graph(app_stem)
        except ConfigError as exc:
            checks.append(_check("Dependency graph", True, False, str(exc)))

    clone_paths = [c.clone_path for c in project_graph.values()] if project_graph else [app_config.clone_path]
    isolation_ok = True
    isolation_detail = []
    for path in clone_paths:
        try:
            ensure_source_isolated(path)
            isolation_detail.append(f"{path}: ok")
        except PipelineSafetyError as exc:
            isolation_ok = False
            isolation_detail.append(str(exc))
    checks.append(
        _check(
            "Source isolation",
            True,
            isolation_ok,
            "; ".join(isolation_detail) if isolation_detail else "clone paths outside orchestrator",
        )
    )

    platform_binary = "adb" if app_config.platform == "android" else "xcrun"
    for binary in (*_REQUIRED_BINARIES, platform_binary):
        found = shutil.which(binary) is not None
        checks.append(_check(f"Dependency: {binary}", True, found, "found on PATH" if found else "NOT FOUND on PATH"))

    from utils.cursor_agent import cursor_api_key

    key_set = bool(cursor_api_key())
    checks.append(
        _check(
            "CURSOR_API_KEY",
            True,
            key_set,
            "set in environment" if key_set else "NOT SET — required for RCA/QA LLM and dev_agent",
        )
    )
    try:
        import cursor_sdk  # noqa: F401

        checks.append(_check("cursor-sdk package", True, True, "installed"))
    except ImportError:
        checks.append(_check("cursor-sdk package", True, False, "run: pip install cursor-sdk"))

    cwd = app_config.clone_path if app_config.clone_path.is_dir() else Path.cwd()
    if uses_jira_integration(app_config):
        jira = app_config.integrations.jira
        assert jira is not None
        auth = jira_api.check_auth(jira.base_url, jira.project_key, cwd=cwd)
        checks.append(_check("Jira authentication", True, auth.authenticated, auth.error or auth.detail or "ok"))
        if auth.authenticated:
            write_ok = auth.can_write is not False
            checks.append(
                _check(f"Jira project '{jira.project_key}'", True, write_ok, auth.detail or "")
            )
    else:
        auth = check_auth(app_config.repo, cwd)
        checks.append(_check("GitHub authentication", True, auth.authenticated, auth.error or "authenticated"))
        if auth.authenticated:
            write_ok = auth.can_write is not False
            detail = auth.detail or ("permission undetermined" if auth.can_write is None else "")
            checks.append(_check(f"GitHub write access to '{app_config.repo}'", True, write_ok, detail))

    if uses_gitlab_integration(app_config):
        from config import gitlab_project_for

        host, project_path = gitlab_project_for(app_config)
        gl_auth = gitlab_api.check_auth(host, project_path, cwd=cwd)
        checks.append(_check("GitLab authentication", True, gl_auth.authenticated, gl_auth.error or gl_auth.detail))
        if gl_auth.authenticated:
            write_ok = gl_auth.can_write is not False
            checks.append(_check(f"GitLab project '{project_path}'", True, write_ok, gl_auth.detail or ""))

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
    stem = app_stem or app_config.name
    checks.extend(_check_base_branch_warnings(project_graph or {stem: app_config}))

    from utils.bootstrap import is_ready, marker_path

    ready = is_ready(stem)
    checks.append(
        _check(
            "Bootstrap ready marker",
            False,
            ready,
            f"found {marker_path(stem)}" if ready else f"missing {marker_path(stem)} — run --bootstrap",
        )
    )

    maestro_email = bool(os.environ.get("MAESTRO_EMAIL"))
    maestro_password = bool(os.environ.get("MAESTRO_PASSWORD"))
    checks.append(
        _check(
            "MAESTRO_EMAIL / MAESTRO_PASSWORD",
            False,
            maestro_email and maestro_password,
            "set for Jira STR→Maestro login prefix (OTP hardcoded 0000)"
            if maestro_email and maestro_password
            else "missing — required for lean Android POC flow generation",
        )
    )

    _print_preflight_report(checks)
    return 0 if all(c["passed"] for c in checks if c["critical"]) else 1


def _check_base_branch_warnings(graph: dict[str, AppConfig]) -> list[dict]:
    """Advisory: warn when a checkout exists but HEAD is not base_branch."""
    from utils.bootstrap import current_branch

    checks: list[dict] = []
    for stem, cfg in graph.items():
        if not (cfg.clone_path / ".git").is_dir():
            checks.append(
                _check(
                    f"Base branch: {stem}",
                    False,
                    False,
                    f"no git checkout at {cfg.clone_path}",
                )
            )
            continue
        branch = current_branch(cfg.clone_path)
        on_base = branch == cfg.base_branch
        detail = (
            f"on '{branch}' (base_branch={cfg.base_branch})"
            if branch
            else f"could not read HEAD (base_branch={cfg.base_branch})"
        )
        if on_base:
            detail = f"on base branch '{cfg.base_branch}'"
        checks.append(_check(f"Base branch: {stem}", False, on_base, detail))
    return checks


def _run_probe_search(app_config: AppConfig, app_stem: str | None = None) -> int:
    """Time two identical semSearch-only agent runs; does not write a ready marker."""
    import time

    from utils.cursor_agent import run_agent_prompt

    cwd = app_config.clone_path
    if not cwd.is_dir():
        print(f"Probe failed: clone_path does not exist: {cwd}", file=sys.stderr)
        return 1

    prompt = (
        "Use only semantic search. Find where the primary login or consent UI screen "
        "is implemented. Reply with a short list of file paths and one sentence. "
        "Do not edit files."
    )
    print(f"=== semSearch probe for '{app_stem or app_config.name}' at {cwd} ===\n")
    latencies: list[float] = []
    for i in range(1, 3):
        started = time.monotonic()
        ok, text, err = run_agent_prompt(
            prompt,
            cwd,
            timeout=180.0,
            tools=["semSearch"],
        )
        elapsed = time.monotonic() - started
        latencies.append(elapsed)
        status = "ok" if ok else f"failed: {err}"
        print(f"Run {i}: {elapsed:.1f}s ({status})")
        if text:
            preview = " ".join(text.split())[:240]
            print(f"  preview: {preview}")
        print()
    if len(latencies) == 2:
        print(f"Delta (run2 - run1): {latencies[1] - latencies[0]:+.1f}s")
    return 0


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
