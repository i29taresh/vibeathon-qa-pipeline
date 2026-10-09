"""Unit tests for utils.maestro.run_flow - with utils.maestro.run_command mocked out.

No real `maestro` binary is ever invoked here.
"""

from __future__ import annotations

from pathlib import Path

import utils.maestro as maestro_mod
from config import load_app_config
from utils.maestro import run_flow
from utils.runner import CommandResult


def _config_with_flow(tmp_path: Path):
    cfg = load_app_config("sample_android")
    flows_dir = tmp_path / "flows"
    flows_dir.mkdir()
    (flows_dir / "login.yaml").write_text("appId: com.example\n---\n- launchApp\n")
    cfg.flows_dir = flows_dir
    cfg.clone_path = tmp_path
    return cfg


def test_run_flow_success(monkeypatch, tmp_path):
    cfg = _config_with_flow(tmp_path)

    def fake_run_command(argv, cwd, timeout=120.0, env=None):
        run_dir = Path(argv[argv.index("--debug-output") + 1])
        (run_dir / "step1.png").write_bytes(b"fake-png")
        (run_dir / "screenrecord.mp4").write_bytes(b"fake-mp4")
        report_path = Path(argv[argv.index("--output") + 1])
        report_path.write_text("<testsuite/>")
        return CommandResult(argv=argv, returncode=0, stdout="PASSED", stderr="")

    monkeypatch.setattr(maestro_mod, "run_command", fake_run_command)

    result = run_flow(cfg, "login.yaml", "emulator-5554", runs_root=tmp_path / "runs")

    assert result.success
    assert result.exit_code == 0
    assert result.report_path.is_file()
    assert len(result.screenshots) == 1
    assert len(result.videos) == 1
    assert result.videos[0].name == "screenrecord.mp4"
    assert result.run_dir.exists()


def test_run_flow_missing_flow_file(tmp_path):
    cfg = _config_with_flow(tmp_path)

    result = run_flow(cfg, "does_not_exist.yaml", "emulator-5554", runs_root=tmp_path / "runs")

    assert not result.success
    assert "not found" in result.error


def test_run_flow_rejects_path_traversal(tmp_path):
    cfg = _config_with_flow(tmp_path)

    result = run_flow(cfg, "../../etc/passwd", "emulator-5554", runs_root=tmp_path / "runs")

    assert not result.success
    assert "outside the allowed directory" in result.error


def test_run_flow_failure_reports_exit_code(monkeypatch, tmp_path):
    cfg = _config_with_flow(tmp_path)

    def fake_run_command(argv, cwd, timeout=120.0, env=None):
        return CommandResult(argv=argv, returncode=1, stdout="", stderr="Assertion failed: element not found")

    monkeypatch.setattr(maestro_mod, "run_command", fake_run_command)

    result = run_flow(cfg, "login.yaml", "emulator-5554", runs_root=tmp_path / "runs")

    assert not result.success
    assert result.exit_code == 1
    assert "element not found" in result.error


def test_run_flow_unique_run_dirs(monkeypatch, tmp_path):
    cfg = _config_with_flow(tmp_path)

    def fake_run_command(argv, cwd, timeout=120.0, env=None):
        return CommandResult(argv=argv, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(maestro_mod, "run_command", fake_run_command)

    result1 = run_flow(cfg, "login.yaml", "emulator-5554", runs_root=tmp_path / "runs")
    result2 = run_flow(cfg, "login.yaml", "emulator-5554", runs_root=tmp_path / "runs")

    assert result1.run_dir != result2.run_dir
