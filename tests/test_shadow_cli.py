from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from research_automation_supervisor import cli
from research_automation_supervisor.cli import app
from research_automation_supervisor.shadow_models import ShadowResult
from tests.shadow_helpers import create_shadow_tree

runner = CliRunner()


def shadow_result(tmp_path: Path, status: str) -> ShadowResult:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir(exist_ok=True)
    return ShadowResult(
        calibration_id="cli-shadow",
        source_stage2_run=str(tmp_path / "source"),
        source_substage_id="source-substage",
        status=status,  # type: ignore[arg-type]
        supervisor_session_id="supervisor-thread",
        supervisor_model="gpt-5.6-sol",
        proposal_count=2,
        comparison_count=2,
        review_count=0,
        disqualification_count=0,
        readiness="insufficient_data",
        artifact_directory=str(artifacts),
        pause_reason=None,
        summary="stable shadow result",
        started_at="2026-01-01T00:00:00.000000Z",
        updated_at="2026-01-01T00:00:01.000000Z",
    )


def test_validate_shadow_cli_is_read_only_and_prints_no_authoritative_prompt(
    tmp_path: Path,
) -> None:
    spec, source_run, project, _ = create_shadow_tree(tmp_path)
    before = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }

    result = runner.invoke(
        app, ["validate-shadow-spec", str(spec), "--json"]
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["calibration_id"] == "minimal-shadow"
    assert payload["decision_count"] == 2
    assert "Implement the substage" not in result.stdout
    assert "Audit the current workspace" not in result.stdout
    assert source_run.is_dir() and project.is_dir()
    assert {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    } == before


def test_run_and_status_cli_use_stage3_exit_contract(
    tmp_path: Path, monkeypatch
) -> None:
    awaiting = shadow_result(tmp_path, "awaiting_reviews")
    monkeypatch.setattr(
        cli, "run_shadow", lambda path, runs_dir: awaiting
    )
    run_result = runner.invoke(
        app, ["run-shadow-calibration", "shadow.yaml", "--json"]
    )
    assert run_result.exit_code == 5
    assert json.loads(run_result.stdout) == awaiting.to_dict()

    monkeypatch.setattr(
        cli, "read_shadow_status", lambda path: awaiting
    )
    status_result = runner.invoke(
        app,
        ["shadow-calibration-status", "run", "--json"],
    )
    assert status_result.exit_code == 0
    assert json.loads(status_result.stdout) == awaiting.to_dict()


def test_report_cli_is_informational_and_returns_zero(monkeypatch) -> None:
    report = {
        "schema_version": 1,
        "calibration_id": "cli-shadow",
        "status": "awaiting_reviews",
        "readiness": {
            "status": "insufficient_data",
            "informational_only": True,
            "automation_enabled": False,
        },
        "assessments": [],
        "reviews": [],
    }
    monkeypatch.setattr(cli, "read_shadow_report", lambda path: report)

    result = runner.invoke(
        app, ["shadow-calibration-report", "run", "--json"]
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout) == report
