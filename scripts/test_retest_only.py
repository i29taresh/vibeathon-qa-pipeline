#!/usr/bin/env python3
"""Run only retest_agent against b2b_android with a synthetic successful dev_finding.

Usage (from repo root, venv active, emulator already booted):

  python scripts/test_retest_only.py
  python scripts/test_retest_only.py --skip-maestro   # stop after select_device+build+install
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Prefill secrets from gitignored dashboard env (CURSOR_*, MAESTRO_*, …).
_env_file = ROOT / "local" / "dashboard_env.yaml"
if _env_file.is_file():
    try:
        import yaml

        for key, value in (yaml.safe_load(_env_file.read_text()) or {}).items():
            if value and not os.environ.get(str(key)):
                os.environ[str(key)] = str(value)
    except Exception:
        pass

# Ensure Android SDK + Maestro on PATH before any adb/maestro call.
os.environ.setdefault("ANDROID_HOME", str(Path.home() / "Library" / "Android" / "sdk"))
os.environ.setdefault("ANDROID_SDK_ROOT", os.environ["ANDROID_HOME"])
os.environ.setdefault("EMULATOR_PIN", "1111")
_sdk = Path(os.environ["ANDROID_HOME"])
_maestro = Path.home() / ".maestro" / "bin"
os.environ["PATH"] = (
    f"{_sdk / 'platform-tools'}:{_sdk / 'emulator'}:{_maestro}:" + os.environ.get("PATH", "")
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-maestro", action="store_true")
    parser.add_argument("--ticket", default="WFDR-25776")
    args = parser.parse_args()

    from config import load_app_config, load_project_graph
    from nodes.retest_agent import retest_agent
    from state import initial_state
    from utils.android_sdk import adb_path
    from utils.platform import select_device
    from utils.runner import run_command

    print(f"[retest-only] adb -> {adb_path()}")
    devices = run_command(["adb", "devices"], cwd=ROOT, timeout=30.0)
    print(devices.stdout or devices.stderr)
    if "device" not in (devices.stdout or ""):
        print("FAIL: no device in `adb devices`. Boot an emulator first.", file=sys.stderr)
        return 2

    cfg = load_app_config("b2b_android", project_root=ROOT)
    graph = load_project_graph("b2b_android", project_root=ROOT)
    sel = select_device(cfg)
    print(f"[retest-only] select_device success={sel.success} details={sel.details} error={sel.error}")
    if not sel.success:
        return 3

    if args.skip_maestro:
        print("[retest-only] --skip-maestro: device selection OK; stopping before full retest.")
        return 0

    flow = ROOT / "local" / "runs" / args.ticket / "repro.yaml"
    if not flow.is_file():
        print(f"FAIL: missing Maestro flow {flow}", file=sys.stderr)
        return 4

    state = initial_state(
        app_name="b2b_android",
        app_config=cfg,
        mode="real",
        flow_name=f"runs/{args.ticket}/repro.yaml",
        project_graph=graph,
        force_unbootstrapped=True,
    )
    state.update(
        {
            "ticket_id": args.ticket,
            "jira_issue_key": args.ticket,
            "jira_issue_input": True,
            "attempt_count": 1,
            "max_attempts": 3,
            "maestro_flow_path": str(flow),
            "jira_baseline": {
                "issue_key": args.ticket,
                "summary": "retest-only harness",
                "expected_behavior": "password fields visible",
                "actual_behavior": "blank password page",
                "reproduction_steps": ["Open forgot password", "Enter OTP", "See password page"],
                "baseline_evidence_paths": [],
            },
            "dev_finding": {
                "branch": f"ai-fix/{args.ticket}",
                "base_branch": "develop",
                "target_app": "b2b_android",
                "files_changed": ["M feature/authentication/presentation/src/ods/kotlin/dummy.kt"],
                "requires_approval": False,
                "committed": False,
                "build": {"success": True, "artifact_path": str(cfg.clone_path / cfg.build.artifact_path)},
            },
        }
    )

    print("[retest-only] invoking retest_agent...")
    out = retest_agent(state)
    print(f"[retest-only] status={out.get('status')} passed={out.get('passed')}")
    print(f"[retest-only] last_error={out.get('last_error')}")
    rf = out.get("retest_finding") or {}
    print(f"[retest-only] build={rf.get('build')} install={rf.get('install')}")
    print(f"[retest-only] verifier={out.get('verifier_finding') or rf.get('jira_verification')}")
    return 0 if out.get("status") == "retest_passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
