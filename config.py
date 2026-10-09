"""Per-app configuration loading and validation.

Loads apps/<name>.yaml, validates it against the schema for the app's
declared platform (android/ios), resolves local paths relative to the
project root, and returns a structured AppConfig.

This module only reads and validates data - it never executes a shell
command, build command, or install command. Node code is expected to read
everything it needs (repo, flow dir, build command, package/bundle id, ...)
from the returned AppConfig instead of hardcoding it.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal, Optional, Union

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator

PROJECT_ROOT = Path(__file__).resolve().parent
APPS_DIR_NAME = "apps"

SUPPORTED_PLATFORMS = ("android", "ios")

TOP_LEVEL_REQUIRED_FIELDS = (
    "name",
    "platform",
    "repo",
    "clone_path",
    "flows_dir",
    "reference_dir",
    "device",
    "build",
)

# Nested GitLab paths (e.g. allios/touchpoint/android/b2b-android).
_REPO_PATTERN = re.compile(r"^[\w.-]+(/[\w.-]+)+$")


class ConfigError(Exception):
    """Raised for any problem loading or validating an app config."""


class AndroidDevice(BaseModel):
    avd_name: str = Field(min_length=1)
    api_level: int = Field(gt=0)


class IOSDevice(BaseModel):
    simulator_name: str = Field(min_length=1)
    os_version: str = Field(min_length=1)


class AndroidBuild(BaseModel):
    module: str = Field(min_length=1)
    build_command: str = Field(min_length=1)
    artifact_path: str = Field(min_length=1)
    install_command: str = Field(min_length=1)
    package_id: str = Field(min_length=1)
    test_command: Optional[str] = None


class IOSBuild(BaseModel):
    scheme: str = Field(min_length=1)
    workspace: str = Field(min_length=1)
    build_command: str = Field(min_length=1)
    artifact_path: str = Field(min_length=1)
    install_command: str = Field(min_length=1)
    bundle_id: str = Field(min_length=1)
    test_command: Optional[str] = None


class GitLabIntegration(BaseModel):
    host: str = ""
    project_path: str = ""


class JiraIntegration(BaseModel):
    base_url: str = ""
    project_key: str = ""
    issue_type: str = "Bug"


class FigmaIntegration(BaseModel):
    file_url: str = ""
    frame_urls: list[str] = Field(default_factory=list)


class Integrations(BaseModel):
    gitlab: Optional[GitLabIntegration] = None
    jira: Optional[JiraIntegration] = None
    figma: Optional[FigmaIntegration] = None


class DependencyRef(BaseModel):
    app: str = Field(min_length=1)
    relationship: str = "library"


class AppConfig(BaseModel):
    """Fully validated, platform-resolved configuration for one app."""

    name: str = Field(min_length=1)
    platform: Literal["android", "ios"]
    repo: str = Field(min_length=1)
    clone_path: Path
    flows_dir: Path
    reference_dir: Path
    device: Union[AndroidDevice, IOSDevice]
    build: Union[AndroidBuild, IOSBuild]
    app_id: str = Field(min_length=1)
    regression_flows: list[str] = Field(default_factory=list)
    # Branch new ai-fix/* branches are cut from, and the merge-request target.
    base_branch: str = "develop"
    integrations: Integrations = Field(default_factory=Integrations)
    dependencies: list[DependencyRef] = Field(default_factory=list)

    @field_validator("base_branch")
    @classmethod
    def _validate_base_branch(cls, value: str) -> str:
        name = value.strip()
        if not name or any(ch.isspace() for ch in name) or ".." in name:
            raise ValueError(f"base_branch must be a git branch name, got {value!r}")
        return name

    @field_validator("repo")
    @classmethod
    def _validate_repo_format(cls, value: str) -> str:
        if not _REPO_PATTERN.match(value):
            raise ValueError(
                f"repo must look like 'group/subgroup/project' (at least one slash), got {value!r}"
            )
        return value


def uses_jira_integration(cfg: AppConfig) -> bool:
    jira = cfg.integrations.jira
    return bool(jira and jira.base_url.strip() and jira.project_key.strip())


def uses_gitlab_integration(cfg: AppConfig) -> bool:
    gitlab = cfg.integrations.gitlab
    return bool(gitlab and gitlab.host.strip() and gitlab.project_path.strip())


def figma_context_lines(cfg: AppConfig) -> list[str]:
    figma = cfg.integrations.figma
    if not figma:
        return []
    lines: list[str] = []
    if figma.file_url.strip():
        lines.append(f"Figma file: {figma.file_url.strip()}")
    for url in figma.frame_urls:
        if url.strip():
            lines.append(f"Figma frame: {url.strip()}")
    return lines


def gitlab_project_for(cfg: AppConfig) -> tuple[str, str]:
    """Return (host, project_path) for GitLab API calls."""
    gitlab = cfg.integrations.gitlab
    if gitlab and gitlab.host.strip() and gitlab.project_path.strip():
        return gitlab.host.strip(), gitlab.project_path.strip()
    return "gitlab.com", cfg.repo


def _read_yaml(path: Path) -> dict:
    if not path.is_file():
        raise ConfigError(f"App config not found: {path}")

    try:
        raw = path.read_text()
    except OSError as exc:
        raise ConfigError(f"Could not read app config {path}: {exc}") from exc

    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ConfigError(f"Malformed YAML in {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise ConfigError(
            f"App config {path} must be a YAML mapping, got {type(data).__name__}"
        )
    return data


def _require_fields(data: dict, fields: tuple, context: str) -> None:
    missing = [f for f in fields if data.get(f) in (None, "")]
    if missing:
        raise ConfigError(f"{context} missing required field(s): {', '.join(missing)}")


def _parse_integrations(data: dict) -> Integrations:
    raw = data.get("integrations")
    if not raw:
        return Integrations()
    if not isinstance(raw, dict):
        raise ConfigError("'integrations' must be a mapping")
    try:
        return Integrations(
            gitlab=GitLabIntegration(**raw["gitlab"]) if isinstance(raw.get("gitlab"), dict) else None,
            jira=JiraIntegration(**raw["jira"]) if isinstance(raw.get("jira"), dict) else None,
            figma=FigmaIntegration(**raw["figma"]) if isinstance(raw.get("figma"), dict) else None,
        )
    except ValidationError as exc:
        raise ConfigError(f"Invalid integrations block:\n{exc}") from exc


def _parse_dependencies(data: dict, context: str) -> list[DependencyRef]:
    raw = data.get("dependencies") or []
    if not isinstance(raw, list):
        raise ConfigError(f"{context}: 'dependencies' must be a list")
    deps: list[DependencyRef] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ConfigError(f"{context}: each dependency must be a mapping")
        try:
            deps.append(DependencyRef(**item))
        except ValidationError as exc:
            raise ConfigError(f"{context}: invalid dependency entry:\n{exc}") from exc
    return deps


def list_app_config_names(project_root: Path | None = None) -> list[str]:
    root = project_root or PROJECT_ROOT
    apps_dir = root / APPS_DIR_NAME
    if not apps_dir.is_dir():
        return []
    return sorted(
        p.stem for p in apps_dir.glob("*.yaml") if p.is_file() and not p.stem.startswith("_")
    )


def dependency_adjacency(project_root: Path | None = None) -> dict[str, list[str]]:
    """Map each app config stem to the list of dependency app stems."""
    graph: dict[str, list[str]] = {}
    for name in list_app_config_names(project_root):
        try:
            cfg = load_app_config(name, project_root=project_root)
        except ConfigError:
            continue
        graph[name] = [d.app for d in cfg.dependencies]
    return graph


def find_dependency_cycle(start: str, graph: dict[str, list[str]]) -> Optional[list[str]]:
    """Return a cycle path including `start` if one exists, else None."""
    path: list[str] = []
    visiting: set[str] = set()
    visited: set[str] = set()

    def dfs(node: str) -> Optional[list[str]]:
        if node in visiting:
            idx = path.index(node)
            return path[idx:] + [node]
        if node in visited:
            return None
        visiting.add(node)
        path.append(node)
        for child in graph.get(node, []):
            cycle = dfs(child)
            if cycle:
                return cycle
        path.pop()
        visiting.remove(node)
        visited.add(node)
        return None

    return dfs(start)


def validate_dependencies_for(app_name: str, project_root: Path | None = None) -> None:
    """Ensure dependency apps exist and the graph has no cycle through app_name."""
    root = project_root or PROJECT_ROOT
    cfg = load_app_config(app_name, project_root=root)
    known = set(list_app_config_names(root))
    for dep in cfg.dependencies:
        if dep.app not in known:
            raise ConfigError(
                f"App '{app_name}' depends on unknown app '{dep.app}' "
                f"(no apps/{dep.app}.yaml)"
            )
    adj = dependency_adjacency(root)
    cycle = find_dependency_cycle(app_name, adj)
    if cycle:
        raise ConfigError(f"Dependency cycle detected: {' -> '.join(cycle)}")


def load_project_graph(
    primary_app: str, project_root: Path | None = None
) -> dict[str, AppConfig]:
    """Load primary app and all transitive dependencies (by config stem)."""
    root = project_root or PROJECT_ROOT
    validate_dependencies_for(primary_app, project_root=root)
    loaded: dict[str, AppConfig] = {}
    stack = [primary_app]
    while stack:
        name = stack.pop()
        if name in loaded:
            continue
        cfg = load_app_config(name, project_root=root)
        loaded[name] = cfg
        for dep in cfg.dependencies:
            if dep.app not in loaded:
                stack.append(dep.app)
    return loaded


def collect_clone_paths(graph: dict[str, AppConfig]) -> list[Path]:
    return [cfg.clone_path for cfg in graph.values()]


def _resolve_data_path(project_root: Path, clone_path: Path, raw: str) -> Path:
    """Resolve flows_dir / reference_dir.

    Relative paths prefer the orchestrator project root (tests and shared assets).
    If that directory does not exist but the same relative path exists under
    ``clone_path``, use the app checkout (typical for absolute clone_path POCs).
    """
    rel = Path(raw).expanduser()
    if rel.is_absolute():
        return rel.resolve()
    under_project = (project_root / rel).resolve()
    under_clone = (clone_path / rel).resolve()
    if under_project.is_dir():
        return under_project
    if under_clone.is_dir():
        return under_clone
    return under_project


def load_app_config(name: str, project_root: Path | None = None) -> AppConfig:
    """Load, validate, and resolve the configuration for app `name`."""
    root = project_root or PROJECT_ROOT
    path = root / APPS_DIR_NAME / f"{name}.yaml"
    data = _read_yaml(path)
    context = f"App config '{name}' ({path})"

    _require_fields(data, TOP_LEVEL_REQUIRED_FIELDS, context)

    platform = data["platform"]
    if platform not in SUPPORTED_PLATFORMS:
        raise ConfigError(
            f"{context} has unsupported platform {platform!r}; "
            f"expected one of {SUPPORTED_PLATFORMS}"
        )

    device_block = data["device"]
    build_block = data["build"]
    if not isinstance(device_block, dict):
        raise ConfigError(f"{context}: 'device' must be a mapping")
    if not isinstance(build_block, dict):
        raise ConfigError(f"{context}: 'build' must be a mapping")

    if not isinstance(device_block.get(platform), dict):
        raise ConfigError(f"{context}: 'device.{platform}' section is required")
    if not isinstance(build_block.get(platform), dict):
        raise ConfigError(f"{context}: 'build.{platform}' section is required")

    integrations = _parse_integrations(data)
    dependencies = _parse_dependencies(data, context)

    try:
        if platform == "android":
            device: Union[AndroidDevice, IOSDevice] = AndroidDevice(**device_block["android"])
            build: Union[AndroidBuild, IOSBuild] = AndroidBuild(**build_block["android"])
            app_id = build.package_id
        else:
            device = IOSDevice(**device_block["ios"])
            build = IOSBuild(**build_block["ios"])
            app_id = build.bundle_id
    except ValidationError as exc:
        raise ConfigError(
            f"{context} has invalid '{platform}' device/build fields:\n{exc}"
        ) from exc

    clone_raw = data["clone_path"]
    clone_path = Path(clone_raw).expanduser()
    if not clone_path.is_absolute():
        clone_path = (root / clone_raw).resolve()

    try:
        return AppConfig(
            name=data["name"],
            platform=platform,
            repo=data["repo"],
            clone_path=clone_path,
            flows_dir=_resolve_data_path(root, clone_path, data["flows_dir"]),
            reference_dir=_resolve_data_path(root, clone_path, data["reference_dir"]),
            device=device,
            build=build,
            app_id=app_id,
            regression_flows=data.get("regression_flows") or [],
            base_branch=str(data.get("base_branch") or "develop"),
            integrations=integrations,
            dependencies=dependencies,
        )
    except ValidationError as exc:
        raise ConfigError(f"{context} is invalid:\n{exc}") from exc


def app_config_to_yaml_dict(cfg: AppConfig, project_root: Path | None = None) -> dict[str, Any]:
    """Serialize AppConfig to a YAML-friendly dict (paths relative to project root when possible)."""
    root = project_root or PROJECT_ROOT

    def _rel_path(p: Path) -> str:
        try:
            return str(p.resolve().relative_to(root.resolve()))
        except ValueError:
            return str(p.resolve())

    device_block: dict[str, Any]
    build_block: dict[str, Any]
    if cfg.platform == "android":
        device_block = {"android": cfg.device.model_dump()}
        build_block = {"android": cfg.build.model_dump()}
    else:
        device_block = {"ios": cfg.device.model_dump()}
        build_block = {"ios": cfg.build.model_dump()}

    integrations: dict[str, Any] = {}
    if cfg.integrations.gitlab:
        integrations["gitlab"] = cfg.integrations.gitlab.model_dump()
    if cfg.integrations.jira:
        integrations["jira"] = cfg.integrations.jira.model_dump()
    if cfg.integrations.figma:
        integrations["figma"] = cfg.integrations.figma.model_dump()

    data: dict[str, Any] = {
        "name": cfg.name,
        "platform": cfg.platform,
        "repo": cfg.repo,
        "clone_path": _rel_path(cfg.clone_path),
        "flows_dir": _rel_path(cfg.flows_dir),
        "reference_dir": _rel_path(cfg.reference_dir),
        "base_branch": cfg.base_branch,
        "device": device_block,
        "build": build_block,
    }
    if cfg.regression_flows:
        data["regression_flows"] = list(cfg.regression_flows)
    if integrations:
        data["integrations"] = integrations
    if cfg.dependencies:
        data["dependencies"] = [d.model_dump() for d in cfg.dependencies]
    return data


def save_app_config_yaml(name: str, data: dict[str, Any], project_root: Path | None = None) -> Path:
    """Write apps/<name>.yaml after validating by round-tripping through load."""
    root = project_root or PROJECT_ROOT
    apps_dir = root / APPS_DIR_NAME
    apps_dir.mkdir(parents=True, exist_ok=True)
    path = apps_dir / f"{name}.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False, default_flow_style=False))
    load_app_config(name, project_root=root)
    deps = data.get("dependencies") or []
    if deps:
        validate_dependencies_for(name, project_root=root)
    return path


def save_app_config(cfg: AppConfig, config_stem: str, project_root: Path | None = None) -> Path:
    return save_app_config_yaml(config_stem, app_config_to_yaml_dict(cfg, project_root), project_root)
