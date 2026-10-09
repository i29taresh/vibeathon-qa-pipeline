"""Resolve multi-repo project graph entries for pipeline nodes."""

from __future__ import annotations

from config import AppConfig


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
