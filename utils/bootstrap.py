"""One-time project bootstrap: clone, warm builds, write a repo map and ready marker.

Mutating by design. Call via `python main.py --app <name> --bootstrap` or the
dashboard Bootstrap button. Preflight stays read-only.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from config import PROJECT_ROOT, AppConfig, ConfigError, gitlab_project_for, load_project_graph
from utils import gitlab as gitlab_api
from utils.platform import build_app
from utils.runner import RunnerError, run_command
from utils.secrets import redact

_MAP_CHAR_CAP = 4_000
_INCLUDE_RE = re.compile(r"""include\s*\(\s*['"]([^'"]+)['"]\s*\)""")
_THEME_SYMBOLS = ("neutralScheme", "blackScheme")
LogFn = Callable[[str], None]


@dataclass
class RepoBootstrapResult:
    app_stem: str
    clone_path: str
    base_branch: str
    head_sha: str | None = None
    cloned: bool = False
    build_success: bool | None = None
    build_duration_sec: float | None = None
    build_error: str | None = None
    map_path: str | None = None
    error: str | None = None


@dataclass
class BootstrapResult:
    success: bool
    primary_app: str
    marker_path: str | None = None
    repos: list[RepoBootstrapResult] = field(default_factory=list)
    error: str | None = None


def bootstrap_dir(project_root: Path | None = None) -> Path:
    root = project_root or PROJECT_ROOT
    return root / "local" / "bootstrap"


def marker_path(primary_app: str, project_root: Path | None = None) -> Path:
    return bootstrap_dir(project_root) / f"{primary_app}.json"


def map_path_for(app_stem: str, project_root: Path | None = None) -> Path:
    return bootstrap_dir(project_root) / f"{app_stem}.map.md"


def is_ready(primary_app: str, project_root: Path | None = None) -> bool:
    path = marker_path(primary_app, project_root)
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return bool(data.get("ready")) and data.get("primary_app") == primary_app


def read_marker(primary_app: str, project_root: Path | None = None) -> dict | None:
    path = marker_path(primary_app, project_root)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def dependency_build_order(graph: dict[str, AppConfig], primary: str) -> list[str]:
    """Return stems with dependencies before dependents (libraries first)."""
    ordered: list[str] = []
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(stem: str) -> None:
        if stem in visited or stem in visiting:
            return
        visiting.add(stem)
        cfg = graph.get(stem)
        if cfg is not None:
            for dep in cfg.dependencies:
                if dep.app in graph:
                    visit(dep.app)
        visiting.remove(stem)
        visited.add(stem)
        ordered.append(stem)

    visit(primary)
    for stem in graph:
        visit(stem)
    return ordered


def bootstrap_project(
    primary_app: str,
    *,
    project_root: Path | None = None,
    skip_build: bool = False,
    log: LogFn | None = None,
) -> BootstrapResult:
    """Clone missing repos, warm builds, write repo maps and a ready marker."""
    root = project_root or PROJECT_ROOT
    emit = log or (lambda msg: print(msg, flush=True))

    from graph import PipelineSafetyError, ensure_source_isolated

    try:
        graph = load_project_graph(primary_app, project_root=root)
    except ConfigError as exc:
        return BootstrapResult(success=False, primary_app=primary_app, error=str(exc))

    for cfg in graph.values():
        try:
            ensure_source_isolated(cfg.clone_path)
        except PipelineSafetyError as exc:
            return BootstrapResult(success=False, primary_app=primary_app, error=str(exc))

    order = dependency_build_order(graph, primary_app)
    emit(f"[bootstrap] project graph order: {' → '.join(order)}")

    results: list[RepoBootstrapResult] = []
    for stem in order:
        cfg = graph[stem]
        emit(f"[bootstrap] --- {stem} ({cfg.clone_path}) ---")
        repo_result = _bootstrap_one_repo(
            stem, cfg, skip_build=skip_build, project_root=root, log=emit
        )
        results.append(repo_result)
        if repo_result.error:
            return BootstrapResult(
                success=False,
                primary_app=primary_app,
                repos=results,
                error=repo_result.error,
            )

    marker = _write_marker(primary_app, results, project_root=root)
    emit(f"[bootstrap] ready marker written: {marker}")
    return BootstrapResult(
        success=True,
        primary_app=primary_app,
        marker_path=str(marker),
        repos=results,
    )


def _bootstrap_one_repo(
    stem: str,
    cfg: AppConfig,
    *,
    skip_build: bool,
    project_root: Path,
    log: LogFn,
) -> RepoBootstrapResult:
    result = RepoBootstrapResult(
        app_stem=stem,
        clone_path=str(cfg.clone_path),
        base_branch=cfg.base_branch,
    )
    err, cloned = ensure_checkout(cfg, log=log)
    if err:
        result.error = err
        return result
    result.cloned = cloned
    result.head_sha = head_sha(cfg.clone_path)

    map_file = write_repo_map(cfg, stem, project_root=project_root)
    result.map_path = str(map_file)
    log(f"[bootstrap] repo map: {map_file}")

    if skip_build:
        log(f"[bootstrap] build skipped for {stem}")
        result.build_success = None
        return result

    log(f"[bootstrap] warming build: {cfg.build.build_command}")
    started = time.monotonic()
    build_result = build_app(cfg)
    result.build_duration_sec = round(time.monotonic() - started, 2)
    result.build_success = build_result.success
    if not build_result.success:
        result.build_error = redact(build_result.error or "build failed")
        result.error = f"Build warm-up failed for '{stem}': {result.build_error}"
        log(f"[bootstrap] build failed ({result.build_duration_sec}s): {result.build_error}")
        return result
    log(f"[bootstrap] build ok ({result.build_duration_sec}s)")
    return result


def ensure_checkout(cfg: AppConfig, *, log: LogFn | None = None) -> tuple[str | None, bool]:
    """Ensure clone_path is a git repo. Returns (error_or_None, cloned)."""
    emit = log or (lambda _msg: None)
    path = cfg.clone_path

    if path.is_dir() and (path / ".git").is_dir():
        emit(f"[bootstrap] checkout exists, leaving branch as-is: {path}")
        return None, False

    if path.exists() and not (path / ".git").is_dir():
        return (
            f"{path} exists but is not a git repository. "
            "Point clone_path at an empty path or an existing git checkout.",
            False,
        )

    parent = path.parent
    if not parent.is_dir():
        return (
            f"Parent directory of clone_path does not exist: {parent}. "
            "Set clone_path in apps/<name>.yaml to a directory on this machine "
            "(do not use another developer's absolute path).",
            False,
        )

    try:
        host, project_path = gitlab_project_for(cfg)
        url = gitlab_api.clone_url(host, project_path)
    except gitlab_api.GitLabError as exc:
        return str(exc), False

    emit(f"[bootstrap] cloning {host}/{project_path} → {path}")
    try:
        clone_result = run_command(
            ["git", "clone", "--branch", cfg.base_branch, url, str(path)],
            cwd=parent,
            timeout=1800.0,
        )
    except RunnerError as exc:
        return redact(str(exc)), False

    if not clone_result.ok:
        try:
            clone_result = run_command(
                ["git", "clone", url, str(path)],
                cwd=parent,
                timeout=1800.0,
            )
        except RunnerError as exc:
            return redact(str(exc)), False
        if not clone_result.ok:
            return redact(clone_result.stderr.strip() or f"git clone exited {clone_result.returncode}"), False
        checkout = run_command(
            ["git", "checkout", cfg.base_branch],
            cwd=path,
            timeout=60.0,
        )
        if not checkout.ok:
            return (
                redact(checkout.stderr.strip() or f"git checkout {cfg.base_branch} failed"),
                False,
            )
    emit(f"[bootstrap] cloned onto {cfg.base_branch}")
    return None, True


def head_sha(clone_path: Path) -> str | None:
    if not (clone_path / ".git").is_dir():
        return None
    try:
        result = run_command(["git", "rev-parse", "HEAD"], cwd=clone_path, timeout=15.0)
    except RunnerError:
        return None
    if not result.ok:
        return None
    return result.stdout.strip() or None


def current_branch(clone_path: Path) -> str | None:
    if not (clone_path / ".git").is_dir():
        return None
    try:
        result = run_command(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=clone_path,
            timeout=15.0,
        )
    except RunnerError:
        return None
    if not result.ok:
        return None
    name = result.stdout.strip()
    return name or None


def generate_repo_map(cfg: AppConfig, app_stem: str) -> str:
    """Build a short structural map of a checkout (no LLM)."""
    root = cfg.clone_path
    lines = [
        f"# Repo map: {app_stem}",
        f"- name: {cfg.name}",
        f"- platform: {cfg.platform}",
        f"- base_branch: {cfg.base_branch}",
        f"- clone_path: {root}",
        "",
    ]

    modules = _gradle_modules(root)
    if modules:
        lines.append("## Gradle modules")
        for mod in modules[:80]:
            lines.append(f"- `{mod}`")
        lines.append("")

    screens = _ods_screens(root)
    if screens:
        lines.append("## Feature screens (ods/<feature>/<screen>/ui)")
        for screen in screens[:60]:
            lines.append(f"- `{screen}`")
            for sib in _sibling_props_factories(root, screen):
                lines.append(f"  - `{sib}`")
        lines.append("")

    themes = _find_theme_symbols(root)
    if themes:
        lines.append("## Theme symbols found")
        for symbol, rel in themes:
            lines.append(f"- `{symbol}` in `{rel}`")
        lines.append("")

    text = "\n".join(lines).strip() + "\n"
    if len(text) > _MAP_CHAR_CAP:
        text = text[: _MAP_CHAR_CAP - 20].rstrip() + "\n…(truncated)\n"
    return text


def write_repo_map(cfg: AppConfig, app_stem: str, project_root: Path | None = None) -> Path:
    path = map_path_for(app_stem, project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(generate_repo_map(cfg, app_stem), encoding="utf-8")
    return path


def _gradle_modules(root: Path) -> list[str]:
    modules: list[str] = []
    for name in ("settings.gradle.kts", "settings.gradle"):
        settings = root / name
        if not settings.is_file():
            continue
        try:
            text = settings.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for match in _INCLUDE_RE.finditer(text):
            mod = match.group(1).strip()
            if mod and mod not in modules:
                modules.append(mod)
    return modules


def _ods_screens(root: Path) -> list[str]:
    screens: list[str] = []
    try:
        for ui_dir in root.rglob("ui"):
            if not ui_dir.is_dir():
                continue
            parts = ui_dir.relative_to(root).parts
            if "ods" not in parts:
                continue
            idx = parts.index("ods")
            if idx + 3 < len(parts) and parts[idx + 3] == "ui":
                rel = str(Path(*parts[: idx + 4]))
                if rel not in screens:
                    screens.append(rel)
    except OSError:
        return screens
    return sorted(screens)


def _sibling_props_factories(root: Path, screen_rel: str) -> list[str]:
    ui_dir = root / screen_rel
    parent = ui_dir.parent if ui_dir.name == "ui" else ui_dir
    if not parent.is_dir():
        return []
    hits: list[str] = []
    try:
        for path in parent.iterdir():
            if path.is_file() and ("Props" in path.name or "Factory" in path.name):
                hits.append(str(path.relative_to(root)))
            elif path.is_dir():
                for child in path.iterdir():
                    if child.is_file() and ("Props" in child.name or "Factory" in child.name):
                        hits.append(str(child.relative_to(root)))
    except OSError:
        return hits
    return sorted(hits)[:12]


def _find_theme_symbols(root: Path) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    try:
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in {".kt", ".kts", ".java", ".xml"}:
                continue
            try:
                if path.stat().st_size > 512_000:
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for symbol in _THEME_SYMBOLS:
                if symbol in text and symbol not in seen:
                    seen.add(symbol)
                    try:
                        found.append((symbol, str(path.relative_to(root))))
                    except ValueError:
                        found.append((symbol, path.name))
            if len(seen) >= len(_THEME_SYMBOLS):
                break
    except OSError:
        return found
    return found


def _write_marker(primary_app: str, repos: list[RepoBootstrapResult], project_root: Path) -> Path:
    path = marker_path(primary_app, project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "ready": True,
        "primary_app": primary_app,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "repos": [asdict(r) for r in repos],
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path
