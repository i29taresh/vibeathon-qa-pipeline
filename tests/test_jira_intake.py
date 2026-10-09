"""Tests for Jira-as-input intake (attachments + optional frame extraction)."""

from __future__ import annotations

from pathlib import Path
from config import load_app_config
from nodes.jira_intake_agent import jira_intake_agent
from nodes.ticket_agent import ticket_agent
from state import initial_state
from utils import jira as jira_api
from utils.jira_intake import intake_from_jira


def test_normalize_issue_input_accepts_browse_url():
    from utils.jira import normalize_issue_input

    assert normalize_issue_input("https://alliws.atlassian.net/browse/WFDR-25182") == "WFDR-25182"
    assert normalize_issue_input("WFDR-25182") == "WFDR-25182"


def test_create_pipeline_state_sets_jira_key_from_url():
    from state import create_pipeline_state

    root = Path(__file__).resolve().parents[1]
    cfg = load_app_config("sample_android", project_root=root)
    state = create_pipeline_state(
        "sample_android",
        cfg,
        mode="real",
        jira_issue_raw="https://alliws.atlassian.net/browse/WFDR-25182",
    )
    assert state.get("jira_issue_key") == "WFDR-25182"


def test_adf_to_plain_text_paragraph():
    doc = {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": "Login button broken"}],
            }
        ],
    }
    assert "Login button broken" in jira_api.adf_to_plain_text(doc)


def test_intake_from_jira_downloads_image(tmp_path, monkeypatch):
    cfg = load_app_config("sample_android", project_root=Path(__file__).resolve().parents[1])

    png_bytes = b"\x89PNG\r\n\x1a\n"
    image_path = tmp_path / "shot.png"
    image_path.write_bytes(png_bytes)

    details = jira_api.IssueDetails(
        key="TST-42",
        url="https://jira.example/browse/TST-42",
        summary="Button does not work",
        description_text="Tap login and nothing happens.",
        attachments=[
            jira_api.JiraAttachmentMeta(
                id="1",
                filename="shot.png",
                mime_type="image/png",
                content_url="https://jira.example/attachment/1",
                size=len(png_bytes),
            )
        ],
    )

    def fake_get_issue_details(issue_key, cwd=None, timeout=30.0):
        return details, None

    def fake_download(url, dest, timeout=120.0):
        dest.write_bytes(png_bytes)
        return True, None

    monkeypatch.setattr(jira_api, "get_issue_details", fake_get_issue_details)
    monkeypatch.setattr(jira_api, "download_attachment", fake_download)

    result = intake_from_jira(cfg, "TST-42", "login.yaml", runs_root=tmp_path / "runs", use_llm=False)
    assert result.success
    assert result.finding is not None
    assert result.finding.failure_type == "functional"
    assert result.finding.passed is False
    assert len(result.finding.evidence_paths) == 1
    assert Path(result.finding.evidence_paths[0]).is_file()


def test_jira_intake_agent_real_sets_ticket_id(monkeypatch):
    root = Path(__file__).resolve().parents[1]
    cfg = load_app_config("sample_android", project_root=root)
    state = initial_state("sample_android", cfg, mode="real", jira_issue_key="TST-99", flow_name="login.yaml")

    from utils.jira_intake import JiraIntakeResult
    from utils.qa_validation import QAFinding

    fake_finding = QAFinding(
        flow_name="login.yaml",
        passed=False,
        failure_type="functional",
        description="from jira",
        expected_behavior="e",
        actual_behavior="a",
        confidence=0.7,
        reproduction_steps=["Tap Settings", "Open Notifications"],
        evidence_paths=[],
    )

    def fake_intake(*_args, **_kwargs):
        return JiraIntakeResult(
            success=True,
            finding=fake_finding,
            issue_key="TST-99",
            issue_url="https://jira.example/browse/TST-99",
            jira_baseline={
                "issue_key": "TST-99",
                "reproduction_steps": ["Tap Settings", "Open Notifications"],
                "description_text": "Tap Settings then Open Notifications",
            },
        )

    from utils.maestro_flow_gen import MaestroFlowGenResult

    flow_path = root / "local" / "runs" / "TST-99" / "repro.yaml"
    flow_path.parent.mkdir(parents=True, exist_ok=True)
    flow_path.write_text("appId: de.payzy.pro.pre_prod\n---\n- launchApp\n", encoding="utf-8")

    monkeypatch.setattr("nodes.jira_intake_agent.intake_from_jira", fake_intake)
    monkeypatch.setattr(
        "nodes.jira_intake_agent.generate_and_save_flow",
        lambda *a, **k: MaestroFlowGenResult(success=True, flow_path=flow_path, yaml_text="x", steps=[]),
    )

    out = jira_intake_agent(state)
    assert out["ticket_id"] == "TST-99"
    assert out["jira_issue_input"] is True
    assert out["qa_finding"]["failure_type"] == "functional"
    assert out.get("maestro_flow_path") == str(flow_path)


def test_ticket_agent_skips_create_on_jira_input_first_pass():
    root = Path(__file__).resolve().parents[1]
    cfg = load_app_config("sample_android", project_root=root)
    state = initial_state("sample_android", cfg, mode="real")
    state["jira_issue_input"] = True
    state["ticket_id"] = "TST-1"
    state["qa_finding"] = {
        "failure_type": "functional",
        "flow_name": "f",
        "description": "d",
    }
    state["rca_finding"] = {"root_cause_hypothesis": "h", "confidence": 0.5}
    out = ticket_agent(state)
    assert out["status"] == "ticket_skipped"
    assert out["ticket_id"] == "TST-1"
