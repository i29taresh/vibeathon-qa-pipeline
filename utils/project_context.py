"""Resolve multi-repo project graph entries and load bootstrap repo maps."""

from __future__ import annotations

from pathlib import Path

from config import PROJECT_ROOT, AppConfig


def resolve_fix_app_config(state: dict) -> tuple[str, AppConfig]:
    """Return (app_stem, AppConfig) where dev_agent / merge_step should apply the fix."""
    primary = state["app_config"]
    primary_stem = state.get("app_name") or primary.name
    graph: dict[str, AppConfig] = state.get("project_graph") or {primary_stem: primary}
    rca = state.get("rca_finding") or {}
    target = rca.get("target_app") or primary_stem
    if target in graph:
        return target, graph[target]
    return primary_stem, primary


def _map_path(app_stem: str, project_root: Path | None = None) -> Path:
    root = project_root or PROJECT_ROOT
    return root / "local" / "bootstrap" / f"{app_stem}.map.md"


def load_repo_map(app_stem: str, project_root: Path | None = None) -> str:
    """Return the bootstrap repo-map markdown for `app_stem`, or '' if missing."""
    path = _map_path(app_stem, project_root)
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def load_project_repo_maps(project_graph: dict[str, AppConfig], project_root: Path | None = None) -> str:
    """Concatenate repo maps for every stem in the graph (empty string if none)."""
    blocks: list[str] = []
    for stem in project_graph:
        text = load_repo_map(stem, project_root)
        if text:
            blocks.append(text)
    return "\n\n".join(blocks).strip()
