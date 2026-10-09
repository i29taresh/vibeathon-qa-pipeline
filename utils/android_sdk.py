"""Resolve Android SDK binaries (adb/emulator) when they are not on PATH.

Streamlit / GUI-launched processes often miss `$ANDROID_HOME/platform-tools`,
which is why retest fails with `Command not found: adb` even when the SDK
is installed under `~/Library/Android/sdk`.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path


def sdk_root() -> Path | None:
    for key in ("ANDROID_SDK_ROOT", "ANDROID_HOME"):
        raw = os.environ.get(key)
        if raw:
            path = Path(raw).expanduser()
            if path.is_dir():
                return path
    default = Path.home() / "Library" / "Android" / "sdk"
    if default.is_dir():
        return default
    return None


@lru_cache(maxsize=1)
def adb_path() -> str:
    """Absolute path to adb, or the bare name if not found (lets runner raise)."""
    root = sdk_root()
    if root is not None:
        candidate = root / "platform-tools" / "adb"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    which = _which("adb")
    return which or "adb"


@lru_cache(maxsize=1)
def emulator_path() -> str:
    root = sdk_root()
    if root is not None:
        candidate = root / "emulator" / "emulator"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    which = _which("emulator")
    return which or "emulator"


def ensure_android_tools_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """Return an env mapping with ANDROID_* and platform-tools on PATH."""
    env = dict(base if base is not None else os.environ)
    root = sdk_root()
    if root is None:
        return env
    env.setdefault("ANDROID_HOME", str(root))
    env.setdefault("ANDROID_SDK_ROOT", str(root))
    extras = [
        str(root / "platform-tools"),
        str(root / "emulator"),
        str(root / "cmdline-tools" / "latest" / "bin"),
        str(root / "tools" / "bin"),
        str(Path.home() / ".maestro" / "bin"),
    ]
    path = env.get("PATH", "")
    prefix = ":".join(p for p in extras if Path(p).is_dir())
    if prefix:
        env["PATH"] = prefix + (":" + path if path else "")
    return env


def _which(name: str) -> str | None:
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if not directory:
            continue
        candidate = Path(directory) / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


@lru_cache(maxsize=1)
def maestro_path() -> str:
    """Absolute path to Maestro CLI (`~/.maestro/bin/maestro` is the usual install)."""
    home_bin = Path.home() / ".maestro" / "bin" / "maestro"
    if home_bin.is_file() and os.access(home_bin, os.X_OK):
        return str(home_bin)
    which = _which("maestro")
    return which or "maestro"
