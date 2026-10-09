"""Publish/pin/cleanup payzyshared versions for b2b retest via mavenLocal()."""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from config import AppConfig
from utils.runner import RunnerError, run_command
from utils.secrets import redact

_PAYZY_SHARED_RE = re.compile(
    r'^(\s*payzyShared\s*=\s*")([^"]+)("\s*)$',
    re.MULTILINE,
)
_ARTIFACT_VERSION_RE = re.compile(r"^\s*artifact\.version\s*=\s*(.+?)\s*$", re.MULTILINE)


@dataclass
class MavenLocalResult:
    success: bool
    version: str | None = None
    original_pin: str | None = None
    error: str | None = None


def read_artifact_version(payzy_cfg: AppConfig) -> str | None:
    props = payzy_cfg.clone_path / "gradle.properties"
    if not props.is_file():
        return None
    text = props.read_text(encoding="utf-8", errors="replace")
    match = _ARTIFACT_VERSION_RE.search(text)
    if not match:
        return None
    return match.group(1).strip()


def ticket_version(base_version: str, ticket_id: str) -> str:
    """Literal `{artifact.version}-{TICKET}` e.g. 3.60.0-mqtt-WFDR-25182."""
    ticket = ticket_id.strip().replace("/", "-")
    return f"{base_version}-{ticket}"


def libs_versions_toml_path(b2b_cfg: AppConfig) -> Path:
    return b2b_cfg.clone_path / "gradle" / "libs.versions.toml"


def read_payzy_shared_pin(b2b_cfg: AppConfig) -> str | None:
    path = libs_versions_toml_path(b2b_cfg)
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8", errors="replace")
    match = _PAYZY_SHARED_RE.search(text)
    return match.group(2) if match else None


def pin_b2b_payzy_shared(b2b_cfg: AppConfig, version: str) -> MavenLocalResult:
    path = libs_versions_toml_path(b2b_cfg)
    if not path.is_file():
        return MavenLocalResult(success=False, error=f"Missing {path}")
    text = path.read_text(encoding="utf-8", errors="replace")
    match = _PAYZY_SHARED_RE.search(text)
    if not match:
        return MavenLocalResult(success=False, error="payzyShared pin not found in libs.versions.toml")
    original = match.group(2)
    # Avoid re.sub backref ambiguity when version starts with digits (\13…).
    new_text = text[: match.start(2)] + version + text[match.end(2) :]
    path.write_text(new_text, encoding="utf-8")
    return MavenLocalResult(success=True, version=version, original_pin=original)


def restore_b2b_payzy_shared(b2b_cfg: AppConfig, original_pin: str) -> MavenLocalResult:
    return pin_b2b_payzy_shared(b2b_cfg, original_pin)


def m2_version_dirs(version: str) -> list[Path]:
    base = Path.home() / ".m2" / "repository" / "gr" / "payzy" / "shared"
    if not base.is_dir():
        return []
    return [p for p in base.rglob(version) if p.is_dir()]


def delete_m2_version(version: str) -> list[str]:
    removed: list[str] = []
    for path in m2_version_dirs(version):
        try:
            shutil.rmtree(path)
            removed.append(str(path))
        except OSError:
            continue
    return removed


def publish_payzyshared(payzy_cfg: AppConfig, ticket_id: str, timeout: float = 900.0) -> MavenLocalResult:
    base = read_artifact_version(payzy_cfg)
    if not base:
        return MavenLocalResult(success=False, error="Could not read artifact.version from payzyshared gradle.properties")
    version = ticket_version(base, ticket_id)
    argv = ["./gradlew", "publishToMavenLocal", f"-Partifact.version={version}"]
    try:
        result = run_command(argv, cwd=payzy_cfg.clone_path, timeout=timeout)
    except RunnerError as exc:
        return MavenLocalResult(success=False, version=version, error=str(exc))
    if not result.ok:
        return MavenLocalResult(
            success=False,
            version=version,
            error=redact((result.stderr or result.stdout or "publishToMavenLocal failed").strip()),
        )
    return MavenLocalResult(success=True, version=version)


def cleanup_maven_local(
    version: str | None,
    b2b_cfg: AppConfig | None,
    original_pin: str | None,
) -> dict:
    """Restore toml pin and delete m2 artifacts for the ticket version."""
    restored = False
    if b2b_cfg is not None and original_pin:
        result = restore_b2b_payzy_shared(b2b_cfg, original_pin)
        restored = result.success
    removed = delete_m2_version(version) if version else []
    return {"restored_pin": restored, "removed_m2": removed, "version": version}
