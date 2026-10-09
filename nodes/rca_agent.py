"""RCA agent: turns a QA failure into a grounded root-cause hypothesis.

Two modes, selected by `state["mode"]` (default "mock", set by
state.initial_state) - same pattern as nodes/qa_agent.py:

- "mock" - the Phase 3 placeholder, unchanged.
- "real" - gathers device logs, repository search hits, a bounded git diff,
  and bounded source excerpts, then asks utils/llm.py for a hypothesis. See
  RCAFinding below for the structured result shape.

Four tools, each independently testable and each bounded in what it reads:
    get_device_logs()    - utils.platform.select_device + capture_logs, tail-truncated
    search_repository()  - plain-text search across clone_path, line-level hits only
    inspect_git_diff()   - `git diff` against a recent ref, truncated
    read_relevant_files() - a handful of specific files, line-capped

None of the four ever writes anything - this node only reads evidence and
reports on it. Repository content, logs, and diffs are all treated as
untrusted text: secrets are redacted before they reach the LLM prompt or the
returned finding, and after the LLM responds, `suspected_files` and
`suspected_methods` are checked against the evidence actually gathered -
anything the model names that doesn't appear in that evidence is dropped
rather than trusted, so this node never reports a file/stack
trace/function that wasn't actually seen.
"""

from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from config import AppConfig, figma_context_lines
from state import PipelineState, log_node
from utils.llm import ask_for_json
from utils.paths import PathSecurityError, ensure_within
from utils.platform import capture_logs, select_device
from utils.runner import RunnerError, run_command
from utils.secrets import redact

_SOURCE_EXTENSIONS = {".kt", ".kts", ".java", ".swift", ".m", ".mm", ".h", ".xml"}
_MAX_FILE_BYTES_SCANNED = 512_000
_MAX_LINE_LENGTH = 300
_MAX_LOG_CHARS = 8_000
_MAX_DIFF_CHARS = 6_000
_MAX_LINES_PER_FILE = 200
_MAX_FILES_READ = 8
_MAX_EVIDENCE_IMAGES = 3
# The agent greps/reads the repos itself, so this is slower than a one-shot call.
_RCA_AGENT_TIMEOUT = float(os.environ.get("RCA_AGENT_TIMEOUT", "420"))
_RCA_AGENT_TOOLS = ["read", "grep", "glob", "ls", "semSearch"]
_MAX_VERIFY_BYTES = 400_000
_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
_SEARCH_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "is", "are", "was", "were", "to",
    "of", "in", "on", "for", "with", "this", "that", "it", "not", "be", "as", "at",
}


# --------------------------------------------------------------------------
# Structured result
# --------------------------------------------------------------------------

@dataclass
class RCAFinding:
    root_cause_hypothesis: str
    suspected_files: list[str] = field(default_factory=list)
    suspected_methods: list[str] = field(default_factory=list)
    supporting_evidence: list[str] = field(default_factory=list)
    confidence: float = 0.0
    suggested_fix: str = ""
    missing_information: list[str] = field(default_factory=list)
    target_app: str = ""


@dataclass
class RepoMatch:
    file_path: str
    line_number: int
    line_text: str
    repo_app: str = ""


@dataclass
class GitDiffResult:
    success: bool
    diff_text: str = ""
    changed_files: list[str] = field(default_factory=list)
    error: Optional[str] = None


@dataclass
class DeviceLogsResult:
    success: bool
    logs: str = ""
    device_id: Optional[str] = None
    error: Optional[str] = None


# --------------------------------------------------------------------------
# Node entry point
# --------------------------------------------------------------------------

def rca_agent(state: PipelineState) -> dict:
    if state.get("mode", "mock") == "mock":
        return _run_mock(state)
    return _run_real(state)


# --------------------------------------------------------------------------
# Mock mode (Phase 3 - unchanged)
# --------------------------------------------------------------------------

def _run_mock(state: PipelineState) -> dict:
    print("\n[rca_agent] Analyzing failure (mock)...")

    attempt_count = state.get("attempt_count", 0)
    flow_name = state.get("flow_name", "default_flow")
    root_cause = f"Mock RCA: flow '{flow_name}' failed on a simulated assertion mismatch."
    suspected_files = ["mock/suspected_file_1", "mock/suspected_file_2"]
    status = "rca_complete"
    history = log_node("rca_agent", status, attempt_count, detail=root_cause)

    return {
        "root_cause": root_cause,
        "suspected_files": suspected_files,
        "status": status,
        "execution_history": [history],
    }


# --------------------------------------------------------------------------
# Real mode
# --------------------------------------------------------------------------

def _run_real(state: PipelineState) -> dict:
    app_config: AppConfig = state["app_config"]
    attempt_count = state.get("attempt_count", 0)
    qa_finding = state.get("qa_finding")

    print(f"\n[rca_agent] Analyzing failure for '{app_config.name}' ({app_config.platform})...")

    if not qa_finding:
        finding = _unresolved_finding(["No QA finding was available in state - nothing to analyze."])
        return _finalize(finding, attempt_count)

    failure_type = qa_finding.get("failure_type")

    if failure_type == "infrastructure":
        finding = RCAFinding(
            root_cause_hypothesis=(
                "The QA run failed because of an infrastructure problem "
                f"({qa_finding.get('description', 'unspecified')}), not an application defect. "
                "No source-code analysis was performed."
            ),
            confidence=0.9,
            missing_information=["Re-run QA once the infrastructure issue is resolved to get an app-level result."],
        )
        return _finalize(finding, attempt_count)

    if failure_type == "unverified":
        finding = _unresolved_finding(
            ["QA result was unverified (no acceptance criteria were available), so there is no confirmed failure to analyze."]
        )
        return _finalize(finding, attempt_count)

    project_graph: dict[str, AppConfig] = state.get("project_graph") or {
        state.get("app_name", app_config.name): app_config
    }
    primary_stem = state.get("app_name", app_config.name)
    finding = _analyze_app_failure(primary_stem, project_graph, qa_finding)
    return _finalize(finding, attempt_count)


def _finalize(finding: RCAFinding, attempt_count: int) -> dict:
    status = "rca_complete"
    history = log_node("rca_agent", status, attempt_count, detail=finding.root_cause_hypothesis)
    return {
        "root_cause": finding.root_cause_hypothesis,
        "suspected_files": finding.suspected_files,
        "status": status,
        "rca_finding": asdict(finding),
        "execution_history": [history],
    }


def _unresolved_finding(missing_information: list[str]) -> RCAFinding:
    return RCAFinding(
        root_cause_hypothesis="Unresolved - insufficient evidence to determine a root cause.",
        confidence=0.0,
        missing_information=missing_information,
    )


def _evidence_id(repo_app: str, relative_path: str) -> str:
    return f"{repo_app}::{relative_path}" if repo_app else relative_path


def _analyze_app_failure(
    primary_stem: str,
    project_graph: dict[str, AppConfig],
    qa_finding: dict[str, Any],
) -> RCAFinding:
    missing_information: list[str] = []
    primary_config = project_graph.get(primary_stem) or next(iter(project_graph.values()))

    print("[rca_agent] capturing device logs...", flush=True)
    logs_result = get_device_logs(primary_config)
    if not logs_result.success:
        missing_information.append(f"Could not capture device logs: {logs_result.error}")
        print(f"[rca_agent] logs unavailable: {logs_result.error}", flush=True)
    else:
        print(f"[rca_agent] logs captured ({len(logs_result.logs)} chars)", flush=True)

    search_hits: list[RepoMatch] = []
    terms = _extract_search_terms(qa_finding)[:8]
    print(f"[rca_agent] searching {len(project_graph)} repo(s) for {len(terms)} term(s)...", flush=True)
    for repo_app, cfg in project_graph.items():
        for term in terms:
            for hit in search_repository(cfg, term):
                search_hits.append(
                    RepoMatch(
                        file_path=hit.file_path,
                        line_number=hit.line_number,
                        line_text=hit.line_text,
                        repo_app=repo_app,
                    )
                )
    if not search_hits:
        missing_information.append("No repository search hits for the terms extracted from the QA finding.")

    diff_by_repo: dict[str, GitDiffResult] = {}
    for repo_app, cfg in project_graph.items():
        diff_by_repo[repo_app] = inspect_git_diff(cfg)
        if not diff_by_repo[repo_app].success:
            missing_information.append(
                f"Could not inspect git history for '{repo_app}': {diff_by_repo[repo_app].error}"
            )

    file_excerpts: dict[str, str] = {}
    for repo_app, cfg in project_graph.items():
        diff_result = diff_by_repo[repo_app]
        repo_hits = {hit.file_path for hit in search_hits if hit.repo_app == repo_app}
        candidate_paths = sorted(repo_hits | set(diff_result.changed_files))
        for rel_path, text in read_relevant_files(cfg, candidate_paths).items():
            file_excerpts[_evidence_id(repo_app, rel_path)] = text

    gathered_evidence_paths: set[str] = set(file_excerpts.keys())
    for hit in search_hits:
        gathered_evidence_paths.add(_evidence_id(hit.repo_app, hit.file_path))
    for repo_app, diff_result in diff_by_repo.items():
        for path in diff_result.changed_files:
            gathered_evidence_paths.add(_evidence_id(repo_app, path))

    if not logs_result.success and not search_hits and not file_excerpts:
        missing_information.append("No logs, repository matches, git history, or file excerpts could be gathered.")
        return _unresolved_finding(missing_information)

    screenshots = _evidence_images(qa_finding)
    llm_data = _ask_llm_for_root_cause(
        primary_stem,
        project_graph,
        qa_finding,
        logs_result,
        search_hits,
        diff_by_repo,
        file_excerpts,
        screenshots,
    )

    return _build_finding(
        llm_data,
        gathered_evidence_paths,
        file_excerpts,
        missing_information,
        primary_stem,
        project_graph,
    )


def _build_finding(
    llm_data: Optional[dict[str, Any]],
    gathered_evidence_paths: set[str],
    file_excerpts: dict[str, str],
    missing_information: list[str],
    primary_stem: str,
    project_graph: dict[str, AppConfig],
) -> RCAFinding:
    missing = list(missing_information)

    if llm_data is None:
        missing.append("The LLM analysis could not be completed or returned no usable result.")
        return RCAFinding(
            root_cause_hypothesis="Unresolved - automated analysis did not produce a usable hypothesis.",
            confidence=0.0,
            missing_information=missing,
        )

    target_app = str(llm_data.get("target_app") or primary_stem)
    if target_app not in project_graph:
        missing.append(
            f"Model named target_app '{target_app}', which is not in the project graph; using primary '{primary_stem}'."
        )
        target_app = primary_stem

    # Grounding: a cited file must either be part of the pre-gathered evidence
    # or genuinely exist inside one of the repositories the agent could search.
    # Existence is checked on disk, so a real file the agent found by grepping
    # is kept, while an invented path is still dropped.
    raw_files = llm_data.get("suspected_files") or []
    verified_files: list[str] = []
    verified_paths: list[Path] = []
    dropped_files: list[str] = []
    for f in raw_files:
        if not isinstance(f, str) or not f.strip():
            continue
        relative = f.split("::", 1)[1] if "::" in f else f
        if _evidence_id(target_app, relative) in gathered_evidence_paths:
            verified_files.append(relative)
            resolved = _resolve_in_repos(relative, target_app, project_graph)
            if resolved:
                verified_paths.append(resolved)
            continue
        resolved = _resolve_in_repos(relative, target_app, project_graph)
        if resolved is not None:
            verified_files.append(relative)
            verified_paths.append(resolved)
        else:
            dropped_files.append(f)
    for dropped in dropped_files:
        missing.append(f"Model referenced file '{dropped}', which does not exist in any configured repository; dropped.")

    searched_text = "\n".join(file_excerpts.values())
    if verified_paths:
        searched_text += "\n" + _read_for_verification(verified_paths)
    raw_methods = llm_data.get("suspected_methods") or []
    verified_methods = [m for m in raw_methods if _method_in_text(m, searched_text)]
    dropped_methods = [m for m in raw_methods if m not in verified_methods]
    for dropped in dropped_methods:
        missing.append(f"Model referenced method/function '{dropped}', which was not found in any cited file; dropped.")

    confidence = _clamp_confidence(llm_data.get("confidence"))
    if dropped_files or dropped_methods:
        # Hallucinated references lower our trust in the whole hypothesis.
        confidence = min(confidence, 0.5)

    missing.extend(str(m) for m in (llm_data.get("missing_information") or []))

    return RCAFinding(
        root_cause_hypothesis=str(llm_data.get("root_cause_hypothesis") or "Unresolved - no hypothesis returned."),
        suspected_files=verified_files,
        suspected_methods=verified_methods,
        supporting_evidence=[str(e) for e in (llm_data.get("supporting_evidence") or [])],
        confidence=confidence,
        suggested_fix=str(llm_data.get("suggested_fix") or ""),
        missing_information=missing,
        target_app=target_app,
    )


def _method_in_text(method: Any, text: str) -> bool:
    """A method may be cited as `Class.method`; the source only contains `method`."""
    if not isinstance(method, str) or not method.strip():
        return False
    if method in text:
        return True
    name = method.split("(", 1)[0].rstrip().rsplit(".", 1)[-1]
    return bool(name) and name in text


def _resolve_in_repos(
    relative_path: str,
    target_app: str,
    project_graph: dict[str, AppConfig],
) -> Optional[Path]:
    """Return the absolute path of `relative_path` if it exists inside a configured repo."""
    ordered = [target_app, *(k for k in project_graph if k != target_app)]
    for key in ordered:
        cfg = project_graph.get(key)
        if cfg is None:
            continue
        root = cfg.clone_path
        if not root.is_dir():
            continue
        try:
            candidate = ensure_within(root / relative_path, root)
        except PathSecurityError:
            continue
        if candidate.is_file():
            return candidate
    return None


def _read_for_verification(paths: list[Path], max_bytes: int = _MAX_VERIFY_BYTES) -> str:
    """Read cited files so suspected_methods can be checked against their real text."""
    chunks: list[str] = []
    budget = max_bytes
    for path in paths:
        if budget <= 0:
            break
        try:
            text = path.read_text(encoding="utf-8", errors="replace")[:budget]
        except OSError:
            continue
        budget -= len(text)
        chunks.append(text)
    return redact("\n".join(chunks))


def _clamp_confidence(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


# --------------------------------------------------------------------------
# Tool 1: get_device_logs
# --------------------------------------------------------------------------

def get_device_logs(app_config: AppConfig, max_chars: int = _MAX_LOG_CHARS) -> DeviceLogsResult:
    """Select the configured device and capture its current log buffer, bounded in size."""
    device_result = select_device(app_config)
    if not device_result.success:
        return DeviceLogsResult(success=False, error=f"Could not select a device: {device_result.error}")

    device_id = device_result.details.get("device_id")
    logs_result = capture_logs(app_config, device_id)
    if not logs_result.success:
        return DeviceLogsResult(success=False, device_id=device_id, error=f"Could not capture logs: {logs_result.error}")

    logs = logs_result.logs  # already redacted by utils.platform
    if len(logs) > max_chars:
        # Keep the tail - log output is chronological, so the lines nearest
        # the failure (most relevant) are at the end.
        logs = "... [log truncated]\n" + logs[-max_chars:]

    return DeviceLogsResult(success=True, logs=logs, device_id=device_id)


# --------------------------------------------------------------------------
# Tool 2: search_repository
# --------------------------------------------------------------------------

def search_repository(
    app_config: AppConfig,
    query: str,
    extensions: Optional[set[str]] = None,
    max_matches: int = 10,
    max_files_scanned: int = 2000,
) -> list[RepoMatch]:
    """Case-insensitive text search for `query` across source files under clone_path.

    Walks the whole tree (no assumption about module/folder layout - works
    the same for a Gradle module tree or an Xcode project tree), skips
    anything outside clone_path, skips large/binary-looking files, and stops
    after max_matches/max_files_scanned so this never loads the whole repo
    into memory.
    """
    if not query or not app_config.clone_path.is_dir():
        return []
    # Resolved once up front so it stays consistent with the resolved paths
    # ensure_within() hands back below (clone_path may contain a symlink
    # segment, e.g. macOS's /var -> /private/var, which would otherwise make
    # relative_to() fail even for a path that's genuinely inside it).
    root = app_config.clone_path.resolve()

    allowed_extensions = extensions or _SOURCE_EXTENSIONS
    query_lower = query.lower()
    matches: list[RepoMatch] = []
    files_scanned = 0

    skip_dirs = {".git", "build", ".gradle", ".idea", "node_modules", "captures", "Pods"}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in skip_dirs]
        if files_scanned >= max_files_scanned or len(matches) >= max_matches:
            break
        for filename in filenames:
            path = Path(dirpath) / filename
            if files_scanned >= max_files_scanned or len(matches) >= max_matches:
                break
            if path.suffix.lower() not in allowed_extensions:
                continue
            try:
                resolved = ensure_within(path, root)
            except PathSecurityError:
                continue
            try:
                if resolved.stat().st_size > _MAX_FILE_BYTES_SCANNED:
                    continue
            except OSError:
                continue

            files_scanned += 1
            try:
                with resolved.open("r", encoding="utf-8", errors="replace") as handle:
                    for line_number, line in enumerate(handle, start=1):
                        if query_lower in line.lower():
                            matches.append(
                                RepoMatch(
                                    file_path=str(resolved.relative_to(root)),
                                    line_number=line_number,
                                    line_text=redact(line.strip()[:_MAX_LINE_LENGTH]),
                                )
                            )
                            if len(matches) >= max_matches:
                                break
            except OSError:
                continue

    return matches


def _extract_search_terms(qa_finding: dict[str, Any]) -> list[str]:
    """Pull candidate identifiers/keywords out of text the QA agent itself produced.

    Never invents a term: quoted substrings first (e.g. "Element not found:
    LoginButton" -> LoginButton), then identifier-looking words as a
    fallback, both lifted verbatim from the QA finding's own text.
    """
    text = " ".join(str(qa_finding.get(key, "")) for key in ("description", "actual_behavior", "expected_behavior"))

    terms: list[str] = []
    for match in re.findall(r'"([^"]+)"|\'([^\']+)\'|`([^`]+)`', text):
        term = next((g for g in match if g), "").strip()
        if term:
            terms.append(term.split(":")[-1].strip())

    for word in re.findall(r"[A-Za-z_][A-Za-z0-9_]{3,}", text):
        if word.lower() in _SEARCH_STOPWORDS:
            continue
        if "_" in word or any(c.isupper() for c in word):
            terms.append(word)

    seen: set[str] = set()
    unique_terms = []
    for term in terms:
        if term and term not in seen:
            seen.add(term)
            unique_terms.append(term)
    return unique_terms[:5]


# --------------------------------------------------------------------------
# Tool 3: inspect_git_diff
# --------------------------------------------------------------------------

def inspect_git_diff(app_config: AppConfig, max_commits: int = 1, max_chars: int = _MAX_DIFF_CHARS) -> GitDiffResult:
    """Read-only summary of recent modifications: changed files + a bounded unified diff.

    Diffs the working tree against `HEAD~max_commits`, so this covers both
    the last committed change and any uncommitted changes in one comparison
    - either could be what introduced a regression. Never commits, stages,
    or otherwise mutates the repository.
    """
    root = app_config.clone_path
    if not root.is_dir() or not (root / ".git").is_dir():
        return GitDiffResult(success=False, error=f"No git repository found at {root}")

    ref = f"HEAD~{max_commits}"
    try:
        name_result = run_command(["git", "diff", "--name-only", ref], cwd=root, timeout=30.0)
    except RunnerError as exc:
        return GitDiffResult(success=False, error=str(exc))
    if not name_result.ok:
        return GitDiffResult(success=False, error=redact(name_result.stderr.strip() or "git diff failed"))

    changed_files = sorted({line.strip() for line in name_result.stdout.splitlines() if line.strip()})

    try:
        diff_result = run_command(["git", "diff", ref], cwd=root, timeout=30.0)
    except RunnerError as exc:
        return GitDiffResult(success=False, changed_files=changed_files, error=str(exc))

    diff_text = redact(diff_result.stdout)
    if len(diff_text) > max_chars:
        diff_text = diff_text[:max_chars] + "\n... [diff truncated]"

    return GitDiffResult(success=True, diff_text=diff_text, changed_files=changed_files)


# --------------------------------------------------------------------------
# Tool 4: read_relevant_files
# --------------------------------------------------------------------------

def read_relevant_files(
    app_config: AppConfig,
    relative_paths: list[str],
    max_lines_per_file: int = _MAX_LINES_PER_FILE,
    max_files: int = _MAX_FILES_READ,
) -> dict[str, str]:
    """Read bounded excerpts of specific files, never anything outside clone_path.

    Silently skips a path that doesn't exist or escapes clone_path rather
    than inventing a stand-in for it - callers can tell a file was skipped
    because it's simply absent from the returned dict.
    """
    root = app_config.clone_path
    excerpts: dict[str, str] = {}

    for relative_path in relative_paths[:max_files]:
        try:
            resolved = ensure_within(root / relative_path, root)
        except PathSecurityError:
            continue
        if not resolved.is_file():
            continue

        lines: list[str] = []
        try:
            with resolved.open("r", encoding="utf-8", errors="replace") as handle:
                for _ in range(max_lines_per_file):
                    line = handle.readline()
                    if not line:
                        break
                    lines.append(line)
        except OSError:
            continue

        excerpts[relative_path] = redact("".join(lines))

    return excerpts


# --------------------------------------------------------------------------
# LLM analysis
# --------------------------------------------------------------------------

def _evidence_images(qa_finding: dict[str, Any]) -> list[Path]:
    images = []
    for raw_path in qa_finding.get("evidence_paths") or []:
        path = Path(raw_path)
        if path.suffix.lower() in _IMAGE_EXTENSIONS and path.is_file():
            images.append(path)
    return images[:_MAX_EVIDENCE_IMAGES]


def _ask_llm_for_root_cause(
    primary_stem: str,
    project_graph: dict[str, AppConfig],
    qa_finding: dict[str, Any],
    logs_result: DeviceLogsResult,
    search_hits: list[RepoMatch],
    diff_by_repo: dict[str, GitDiffResult],
    file_excerpts: dict[str, str],
    screenshots: list[Path],
) -> Optional[dict[str, Any]]:
    primary_config = project_graph.get(primary_stem) or next(iter(project_graph.values()))
    repo_list = ", ".join(f"{k} ({v.clone_path})" for k, v in project_graph.items())
    figma_lines = figma_context_lines(primary_config)
    sections = [
        f"Primary app under test: {primary_config.name} ({primary_config.platform}), config key '{primary_stem}'",
        f"Repositories in scope: {repo_list}",
    ]
    if figma_lines:
        sections.append("Figma design references:\n" + "\n".join(figma_lines))
    sections += [
        f"Flow: {qa_finding.get('flow_name')}",
        f"QA failure type: {qa_finding.get('failure_type')}",
        f"QA description: {qa_finding.get('description')}",
        f"Expected behavior: {qa_finding.get('expected_behavior')}",
        f"Actual behavior: {qa_finding.get('actual_behavior')}",
        f"Reproduction steps: {qa_finding.get('reproduction_steps')}",
    ]

    if logs_result.success:
        sections.append(f"Device logs (most recent, truncated):\n{logs_result.logs}")
    else:
        sections.append(f"Device logs: unavailable ({logs_result.error})")

    if search_hits:
        hit_lines = "\n".join(
            f"{hit.repo_app}:{hit.file_path}:{hit.line_number}: {hit.line_text}" for hit in search_hits
        )
        sections.append(f"Repository search hits:\n{hit_lines}")
    else:
        sections.append("Repository search hits: none found")

    for repo_app, diff_result in diff_by_repo.items():
        if diff_result.success:
            sections.append(
                f"Repo '{repo_app}' recently changed files: {', '.join(diff_result.changed_files) or 'none'}\n"
                f"Diff (truncated):\n{diff_result.diff_text}"
            )
        else:
            sections.append(f"Repo '{repo_app}' git history: unavailable ({diff_result.error})")

    if file_excerpts:
        excerpt_blocks = "\n\n".join(f"--- {path} ---\n{text}" for path, text in file_excerpts.items())
        sections.append(f"Source excerpts:\n{excerpt_blocks}")

    evidence_block = "\n\n".join(sections)

    repo_roots = "\n".join(f"- {key}: {cfg.clone_path}" for key, cfg in project_graph.items())
    from utils.project_context import load_project_repo_maps

    repo_map = load_project_repo_maps(project_graph)
    repo_map_block = (
        f"Project structure (from bootstrap repo map — use this to jump to the right modules "
        f"instead of rediscovering layout):\n{repo_map}\n\n"
        if repo_map
        else ""
    )
    prompt = (
        "You are a root-cause-analysis assistant for a mobile QA pipeline.\n\n"
        "You have read-only tools (grep, glob, ls, read, semantic search) over these checked-out "
        f"repositories:\n{repo_roots}\n\n"
        f"{repo_map_block}"
        "Investigate the failure in the source code before answering. Do not stop at the pre-gathered "
        "keyword hits below - they are only a starting point and are often shallow (for example they may "
        "match a strings resource rather than the screen that renders the UI). Work from the user-visible "
        "symptom to the code: search for the feature/screen name, the components it renders, the state "
        "that drives them, and the styling/theme that maps state to appearance. Open the files you find "
        "and read enough of them to judge the cause.\n\n"
        "Rules: only cite files you actually opened, and only cite functions/classes you actually saw in "
        "them. Never guess a path. If, after searching, you still cannot locate the relevant code, say so "
        "and set confidence to 0.\n\n"
        f"{evidence_block}\n\n"
        "Respond with ONLY a JSON object with these keys: "
        f'"target_app" (string, one of: {", ".join(project_graph.keys())} — the repo that needs the fix), '
        '"root_cause_hypothesis" (string), "suspected_files" (array of strings, repo-relative paths you '
        'opened), "suspected_methods" (array of strings: function, class, or composable names you saw in '
        'those files), "supporting_evidence" (array of short strings quoting what you read), '
        '"confidence" (number from 0 to 1), "suggested_fix" (string, a high-level description only, no code), '
        '"missing_information" (array of strings describing what would help confirm this).'
    )

    result = ask_for_json(
        prompt,
        images=screenshots or None,
        cwd=primary_config.clone_path,
        timeout=_RCA_AGENT_TIMEOUT,
        tools=_RCA_AGENT_TOOLS,
        # Screenshots live outside the checkouts; without their directory the
        # agent cannot open the evidence images referenced in the prompt.
        dirs=[cfg.clone_path for cfg in project_graph.values()]
        + [shot.parent for shot in screenshots],
    )
    if not result.success or not isinstance(result.data, dict):
        print(f"[rca_agent] analysis unavailable: {result.error}", flush=True)
        return None
    return result.data
