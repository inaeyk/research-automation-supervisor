from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from research_automation_supervisor import cli
from research_automation_supervisor.cli import app
from research_automation_supervisor.errors import (
    ShadowDependencyError,
    ShadowInputError,
    ShadowIntegrityError,
)
from research_automation_supervisor.shadow_models import ShadowResult
from tests.shadow_helpers import SUPERVISOR_UUID, create_shadow_tree

runner = CliRunner()


def shadow_result(tmp_path: Path, status: str) -> ShadowResult:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir(exist_ok=True)
    return ShadowResult(
        calibration_id="cli-shadow",
        source_stage2_run=str(tmp_path / "source"),
        source_substage_id="source-substage",
        status=status,  # type: ignore[arg-type]
        supervisor_session_id=SUPERVISOR_UUID,
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


@pytest.mark.parametrize(
    ("attribute", "arguments"),
    [
        ("validate_shadow", ["validate-shadow-spec", "spec.yaml"]),
        ("run_shadow", ["run-shadow-calibration", "spec.yaml"]),
        ("resume_shadow", ["resume-shadow-calibration", "run"]),
        ("read_shadow_status", ["shadow-calibration-status", "run"]),
        (
            "record_review",
            ["record-shadow-review", "run", "proposal", "review.yaml"],
        ),
        ("read_shadow_report", ["shadow-calibration-report", "run"]),
        (
            "abort_shadow",
            ["abort-shadow-calibration", "run", "--reason", "stop"],
        ),
    ],
)
@pytest.mark.parametrize("as_json", [False, True])
def test_all_shadow_commands_map_integrity_to_sanitized_exit_four(
    monkeypatch,
    attribute: str,
    arguments: list[str],
    as_json: bool,
) -> None:
    sensitive = "cli-secret-value"
    monkeypatch.setenv("AUDIT_TOKEN", sensitive)

    def fail(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise ShadowIntegrityError(
            f"trusted artifact contains {sensitive}"
        )

    monkeypatch.setattr(cli, attribute, fail)
    command = [*arguments, *(["--json"] if as_json else [])]

    result = runner.invoke(app, command)

    assert result.exit_code == 4
    assert sensitive not in result.output
    if as_json:
        payload = json.loads(result.stdout)
        assert payload["error_kind"] == "integrity"
        assert payload["ok"] is False


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (ShadowInputError("invalid input"), 2),
        (ShadowDependencyError("missing dependency"), 3),
        (RuntimeError("unexpected"), 1),
    ],
)
def test_shadow_cli_error_classification(
    monkeypatch,
    error: Exception,
    expected: int,
) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise error

    monkeypatch.setattr(cli, "validate_shadow", fail)

    result = runner.invoke(
        app, ["validate-shadow-spec", "spec.yaml", "--json"]
    )

    assert result.exit_code == expected


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("completed", 0),
        ("awaiting_reviews", 5),
        ("human_paused", 5),
        ("aborted", 8),
    ],
)
def test_run_cli_maps_terminal_and_pause_results(
    tmp_path: Path,
    monkeypatch,
    status: str,
    expected: int,
) -> None:
    value = shadow_result(tmp_path, status)
    monkeypatch.setattr(cli, "run_shadow", lambda *args, **kwargs: value)

    result = runner.invoke(
        app, ["run-shadow-calibration", "spec.yaml", "--json"]
    )

    assert result.exit_code == expected
    assert json.loads(result.stdout)["status"] == status


@pytest.mark.parametrize("as_json", [False, True])
@pytest.mark.parametrize(
    ("attribute", "arguments", "expected"),
    [
        ("validate_shadow", ["validate-shadow-spec", "spec.yaml"], 0),
        ("run_shadow", ["run-shadow-calibration", "spec.yaml"], 5),
        ("resume_shadow", ["resume-shadow-calibration", "run"], 5),
        ("read_shadow_status", ["shadow-calibration-status", "run"], 0),
        (
            "record_review",
            ["record-shadow-review", "run", "proposal", "review.yaml"],
            5,
        ),
        ("read_shadow_report", ["shadow-calibration-report", "run"], 0),
        (
            "abort_shadow",
            ["abort-shadow-calibration", "run", "--reason", "stop"],
            8,
        ),
    ],
)
def test_all_seven_commands_support_human_and_json_success_modes(
    tmp_path: Path,
    monkeypatch,
    attribute: str,
    arguments: list[str],
    expected: int,
    as_json: bool,
) -> None:
    if attribute == "validate_shadow":
        value: object = SimpleNamespace(
            specification=SimpleNamespace(calibration_id="cli-shadow"),
            source=SimpleNamespace(
                run_directory=tmp_path / "source",
                decisions=(),
            ),
        )
    elif attribute == "read_shadow_report":
        value = {
            "schema_version": 1,
            "calibration_id": "cli-shadow",
            "source_stage2_run": str(tmp_path / "source"),
            "status": "awaiting_reviews",
            "readiness": {
                "status": "insufficient_data",
                "informational_only": True,
                "automation_enabled": False,
            },
            "assessments": [],
            "reviews": [],
        }
    else:
        status = "aborted" if attribute == "abort_shadow" else "awaiting_reviews"
        value = shadow_result(tmp_path, status)
    monkeypatch.setattr(
        cli,
        attribute,
        lambda *args, **kwargs: value,
    )
    command = [*arguments, *(["--json"] if as_json else [])]

    result = runner.invoke(app, command)

    assert result.exit_code == expected
    assert result.output


@pytest.mark.parametrize("command", ["validate-shadow-spec", "run-shadow-calibration"])
@pytest.mark.parametrize("as_json", [False, True])
def test_source_integrity_replacement_is_actual_cli_exit_four(
    tmp_path: Path,
    command: str,
    as_json: bool,
) -> None:
    spec, source_run, _, _ = create_shadow_tree(tmp_path)
    action = source_run / "actions/worker-r000.json"
    value = json.loads(action.read_text(encoding="utf-8"))
    value["repair_round"] = 99
    action.write_text(
        json.dumps(value, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    arguments = [command, str(spec)]
    if command == "run-shadow-calibration":
        arguments.extend(("--runs-dir", str(tmp_path / "runs")))
    if as_json:
        arguments.append("--json")

    result = runner.invoke(app, arguments)

    assert result.exit_code == 4
    assert not (tmp_path / "runs").exists()
    if as_json:
        assert json.loads(result.stdout)["error_kind"] == "integrity"
