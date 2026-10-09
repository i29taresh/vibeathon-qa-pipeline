# QA Pipeline

A platform-agnostic Android/iOS QA pipeline: automates `test -> RCA -> ticket -> fix -> retest -> PR`,
orchestrated with LangGraph. Every node reads its target app's details from a
per-app config file (`apps/<name>.yaml`) - nothing is hardcoded to one
application, repo, or build command.

For the full architecture (state shape, each agent's internals, the safety
model for real-mode writes), see **[CLAUDE.md](CLAUDE.md)**. This file
covers day-to-day commands and setup.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt   # runtime deps + pytest
```

### Required environment variables (real mode only)

| Variable | Used by | Required? |
|---|---|---|
| `CURSOR_API_KEY` | `utils/llm.py` and `dev_agent` via the Cursor SDK (`cursor-sdk`, local agent runtime) | Yes, for any real-mode run that reaches `qa_agent`'s visual check, `rca_agent`, or `dev_agent` |
| `CURSOR_MODEL` | Optional model id for Cursor agents (default: `composer-2.5`) | No |
| `GH_TOKEN` or `GITHUB_TOKEN` | the `gh` CLI, for non-interactive auth (e.g. in CI) | Only if you haven't already run `gh auth login` interactively |

Mock mode (the default) needs neither - it never calls the network, an LLM, or `gh`.

### Required external CLIs (real mode only)

These must be installed and on `PATH`; `python main.py --app <name> --preflight` checks all of them for you.

| Tool | Used for |
|---|---|
| `git` | branch/commit/push in `dev_agent` and `merge_step` |
| `gh` (GitHub CLI, authenticated) | issues/PRs in `ticket_agent` and `merge_step` |
| `cursor-sdk` (Python package) | local Cursor agents for RCA/QA LLM and `dev_agent` code edits |
| `maestro` | running UI flows in `qa_agent`/`retest_agent` |
| `adb` (Android) or `xcrun` (iOS) | device/emulator/simulator access |

## Commands

```bash
# Run the full test suite
python -m pytest -v

# Run a single test
python -m pytest tests/test_graph.py::test_retry_limit_is_respected

# --- Mock mode (default) - no network, no LLM, no gh, no device ---
python main.py --app sample_android --mock-scenario fix_success
python main.py --app sample_ios --mock-scenario always_fail
# --mock-scenario: initial_pass | fix_success | retry_success | always_fail (default: fix_success)

# --- Real mode - invokes the actual QA/RCA/Ticket/Dev/Retest/PR agents ---
python main.py --app <name> --mode real --flow <flow_file.yaml>

# Preview what ticket_agent/merge_step would publish, without ever calling
# gh or pushing a branch:
python main.py --app <name> --mode real --flow <flow_file.yaml> --dry-run

# Bound the whole run's wall-clock time (seconds):
python main.py --app <name> --mode real --flow <flow_file.yaml> --timeout 1800

# Change the dev/retest retry budget (default 3):
python main.py --app <name> --mode real --flow <flow_file.yaml> --max-attempts 5

# Read-only diagnostic before attempting a real run: checks config,
# source isolation, required CLIs on PATH, GitHub auth/permissions,
# device availability, test-command configuration, and reference assets.
# Never builds, installs, edits, or touches a real application.
python main.py --app <name> --preflight

# Bootstrap (clone/warm build/repo map) then force if needed:
python main.py --app b2b_android --bootstrap
```

### Lean Android POC (Jira → Maestro → local commit)

Android-only path for `b2b_android` + `payzyshared`: no GitLab push/MR.

1. Set env: `JIRA_*`, `CURSOR_API_KEY`, `MAESTRO_EMAIL`, `MAESTRO_PASSWORD` (OTP in the generated flow is hardcoded `0000`).
2. Emulator already running; bootstrap once: `python main.py --app b2b_android --bootstrap`.
3. Dashboard **Run pipeline**: mode `real`, primary `b2b_android`, paste a Jira key (required). Or CLI with `--jira-issue KEY-123`.
4. Pipeline: ensure `develop` → Jira STR → `local/runs/<KEY>/repro.yaml` (login + STR) → RCA → reuse Jira ticket → uncommitted fix on `ai-fix/<KEY>` → optional `publishToMavenLocal` `{artifact.version}-{TICKET}` + pin b2b → build/install DeOdsPre-prodDebug → Maestro + after-video verifier → on pass, **local commit** + maven cleanup + history under `local/runs/<KEY>/`.
5. Inspect **Test results** / **History** in the dashboard (after video + verifier comments + branch/sha).

## Configuring a new app

Copy `apps/_template.yaml` to `apps/<name>.yaml` and fill in the fields for
your target app (see the template's comments for what each one means).
Two things to get right:

- **`clone_path` must resolve outside this repository** (e.g.
  `"../workspace/<name>"`) - real mode refuses to run otherwise
  (`graph.ensure_source_isolated`), since this pipeline builds, edits,
  commits, and pushes inside `clone_path`.
- **`repo` is the target app's `owner/repository`** on GitHub - never a
  literal used anywhere in node code.

`apps/sample_android.yaml` and `apps/sample_ios.yaml` are placeholder
configs (never actually cloned or built) used by the test suite and the
demo scripts under `tests/_*_demo.py`.

## Current status

Every agent (`qa`, `rca`, `ticket`, `dev`, `retest`, and the PR-opening
`merge_step`) has both a mock implementation (Phase 3, the default) and a
real implementation (Phase 4, `--mode real`). What's intentionally **not**
yet connected, pending Phase 5:

- No real application has been cloned or built through this pipeline end
  to end - every real-mode test and demo mocks the device/Maestro/LLM/
  Claude Code/GitHub boundary (see `tests/test_graph_real_integration.py`
  and `tests/_*_demo.py`).
- `merge_step` only opens a Pull Request; nothing in this codebase merges
  one or deploys anything.
- Tool-level timeouts (per `adb`/`gh`/`maestro`/`claude` call) use the
  sensible per-tool defaults set in `utils/*.py`; only the pipeline's
  overall wall-clock budget (`--timeout`) is configurable from the CLI today.
