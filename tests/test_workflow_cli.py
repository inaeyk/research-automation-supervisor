from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from research_automation_supervisor import cli
from research_automation_supervisor.cli import app
from research_automation_supervisor.workflow_engine import WorkflowServices, run_substage
from research_automation_supervisor.workflow_models import WorkflowResult
from tests.workflow_helpers import create_workflow_tree

runner = CliRunner()


def workflow_result(tmp_path: Path, status: str = "completed") -> WorkflowResult:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir(exist_ok=True)
    return WorkflowResult(
        substage_id="cli-substage",
        run_token="abcdef123456",
        status=status,  # type: ignore[arg-type]
        repair_round=0,
        max_repair_rounds=2,
        checkpoint_after=status == "checkpoint_paused",
        workspace=str(tmp_path),
        baseline_commit="a" * 40,
        worker_thread_id="thread-1",
        latest_worker_action_id="worker-r000",
        latest_audit_action_id="auditor-r000",
        tests_passed=True,
        scope_compliant=True,
        contract_satisfied=True,
        artifact_directory=str(artifacts),
        pause_reason=(
            "auditor_passed_checkpoint"
            if status == "checkpoint_paused"
            else None
        ),
        summary="stable result",
        started_at="2026-01-01T00:00:00.000000Z",
        updated_at="2026-01-01T00:00:01.000000Z",
    )


def test_validate_substage_cli_is_read_only_and_prints_no_prompt_content(tmp_path: Path) -> None:
    spec, project, _ = create_workflow_tree(tmp_path)
    before = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))

    result = runner.invoke(app, ["validate-substage", str(spec), "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["substage_id"] == "minimal-substage"
    assert "Implement the substage" not in result.stdout
    assert "Frozen contract sentence" not in result.stdout
    assert sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*")) == before
    assert project.is_dir()


def test_run_cli_json_agrees_with_result_and_frozen_exit_code(
    tmp_path: Path, monkeypatch
) -> None:
    expected = workflow_result(tmp_path, "checkpoint_paused")
    monkeypatch.setattr(cli, "run_workflow", lambda path, runs_dir: expected)

    result = runner.invoke(app, ["run-substage", "spec.yaml", "--json"])

    assert result.exit_code == 7
    assert json.loads(result.stdout) == expected.to_dict()


def test_status_cli_returns_zero_for_paused_readable_state(tmp_path: Path, monkeypatch) -> None:
    expected = workflow_result(tmp_path, "human_paused")
    monkeypatch.setattr(cli, "read_workflow_status", lambda path: expected)

    result = runner.invoke(app, ["substage-status", "run", "--json"])

    assert result.exit_code == 0
    assert json.loads(result.stdout) == expected.to_dict()


def test_checkpoint_status_cli_human_and_json_render_the_same_reason(
    tmp_path: Path,
) -> None:
    spec, _, fake = create_workflow_tree(tmp_path, checkpoint_after=True)
    expected = run_substage(
        spec,
        runs_dir=tmp_path / "runs",
        services=WorkflowServices(codex_executable=str(fake)),
    )
    run_directory = Path(expected.artifact_directory)

    human = runner.invoke(app, ["substage-status", str(run_directory)])
    machine = runner.invoke(
        app,
        ["substage-status", str(run_directory), "--json"],
    )

    assert human.exit_code == 0
    assert machine.exit_code == 0
    assert "Pause reason: auditor_passed_checkpoint" in human.stdout
    assert json.loads(machine.stdout) == expected.to_dict()
    assert json.loads(machine.stdout)["pause_reason"] == "auditor_passed_checkpoint"
