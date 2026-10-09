# Run with:  streamlit run dashboard.py
"""Streamlit ops dashboard for the vibeathon QA pipeline (Cyberpunk Developer theme)."""

from __future__ import annotations

import html
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

ACCENT = "#FF2A6D"
CYAN = "#05D9E8"
CANVAS = "#090A0F"
NEUTRAL = "#8B93A7"
BORDER = "#1E222B"
CARD = "#13151A"
INPUT_BG = "#1A1D26"
INPUT_BORDER = "#2A2E3D"
SIDEBAR = "#13151A"
WHITE = "#E5E9F0"
SUCCESS = "#39FF88"
WARNING = "#FFC857"
ERROR = "#FF4D5E"

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


_ERR_MARKERS = ("failed", "error", "needs_human_review", "crash", "timeout")
_WARN_MARKERS = ("blocked", "skipped", "requires_approval", "no_changes", "dry_run", "unverified", "bug_detected")
_OK_MARKERS = (
    "passed", "success", "created", "updated", "fixed", "applied", "ready", "no_bugs", "complete", "done", "connected"
)


def _status_tone(status: str | None) -> str:
    s = (status or "").lower()
    if not s or s == "pending":
        return "idle"
    if "progress" in s or "running" in s:
        return "run"
    if any(m in s for m in _ERR_MARKERS):
        return "err"
    if any(m in s for m in _WARN_MARKERS):
        return "warn"
    if any(m in s for m in _OK_MARKERS):
        return "ok"
    return "info"


def _pill(status: str | None, tone: str | None = None) -> str:
    label = html.escape(str(status or "pending"))
    return f"<span class='ox-pill {tone or _status_tone(status)}'>{label}</span>"


def _section(title: str, subtitle: str = "") -> None:
    sub = f" <small>{html.escape(subtitle)}</small>" if subtitle else ""
    st.markdown(f"<div class='ox-section-title'>{html.escape(title)}{sub}</div>", unsafe_allow_html=True)


def _stats_html(items: list[tuple[str, str]]) -> str:
    cells = "".join(
        f"<div class='ox-stat'><div class='k'>{html.escape(k)}</div><div class='v'>{v}</div></div>" for k, v in items
    )
    return f"<div class='ox-stats'>{cells}</div>"


def _history_table_html(history: list[dict], active: str | None = None, attempt: int | str = "") -> str:
    rows = []
    for rec in history:
        detail = str(rec.get("detail") or "—")
        if len(detail) > 220:
            detail = detail[:217] + "..."
        rows.append(
            "<tr>"
            f"<td class='ox-node'>{html.escape(str(rec.get('node', '?')))}</td>"
            f"<td>{_pill(rec.get('status'))}</td>"
            f"<td class='ox-num'>{html.escape(str(rec.get('attempt_count', '')))}</td>"
            f"<td class='ox-detail'>{html.escape(detail)}</td>"
            "</tr>"
        )
    if active and active not in {r.get("node") for r in history}:
        rows.append(
            "<tr>"
            f"<td class='ox-node'>{html.escape(active)}</td>"
            f"<td>{_pill('in progress', 'run')}</td>"
            f"<td class='ox-num'>{html.escape(str(attempt))}</td>"
            "<td class='ox-detail'>Waiting for this step to finish</td>"
            "</tr>"
        )
    if not rows:
        rows.append("<tr><td colspan='4' class='ox-detail'>No steps recorded yet.</td></tr>")
    return (
        "<table class='ox-table'><thead><tr><th>Step</th><th>Status</th><th>Attempt</th><th>Detail</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _guess_active_node(history: list[dict], jira_intake: bool, running: bool) -> str | None:
    if not running:
        return None
    completed = {r["node"] for r in history}
    for node in _pipeline_step_order(jira_intake):
        if node not in completed:
            return node
    return "finishing"


def _progress_log(state: dict, elapsed_sec: float, running: bool) -> str:
    history = state.get("execution_history") or []
    jira_intake = bool(state.get("jira_issue_key") or state.get("jira_issue_input"))
    active = _guess_active_node(history, jira_intake, running)
    status = state.get("status") or "pending"
    attempt = state.get("attempt_count", 0)
    max_att = state.get("max_attempts", 3)
    lines = [
        f"elapsed {_format_elapsed(elapsed_sec)}    attempts {attempt}/{max_att}    status {status}",
    ]
    if running and active:
        hint = " (may take several minutes)" if active in _LONG_RUNNING_NODES else ""
        lines.append(f"running {active}{hint}")
    lines.append("")
    if not history and not (running and active):
        lines.append("Waiting for the first agent to report…")
    for rec in history:
        detail = " ".join(str(rec.get("detail") or "").split())
        lines.append(
            f"[{rec.get('node', '?')}] {rec.get('status', '')}  attempt {rec.get('attempt_count', '')}"
        )
        if detail:
            lines.append(f"    {detail}")
    if running and active and active not in {r.get("node") for r in history}:
        lines.append(f"[{active}] in progress  attempt {attempt}")
        lines.append("    Waiting for this step to finish")
    return "\n".join(lines)


def _render_live_progress(
    state: dict,
    elapsed_sec: float,
    running: bool,
    container: Any,
) -> None:
    history = state.get("execution_history") or []
    jira_intake = bool(state.get("jira_issue_key") or state.get("jira_issue_input"))
    active = _guess_active_node(history, jira_intake, running)
    log = html.escape(_progress_log(state, elapsed_sec, running))
    container.markdown(
        f"<pre class='ox-log'>{log}</pre>"
        + _history_table_html(history, active if running else None, state.get("attempt_count", "")),
        unsafe_allow_html=True,
    )


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

    with st.status("Agent pipeline running…", expanded=True) as status:
        body = st.empty()
        _render_live_progress(latest["state"], 0.0, True, body)
        while thread.is_alive():
            elapsed = time.monotonic() - started
            _render_live_progress(latest["state"], max(elapsed, latest["elapsed"]), True, body)
            status.update(label=f"Agent pipeline running… {_format_elapsed(elapsed)}", expanded=True)
            time.sleep(0.4)

        kind, payload = result_queue.get()
        if kind == "err":
            status.update(label="Pipeline failed", state="error", expanded=True)
            raise payload

        final = payload
        total = time.monotonic() - started
        _render_live_progress(final, total, False, body)
        status.update(label=f"Pipeline finished in {_format_elapsed(total)}", state="complete", expanded=True)
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
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500&display=swap');
.stApp {{
  background:
    radial-gradient(1200px 600px at 100% -10%, {ACCENT}14, transparent 60%),
    radial-gradient(900px 500px at -10% 110%, {CYAN}10, transparent 60%),
    {CANVAS};
  color: {WHITE};
  font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
}}
.stApp p, .stApp label, .stApp li, .stApp input, .stApp textarea, .stApp button {{
  font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
}}
/* Keep Streamlit's ligature icons (sidebar toggle, deploy, alerts) as glyphs. */
[data-testid="stIconMaterial"],
.material-symbols-rounded,
.material-symbols-outlined,
.material-symbols-sharp {{
  font-family: "Material Symbols Rounded" !important;
  font-weight: normal !important;
  font-style: normal !important;
  letter-spacing: normal !important;
  text-transform: none !important;
  line-height: 1 !important;
  white-space: nowrap !important;
}}
.block-container {{ padding: 2rem 2.5rem 3rem 2.5rem; max-width: 1400px; }}
h1, h2, h3, h4 {{ color: {WHITE}; letter-spacing: -0.5px; font-weight: 700; }}
[data-testid="stWidgetLabel"] p {{
  color: {NEUTRAL} !important; font-size: 0.78rem !important; font-weight: 600;
  text-transform: uppercase; letter-spacing: 0.06em;
}}
.ox-section-title {{
  display: flex; align-items: center; gap: 10px; margin: 0 0 14px 0;
  font-size: 0.95rem; font-weight: 700; color: {WHITE}; letter-spacing: -0.2px;
}}
.ox-section-title::before {{
  content: ""; width: 4px; height: 18px; border-radius: 4px;
  background: linear-gradient(180deg, {ACCENT}, {CYAN}); box-shadow: 0 0 10px {ACCENT}88;
}}
.ox-section-title small {{ color: {NEUTRAL}; font-weight: 500; font-size: 0.78rem; }}
a {{ color: {CYAN} !important; }}

[data-testid="stSidebar"] {{ background-color: {SIDEBAR}; border-right: 1px solid {BORDER}; }}
[data-testid="stSidebar"] [role="radiogroup"] label {{
  padding: 8px 12px; border-radius: 10px; margin-bottom: 4px; border: 1px solid transparent;
  transition: all 0.15s ease;
}}
[data-testid="stSidebar"] [role="radiogroup"] label:hover {{
  border-color: {CYAN}55; background-color: {CYAN}0D;
}}

.mm-header-strip {{
  position: relative; background: {CARD}; border: 1px solid {BORDER}; border-radius: 16px;
  padding: 22px 28px; margin-bottom: 22px; overflow: hidden;
  box-shadow: 0 0 0 1px {ACCENT}22, 0 10px 40px -12px {ACCENT}55;
}}
.mm-header-strip::before {{
  content: ""; position: absolute; inset: 0 0 auto 0; height: 3px;
  background: linear-gradient(90deg, {ACCENT}, {CYAN});
}}
.mm-header-strip h1 {{
  margin: 0; font-size: 1.9rem; font-weight: 800; border: none; padding: 0;
  background: linear-gradient(90deg, {ACCENT}, {CYAN});
  -webkit-background-clip: text; background-clip: text; -webkit-text-fill-color: transparent;
}}
.mm-header-strip p {{
  margin: 6px 0 0 0; font-size: 0.85rem; color: {NEUTRAL};
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace; letter-spacing: 0.04em;
}}

.stButton > button, .stDownloadButton > button, .stFormSubmitButton > button {{
  background: linear-gradient(135deg, {ACCENT} 0%, #D91A53 100%); color: #FFFFFF;
  border: none; border-radius: 8px; font-weight: 600; padding: 0.5rem 1.1rem;
  box-shadow: 0 4px 12px rgba(255, 42, 109, 0.3); transition: all 0.2s ease-in-out;
}}
.stButton > button:hover, .stDownloadButton > button:hover, .stFormSubmitButton > button:hover {{
  color: #FFFFFF; opacity: 0.92; transform: translateY(-1px);
  box-shadow: 0 6px 16px rgba(255, 42, 109, 0.5);
}}
.stButton > button:active, .stFormSubmitButton > button:active {{ transform: translateY(0); }}
.stButton > button:focus:not(:active), .stFormSubmitButton > button:focus:not(:active) {{
  color: #FFFFFF; border: none; box-shadow: 0 0 0 2px {CYAN}88, 0 4px 12px rgba(255, 42, 109, 0.4);
}}
.stButton > button p, .stFormSubmitButton > button p {{ color: #FFFFFF !important; font-weight: 600; }}

div[data-baseweb="select"] > div, div[data-baseweb="input"], div[data-baseweb="base-input"],
div[data-baseweb="textarea"], [data-testid="stNumberInputContainer"] {{
  background-color: {INPUT_BG} !important; border: 1px solid {INPUT_BORDER} !important;
  border-radius: 8px !important; color: {WHITE} !important;
  transition: border-color 0.15s ease, box-shadow 0.15s ease;
}}
div[data-baseweb="input"] > div, div[data-baseweb="base-input"] > div,
div[data-baseweb="input"] input, div[data-baseweb="base-input"] input,
div[data-baseweb="textarea"] textarea, [data-testid="stNumberInputContainer"] input {{
  background-color: transparent !important; border: none !important; color: {WHITE} !important;
}}
div[data-baseweb="select"] > div:hover, div[data-baseweb="input"]:hover,
div[data-baseweb="textarea"]:hover, [data-testid="stNumberInputContainer"]:hover {{
  border-color: {ACCENT}AA !important;
}}
div[data-baseweb="select"] > div:focus-within, div[data-baseweb="input"]:focus-within,
div[data-baseweb="textarea"]:focus-within, [data-testid="stNumberInputContainer"]:focus-within {{
  border-color: {CYAN} !important;
  box-shadow: 0 0 0 1px {CYAN}66, 0 0 18px -4px {CYAN}88, 0 0 28px -10px {ACCENT}88 !important;
}}
[data-testid="stNumberInputContainer"] button {{
  background-color: transparent !important; color: {NEUTRAL} !important; border: none !important;
}}
[data-testid="stNumberInputContainer"] button:hover {{ color: {ACCENT} !important; background-color: {ACCENT}14 !important; }}
div[data-baseweb="popover"] ul[role="listbox"] {{
  background-color: {INPUT_BG} !important; border: 1px solid {INPUT_BORDER}; border-radius: 10px;
  box-shadow: 0 16px 40px rgba(0, 0, 0, 0.6);
}}
div[data-baseweb="popover"] li[role="option"]:hover, div[data-baseweb="popover"] li[aria-selected="true"] {{
  background-color: {ACCENT}22 !important;
}}
span[data-baseweb="tag"] {{ background-color: {ACCENT}33 !important; border: 1px solid {ACCENT}88; border-radius: 6px; }}
[data-testid="stCheckbox"] label span:first-child {{ border-color: {INPUT_BORDER} !important; border-radius: 5px; }}

[data-testid="stMetric"] {{
  background-color: {CARD}; border: 1px solid {BORDER}; border-radius: 14px; padding: 16px 18px;
  box-shadow: inset 0 1px 0 #FFFFFF08;
}}
[data-testid="stMetricLabel"] {{
  color: {NEUTRAL} !important; text-transform: uppercase; font-size: 0.72rem; letter-spacing: 0.08em;
}}
[data-testid="stMetricValue"] {{ color: {CYAN} !important; font-weight: 700; text-shadow: 0 0 12px {CYAN}55; }}

[data-testid="stForm"], [data-testid="stVerticalBlockBorderWrapper"] {{
  background: linear-gradient(180deg, {CARD}F2 0%, {CARD}CC 100%) !important;
  border: 1px solid {BORDER} !important; border-radius: 12px !important;
  padding: 20px !important; margin-bottom: 18px;
  box-shadow: 0 4px 20px rgba(0, 0, 0, 0.4), inset 0 1px 0 #FFFFFF0A;
  backdrop-filter: blur(10px); -webkit-backdrop-filter: blur(10px);
}}
[data-testid="stVerticalBlockBorderWrapper"] [data-testid="stVerticalBlockBorderWrapper"] {{
  box-shadow: none; padding: 14px !important; background: {INPUT_BG}80 !important;
}}
[data-testid="stExpander"] {{
  border: 1px solid {BORDER} !important; border-radius: 12px !important; background-color: {CARD}CC;
  box-shadow: 0 4px 20px rgba(0, 0, 0, 0.35);
}}
[data-testid="stExpander"] details {{ border: none !important; }}
[data-testid="stExpander"] summary:hover {{ color: {CYAN}; }}
[data-testid="stAlert"] {{ border-radius: 10px; border: 1px solid {BORDER}; }}
[data-testid="stStatusWidget"], [data-testid="stStatus"] {{
  background-color: {CARD}; border: 1px solid {BORDER}; border-radius: 10px;
}}

.stTabs [data-baseweb="tab-list"] {{ gap: 6px; border-bottom: 1px solid {BORDER}; }}
.stTabs [data-baseweb="tab"] {{ border-radius: 10px 10px 0 0; padding: 8px 16px; color: {NEUTRAL}; }}
.stTabs [aria-selected="true"] {{ color: {WHITE} !important; background-color: {ACCENT}1A; }}
.stTabs [data-baseweb="tab-highlight"] {{ background-color: {ACCENT}; }}

[data-testid="stDataFrame"], [data-testid="stJson"] {{
  border: 1px solid {BORDER}; border-radius: 12px; overflow: hidden;
}}
table {{
  background-color: {CARD} !important; border: 1px solid {BORDER} !important;
  border-radius: 8px; border-collapse: separate !important; border-spacing: 0; overflow: hidden;
}}
th {{
  background-color: {INPUT_BG} !important; color: {CYAN} !important; font-weight: 600;
  font-size: 0.74rem; text-transform: uppercase; letter-spacing: 0.08em;
}}
td {{ border-bottom: 1px solid {BORDER} !important; color: #C5C9D1 !important; }}

.ox-table {{ width: 100%; font-size: 0.85rem; margin: 6px 0 4px 0; }}
.ox-table th, .ox-table td {{ padding: 10px 14px; text-align: left; border-left: none !important; border-right: none !important; }}
.ox-table th {{ border-bottom: 1px solid {INPUT_BORDER} !important; border-top: none !important; }}
.ox-table tr:last-child td {{ border-bottom: none !important; }}
.ox-table tbody tr {{ transition: background-color 0.12s ease; }}
.ox-table tbody tr:hover td {{ background-color: {ACCENT}0D; }}
.ox-table td.ox-node {{ font-family: 'JetBrains Mono', ui-monospace, monospace; color: {WHITE} !important; white-space: nowrap; }}
.ox-table td.ox-num {{ color: {NEUTRAL} !important; text-align: center; width: 80px; }}
.ox-table td.ox-detail {{ color: #AEB4C2 !important; line-height: 1.5; }}

.ox-pill {{
  display: inline-flex; align-items: center; gap: 6px; padding: 3px 10px; border-radius: 999px;
  font-size: 0.72rem; font-weight: 600; letter-spacing: 0.02em; white-space: nowrap;
  font-family: 'JetBrains Mono', ui-monospace, monospace; border: 1px solid transparent;
}}
.ox-pill::before {{ content: ""; width: 6px; height: 6px; border-radius: 50%; background: currentColor; box-shadow: 0 0 6px currentColor; }}
.ox-pill.ok {{ color: {SUCCESS}; background-color: {SUCCESS}14; border-color: {SUCCESS}44; }}
.ox-pill.warn {{ color: {WARNING}; background-color: {WARNING}14; border-color: {WARNING}44; }}
.ox-pill.err {{ color: {ERROR}; background-color: {ERROR}14; border-color: {ERROR}44; }}
.ox-pill.info {{ color: {CYAN}; background-color: {CYAN}14; border-color: {CYAN}44; }}
.ox-pill.run {{ color: {ACCENT}; background-color: {ACCENT}1A; border-color: {ACCENT}66; animation: ox-pulse 1.4s ease-in-out infinite; }}
.ox-pill.idle {{ color: {NEUTRAL}; background-color: {NEUTRAL}14; border-color: {NEUTRAL}33; }}
@keyframes ox-pulse {{ 0%, 100% {{ box-shadow: 0 0 0 0 {ACCENT}55; }} 50% {{ box-shadow: 0 0 0 4px {ACCENT}00; }} }}

.ox-stats {{ display: flex; flex-wrap: wrap; gap: 10px; margin: 4px 0 12px 0; }}
.ox-stat {{
  background-color: {INPUT_BG}; border: 1px solid {INPUT_BORDER}; border-radius: 10px;
  padding: 8px 14px; min-width: 120px;
}}
.ox-stat .k {{ color: {NEUTRAL}; font-size: 0.68rem; text-transform: uppercase; letter-spacing: 0.08em; font-weight: 600; }}
.ox-stat .v {{ color: {WHITE}; font-size: 0.95rem; font-weight: 600; margin-top: 2px; font-family: 'JetBrains Mono', ui-monospace, monospace; }}
.ox-conn {{ display: flex; align-items: center; gap: 10px; margin-top: 8px; font-size: 0.82rem; color: #AEB4C2; }}
.ox-log {{
  background: {INPUT_BG}; border: 1px solid {BORDER}; border-radius: 8px;
  padding: 12px 14px; margin: 0 0 10px 0; max-height: 320px; overflow: auto;
  color: #C5C9D1; font-family: 'JetBrains Mono', ui-monospace, monospace;
  font-size: 0.78rem; line-height: 1.55; white-space: pre-wrap;
}}
.ox-video-note {{ color: {NEUTRAL}; font-size: 0.8rem; margin: 4px 0 10px 0; }}
code {{ color: {CYAN} !important; background-color: {CYAN}12 !important; border-radius: 6px; }}

.config-box {{
  background-color: {CARD}; border: 1px solid {BORDER}; border-radius: 12px;
  padding: 14px 16px; font-size: 0.82rem; line-height: 1.7; margin: 8px 0 16px 0;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}}
.exit-banner {{
  background-color: {CARD}; border: 1px solid {BORDER}; border-left: 4px solid {NEUTRAL};
  border-radius: 12px; padding: 16px 20px; margin: 14px 0;
}}
.exit-banner h3 {{ margin: 0 0 4px 0; font-size: 1.05rem; border: none; padding: 0; letter-spacing: 0.06em; }}
.exit-banner p {{ margin: 0; color: {WHITE}cc; font-size: 0.9rem; }}
.exit-success {{ border-left-color: {SUCCESS}; box-shadow: -6px 0 24px -12px {SUCCESS}; }}
.exit-success h3 {{ color: {SUCCESS}; }}
.exit-warning {{ border-left-color: {WARNING}; box-shadow: -6px 0 24px -12px {WARNING}; }}
.exit-warning h3 {{ color: {WARNING}; }}
.exit-error {{ border-left-color: {ERROR}; box-shadow: -6px 0 24px -12px {ERROR}; }}
.exit-error h3 {{ color: {ERROR}; }}

.pipeline-row {{ display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin: 10px 0 6px 0; }}
.pipeline-step {{
  border: 1px solid {BORDER}; color: {NEUTRAL}; background-color: {CARD}; border-radius: 999px;
  padding: 5px 14px; font-size: 0.78rem; font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}}
.pipeline-step.done {{ border-color: {SUCCESS}88; color: {SUCCESS}; box-shadow: 0 0 10px {SUCCESS}33; }}
.pipeline-step.active {{
  border-color: {ACCENT}; color: #FFFFFF; background-color: {ACCENT}; box-shadow: 0 0 16px {ACCENT}88;
}}
.pipeline-step.failed {{ border-color: {ERROR}; color: {ERROR}; box-shadow: 0 0 10px {ERROR}44; }}
.pipeline-arrow {{ color: {CYAN}88; }}
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
    with st.container(border=True):
        _section("Credentials & settings", f"{len(SESSION_ENV_KEYS)} keys")
        cols = st.columns(2)
        for idx, key in enumerate(SESSION_ENV_KEYS):
            current = overrides.get(key, os.environ.get(key, ""))
            with cols[idx % 2]:
                overrides[key] = st.text_input(
                    key, value=current or "", type="password" if "TOKEN" in key or "KEY" in key else "default"
                )

        col1, col2 = st.columns(2)
        with col1:
            if st.button("Apply to session", type="primary", width="stretch"):
                st.session_state.env_overrides = overrides
                _apply_env_overrides()
                st.success("Session environment updated.")
        with col2:
            if st.button("Clear session overrides", width="stretch"):
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

    with st.container(border=True):
        _section("Dependencies", "repos the primary app consumes")
        primary = st.selectbox("Primary app (pipeline target)", apps)
        cfg = load_app_config(primary, project_root=ROOT)
        known = [a for a in apps if a != primary]
        current_deps = [d.app for d in cfg.dependencies]

        c1, c2 = st.columns([2, 1])
        with c1:
            selected = st.multiselect("Dependencies", known, default=[d for d in current_deps if d in known])
        with c2:
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


def _auth_result_html(status: Any) -> str:
    if getattr(status, "authenticated", False):
        tone, label = ("ok", "connected") if getattr(status, "can_write", None) is not False else ("warn", "read-only")
    else:
        tone, label = "err", "not connected"
    message = getattr(status, "error", None) or getattr(status, "detail", "") or ""
    return f"<div class='ox-conn'>{_pill(label, tone)}<span>{html.escape(str(message))}</span></div>"


def _connection_tests(cfg: AppConfig) -> None:
    with st.container(border=True):
        _section("Connection tests", "verify integrations before running in real mode")
        c1, c2 = st.columns(2)
        with c1:
            if st.button("Test Jira", width="stretch"):
                jira = cfg.integrations.jira
                if not jira or not jira.base_url:
                    st.markdown(_pill("Configure Jira on the project first", "warn"), unsafe_allow_html=True)
                else:
                    _apply_env_overrides()
                    status = jira_api.check_auth(jira.base_url, jira.project_key)
                    st.markdown(_auth_result_html(status), unsafe_allow_html=True)
        with c2:
            if st.button("Test GitLab", width="stretch"):
                gl = cfg.integrations.gitlab
                if not gl or not gl.host:
                    st.markdown(_pill("Configure GitLab on the project first", "warn"), unsafe_allow_html=True)
                else:
                    _apply_env_overrides()
                    status = gitlab_api.check_auth(gl.host, gl.project_path or cfg.repo)
                    st.markdown(_auth_result_html(status), unsafe_allow_html=True)


def page_run_pipeline() -> None:
    apps = list_app_config_names(ROOT)
    if not apps:
        st.error("No app configs in apps/.")
        return

    with st.container(border=True):
        _section("Pipeline configuration", "target app and execution mode")
        c1, c2 = st.columns(2)
        with c1:
            app_name = st.selectbox(
                "Primary app", apps, index=apps.index("sample_android") if "sample_android" in apps else 0
            )
        with c2:
            mode = st.selectbox("Mode", ["mock", "real"], index=0)

        try:
            cfg = load_app_config(app_name, project_root=ROOT)
        except ConfigError as exc:
            st.error(str(exc))
            cfg = None

        c3, c4 = st.columns(2)
        scenario = None
        with c3:
            if mode == "mock":
                scenario = st.selectbox("Mock scenario", list(SCENARIOS), index=list(SCENARIOS).index("fix_success"))
            else:
                st.selectbox("Mock scenario", ["— (real mode)"], disabled=True)
        with c4:
            max_attempts = st.number_input("Max attempts", min_value=1, max_value=10, value=3, step=1)
        dry_run = st.checkbox("Dry run (real: no publish/push)", value=False, disabled=(mode == "mock"))

        if cfg is not None:
            st.markdown(
                _stats_html(
                    [
                        ("Platform", html.escape(cfg.platform)),
                        ("Repo", html.escape(str(cfg.repo))),
                        ("Mode", _pill(mode, "run" if mode == "real" else "info")),
                    ]
                ),
                unsafe_allow_html=True,
            )

    flow = "default_flow"
    jira_issue = ""
    if mode == "real" and cfg is not None:
        with st.container(border=True):
            _section("Real-mode inputs", "Maestro flow and optional Jira intake")
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

    if st.button("Trigger Agent Pipeline", type="primary", width="stretch"):
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


_VIDEO_SUFFIXES = {".mp4", ".mov", ".webm", ".mkv", ".m4v"}


def _existing_videos(raw_paths: list) -> list[Path]:
    """Keep real video files, and any video sitting next to saved evidence (Jira attachments, run dirs)."""
    found: list[Path] = []
    seen: set[Path] = set()
    scanned: set[Path] = set()

    def add(path: Path) -> None:
        try:
            resolved = path.resolve()
        except OSError:
            return
        if resolved in seen or resolved.suffix.lower() not in _VIDEO_SUFFIXES or not resolved.is_file():
            return
        if resolved.stat().st_size <= 0:
            return
        seen.add(resolved)
        found.append(resolved)

    for raw in raw_paths:
        if not raw:
            continue
        path = Path(str(raw))
        add(path)
        for directory in (
            path.parent,
            path.parent.parent,
            path.parent / "attachments",
            path.parent.parent / "attachments",
        ):
            if directory in scanned or not directory.is_dir():
                continue
            scanned.add(directory)
            for child in directory.iterdir():
                add(child)
    return found


def _before_videos(final: dict) -> list[Path]:
    baseline = final.get("jira_baseline") or {}
    seeds = list(final.get("before_video_paths") or [])
    seeds.extend(baseline.get("baseline_video_paths") or [])
    seeds.extend(baseline.get("baseline_evidence_paths") or [])
    # After a retest, qa_finding is the latest failure, so it is not the "before" recording.
    if not final.get("retest_finding"):
        qa = final.get("qa_finding") or {}
        seeds.extend(qa.get("video_paths") or [])
        seeds.extend(qa.get("evidence_paths") or [])
    return _existing_videos(seeds)


def _after_videos(final: dict) -> list[Path]:
    retest = final.get("retest_finding") or {}
    primary = retest.get("primary_result") or {}
    seeds = list(final.get("after_video_paths") or [])
    seeds.extend(primary.get("video_paths") or [])
    seeds.extend(primary.get("evidence_paths") or [])
    return _existing_videos(seeds)


def _render_video_column(title: str, caption: str, videos: list[Path]) -> None:
    st.markdown(f"**{html.escape(title)}**")
    st.markdown(f"<p class='ox-video-note'>{html.escape(caption)}</p>", unsafe_allow_html=True)
    if not videos:
        st.markdown(_pill("No recording yet", "idle"), unsafe_allow_html=True)
        return
    for path in videos:
        st.video(str(path))
        st.caption(path.name)


def _render_recordings(final: dict) -> None:
    with st.container(border=True):
        _section("Screen recordings", "before the fix, and after the fix")
        left, right = st.columns(2)
        with left:
            _render_video_column(
                "Before fix",
                "Jira attachment, or the screen recording from the first agent run.",
                _before_videos(final),
            )
        with right:
            _render_video_column(
                "After fix",
                "Screen recording captured during the retest run.",
                _after_videos(final),
            )


def _render_results(final: dict) -> None:
    last_run = st.session_state.get("last_run") or {}
    duration = last_run.get("duration_sec")
    if duration is not None:
        st.caption(f"Last run duration: {_format_elapsed(float(duration))}")

    css, title, msg = _banner(final)
    st.markdown(_step_chips(final), unsafe_allow_html=True)
    st.markdown(f"<div class='exit-banner {css}'><h3>{title}</h3><p>{msg}</p></div>", unsafe_allow_html=True)
    _render_recordings(final)

    st.markdown(
        _stats_html(
            [
                ("Final status", _pill(final.get("status"))),
                ("Attempts", f"{final.get('attempt_count', 0)}/{final.get('max_attempts', '?')}"),
                ("Failure type", _pill((final.get("qa_finding") or {}).get("failure_type") or "n/a", "info")),
                ("Ticket", html.escape(str(final.get("ticket_id") or "—"))),
            ]
        ),
        unsafe_allow_html=True,
    )

    with st.expander("Execution log (all steps)", expanded=False):
        st.markdown(_history_table_html(final.get("execution_history") or []), unsafe_allow_html=True)

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
        st.markdown(_history_table_html(final.get("execution_history") or []), unsafe_allow_html=True)
    with tab_raw:
        st.json(_jsonable(final))


def main() -> None:
    st.set_page_config(page_title="One-X Agent (Self Healing)", page_icon="🚀", layout="wide")
    st.markdown(CSS, unsafe_allow_html=True)
    st.markdown(
        "<div class='mm-header-strip'><h1>One-X Agent (Self Healing)</h1>"
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
