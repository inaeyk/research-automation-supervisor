from __future__ import annotations

import json
from pathlib import Path

import yaml
from typer.testing import CliRunner

from research_automation_supervisor import __version__
from research_automation_supervisor.cli import app
from research_automation_supervisor.doctor import (
    CodexDiagnostic,
    DoctorReport,
    GitDiagnostic,
    PythonDiagnostic,
)

runner = CliRunner()


def contract_data() -> dict[str, object]:
    return {
        "schema_version": 1,
        "stage_id": "AUTOMATION-0",
        "title": "Foundation",
        "goal": "Validate it.",
        "allowed_paths": ["src/**"],
        "protected_paths": ["STAGE_0_CONTRACT.md"],
        "acceptance_tests": [
            {"id": "tests", "command": "pytest -q", "timeout_seconds": 60}
        ],
        "max_repair_rounds": 1,
        "checkpoint_after": True,
    }


def ready_report() -> DoctorReport:
    return DoctorReport(
        python=PythonDiagnostic("3.12.1", True),
        git=GitDiagnostic(True, "2.45.1", True, "/repo", True),
        codex=CodexDiagnostic(
            True, "0.144.0", True, authenticated=True, login_status="authenticated"
        ),
        dependency_errors=(),
    )


def test_version() -> None:
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert result.stdout == f"{__version__}\n"


def test_validate_contract_human_output(tmp_path: Path) -> None:
    path = tmp_path / "stage.yaml"
    path.write_text(yaml.safe_dump(contract_data()), encoding="utf-8")

    result = runner.invoke(app, ["validate-contract", str(path)])

    assert result.exit_code == 0
    assert result.stdout == f"Valid contract AUTOMATION-0: {path}\n"


def test_validate_contract_json_output(tmp_path: Path) -> None:
    path = tmp_path / "stage.yaml"
    path.write_text(yaml.safe_dump(contract_data()), encoding="utf-8")

    result = runner.invoke(app, ["validate-contract", str(path), "--json"])

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {
        "ok": True,
        "path": str(path),
        "stage_id": "AUTOMATION-0",
    }
    assert result.stdout.index('"ok"') < result.stdout.index('"path"')


def test_invalid_contract_human_exit_code(tmp_path: Path) -> None:
    path = tmp_path / "invalid.yaml"
    path.write_text("schema_version: 2\n", encoding="utf-8")

    result = runner.invoke(app, ["validate-contract", str(path)])

    assert result.exit_code == 2
    assert "Invalid contract: contract validation failed" in result.stderr


def test_invalid_contract_json_exit_code(tmp_path: Path) -> None:
    path = tmp_path / "invalid.yaml"
    path.write_text("not: [valid", encoding="utf-8")

    result = runner.invoke(app, ["validate-contract", str(path), "--json"])

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert "malformed YAML" in payload["error"]


def test_doctor_human_output(monkeypatch) -> None:
    monkeypatch.setattr("research_automation_supervisor.cli.run_doctor", ready_report)

    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0
    assert "Python: 3.12.1 (supported; >= 3.11)" in result.stdout
    assert "Repository clean: yes" in result.stdout
    assert "Codex login: authenticated" in result.stdout
    assert "Environment ready: yes" in result.stdout


def test_doctor_json_output(monkeypatch) -> None:
    monkeypatch.setattr("research_automation_supervisor.cli.run_doctor", ready_report)

    result = runner.invoke(app, ["doctor", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["git"]["repository_root"] == "/repo"
    assert payload["codex"]["authenticated"] is True


def test_doctor_dependency_failure_uses_exit_three(monkeypatch) -> None:
    report = DoctorReport(
        python=PythonDiagnostic("3.12.1", True),
        git=GitDiagnostic(True, "2.45.1", False),
        codex=CodexDiagnostic(False, error="Codex executable was not found."),
        dependency_errors=("Codex executable is required.",),
    )
    monkeypatch.setattr("research_automation_supervisor.cli.run_doctor", lambda: report)

    result = runner.invoke(app, ["doctor", "--json"])

    assert result.exit_code == 3
    assert json.loads(result.stdout)["ok"] is False
