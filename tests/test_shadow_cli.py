from __future__ import annotations

import json
import os
import shutil
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
from research_automation_supervisor.shadow_engine import (
    run_shadow_calibration,
)
from research_automation_supervisor.shadow_models import ShadowResult
from tests.shadow_helpers import (
    SUPERVISOR_UUID,
    create_shadow_tree,
    shadow_services,
    write_review,
)

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


@pytest.mark.parametrize("as_json", [False, True])
def test_actual_cli_rejects_raw_specification_locator_before_normalization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    as_json: bool,
) -> None:
    sensitive = "CLI_RAW_SPEC_SENTINEL_d1a7"
    spec, _, _, _ = create_shadow_tree(tmp_path)
    sensitive_directory = tmp_path / sensitive
    sensitive_directory.mkdir()
    lexical = sensitive_directory / ".." / spec.relative_to(tmp_path)
    before = _file_snapshot(tmp_path)
    monkeypatch.setenv("AUDIT_TOKEN", sensitive)
    arguments = ["validate-shadow-spec", str(lexical)]
    if as_json:
        arguments.append("--json")

    result = runner.invoke(app, arguments)

    assert result.exit_code == 2
    assert sensitive not in result.output
    assert _file_snapshot(tmp_path) == before
    if as_json:
        assert json.loads(result.stdout)["error_kind"] == "input"


@pytest.mark.parametrize("as_json", [False, True])
def test_actual_cli_rejects_raw_runs_dir_before_spec_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    as_json: bool,
) -> None:
    sensitive = "CLI_RAW_RUNS_SENTINEL_47be"
    spec, _, _, _ = create_shadow_tree(tmp_path)
    sensitive_directory = tmp_path / sensitive
    sensitive_directory.mkdir()
    lexical_runs = sensitive_directory / ".." / "shadow-runs"
    before = _file_snapshot(tmp_path)
    monkeypatch.setenv("AUDIT_TOKEN", sensitive)
    arguments = [
        "run-shadow-calibration",
        str(spec),
        "--runs-dir",
        str(lexical_runs),
    ]
    if as_json:
        arguments.append("--json")

    result = runner.invoke(app, arguments)

    assert result.exit_code == 2
    assert sensitive not in result.output
    assert not (tmp_path / "shadow-runs").exists()
    assert not (tmp_path / "shadow-counter").exists()
    assert _file_snapshot(tmp_path) == before


@pytest.mark.parametrize("as_json", [False, True])
def test_actual_cli_rejects_sensitive_discovered_executable_before_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    as_json: bool,
) -> None:
    sensitive = "CLI_EXECUTABLE_SENTINEL_6a83"
    spec, _, _, fake = create_shadow_tree(tmp_path)
    executable_directory = tmp_path / sensitive
    executable_directory.mkdir()
    executable = executable_directory / "codex"
    shutil.copy2(fake, executable)
    executable.chmod(0o755)
    before = _file_snapshot(tmp_path)
    monkeypatch.setenv("AUDIT_TOKEN", sensitive)
    monkeypatch.setenv(
        "PATH",
        f"{executable_directory}{os.pathsep}{os.environ['PATH']}",
    )
    arguments = [
        "run-shadow-calibration",
        str(spec),
        "--runs-dir",
        str(tmp_path / "shadow-runs"),
    ]
    if as_json:
        arguments.append("--json")

    result = runner.invoke(app, arguments)

    assert result.exit_code == 2
    assert sensitive not in result.output
    assert not (tmp_path / "shadow-runs").exists()
    assert not (tmp_path / "shadow-counter").exists()
    assert _file_snapshot(tmp_path) == before


@pytest.mark.parametrize("as_json", [False, True])
@pytest.mark.parametrize(
    "command",
    [
        "resume-shadow-calibration",
        "shadow-calibration-status",
        "record-shadow-review",
        "shadow-calibration-report",
        "abort-shadow-calibration",
    ],
)
def test_actual_cli_rejects_raw_run_directory_for_every_existing_run_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    as_json: bool,
) -> None:
    spec, _, _, fake = create_shadow_tree(tmp_path)
    completed = run_shadow_calibration(
        spec,
        runs_dir=tmp_path / "shadow-runs",
        services=shadow_services(fake),
    )
    run_directory = Path(completed.artifact_directory)
    review = write_review(
        tmp_path / "review.yaml",
        "worker_initial-r000-a001",
    )
    sensitive = "CLI_RAW_RUN_SENTINEL_b52c"
    sensitive_directory = run_directory.parent / sensitive
    sensitive_directory.mkdir()
    lexical_run = sensitive_directory / ".." / run_directory.name
    before = _file_snapshot(run_directory)
    counter = (tmp_path / "shadow-counter").read_text()
    monkeypatch.setenv("AUDIT_TOKEN", sensitive)
    arguments = [command, str(lexical_run)]
    if command == "record-shadow-review":
        arguments.extend(
            ("worker_initial-r000-a001", str(review))
        )
    elif command == "abort-shadow-calibration":
        arguments.extend(("--reason", "stop"))
    if as_json:
        arguments.append("--json")

    result = runner.invoke(app, arguments)

    assert result.exit_code == 2
    assert sensitive not in result.output
    assert _file_snapshot(run_directory) == before
    assert (tmp_path / "shadow-counter").read_text() == counter


@pytest.mark.parametrize("as_json", [False, True])
def test_actual_cli_rejects_raw_review_locator_before_run_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    as_json: bool,
) -> None:
    spec, _, _, fake = create_shadow_tree(tmp_path)
    completed = run_shadow_calibration(
        spec,
        runs_dir=tmp_path / "shadow-runs",
        services=shadow_services(fake),
    )
    run_directory = Path(completed.artifact_directory)
    review = write_review(
        tmp_path / "review.yaml",
        "worker_initial-r000-a001",
    )
    sensitive = "CLI_RAW_REVIEW_SENTINEL_91ce"
    sensitive_directory = tmp_path / sensitive
    sensitive_directory.mkdir()
    lexical_review = sensitive_directory / ".." / review.name
    before = _file_snapshot(run_directory)
    counter = (tmp_path / "shadow-counter").read_text()
    monkeypatch.setenv("AUDIT_TOKEN", sensitive)
    arguments = [
        "record-shadow-review",
        str(run_directory),
        "worker_initial-r000-a001",
        str(lexical_review),
    ]
    if as_json:
        arguments.append("--json")

    result = runner.invoke(app, arguments)

    assert result.exit_code == 2
    assert sensitive not in result.output
    assert _file_snapshot(run_directory) == before
    assert (tmp_path / "shadow-counter").read_text() == counter


@pytest.mark.parametrize("as_json", [False, True])
@pytest.mark.parametrize("field", ["proposal_id", "abort_reason"])
def test_actual_cli_rejects_sensitive_structural_command_strings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    as_json: bool,
) -> None:
    spec, _, _, fake = create_shadow_tree(tmp_path)
    completed = run_shadow_calibration(
        spec,
        runs_dir=tmp_path / "shadow-runs",
        services=shadow_services(fake),
    )
    run_directory = Path(completed.artifact_directory)
    review = write_review(tmp_path / "review.yaml", "safe-proposal")
    sensitive = "CLI_COMMAND_STRING_SENTINEL_e43f"
    before = _file_snapshot(run_directory)
    counter = (tmp_path / "shadow-counter").read_text()
    monkeypatch.setenv("AUDIT_TOKEN", sensitive)
    if field == "proposal_id":
        arguments = [
            "record-shadow-review",
            str(run_directory),
            sensitive,
            str(review),
        ]
    else:
        arguments = [
            "abort-shadow-calibration",
            str(run_directory),
            "--reason",
            sensitive,
        ]
    if as_json:
        arguments.append("--json")

    result = runner.invoke(app, arguments)

    assert result.exit_code == 2
    assert sensitive not in result.output
    assert _file_snapshot(run_directory) == before
    assert (tmp_path / "shadow-counter").read_text() == counter


@pytest.mark.parametrize("as_json", [False, True])
def test_actual_cli_maps_sensitive_durable_state_to_integrity_exit_four(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    as_json: bool,
) -> None:
    spec, _, _, fake = create_shadow_tree(tmp_path)
    completed = run_shadow_calibration(
        spec,
        runs_dir=tmp_path / "shadow-runs",
        services=shadow_services(fake),
    )
    run_directory = Path(completed.artifact_directory)
    sensitive = "DURABLE_STATE_SENTINEL_c39d"
    state_path = run_directory / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["summary"] = sensitive
    state_path.write_text(
        json.dumps(state, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    before = _file_snapshot(run_directory)
    counter = (tmp_path / "shadow-counter").read_text()
    monkeypatch.setenv("AUDIT_TOKEN", sensitive)
    arguments = ["shadow-calibration-status", str(run_directory)]
    if as_json:
        arguments.append("--json")

    result = runner.invoke(app, arguments)

    assert result.exit_code == 4
    assert sensitive not in result.output
    assert _file_snapshot(run_directory) == before
    assert (tmp_path / "shadow-counter").read_text() == counter
    if as_json:
        assert json.loads(result.stdout)["error_kind"] == "integrity"


def _file_snapshot(root: Path) -> dict[Path, bytes]:
    return {
        path.relative_to(root): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }
