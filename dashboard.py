# Run with:  streamlit run dashboard.py
"""Streamlit ops dashboard for the vibeathon QA pipeline (MeinMagenta theme)."""

from __future__ import annotations

import os
import queue
import threading
import time
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

import streamlit as st
import yaml

from config import (
    AppConfig,
    ConfigError,
    dependency_adjacency,
    find_dependency_cycle,
    list_app_config_names,
    load_app_config,
    load_project_graph,
    save_app_config_yaml,
)
from graph import PipelineSafetyError, ensure_all_sources_isolated, run_pipeline
from mock_scenarios import SCENARIOS
from state import create_pipeline_state
from utils import gitlab as gitlab_api
from utils import jira as jira_api

ROOT = Path(__file__).resolve().parent

ACCENT = "#E20074"
CANVAS = "#0E1117"
NEUTRAL = "#999B9E"
CARD = "#1a1f2a"
SIDEBAR = "#141820"
WHITE = "#FFFFFF"
SUCCESS = "#28A745"
WARNING = "#FFC107"
ERROR = "#DC3545"

PIPELINE_STEPS = ["qa_agent", "rca_agent", "ticket_agent", "dev_agent", "retest_agent", "merge_step"]
FLOW_SUFFIXES = (".yaml", ".yml")

# LangGraph nodes that may run longer than a few seconds (show a “in progress” hint).
_LONG_RUNNING_NODES = frozenset({"dev_agent", "qa_agent", "retest_agent", "jira_intake_agent", "rca_agent"})


def _list_maestro_flows(cfg: AppConfig) -> list[str]:
    flows_dir = cfg.flows_dir
    if not flows_dir.is_dir():
        return []
    return sorted(p.name for p in flows_dir.iterdir() if p.is_file() and p.suffix.lower() in FLOW_SUFFIXES)


def _pipeline_step_order(jira_intake: bool) -> list[str]:
    if jira_intake:
        return ["jira_intake_agent", *PIPELINE_STEPS[1:]]  # qa_agent skipped
    return list(PIPELINE_STEPS)


def _format_elapsed(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, rem = divmod(int(seconds), 60)
    return f"{minutes}m {rem}s"


def _guess_active_node(history: list[dict], jira_intake: bool, running: bool) -> str | None:
    if not running:
        return None
    completed = {r["node"] for r in history}
    for node in _pipeline_step_order(jira_intake):
        if node not in completed:
            return node
    return "finishing"


def _render_live_progress(
    state: dict,
    elapsed_sec: float,
    running: bool,
    container: Any,
) -> None:
    history = state.get("execution_history") or []
    jira_intake = bool(state.get("jira_issue_key") or state.get("jira_issue_input"))
    active = _guess_active_node(history, jira_intake, running)
    status = state.get("status") or "pending"
    attempt = state.get("attempt_count", 0)
    max_att = state.get("max_attempts", 3)

    header = (
        f"**Elapsed:** {_format_elapsed(elapsed_sec)} · "
        f"**Attempts:** {attempt}/{max_att} · "
        f"**Status:** `{status}`"
    )
    if running and active:
        hint = " (may take several minutes)" if active in _LONG_RUNNING_NODES else ""
        header += f" · **Running:** `{active}`{hint}"

    lines = ["| Step | Status | Attempt | Detail |", "| --- | --- | --- | --- |"]
    for rec in history:
        detail = (rec.get("detail") or "").replace("|", "\\|").replace("\n", " ")
        if len(detail) > 120:
            detail = detail[:117] + "..."
        lines.append(
            f"| `{rec.get('node', '?')}` | `{rec.get('status', '')}` | "
            f"{rec.get('attempt_count', '')} | {detail or '—'} |"
        )
    if running and active and active not in {r.get("node") for r in history}:
        lines.append(f"| `{active}` | *in progress…* | {attempt} | Waiting for this step to finish |")

    container.markdown(header + "\n\n" + "\n".join(lines))


def _run_pipeline_with_live_ui(state: dict) -> tuple[dict, float]:
    """Run pipeline on a worker thread; refresh UI with elapsed time and step log."""
    result_queue: queue.Queue = queue.Queue()
    latest: dict[str, Any] = {"state": state, "elapsed": 0.0}
    started = time.monotonic()

    def on_progress(snapshot: dict, elapsed: float) -> None:
        latest["state"] = snapshot
        latest["elapsed"] = elapsed

    def worker() -> None:
        try:
            final = run_pipeline(state, on_progress=on_progress)
            result_queue.put(("ok", final))
        except Exception as exc:  # noqa: BLE001
            result_queue.put(("err", exc))

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()

    status = st.status("Agent pipeline running…", expanded=True)
    body = st.empty()
    tick = 0.0
    while thread.is_alive():
        elapsed = time.monotonic() - started
        _render_live_progress(latest["state"], max(elapsed, latest["elapsed"]), True, body)
        status.update(label=f"Agent pipeline running… {_format_elapsed(elapsed)}")
        time.sleep(0.4)
        tick = elapsed

    kind, payload = result_queue.get()
    if kind == "err":
        status.update(label="Pipeline failed", state="error")
        raise payload

    final = payload
    total = time.monotonic() - started
    _render_live_progress(final, total, False, body)
    status.update(label=f"Pipeline finished in {_format_elapsed(total)}", state="complete")
    return final, total


def _default_flow_for_app(cfg: AppConfig) -> str:
    flows = _list_maestro_flows(cfg)
    if flows:
        return flows[0]
    return "login.yaml"

SESSION_ENV_KEYS = [
    "CURSOR_API_KEY",
    "CURSOR_MODEL",
    "GITLAB_TOKEN",
    "GITLAB_API_ACCESSTOKEN",
    "JIRA_BASE_URL",
    "JIRA_EMAIL",
    "JIRA_API_TOKEN",
    "GH_TOKEN",
    "GITHUB_TOKEN",
]

CSS = f"""
<style>
.stApp {{ background-color: {CANVAS}; color: {WHITE}; }}
[data-testid="stSidebar"] {{ background-color: {SIDEBAR}; border-right: 1px solid {NEUTRAL}33; }}
.mm-header-strip {{
  background: linear-gradient(90deg, {ACCENT} 0%, #a0005a 100%);
  color: {WHITE}; padding: 18px 24px; border-radius: 6px; margin-bottom: 16px;
}}
.mm-header-strip h1 {{ margin: 0; font-size: 1.6rem; color: {WHITE}; }}
.mm-header-strip p {{ margin: 4px 0 0 0; font-size: 0.9rem; opacity: 0.9; }}
.stButton > button[kind="primary"] {{
  background-color: {ACCENT}; border: 1px solid {ACCENT}; color: {WHITE};
}}
[data-testid="stMetric"] {{
  background-color: {CARD}; border: 1px solid {NEUTRAL}66; border-radius: 6px; padding: 10px 14px;
}}
.config-box {{
  background-color: {CARD}; border: 1px solid {NEUTRAL}66; border-radius: 6px;
  padding: 10px 12px; font-size: 0.82rem; line-height: 1.6; margin: 8px 0 14px 0;
}}
.exit-banner {{
  background-color: {CARD}; border-left: 6px solid {NEUTRAL}; border-radius: 4px;
  padding: 14px 18px; margin: 12px 0;
}}
.exit-banner h3 {{ margin: 0 0 4px 0; font-size: 1.05rem; }}
.exit-banner p {{ margin: 0; color: #d0d2d6; font-size: 0.9rem; }}
.exit-success {{ border-left-color: {SUCCESS}; }} .exit-success h3 {{ color: {SUCCESS}; }}
.exit-warning {{ border-left-color: {WARNING}; }} .exit-warning h3 {{ color: {WARNING}; }}
.exit-error {{ border-left-color: {ERROR}; }} .exit-error h3 {{ color: {ERROR}; }}
.pipeline-row {{ display: flex; flex-wrap: wrap; gap: 6px; align-items: center; margin: 8px 0 4px 0; }}
.pipeline-step {{
  border: 1px solid {NEUTRAL}; color: {NEUTRAL}; border-radius: 14px;
  padding: 4px 12px; font-size: 0.8rem; font-family: monospace;
}}
.pipeline-step.done {{ border-color: {SUCCESS}; color: {SUCCESS}; }}
.pipeline-step.active {{ border-color: {ACCENT}; color: {WHITE}; background-color: {ACCENT}; }}
.pipeline-step.failed {{ border-color: {ERROR}; color: {ERROR}; }}
.pipeline-arrow {{ color: {NEUTRAL}; }}
</style>
"""


def _jsonable(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, Path):
        return str(obj)
    if hasattr(obj, "model_dump"):
        return _jsonable(obj.model_dump())
    if is_dataclass(obj) and not isinstance(obj, type):
        return _jsonable(asdict(obj))
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    return str(obj)


def _load_local_env_defaults() -> dict[str, str]:
    """Optional gitignored prefills: local/dashboard_env.yaml (never committed)."""
    path = ROOT / "local" / "dashboard_env.yaml"
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if k in SESSION_ENV_KEYS and v}


def _init_env_session() -> None:
    if "env_baseline" not in st.session_state:
        st.session_state.env_baseline = {k: os.environ.get(k) for k in SESSION_ENV_KEYS}
    if "env_overrides" not in st.session_state:
        defaults = _load_local_env_defaults()
        st.session_state.env_overrides = defaults
        for key, value in defaults.items():
            if value:
                os.environ[key] = value


def _apply_env_overrides() -> None:
    for key, value in st.session_state.get("env_overrides", {}).items():
        if value:
            os.environ[key] = value


def _clear_env_overrides() -> None:
    baseline = st.session_state.get("env_baseline", {})
    for key in SESSION_ENV_KEYS:
        original = baseline.get(key)
        if original is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = original
    st.session_state.env_overrides = {}


def page_environment() -> None:
    st.subheader("Environment variables (session only)")
    st.caption(
        "Values apply to this Streamlit process. Optional prefills load from "
        "`local/dashboard_env.yaml` (gitignored) on startup."
    )
    _init_env_session()

    overrides = st.session_state.env_overrides
    for key in SESSION_ENV_KEYS:
        current = overrides.get(key, os.environ.get(key, ""))
        overrides[key] = st.text_input(key, value=current or "", type="password" if "TOKEN" in key or "KEY" in key else "default")

    col1, col2 = st.columns(2)
    with col1:
        if st.button("Apply to session", type="primary"):
            st.session_state.env_overrides = overrides
            _apply_env_overrides()
            st.success("Session environment updated.")
    with col2:
        if st.button("Clear session overrides"):
            _clear_env_overrides()
            st.info("Restored environment from dashboard startup snapshot.")


def _load_yaml_raw(stem: str) -> dict:
    path = ROOT / "apps" / f"{stem}.yaml"
    return yaml.safe_load(path.read_text()) or {}


def page_projects() -> None:
    st.subheader("Projects")
    apps = list_app_config_names(ROOT)
    action = st.radio("Action", ["Edit existing", "Create new"], horizontal=True)

    if action == "Create new":
        stem = st.text_input("Config file name (apps/<name>.yaml)", value="my_app")
        data = yaml.safe_load((ROOT / "apps" / "_template.yaml").read_text()) or {}
    else:
        if not apps:
            st.warning("No projects yet. Create one below.")
            return
        stem = st.selectbox("Project", apps)
        try:
            data = _load_yaml_raw(stem)
        except OSError as exc:
            st.error(str(exc))
            return

    with st.form("project_form"):
        data["name"] = st.text_input("Display name", value=data.get("name", ""))
        data["platform"] = st.selectbox("Platform", ["android", "ios"], index=0 if data.get("platform") != "ios" else 1)
        data["repo"] = st.text_input("Repo path (group/sub/project)", value=data.get("repo", ""))
        data["clone_path"] = st.text_input("Clone path", value=data.get("clone_path", ""))
        data["base_branch"] = st.text_input(
            "Base branch",
            value=data.get("base_branch") or "develop",
            help="Branch new fix branches are created from, and the GitLab merge-request target.",
        )
        data["flows_dir"] = st.text_input("Flows directory", value=data.get("flows_dir", "flows/app"))
        data["reference_dir"] = st.text_input("Reference directory", value=data.get("reference_dir", "references/app"))

        st.markdown("**GitLab**")
        integ = data.get("integrations") or {}
        gl = integ.get("gitlab") or {}
        gl_host = st.text_input("GitLab host", value=gl.get("host", ""))
        gl_path = st.text_input("GitLab project path", value=gl.get("project_path", ""))

        st.markdown("**Jira**")
        jr = integ.get("jira") or {}
        jr_url = st.text_input("Jira base URL", value=jr.get("base_url", ""))
        jr_key = st.text_input("Jira project key", value=jr.get("project_key", ""))
        jr_type = st.text_input("Jira issue type", value=jr.get("issue_type", "Bug"))

        st.markdown("**Figma**")
        fg = integ.get("figma") or {}
        fg_file = st.text_input("Figma file URL", value=fg.get("file_url", ""))
        fg_frames = st.text_area(
            "Figma frame URLs (one per line)",
            value="\n".join(fg.get("frame_urls") or []),
        )

        if data.get("platform") == "android":
            dev = (data.get("device") or {}).get("android") or {}
            bld = (data.get("build") or {}).get("android") or {}
            avd = st.text_input("AVD name", value=dev.get("avd_name", ""))
            api = st.number_input("API level", min_value=1, value=int(dev.get("api_level") or 34))
            data["device"] = {"android": {"avd_name": avd, "api_level": int(api)}}
            data["build"] = {
                "android": {
                    "module": st.text_input("Gradle module", value=bld.get("module", ":app")),
                    "build_command": st.text_input("Build command", value=bld.get("build_command", "")),
                    "artifact_path": st.text_input("Artifact path", value=bld.get("artifact_path", "")),
                    "install_command": st.text_input("Install command", value=bld.get("install_command", "")),
                    "package_id": st.text_input("Package ID", value=bld.get("package_id", "")),
                    "test_command": st.text_input("Test command (optional)", value=bld.get("test_command") or ""),
                }
            }

        submitted = st.form_submit_button("Save project", type="primary")

    if submitted:
        data["integrations"] = {
            "gitlab": {"host": gl_host, "project_path": gl_path},
            "jira": {"base_url": jr_url, "project_key": jr_key, "issue_type": jr_type},
            "figma": {"file_url": fg_file, "frame_urls": [ln.strip() for ln in fg_frames.splitlines() if ln.strip()]},
        }
        try:
            save_app_config_yaml(stem, data, project_root=ROOT)
            st.success(f"Saved apps/{stem}.yaml")
        except ConfigError as exc:
            st.error(str(exc))


def page_dependency_graph() -> None:
    st.subheader("Dependency graph")
    apps = list_app_config_names(ROOT)
    if not apps:
        st.info("Create at least one project first.")
        return

    primary = st.selectbox("Primary app (pipeline target)", apps)
    cfg = load_app_config(primary, project_root=ROOT)
    known = [a for a in apps if a != primary]
    current_deps = [d.app for d in cfg.dependencies]

    selected = st.multiselect("Dependencies", known, default=[d for d in current_deps if d in known])
    rel = st.text_input("Relationship label", value="library")

    if st.button("Save dependencies"):
        data = _load_yaml_raw(primary)
        data["dependencies"] = [{"app": name, "relationship": rel} for name in selected]
        try:
            save_app_config_yaml(primary, data, project_root=ROOT)
            st.success("Dependency list saved.")
        except ConfigError as exc:
            st.error(str(exc))

    adj = dependency_adjacency(ROOT)
    cycle = find_dependency_cycle(primary, adj)
    if cycle:
        st.error(f"Cycle detected: {' → '.join(cycle)}")
    else:
        st.success("No dependency cycle through the primary app.")

    lines = ["graph LR", f'  {primary}["{primary}"]']
    for dep in adj.get(primary, []):
        lines.append(f"  {primary} --> {dep}")
    st.markdown("```mermaid\n" + "\n".join(lines) + "\n```")

    st.caption(
        "When a fix is attributed to a dependency, dev_agent and merge_step run against that repo. "
        "Retest always rebuilds the primary consumer app — publish payzyshared to mavenLocal before retest if needed."
    )


def _connection_tests(cfg: AppConfig) -> None:
    st.markdown("**Connection tests**")
    c1, c2 = st.columns(2)
    with c1:
        if st.button("Test Jira"):
            jira = cfg.integrations.jira
            if not jira or not jira.base_url:
                st.warning("Configure Jira on the project first.")
            else:
                _apply_env_overrides()
                status = jira_api.check_auth(jira.base_url, jira.project_key)
                st.write(status)
    with c2:
        if st.button("Test GitLab"):
            gl = cfg.integrations.gitlab
            if not gl or not gl.host:
                st.warning("Configure GitLab on the project first.")
            else:
                _apply_env_overrides()
                status = gitlab_api.check_auth(gl.host, gl.project_path or cfg.repo)
                st.write(status)


def page_run_pipeline() -> None:
    apps = list_app_config_names(ROOT)
    if not apps:
        st.error("No app configs in apps/.")
        return

    app_name = st.selectbox("Primary app", apps, index=apps.index("sample_android") if "sample_android" in apps else 0)
    mode = st.selectbox("Mode", ["mock", "real"], index=0)

    try:
        cfg = load_app_config(app_name, project_root=ROOT)
    except ConfigError as exc:
        st.error(str(exc))
        cfg = None

    scenario = None
    if mode == "mock":
        scenario = st.selectbox("Mock scenario", list(SCENARIOS), index=list(SCENARIOS).index("fix_success"))

    max_attempts = st.number_input("Max attempts", min_value=1, max_value=10, value=3, step=1)
    dry_run = st.checkbox("Dry run (real: no publish/push)", value=False, disabled=(mode == "mock"))

    flow = "default_flow"
    jira_issue = ""
    if mode == "real" and cfg is not None:
        flows = _list_maestro_flows(cfg)
        if flows:
            flow = st.selectbox(
                "Maestro flow (retest after fix)",
                flows,
                help="YAML file under flows_dir. Used after dev_agent to verify the fix (and for Jira+LLM verification screenshots).",
            )
        else:
            st.warning(
                f"No Maestro flows found under `{cfg.flows_dir}`. "
                "Set flows_dir in the app config or add .yaml flows there."
            )
            flow = st.text_input(
                "Maestro flow filename",
                value="login.yaml",
                help="e.g. login.yaml — must exist under the app's flows_dir.",
            )

        jira_issue = st.text_input(
            "Jira issue (optional)",
            value="",
            placeholder="WFDR-25182 or https://…/browse/WFDR-25182",
            help="When set, skips initial Maestro QA and starts from this ticket (attachments + video frames).",
        )

    if cfg is not None:
        _connection_tests(cfg)

    if st.button("Trigger Agent Pipeline", type="primary"):
        _apply_env_overrides()
        try:
            if cfg is None:
                cfg = load_app_config(app_name, project_root=ROOT)
            project_graph = None
            if mode == "real":
                project_graph = load_project_graph(app_name, project_root=ROOT)
                ensure_all_sources_isolated([c.clone_path for c in project_graph.values()])
                flow_path = cfg.flows_dir / flow
                if not flow_path.is_file():
                    alt = cfg.clone_path / Path(cfg.flows_dir.name) / flow
                    if not alt.is_file():
                        alt = cfg.clone_path / "flows" / flow
                    hint = f"Expected under `{cfg.flows_dir}`"
                    if alt != flow_path:
                        hint += f" or `{alt}`"
                    st.error(f"Maestro flow not found: `{flow}`. {hint}.")
                    st.stop()
            state = create_pipeline_state(
                app_name=app_name,
                app_config=cfg,
                mode=mode,
                mock_scenario=scenario,
                flow_name=flow.strip(),
                jira_issue_raw=jira_issue.strip() if mode == "real" else None,
                max_attempts=int(max_attempts),
                dry_run=dry_run,
                project_graph=project_graph,
            )
            final, duration_sec = _run_pipeline_with_live_ui(state)
            st.session_state["result_state"] = final
            st.session_state["last_run"] = {
                "app": app_name,
                "scenario": scenario,
                "mode": mode,
                "duration_sec": duration_sec,
            }
        except (ConfigError, PipelineSafetyError, ValueError) as exc:
            st.error(f"Pipeline could not start: {exc}")
        except Exception as exc:  # noqa: BLE001
            st.error(f"Pipeline crashed: {type(exc).__name__}: {exc}")

    final = st.session_state.get("result_state")
    if final:
        _render_results(final)


def _step_chips(final: dict) -> str:
    history = final.get("execution_history", [])
    last_by_node = {r["node"]: r["status"] for r in history}
    bad = ("failed", "blocked", "needs_human_review", "skipped")
    jira_intake = bool(final.get("jira_issue_key") or final.get("jira_issue_input"))
    steps = _pipeline_step_order(jira_intake)
    parts = []
    for step in steps:
        status = last_by_node.get(step)
        cls = ""
        if status is not None:
            cls = "failed" if any(b in status for b in bad) else "done"
        parts.append(f"<span class='pipeline-step {cls}'>{step}</span>")
    parts.append(f"<span class='pipeline-step active'>END</span>")
    return "<div class='pipeline-row'>" + "<span class='pipeline-arrow'>→</span>".join(parts) + "</div>"


def _banner(final: dict) -> tuple[str, str, str]:
    status = final.get("status", "")
    failure_type = (final.get("qa_finding") or {}).get("failure_type")
    attempts = f"{final.get('attempt_count', 0)}/{final.get('max_attempts', '?')}"
    if status == "no_bugs_found":
        return "exit-success", "SUCCESS", "QA passed on the first run."
    if status in ("ready_for_review", "merge_dry_run"):
        return "exit-success", "SUCCESS AFTER HEALING", f"Fix verified after {attempts} attempt(s)."
    if status == "needs_human_review":
        return "exit-error", "NEEDS HUMAN REVIEW", f"Still failing after {attempts} attempts."
    if failure_type in ("infrastructure", "unverified"):
        return "exit-warning", "BLOCKED", f"QA inconclusive ({failure_type})."
    if status == "pipeline_timeout":
        return "exit-warning", "TIMEOUT", "Pipeline timed out."
    if "blocked" in status or "failed" in status:
        return "exit-warning", "BLOCKED", final.get("last_error") or status
    return "exit-warning", "ENDED", f"status={status}"


def _kv(data: dict | None, keys: list[str]) -> None:
    for key in keys:
        value = (data or {}).get(key)
        if value in (None, "", [], {}):
            continue
        st.markdown(f"**{key}**")
        if isinstance(value, (dict, list)):
            st.json(_jsonable(value))
        else:
            st.write(value)


def _render_results(final: dict) -> None:
    last_run = st.session_state.get("last_run") or {}
    duration = last_run.get("duration_sec")
    if duration is not None:
        st.caption(f"Last run duration: {_format_elapsed(float(duration))}")

    css, title, msg = _banner(final)
    st.markdown(_step_chips(final), unsafe_allow_html=True)
    st.markdown(f"<div class='exit-banner {css}'><h3>{title}</h3><p>{msg}</p></div>", unsafe_allow_html=True)

    with st.expander("Execution log (all steps)", expanded=False):
        history = final.get("execution_history") or []
        if history:
            st.dataframe(
                [
                    {
                        "node": r.get("node"),
                        "status": r.get("status"),
                        "attempt": r.get("attempt_count"),
                        "detail": r.get("detail"),
                    }
                    for r in history
                ],
                use_container_width=True,
                hide_index=True,
            )
        else:
            st.write("No steps recorded.")

    tab_rca, tab_dev, tab_qa, tab_raw = st.tabs(
        ["Ticket & RCA", "Dev & MR", "QA & Retest", "Raw JSON"]
    )
    rca = final.get("rca_finding") or {}
    with tab_rca:
        st.markdown(f"**ticket_id:** `{final.get('ticket_id')}`")
        if final.get("ticket_url"):
            st.markdown(f"[Ticket]({final['ticket_url']})")
        _kv(rca, ["target_app", "root_cause_hypothesis", "suspected_files", "confidence", "suggested_fix"])
        _kv(final.get("ticket_finding"), ["title", "body", "reason"])
    with tab_dev:
        _kv(final.get("dev_finding"), ["target_app", "branch", "build", "tests"])
        if final.get("pr_url"):
            st.markdown(f"[Merge request]({final['pr_url']})")
    with tab_qa:
        _kv(final.get("qa_finding"), ["failure_type", "description", "evidence_paths"])
        st.dataframe(_jsonable(final.get("execution_history", [])), use_container_width=True)
    with tab_raw:
        st.json(_jsonable(final))


def main() -> None:
    st.set_page_config(page_title="Vibeathon QA Pipeline", page_icon="🚀", layout="wide")
    st.markdown(CSS, unsafe_allow_html=True)
    st.markdown(
        "<div class='mm-header-strip'><h1>Vibeathon QA Pipeline</h1>"
        "<p>Multi-project · GitLab · Jira · LangGraph</p></div>",
        unsafe_allow_html=True,
    )
    _init_env_session()

    page = st.sidebar.radio(
        "Navigation",
        ["Run pipeline", "Projects", "Dependency graph", "Environment variables"],
    )
    if page == "Environment variables":
        page_environment()
    elif page == "Projects":
        page_projects()
    elif page == "Dependency graph":
        page_dependency_graph()
    else:
        page_run_pipeline()


main()
