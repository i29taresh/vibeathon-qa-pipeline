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
from typing import Literal, Optional, Union

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

_REPO_PATTERN = re.compile(r"^[\w.-]+/[\w.-]+$")


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
    # Optional: the automated (unit/regression) test command, distinct from
    # build_command and from a Maestro flow. None means "not configured" -
    # dev_agent reports tests as skipped rather than guessing one.
    test_command: Optional[str] = None


class IOSBuild(BaseModel):
    scheme: str = Field(min_length=1)
    workspace: str = Field(min_length=1)
    build_command: str = Field(min_length=1)
    artifact_path: str = Field(min_length=1)
    install_command: str = Field(min_length=1)
    bundle_id: str = Field(min_length=1)
    test_command: Optional[str] = None


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
    app_id: str = Field(min_length=1)  # package_id (android) or bundle_id (ios)
    # Optional: flow filenames (relative to flows_dir) retest_agent re-runs
    # as a regression suite after a fix, in addition to the originally
    # failing flow. Empty by default - "configured" regression flows, never
    # guessed from the contents of flows_dir.
    regression_flows: list[str] = Field(default_factory=list)

    @field_validator("repo")
    @classmethod
    def _validate_repo_format(cls, value: str) -> str:
        if not _REPO_PATTERN.match(value):
            raise ValueError(f"repo must look like 'owner/repository', got {value!r}")
        return value


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


def load_app_config(name: str, project_root: Path | None = None) -> AppConfig:
    """Load, validate, and resolve the configuration for app `name`.

    Reads `apps/<name>.yaml` under `project_root` (defaults to this file's
    directory), checks that all required fields are present, validates the
    device/build block for the declared platform, resolves clone_path/
    flows_dir/reference_dir relative to the project root, and returns an
    AppConfig.

    Raises ConfigError for any missing, invalid, unsupported-platform, or
    malformed configuration. Never runs a shell command.
    """
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

    try:
        return AppConfig(
            name=data["name"],
            platform=platform,
            repo=data["repo"],
            clone_path=(root / data["clone_path"]).resolve(),
            flows_dir=(root / data["flows_dir"]).resolve(),
            reference_dir=(root / data["reference_dir"]).resolve(),
            device=device,
            build=build,
            app_id=app_id,
            regression_flows=data.get("regression_flows") or [],
        )
    except ValidationError as exc:
        raise ConfigError(f"{context} is invalid:\n{exc}") from exc
