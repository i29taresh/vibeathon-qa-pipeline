# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project status

Phase 2 (per-app configuration), Phase 3 (LangGraph orchestrator with mock agents), Phase 4 Steps 1-7 (real-tool integrations plus real `qa_agent`/`rca_agent`/`ticket_agent`/`dev_agent`/`retest_agent`/`merge_step`), and Phase 4's final integration step (real mode wired into `graph.py`/`main.py`, see "Real-mode orchestration" below) are all implemented. `main.py` now exposes `--mode mock|real`, `--dry-run`, `--timeout`, `--max-attempts`, and `--preflight`. Not yet done (explicitly deferred to Phase 5): no real application has actually been cloned/built through this pipeline - every real-mode test and demo mocks the device/Maestro/LLM/Claude-Code/GitHub boundary (`tests/test_graph_real_integration.py` is the broadest of these: a full `graph.run_pipeline()` run against a real disposable git repo + a real local bare "origin" remote, with only those external calls mocked).

## Commands

```bash
source .venv/bin/activate

# Run the full test suite
python -m pytest -v

# Run a single test
python -m pytest tests/test_config.py::test_load_valid_android_config
python -m pytest tests/test_graph.py::test_retry_limit_is_respected
python -m pytest tests/test_utils_platform.py::test_build_app_success

# Run the mock pipeline for a configured app
python main.py --app sample_android --mock-scenario fix_success
python main.py --app sample_ios --mock-scenario always_fail
# --mock-scenario: initial_pass | fix_success | retry_success | always_fail (default: fix_success)

# Real mode, dry-run, timeout, and the read-only preflight diagnostic - see README.md for the full list.
python main.py --app <name> --mode real --flow <flow.yaml> --dry-run
python main.py --app <name> --preflight
```

Dependencies: `requirements.txt` (runtime: PyYAML, pydantic, langgraph, anthropic) and `requirements-dev.txt` (adds pytest). Install with `pip install -r requirements-dev.txt`. See **README.md** for required environment variables (`ANTHROPIC_API_KEY`, `GH_TOKEN`/`GITHUB_TOKEN`) and external CLIs (`git`, `gh`, `claude`, `maestro`, `adb`/`xcrun`) needed for real mode - none of that is needed to run the test suite, which mocks every external call.

## Per-app configuration (Phase 2)

Every pipeline node must get its app-specific details (repo, paths, build/install commands, device, package/bundle id) from `apps/<app_name>.yaml`, loaded via `config.load_app_config(name)` — never hardcode them in node logic. `apps/_template.yaml` documents the schema; `apps/sample_android.yaml` and `apps/sample_ios.yaml` are dummy configs (no real app has been chosen yet, and these are never cloned or built) used for development and tests.

- `load_app_config(name, project_root=...)` reads `apps/<name>.yaml`, validates required top-level fields, validates only the `device.<platform>`/`build.<platform>` block matching the declared platform (Android: Java/Kotlin/XML/Compose all share one Gradle-module shape; iOS: Swift/SwiftUI/UIKit share one Xcode scheme/workspace shape), resolves `clone_path`/`flows_dir`/`reference_dir` relative to the project root, and returns a pydantic `AppConfig`. It raises `ConfigError` with a specific message for missing fields, unsupported platforms, and malformed YAML — and **never executes a shell command**; `build_command`/`install_command` are stored as plain strings for later nodes to run.
- The optional `project_root` param exists so tests can point at a `tmp_path` fixture instead of the real `apps/` directory.

## LangGraph orchestrator (Phase 3)

- `state.py` — `PipelineState` (a `TypedDict`) plus `initial_state()` and `log_node()`. `execution_history` uses `Annotated[list, operator.add]` so every node returns a single-item list and LangGraph appends it, rather than each node having to read-then-append the full history itself.
- `nodes/` — one file per agent (`qa_agent`, `rca_agent`, `ticket_agent`, `dev_agent`, `retest_agent`, `merge_step`), each a plain `state -> dict` function. Mock mode (the `mock_scenarios.py` lookup tables: `initial_pass`, `fix_success`, `retry_success`, `always_fail`) is unchanged and still the default for every node, never randomness, so a node file never needs to know which app or platform it's running against. All six now also have real implementations (Phase 4 Steps 2-7, below), selected per-node via `state["mode"]`.
- `graph.py` — the only place the graph shape is defined. `qa_agent` passing routes straight to `END`; failing routes to `rca_agent -> ticket_agent -> dev_agent -> retest_agent`. `retest_agent` passing routes to `merge_step -> END`; failing with attempts remaining loops back to `rca_agent` (same chain, so `ticket_agent` reuses the existing `ticket_id` instead of filing a duplicate); failing with `attempt_count >= max_attempts` routes to `END` with `status=needs_human_review` (set by `retest_agent`, read by the router).
- When real agents replace the remaining mocks, they should keep the same `state -> dict` node signature and keep reading everything app/platform-specific from `state["app_config"]` (a `config.AppConfig`) — `graph.py` and `state.py` should not need to change.

## Shared tools and integrations (Phase 4 Step 1)

`utils/` holds the real-execution building blocks that a non-mock `nodes/` implementation calls into. As of Phase 4 Step 2, `qa_agent` is the only node that does so (in `state["mode"] == "real"`); the rest of `nodes/` still only reads `mock_scenarios.py`, keeping simulated and real execution strictly separate per node until each one is migrated.

- `utils/runner.py` — the only place `subprocess.run` is called. `run_command(argv, cwd, timeout, env)` always takes an argv list (never `shell=True`, never a shell string) and returns a `CommandResult` with `returncode`/`stdout`/`stderr`/`timed_out`/`.ok`. Every other `utils/*.py` module calls `run_command` instead of `subprocess` directly, so there's one spot to audit for command-injection safety.
- `utils/paths.py` — `ensure_within(path, allowed_root)` resolves a path and raises `PathSecurityError` if it falls outside `allowed_root`. Used by `platform.py` and `maestro.py` before touching any path derived from config or a flow name, so a crafted `../` can't escape `clone_path`/`flows_dir`.
- `utils/secrets.py` — `redact(text)` strips known secret env var values (`ANTHROPIC_API_KEY`, `GITHUB_TOKEN`, `GH_TOKEN`, `OPENAI_API_KEY`) and common token shapes (`sk-ant-...`, `ghp_...`, ...) out of any text before it's logged, stored in a result, or sent as part of an LLM prompt. Called from `platform.py`, `maestro.py`, `github.py`, and `llm.py` wherever subprocess/API output or user-supplied text gets surfaced.
- `utils/platform.py` — the single dispatch point per concern that the project goal calls for: `select_device`, `capture_logs`, `build_app`, `install_app`. Each is one function, called the same way regardless of platform; all android-vs-ios branching is contained inside these four functions. `build_app`/`install_app` run the `build_command`/`install_command` already specified in `AppConfig.build` (so custom Gradle modules/variants or Xcode workspace/scheme are whatever the app's own `apps/<name>.yaml` says — nothing is hardcoded here) and validate `artifact_path` stays inside `clone_path` via `ensure_within`.
- `utils/maestro.py` — `run_flow(app_config, flow_name, device_id, runs_root)` runs one flow from `flows_dir` and saves its JUnit report + screenshots under a fresh `runs_root/<app_name>/<flow>-<uuid>/` directory per call (never reused across runs).
- `utils/llm.py` — `get_client()` (a cached `anthropic.Anthropic`) and `ask_for_json(prompt, images=...)`, which accepts text and/or image paths and returns an `LLMResult(success, data, raw_text, error)` — timeouts, API errors, and non-JSON/non-object replies all come back as `success=False` with an `error` string rather than raising.
- `utils/github.py` — `create_issue`/`get_issue`/`comment_on_issue`/`update_issue`/`create_pull_request`, all via the `gh` CLI, plus `parse_issue_reference`/`parse_pr_reference` for pulling an issue/PR number out of a URL or `#123` reference. GitHub has no attachment-upload endpoint via `gh`/the REST API, so `attachments=[...]` just validates each file exists and references it by name/path in the issue/PR body (`format_attachments`) — real binary upload is a separate later step.
- All five modules return structured result objects (dataclasses) with a `success`/`.ok` flag instead of raising for ordinary failures (bad exit code, timeout, malformed JSON) — callers branch on the result instead of wrapping every call in `try`/`except`.

## Real qa_agent (Phase 4 Step 2)

`nodes/qa_agent.py` dispatches on `state["mode"]` (default `"mock"`, set by `state.initial_state`): `"mock"` is the unchanged Phase 3 behavior; `"real"` runs the pipeline below and returns a `QAFinding` (as a dict, under `state["qa_finding"]`) with `flow_name, passed, failure_type, description, expected_behavior, actual_behavior, confidence, reproduction_steps, evidence_paths`. `failure_type` is one of `none | functional | visual | infrastructure | unverified`.

- **Validate flow exists** — `flows_dir/<flow_name>` is checked with `ensure_within` + `is_file()` before anything else runs; missing or path-escaping flow names become `failure_type="infrastructure"`.
- **Load references** — acceptance criteria come from `reference_dir/<flow_stem>/{acceptance_criteria,criteria}.{md,txt}` (first match wins) and expected screenshots from `reference_dir/<flow_stem>/screenshots/*.png`. If no criteria text is found (file missing, or present but blank), the node returns `failure_type="unverified"`, `confidence=0.0` **immediately — without selecting a device or running Maestro** — rather than inventing what "expected behavior" should be.
- **Select device** — `utils.platform.select_device`; a failure here (emulator not booted, simulator not found, ...) is `failure_type="infrastructure"`, not a bug in the app.
- **Run Maestro** — `utils.maestro.run_flow`. If it couldn't even execute (`exit_code is None`, e.g. the `maestro` binary is missing), that's `failure_type="infrastructure"` too.
- **Parse the JUnit report** (`xml.etree.ElementTree`, stdlib only) into per-step pass/fail — this is the *only* source of truth for functional pass/fail. A flow is never `passed=True` if any step failed or Maestro exited non-zero, no matter what the LLM says (`confidence=1.0` on a functional failure/pass, since it's deterministic).
- **LLM visual comparison** (`utils.llm.ask_for_json`) runs only when criteria text *and* at least one actual screenshot exist, and only ever contributes a `failure_type="visual"` verdict *layered on top of* an already-functionally-passing run (a `visual_mismatch` verdict never flips a functional pass to a functional fail, and never flips a functional fail to a pass) — this is how functional and visual regressions stay distinguished per the "don't infer functional correctness from screenshots alone" rule.
- `qa_agent` never files a ticket or edits source — it only reads/runs/reports.

## Real rca_agent (Phase 4 Step 3)

`nodes/rca_agent.py` dispatches on `state["mode"]` the same way as `qa_agent`. In `"real"` mode it reads `state["qa_finding"]` (produced by the real `qa_agent`) and returns an `RCAFinding` (as a dict, under `state["rca_finding"]`) with `root_cause_hypothesis, suspected_files, suspected_methods, supporting_evidence, confidence, suggested_fix, missing_information`.

- **No qa_finding, or `failure_type="unverified"`** — returns `confidence=0.0` immediately ("Unresolved - insufficient evidence...") without touching a device, the repo, or an LLM, rather than inventing a cause for a failure that was never actually confirmed.
- **`failure_type="infrastructure"`** — returns a finding explaining it was an infra problem, not an app bug, `confidence=0.9`, `suspected_files=[]`; also skips all repo/device analysis.
- **Otherwise (`functional`/`visual`)**, four tools gather evidence, each independently importable and unit-tested:
  - `get_device_logs(app_config)` — `utils.platform.select_device` + `capture_logs`, tail-truncated to the most recent ~8000 chars (the lines nearest the failure).
  - `search_repository(app_config, query)` — plain-Python recursive text search under `clone_path` for `.kt/.java/.swift/.m/.mm/.h/.xml` files (works for a Gradle tree or an Xcode tree without assuming either layout), returning only the matching line + its file/line number, bounded by `max_matches`/`max_files_scanned`. Search terms come from `_extract_search_terms`, which only lifts quoted substrings/identifier-looking words out of the QA finding's own text — it never invents a term.
  - `inspect_git_diff(app_config)` — read-only `git diff --name-only HEAD~N` + `git diff HEAD~N` (working tree vs. N commits back, so it covers uncommitted changes too), diff text truncated to ~6000 chars. Returns `success=False` cleanly if `clone_path` isn't a git repo.
  - `read_relevant_files(app_config, paths)` — reads only the specific files `search_repository`/`inspect_git_diff` already found, capped at 200 lines/file and 8 files; silently skips a path that doesn't exist or escapes `clone_path` rather than inventing a stand-in.
  - If literally none of the four produced anything, it returns an unresolved finding without calling the LLM.
  - All gathered text (logs, diff, file excerpts, search hit lines) is redacted via `utils.secrets.redact` before it reaches the LLM prompt or the returned finding — repo content and logs are treated as untrusted.
- **Evidence-grounding filter (the key anti-hallucination step)**: after the LLM responds, every entry in its `suspected_files` is checked against the set of paths this run actually gathered (`search_repository` hits ∪ `inspect_git_diff` changed files ∪ `read_relevant_files` results); every entry in `suspected_methods` is checked against the literal text of files that were read. Anything that doesn't match is **dropped** from the finding and recorded in `missing_information` instead ("Model referenced file/method '...', which was not found in the gathered evidence; dropped."), and dropping anything caps `confidence` at 0.5 — this is enforced in code, not just prompted for. See `tests/_rca_demo.py` for a worked example with a planted hallucination.
- `rca_agent` never writes to the repository or runs anything beyond read-only `git diff`/log capture.

## Real ticket_agent (Phase 4 Step 4)

`nodes/ticket_agent.py` dispatches on `state["mode"]` the same way as `qa_agent`/`rca_agent`. In `"real"` mode it reads `state["qa_finding"]` + `state["rca_finding"]`, builds a structured Markdown bug report, and files/updates a GitHub Issue via `utils/github.py` against **`app_config.repo`** — never a hardcoded repository. On success it persists both `state["ticket_id"]` (the issue number, as a string) and `state["ticket_url"]`; the full result (title/body/labels/attachments, or why nothing was published) is always under `state["ticket_finding"]`.

- **Skips filing anything** when there's no confirmed app defect: no `qa_finding`, `failure_type="none"` (QA passed), `failure_type="unverified"` (no acceptance criteria), or `failure_type="infrastructure"` (unless `state["file_ticket_for_infrastructure"]=True` is explicitly set) — `status="ticket_skipped"`, reason in `ticket_finding["reason"]`, no `gh` call made.
- **Retry (`state["ticket_id"]` already set)** calls `comment_on_issue` with a shorter "Retry update - attempt #N" body (QA result, RCA hypothesis/confidence, unresolved questions) instead of `create_issue` — never a duplicate. A non-numeric `ticket_id` (e.g. a leftover mock ID) fails cleanly rather than guessing.
- **`state["dry_run"]=True`** renders the full title/body (or the retry comment) and returns it under `ticket_finding`, `status="ticket_dry_run"` — `utils.github.check_auth`/`create_issue`/`comment_on_issue` are never called at all. See `tests/_ticket_demo.py` for a worked example.
- **Auth/permission check**: `utils.github.check_auth(repo, cwd)` runs `gh auth status` then `gh repo view --json viewerPermission` before every create/comment. Not authenticated, or a confirmed non-write permission, both fail with `status="ticket_failed"` + `last_error` **without calling `gh issue create`/`comment`** — an undetermined permission (the probe itself failed for an unrelated reason) is not treated as a hard block; the real create/comment call is left to be the final word.
- **Any `gh` failure** (auth, permissions, non-zero exit) sets `status="ticket_failed"` + `last_error`, but `ticket_finding["title"]`/`["body"]` still hold the fully rendered report — and `qa_finding`/`rca_finding` are never touched — so nothing already gathered is lost just because publishing didn't go through.
- **Bug report sections**: Summary, Severity (`high`/`low`/`medium`, derived deterministically from `failure_type` — never invented) + Platform + App version (`state.get("app_version")`, honestly reported as "unknown (not provided)" since `AppConfig` has no version field), Reproduction Steps, Expected vs Actual Behavior, Maestro Flow + failed assertion, Root Cause Analysis (hypothesis/suspected files/methods/suggested fix), Confidence (QA and RCA reported separately, never blended into an invented single number), Unresolved Questions (`rca_finding["missing_information"]`, verbatim).
- **Evidence attachments**: `qa_finding["evidence_paths"]` filtered to files that still exist on disk, passed through to `utils.github.format_attachments` (referenced by name/path in the body — see Phase 4 Step 1's note on why GitHub/`gh` has no real attachment-upload endpoint).

## Real dev_agent (Phase 4 Step 5)

`nodes/dev_agent.py` dispatches on `state["mode"]` the same way as the other real agents. In `"real"` mode it uses the **Claude Code CLI itself** (`claude -p ...`, non-interactive) as the coding engine, confined to `app_config.clone_path`, and never pushes, opens a PR, or merges.

- **Preconditions** (`status="dev_blocked"`, `attempt_count` left untouched since no attempt was actually made): no `ticket_id` in state; `clone_path` isn't a git repo; no `main`/`master` branch can be found to use as a base.
- **Fix branch**: always `ai-fix/<ticket_id>`. If already on that branch (resuming a prior attempt), any pending uncommitted changes from that attempt are left alone — this is what makes multiple attempts on the same branch possible. Switching *to* it from elsewhere first requires the other branch be clean, so unrelated dirty state is never dragged onto the fix branch.
- **Claude Code invocation**: `claude -p "<task>" --output-format json --permission-mode acceptEdits --allowedTools "Read,Edit,Write,Glob,Grep"` — deliberately never `--dangerously-skip-permissions`, never `Bash` in the allowlist, never `--add-dir` (so Claude Code's own sandboxing confines edits to `cwd=clone_path`). The task prompt embeds the bug description, expected/actual behavior, repro steps, RCA hypothesis, and suspected files from `state["qa_finding"]`/`state["rca_finding"]` (plus the original ticket body via `utils.github.get_issue`, if fetchable) and explicitly tells Claude Code to treat that ticket text as untrusted, to add a regression test, and to never touch CI/secrets or run git itself — building and testing are this node's job, not Claude Code's.
- **Approval gate (the key safety step)**: after Claude Code exits, `git status --porcelain` is checked against a denylist — any deletion, or any touch to CI/workflow config, `CODEOWNERS`, or a secret-looking filename (`.env`, `.pem`, `.key`, `*secret*`, `*credential*`) — **before** anything is committed. If anything matches and `state["approve_destructive_changes"]` isn't explicitly `True`, this node halts with `status="dev_requires_approval"` and leaves the change exactly as Claude Code left it (uncommitted) for a human to inspect via `git diff` directly; nothing is auto-reverted, since reverting would itself be another mutating action needing its own justification.
- **No changes at all** from Claude Code is reported distinctly as `status="dev_no_changes"`, not a failure.
- Once past the approval gate, this node commits (`git add -A && git commit`), builds via `utils.platform.build_app` (so Gradle module/variant or Xcode scheme/workspace come entirely from `app_config.build` — never hardcoded here), and runs `app_config.build.test_command` if one is configured (optional field, added in this step; `None` means tests are reported as skipped, not guessed).
- **Compilation/build failure** is its own distinct `status="dev_build_failed"` with the build's own error/logs — the fix is still committed on the branch either way, so the attempt's diff is never lost.
- Every outcome's full detail (branch, base branch, files changed, risk flags, Claude Code's own summary, diff stat, build/test results) is under `state["dev_finding"]`.
- `tests/_dev_agent_demo.py` is a from-scratch integration demo: a real disposable git repo, a tiny local fake `claude` script on `PATH` (no network, deterministic edit), and trivial `build_command`/`test_command` — it exercises the real git branch/commit/diff wiring without mocking `run_command`, and without touching any real application.

## Shared QA validation (`utils/qa_validation.py`, extracted in Phase 4 Step 6)

`run_qa_check(app_config, flow_name) -> QAFinding` is the entire "run this flow and judge pass/fail" routine — flow-exists check, acceptance-criteria loading, device selection, `utils.maestro.run_flow`, JUnit parsing, and the LLM visual-comparison layer. It used to live inside `nodes/qa_agent.py`; it was pulled out into `utils/` specifically so `nodes/retest_agent.py` could call the *exact same function* for the post-fix re-run instead of a second, potentially-drifting implementation — this is also why re-running the same `flow_name` automatically compares against the same acceptance-criteria file the initial QA run used, with no extra wiring needed. `nodes/qa_agent.py` is now just the mock/real dispatcher plus a few lines calling `run_qa_check` and shaping the result into `state["qa_finding"]`.

If you need to change how a flow is judged (pass/fail rules, JUnit parsing, the LLM prompt), change it once in `utils/qa_validation.py` — both `qa_agent` and `retest_agent` pick it up automatically. Tests that mock Maestro/device/LLM for either agent patch `utils.qa_validation.{select_device,run_flow,ask_for_json}`, not the node module, since that's where the real call sites now live.

## Real retest_agent (Phase 4 Step 6)

`nodes/retest_agent.py` dispatches on `state["mode"]` the same way as the other real agents. In `"real"` mode it re-verifies a fix dev_agent produced, never touches GitHub (no import of `utils/github.py` at all, so it's structurally incapable of filing a duplicate ticket), and never pushes/merges.

- **Validates the patch first** (`status="retest_failed"` or `needs_human_review`, see below — never silently skipped): no `dev_finding`, a `dev_finding["error"]`, or `dev_finding["build"]["success"]` not `True` all mean there's nothing to retest yet, and this node says so without attempting a build.
- **Builds and installs fresh** via `utils.platform.build_app`/`select_device`/`install_app` — this is a new build/install, not a reuse of dev_agent's own compile-check, since dev_agent never installed or ran anything on a device.
- **Re-runs the originally failing flow, then any `app_config.regression_flows`**, each through `utils.qa_validation.run_qa_check` (see above) — never a re-implementation.
- **Pass** requires the original flow to have passed *and* every configured regression flow to have passed; `status="retest_passed"`, `passed=True`, full evidence (both flows' screenshots/reports) under `state["retest_finding"]`, and `graph.py` routes on to `merge_step`.
- **Fail** picks the most relevant finding to focus on: the original flow's finding if it's still failing, otherwise the first regression flow that broke (the original fix is fine; something else regressed) — distinguishing "defect still present" from "new regression" per the requirement. That finding's `failure_type` (`functional`/`visual`/`infrastructure`/`unverified`) is what carries the infra-vs-defect distinction; the top-level `status` only ever says `retest_failed` or `needs_human_review` (attempts exhausted) so `graph.py`'s routing vocabulary never changes shape.
- **`state["qa_finding"]` is overwritten with the focus finding on failure** — this is "keep failed-fix evidence available for the next RCA attempt": the next `rca_agent` pass reads `state["qa_finding"]` exactly like it does after the initial `qa_agent` run, so it analyzes the freshest failure automatically.
- **Does not increment `attempt_count` itself** — `dev_agent` already does, once per genuine fix attempt (the contract the mock pipeline and `graph.py`'s 3-attempt budget are built on); incrementing it again here would silently double-count attempts. `retest_agent` only *reads* `attempt_count`/`max_attempts` to decide `retest_failed` vs `needs_human_review`.
- `tests/_retest_agent_demo.py` runs the same flow twice with mocked Maestro output — attempt #1 still fails (defect persists), attempt #2 passes (fix confirmed) — the same shape as the `retry_success` mock scenario, through the real validation path.

## Real merge_step (Phase 4 Step 7)

`nodes/merge_step.py` dispatches on `state["mode"]` the same way as the other real agents. Despite the node's historical name, in `"real"` mode it **only opens a Pull Request** against `app_config.repo` — it never merges, and never pushes to `main`/`master`.

- **Preconditions, checked before touching git or GitHub at all** (`status="merge_blocked"` with a specific reason each time, never generic): retest must have actually passed (`state["passed"]` and `status=="retest_passed"`); `dev_finding` must be error-free with a successful build and non-empty `files_changed`; `retest_finding["build"]["success"]` must be true; `retest_finding["original_defect_resolved"]` must be true with no `new_regressions`; the branch must start with `ai-fix/`.
- **Branch-drift check**: `git diff base...HEAD` on the real branch is compared against `dev_finding["files_changed"]` — any extra file not already reported blocks the PR ("the fix branch contains only the intended changes").
- **Clean-tree check**: this node never commits content itself — a dirty working tree at merge time is unexpected (dev_agent should already have committed everything) and blocks rather than folding unknown changes into the PR.
- **Two-tier approval for pushing**: `dev_finding["risk_flags"]` (set by dev_agent at commit time) being non-empty means pushing *also* requires an explicit, separate `state["approve_push"]=True` — approving a risky local commit and approving pushing it to a shared remote are different decisions.
- **Never force-pushes**, and explicitly refuses to push anything literally named `main`/`master` even if a branch were somehow misconfigured to be one.
- **Duplicate-PR prevention, two layers**: a fast in-state check (`state["pr_url"]` already set skips everything, including the auth check) and, before creating, `utils.github.get_pull_request_for_branch` against GitHub itself (covers a PR created by an earlier, interrupted run).
- **Auth/permission check**: `utils.github.check_auth` — not authenticated or confirmed read-only both block with an explicit, specific `last_error` before any push or `gh pr create` call.
- **`state["dry_run"]=True`** still runs every validation check above (so the preview is accurate against the real branch) but stops before pushing or calling `gh` — `status="merge_dry_run"`, full title/body under `merge_finding`. See `tests/_merge_step_demo.py`.
- **PR body sections**: `Fixes #<ticket_id>`, Root Cause (`rca_finding`), Fix Summary (`dev_finding["claude_summary"]` + diff stat), Tests Executed (dev_agent's test run + retest's primary/regression results), Before/After Evidence (`qa_finding["evidence_paths"]` vs. `retest_finding["primary_result"]["evidence_paths"]`), Remaining Limitations (`rca_finding["missing_information"]` plus any approved risk flags, verbatim — never invented).
- On success, `state["pr_url"]`/`state["pr_number"]` are persisted and `status="ready_for_review"`.

## Real-mode orchestration (Phase 4's final integration step)

This step only touched `graph.py`, `state.py`, and `main.py` - every node file from Steps 2-7 is unchanged. `state.initial_state` now validates `mode` against `state.VALID_MODES = ("mock", "real")` and raises `ValueError` on anything else, rather than silently accepting a typo.

- **Routing now distinguishes "confirmed defect" from "not fixable by writing code"** - something the per-node mock/real dispatch couldn't do on its own, since routing lives in `graph.py`:
  - `_route_after_qa`: `qa_finding["failure_type"]` of `"infrastructure"` or `"unverified"` routes straight to `END` (no `rca_agent`, no ticket) instead of the old unconditional "any failure goes to rca_agent." `"functional"`/`"visual"` (or mock mode, where `qa_finding` is simply absent) still goes to `rca_agent` as before.
  - `_route_after_retest`: an `"infrastructure"` failure routes to `END` immediately, *before* checking `attempt_count` - retrying a fix for a problem that isn't in the code would just burn the retry budget pointlessly. A confirmed defect still follows the existing `retry`/`needs_human_review` logic (unchanged, since that logic already lived in `retest_agent` and still does).
  - Both functions fall back to the exact Phase 3 pass/status-based logic whenever `qa_finding` is absent (mock mode, or a real run that never got that far) - Phase 3's mock scenarios are provably unaffected (`tests/test_graph.py`, unchanged, still 15/15 green).
  - Note: since node files weren't touched, the node-reported `status` string itself still only ever says e.g. `"bug_detected"` for *any* qa failure (infra included) - `main.py`'s summary additionally prints `qa_finding.failure_type` so the real reason is visible, and anything reading `final_state` programmatically should check `failure_type`, not just `status`, to tell "blocked" apart from "confirmed defect."
- **Source isolation** (`graph.ensure_source_isolated`): in real mode, `run_pipeline` refuses to proceed - raising `PipelineSafetyError` before building or invoking the graph at all - if `app_config.clone_path` resolves to this orchestrator repo itself or any ancestor/descendant of it. This caught a real issue: `apps/sample_android.yaml`/`sample_ios.yaml`'s original `clone_path` (`"workspace/sample-android-app"`, relative to this repo's root per Phase 2's resolution rule) resolved *inside* this repo - both were fixed to `"../workspace/<name>"`.
- **Pipeline-level timeout** (`run_pipeline(state, timeout=...)`, or `state["timeout_seconds"]`): bounds total wall-clock time via `pipeline.stream(state, stream_mode="values")` plus a `time.monotonic()` deadline, rather than `pipeline.invoke`. On expiry, the most recently streamed full state is returned with `status="pipeline_timeout"` - `execution_history`/findings/evidence gathered so far are preserved, never fabricated. (Per-tool timeouts - one `adb`/`gh`/`maestro`/`claude` call - still use the sensible defaults already set in each `utils/*.py` function; wiring a configurable value down to each of those would mean editing the node files, out of scope for this integration step.)
- **Dry-run** (`--dry-run` / `state["dry_run"]`) and the **two approval gates** (`approve_destructive_changes`, `approve_push`) were already implemented in `ticket_agent`/`dev_agent`/`merge_step` (Steps 4/5/7) - this step just exposes `--dry-run` on the CLI; nothing new needed in `graph.py`/`state.py` beyond what already existed.
- **`main.py`** gained `--mode {mock,real}` (explicit, no implicit default behavior beyond "mock"), `--flow` (real mode's target flow file), `--dry-run`, `--timeout`, `--max-attempts`, and `--preflight`.
- **`--preflight`** (`main.py`'s `_run_preflight`) is read-only and never builds/installs/edits/pushes: it checks source isolation, required CLIs on `PATH` (`gh`, `claude`, `maestro`, plus `adb`/`xcrun`), GitHub auth + write access (`utils.github.check_auth`), device availability (`utils.platform.select_device`, advisory - a device not being booted yet is a warning, not a blocker), whether `app_config.build.test_command` is configured (advisory), and whether each flow under `flows_dir` (plus `regression_flows`) has a matching acceptance-criteria file under `reference_dir` (advisory). Exit code is 0 unless a *critical* check (isolation, dependencies, auth) failed.
- **`tests/test_graph_routing.py`** unit-tests the routing functions, `ensure_source_isolated`, and the timeout mechanism directly. **`tests/test_graph_real_integration.py`** is the full end-to-end proof: for both `sample_android` and `sample_ios`, a real disposable git repo (plus a real local bare "origin" so `dev_agent`/`merge_step`'s branch-push machinery genuinely runs) is driven through `graph.run_pipeline()` with only device/Maestro/LLM/Claude-Code/GitHub mocked - QA fails, the first fix attempt still fails retest, the second passes, and a PR is "opened." It asserts the ticket is created exactly once (reused via comment on retry) and the fix branch is created exactly once (reused, never recreated) - requirement #7, verified at the full-graph level, not just per-node.

## Goal

A **platform-agnostic Android/iOS QA pipeline**: automate the loop `test -> RCA -> ticket -> fix -> retest`. The pipeline is not tied to any one demo app — the target app has not even been chosen yet. This has one hard architectural consequence:

- **Every node must read its target from a per-app config file.** Never hardcode a repo name, test-flow path, or build command anywhere in node logic. If a node needs to know "which app, which flow, which build command," it reads that from config, not from a literal.

## Stack

- **Python + LangGraph** — orchestrates the pipeline as a graph of agent nodes.
- **Anthropic SDK** — LLM calls (e.g. RCA reasoning, ticket drafting).
- **Maestro CLI** — cross-platform UI driving for both Android and iOS; this is the whole reason the pipeline can stay platform-agnostic at the test layer.
- **GitHub Issues** — the ticket tracker.
- **Claude Code headless** — the dev agent that actually writes fixes.

## Pipeline architecture

### State

A single shared state object flows through the graph with (at least) these fields:
`app_name, ticket_id, screenshots, attempt_count, last_error, status`.

### Agents / nodes

- `qa_agent` — runs the test flow (via Maestro) against the configured app.
- `rca_agent` — performs root-cause analysis on a failure (screenshots, logs, last_error).
- `ticket_agent` — files/updates a GitHub Issue from the RCA output.
- `dev_agent` — invokes Claude Code headless to implement a fix for the ticket.
- `retest_agent` — reruns the test flow after a fix.
- `merge_step` — merges the fix once retest passes.

### Control flow

`retest_agent` loops back to `rca_agent` on failure, up to **3 attempts** (tracked via `attempt_count`), then stops and hands off to a human rather than looping forever.

### Platform differences

Android/iOS differences — device boot, log capture, build/install — must be isolated behind **a single dispatch point per concern**. Never branch on platform inline inside node logic; each concern (boot, logs, build/install) gets one dispatcher that the nodes call into, keeping the graph nodes themselves platform-agnostic.
