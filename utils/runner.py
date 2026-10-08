"""Safe subprocess execution for trusted CLI tools (adb, xcrun, gradlew,
xcodebuild, maestro, gh).

Every call here takes an explicit argv list (never a shell string), an
explicit working directory, and a timeout:

- `shell=False` always - no command is ever parsed by a shell, so shell
  metacharacters in any argument are inert.
- Callers build argv from fixed tool names plus validated/trusted
  arguments (resolved paths, config-provided values) - never by splicing
  untrusted text (issue bodies, repo file contents, branch names from a
  fork, ...) into a command line.

This module is the single place that actually calls subprocess.run for the
rest of utils/*.py, so there is one spot to audit for command-injection
safety.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


class RunnerError(Exception):
    """Raised when a command cannot even be started (bad args, missing binary, bad cwd)."""


@dataclass
class CommandResult:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


def run_command(
    argv: Sequence[str],
    cwd: Path,
    timeout: float = 120.0,
    env: dict[str, str] | None = None,
) -> CommandResult:
    """Run a trusted command (argv list) and capture its output.

    `cwd` must already exist. This function does not create directories or
    otherwise touch the filesystem beyond what the command itself does -
    callers are responsible for confirming `cwd` (and any paths in `argv`)
    are inside an allowed project/workspace directory before calling this.
    """
    if isinstance(argv, (str, bytes)):
        raise RunnerError("argv must be a list of strings, not a single shell string")
    argv = list(argv)
    if not argv:
        raise RunnerError("argv must not be empty")

    cwd = Path(cwd)
    if not cwd.is_dir():
        raise RunnerError(f"Working directory does not exist: {cwd}")

    try:
        completed = subprocess.run(
            argv,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
        )
    except FileNotFoundError as exc:
        raise RunnerError(f"Command not found: {argv[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        return CommandResult(
            argv=argv,
            returncode=-1,
            stdout=exc.stdout or "",
            stderr=(exc.stderr or "") + f"\n[runner] timed out after {timeout}s",
            timed_out=True,
        )

    return CommandResult(
        argv=argv,
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )
