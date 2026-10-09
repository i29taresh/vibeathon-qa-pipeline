"""Persist pipeline run artifacts under local/runs/<JIRA-KEY>/<run-id>/."""

from __future__ import annotations

import json
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from utils.secrets import redact


def default_runs_root(orchestrator_root: Path | None = None) -> Path:
    if orchestrator_root is None:
        orchestrator_root = Path(__file__).resolve().parent.parent
    return orchestrator_root / "local" / "runs"


def _utc_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{uuid.uuid4().hex[:8]}"


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    # AppConfig and other objects
    if hasattr(value, "model_dump"):
        return _json_safe(value.model_dump())
    if hasattr(value, "__dict__"):
        return {"_type": type(value).__name__, "repr": redact(repr(value))[:500]}
    return redact(str(value))[:500]


def save_run(state: dict[str, Any], runs_root: Path | None = None) -> Path:
    """Write manifest + copy key artifacts. Returns the run directory."""
    root = runs_root or default_runs_root()
    jira_key = str(
        state.get("jira_issue_key")
        or state.get("ticket_id")
        or "UNKNOWN"
    ).strip()
    run_id = str(state.get("run_history_id") or _utc_run_id())
    run_dir = root / jira_key / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    frames_dir = run_dir / "frames"
    frames_dir.mkdir(exist_ok=True)

    # Copy after video
    for i, raw in enumerate(state.get("after_video_paths") or []):
        src = Path(raw)
        if src.is_file():
            dest = run_dir / ("after.mp4" if i == 0 else f"after-{i}{src.suffix}")
            try:
                shutil.copy2(src, dest)
            except OSError:
                pass

    verifier = state.get("verifier_finding") or {}
    for i, raw in enumerate(verifier.get("frame_paths") or []):
        src = Path(raw)
        if src.is_file():
            try:
                shutil.copy2(src, frames_dir / src.name)
            except OSError:
                pass

    # Repro flow
    flow = state.get("maestro_flow_path")
    if flow and Path(flow).is_file():
        try:
            shutil.copy2(flow, run_dir / "repro.yaml")
        except OSError:
            pass

    # Patches from retest failures (if present on state)
    for raw in state.get("attempt_patch_paths") or []:
        src = Path(raw)
        if src.is_file():
            try:
                shutil.copy2(src, run_dir / src.name)
            except OSError:
                pass

    if verifier:
        (run_dir / "verifier.json").write_text(
            json.dumps(_json_safe(verifier), indent=2),
            encoding="utf-8",
        )

    finalize = state.get("finalize_finding") or state.get("merge_finding") or {}
    slice_keys = [
        "status",
        "passed",
        "attempt_count",
        "jira_issue_key",
        "ticket_id",
        "flow_name",
        "maestro_flow_path",
        "qa_finding",
        "rca_finding",
        "dev_finding",
        "retest_finding",
        "verifier_finding",
        "after_video_paths",
        "finalize_finding",
        "merge_finding",
        "maven_local_version",
        "jira_baseline",
    ]
    manifest = {
        "run_id": run_id,
        "jira_key": jira_key,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "branch": finalize.get("branch") or (state.get("dev_finding") or {}).get("branch"),
        "commit_sha": finalize.get("commit_sha"),
        "state": {k: _json_safe(state.get(k)) for k in slice_keys},
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return run_dir


def list_runs(runs_root: Path | None = None) -> list[dict[str, Any]]:
    """Flat list of runs newest-first."""
    root = runs_root or default_runs_root()
    if not root.is_dir():
        return []
    runs: list[dict[str, Any]] = []
    for jira_dir in sorted(root.iterdir()):
        if not jira_dir.is_dir():
            continue
        for run_dir in sorted(jira_dir.iterdir(), reverse=True):
            if not run_dir.is_dir():
                continue
            manifest_path = run_dir / "manifest.json"
            if not manifest_path.is_file():
                continue
            try:
                data = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            data["_path"] = str(run_dir)
            runs.append(data)
    runs.sort(key=lambda r: r.get("saved_at") or r.get("run_id") or "", reverse=True)
    return runs


def list_attempts(jira_key: str, runs_root: Path | None = None) -> list[dict[str, Any]]:
    root = runs_root or default_runs_root()
    jira_dir = root / jira_key
    if not jira_dir.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for run_dir in sorted(jira_dir.iterdir(), reverse=True):
        manifest_path = run_dir / "manifest.json"
        if not manifest_path.is_file():
            continue
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        data["_path"] = str(run_dir)
        out.append(data)
    return out


def load_run(jira_key: str, run_id: str, runs_root: Path | None = None) -> dict[str, Any] | None:
    root = runs_root or default_runs_root()
    run_dir = root / jira_key / run_id
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.is_file():
        return None
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    data["_path"] = str(run_dir)
    data["_after_video"] = str(run_dir / "after.mp4") if (run_dir / "after.mp4").is_file() else None
    data["_verifier"] = None
    vpath = run_dir / "verifier.json"
    if vpath.is_file():
        try:
            data["_verifier"] = json.loads(vpath.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    return data


def list_jira_keys(runs_root: Path | None = None) -> list[str]:
    root = runs_root or default_runs_root()
    if not root.is_dir():
        return []
    return sorted(
        [p.name for p in root.iterdir() if p.is_dir() and any(p.iterdir())],
        reverse=True,
    )
