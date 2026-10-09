"""Git helpers for POC workspace hygiene (checkout base branch, patch, commit, reset)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from config import AppConfig
from utils.runner import CommandResult, RunnerError, run_command
from utils.secrets import redact


@dataclass
class GitWorkspaceResult:
    success: bool
    detail: str = ""
    error: str | None = None


def _git(argv: list[str], root: Path, timeout: float = 60.0) -> CommandResult:
    try:
        return run_command(argv, cwd=root, timeout=timeout)
    except RunnerError as exc:
        return CommandResult(argv=argv, returncode=-1, stdout="", stderr=str(exc))


def current_branch(root: Path) -> str | None:
    result = _git(["git", "rev-parse", "--abbrev-ref", "HEAD"], root, timeout=15.0)
    if not result.ok:
        return None
    name = result.stdout.strip()
    return name or None


def is_dirty(root: Path) -> bool:
    result = _git(["git", "status", "--porcelain"], root, timeout=15.0)
    return bool(result.ok and result.stdout.strip())


def ensure_on_base_branch(
    cfg: AppConfig,
    *,
    allow_fix_branch_prefix: str = "ai-fix/",
    ticket_id: str | None = None,
) -> GitWorkspaceResult:
    """Ensure checkout is on base_branch, unless already on this run's ai-fix branch.

    Refuses to switch when the working tree is dirty (does not wipe foreign work).
    """
    root = cfg.clone_path
    if not root.is_dir() or not (root / ".git").is_dir():
        return GitWorkspaceResult(success=False, error=f"No git repository at {root}")

    branch = current_branch(root)
    allowed_fix = f"{allow_fix_branch_prefix}{ticket_id}" if ticket_id else None
    if branch == cfg.base_branch:
        return GitWorkspaceResult(success=True, detail=f"{cfg.name}: already on {cfg.base_branch}")
    if allowed_fix and branch == allowed_fix:
        return GitWorkspaceResult(success=True, detail=f"{cfg.name}: on fix branch {branch}")

    if is_dirty(root):
        return GitWorkspaceResult(
            success=False,
            error=(
                f"{cfg.name} ({root}) is on '{branch}' with a dirty working tree. "
                f"Commit/stash those changes or reset manually, then re-run so the pipeline "
                f"can check out '{cfg.base_branch}'."
            ),
        )

    # Prefer local base, else origin/base
    start = cfg.base_branch
    if not _git(["git", "rev-parse", "--verify", "--quiet", start], root, timeout=15.0).ok:
        remote = f"origin/{cfg.base_branch}"
        if _git(["git", "rev-parse", "--verify", "--quiet", remote], root, timeout=15.0).ok:
            start = remote
        else:
            return GitWorkspaceResult(
                success=False,
                error=f"{cfg.name}: base branch '{cfg.base_branch}' not found locally or on origin.",
            )

    checkout = _git(["git", "checkout", start], root)
    if not checkout.ok:
        return GitWorkspaceResult(
            success=False,
            error=redact(checkout.stderr.strip() or f"git checkout {start} failed"),
        )
    # If we checked out origin/base, create/switch to local base name
    if start.startswith("origin/"):
        local = _git(["git", "checkout", "-B", cfg.base_branch, start], root)
        if not local.ok:
            return GitWorkspaceResult(
                success=False,
                error=redact(local.stderr.strip() or f"could not create local {cfg.base_branch}"),
            )
    return GitWorkspaceResult(success=True, detail=f"{cfg.name}: checked out {cfg.base_branch}")


def ensure_project_graph_on_base(
    project_graph: dict[str, AppConfig],
    ticket_id: str | None = None,
) -> None:
    """Raise ValueError if any repo cannot be placed on its base branch."""
    errors: list[str] = []
    for stem, cfg in project_graph.items():
        result = ensure_on_base_branch(cfg, ticket_id=ticket_id)
        if not result.success:
            errors.append(result.error or f"{stem}: unknown error")
    if errors:
        raise ValueError("Workspace not ready for pipeline:\n- " + "\n- ".join(errors))


def write_patch(root: Path, dest: Path) -> bool:
    """Write `git diff HEAD` (plus untracked via diff against empty when needed) to dest."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Include staged/unstaged tracked changes
    result = _git(["git", "diff", "HEAD"], root)
    untracked = _git(["git", "ls-files", "--others", "--exclude-standard"], root)
    parts = [result.stdout or ""]
    if untracked.ok and untracked.stdout.strip():
        for rel in untracked.stdout.splitlines():
            path = root / rel.strip()
            if path.is_file():
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                parts.append(f"diff --git a/{rel} b/{rel}\n--- /dev/null\n+++ b/{rel}\n")
                for line in text.splitlines():
                    parts.append(f"+{line}\n")
    blob = "".join(parts)
    if not blob.strip():
        blob = "# empty patch\n"
    try:
        dest.write_text(redact(blob), encoding="utf-8")
    except OSError:
        return False
    return True


def commit_paths(root: Path, message: str, paths: list[str]) -> tuple[bool, str | None, str | None]:
    """Stage paths and commit. Returns (ok, commit_sha, error)."""
    if not paths:
        return False, None, "No paths to commit"
    add = _git(["git", "add", "--", *paths], root)
    if not add.ok:
        return False, None, redact(add.stderr.strip() or "git add failed")
    commit = _git(["git", "commit", "-m", message], root)
    if not commit.ok:
        return False, None, redact(commit.stderr.strip() or "git commit failed")
    sha = _git(["git", "rev-parse", "HEAD"], root, timeout=15.0)
    return True, (sha.stdout.strip() if sha.ok else None), None


def reset_hard(root: Path) -> bool:
    result = _git(["git", "reset", "--hard", "HEAD"], root)
    clean = _git(["git", "clean", "-fd"], root)
    return result.ok and clean.ok


def working_tree_diff_stat(root: Path, max_chars: int = 4000) -> str:
    result = _git(["git", "diff", "--stat", "HEAD"], root)
    if not result.ok:
        return ""
    text = redact(result.stdout)
    return text if len(text) <= max_chars else text[:max_chars] + "\n... [truncated]"
