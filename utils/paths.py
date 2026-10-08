"""Path confinement helper shared by platform.py and maestro.py.

Both modules build file paths from configuration *and* from values that
ultimately trace back to app source/flow content, so every path gets
checked against an explicit allowed root before being opened, read, or
passed to a subprocess - never trusting that a relative path a config
or flow file supplies stays inside its own directory.
"""

from __future__ import annotations

from pathlib import Path


class PathSecurityError(Exception):
    """Raised when a resolved path falls outside its allowed root directory."""


def ensure_within(path: Path, allowed_root: Path) -> Path:
    """Resolve `path` and confirm it is `allowed_root` or inside it.

    Returns the resolved path on success; raises PathSecurityError
    otherwise. Resolution (symlink/`..` normalization) happens before the
    containment check, so a crafted `../` segment cannot escape the root.
    """
    resolved = Path(path).resolve()
    root = Path(allowed_root).resolve()

    if resolved != root and root not in resolved.parents:
        raise PathSecurityError(f"Path {resolved} is outside the allowed directory {root}")

    return resolved
