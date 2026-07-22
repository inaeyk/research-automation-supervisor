from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from research_automation_supervisor import __version__
from research_automation_supervisor.cli import app
from research_automation_supervisor.doctor import (
    CodexDiagnostic,
    CommandResult,
    DoctorReport,
    GitDiagnostic,
    PythonDiagnostic,
    run_doctor,
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


def hostile_git_suffix_report() -> DoctorReport:
    root = Path("/repo")
    git = "/tools/git"
    codex = "/tools/codex"
    responses = {
        (git, "--version"): CommandResult(
            0, "git version 2.45.1 (token=AUDIT_SECRET_SENTINEL)\n"
        ),
        (git, "-C", str(root), "rev-parse", "--show-toplevel"): CommandResult(
            0, f"{root}\n"
        ),
        (
            git,
            "--no-optional-locks",
            "-C",
            str(root),
            "status",
            "--porcelain",
            "--untracked-files=normal",
        ): CommandResult(0),
        (codex, "--version"): CommandResult(0, "codex-cli 0.145.0\n"),
        (codex, "login", "status"): CommandResult(0, "authenticated\n"),
    }

    def command_runner(args: Sequence[str], *, timeout: float) -> CommandResult:
        assert timeout == 10.0
        return responses[tuple(args)]

    def which(command: str) -> str | None:
        return {"git": git, "codex": codex}.get(command)

    return run_doctor(
        runner=command_runner,
        which=which,
        cwd=root,
        python_version=(3, 11, 0),
    )


def hostile_codex_version_report() -> DoctorReport:
    root = Path("/repo")
    git = "/tools/git"
    codex = "/tools/codex"
    responses = {
        (git, "--version"): CommandResult(0, "git version 2.45.1\n"),
        (git, "-C", str(root), "rev-parse", "--show-toplevel"): CommandResult(
            0, f"{root}\n"
        ),
        (
            git,
            "--no-optional-locks",
            "-C",
            str(root),
            "status",
            "--porcelain",
            "--untracked-files=normal",
        ): CommandResult(0),
        (codex, "--version"): CommandResult(
            0, "codex-cli 0.145.0+SYNTHETICSECRETTOKEN\n"
        ),
        (codex, "login", "status"): CommandResult(0, "authenticated\n"),
    }

    def command_runner(args: Sequence[str], *, timeout: float) -> CommandResult:
        assert timeout == 10.0
        return responses[tuple(args)]

    def which(command: str) -> str | None:
        return {"git": git, "codex": codex}.get(command)

    return run_doctor(
        runner=command_runner,
        which=which,
        cwd=root,
        python_version=(3, 11, 0),
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


@pytest.mark.parametrize("as_json", [False, True])
def test_invalid_utf8_contract_uses_exit_two_without_traceback(
    tmp_path: Path, as_json: bool
) -> None:
    path = tmp_path / "invalid-utf8.yaml"
    path.write_bytes(b"\xffcredential-like-bytes")
    arguments = ["validate-contract", str(path)]
    if as_json:
        arguments.append("--json")

    result = runner.invoke(app, arguments)
    rendered = f"{result.stdout}\n{result.stderr}"

    assert result.exit_code == 2
    assert "not valid UTF-8 at byte offset 0" in rendered
    assert "credential-like-bytes" not in rendered
    assert "Traceback" not in rendered
    if as_json:
        assert json.loads(result.stdout)["ok"] is False
    else:
        assert "Invalid contract:" in result.stderr


def test_duplicate_yaml_key_uses_json_error_and_exit_two(tmp_path: Path) -> None:
    path = tmp_path / "duplicate.yaml"
    source = yaml.safe_dump(contract_data(), sort_keys=False)
    path.write_text(
        source.replace("schema_version: 1\n", "schema_version: 1\nschema_version: 1\n", 1),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["validate-contract", str(path), "--json"])

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert "duplicate mapping key" in payload["error"]
    assert "Traceback" not in result.stdout


@pytest.mark.parametrize("as_json", [False, True])
def test_unsafe_python_yaml_tag_uses_sanitized_exit_two(
    tmp_path: Path, as_json: bool
) -> None:
    sentinel = "AUDIT_SECRET_SENTINEL"
    path = tmp_path / "unsafe-tag.yaml"
    path.write_text(
        "goal: !!python/object/apply:os.system " f"['{sentinel}']\n",
        encoding="utf-8",
    )
    arguments = ["validate-contract", str(path)]
    if as_json:
        arguments.append("--json")

    result = runner.invoke(app, arguments)
    rendered = f"{result.stdout}\n{result.stderr}"

    assert result.exit_code == 2
    assert "could not determine a constructor" in rendered
    assert sentinel not in rendered
    assert "Traceback" not in rendered
    if as_json:
        assert json.loads(result.stdout)["ok"] is False
    else:
        assert "Invalid contract:" in result.stderr


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
    assert result.stdout == """{
  "codex": {
    "authenticated": true,
    "error": null,
    "login_status": "authenticated",
    "minimum_version": "0.144.0",
    "present": true,
    "supported": true,
    "version": "0.144.0"
  },
  "dependency_errors": [],
  "git": {
    "clean": true,
    "error": null,
    "inside_repository": true,
    "present": true,
    "repository_root": "/repo",
    "version": "2.45.1"
  },
  "ok": true,
  "python": {
    "minimum_version": "3.11",
    "supported": true,
    "version": "3.12.1"
  }
}
"""


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


def test_doctor_operational_failure_is_rendered_and_exits_three(monkeypatch) -> None:
    error = "Git repository probe failed or timed out."
    report = DoctorReport(
        python=PythonDiagnostic("3.12.1", True),
        git=GitDiagnostic(True, "2.45.1", None, error=error),
        codex=CodexDiagnostic(
            True,
            "0.145.0",
            True,
            authenticated=True,
            login_status="authenticated",
        ),
        dependency_errors=(error,),
    )
    monkeypatch.setattr("research_automation_supervisor.cli.run_doctor", lambda: report)

    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 3
    assert "Inside Git repository: indeterminate" in result.stdout
    assert "Environment ready: no" in result.stdout
    assert f"ERROR: {error}" in result.stdout


@pytest.mark.parametrize("as_json", [False, True])
def test_hostile_git_suffix_is_sanitized_and_exits_three(monkeypatch, as_json: bool) -> None:
    report = hostile_git_suffix_report()
    monkeypatch.setattr("research_automation_supervisor.cli.run_doctor", lambda: report)
    arguments = ["doctor"]
    if as_json:
        arguments.append("--json")

    result = runner.invoke(app, arguments)
    rendered = f"{result.stdout}\n{result.stderr}"

    assert not report.ok
    assert result.exit_code == 3
    assert "AUDIT_SECRET_SENTINEL" not in rendered
    assert "token=" not in rendered
    assert "Git version could not be determined." in rendered
    if as_json:
        payload = json.loads(result.stdout)
        assert payload["ok"] is False
        assert payload["git"]["version"] is None
    else:
        assert "Git: unavailable" in result.stdout
        assert "Environment ready: no" in result.stdout


@pytest.mark.parametrize("as_json", [False, True])
def test_hostile_codex_version_is_sanitized_and_exits_three(
    monkeypatch, as_json: bool
) -> None:
    report = hostile_codex_version_report()
    monkeypatch.setattr("research_automation_supervisor.cli.run_doctor", lambda: report)
    arguments = ["doctor"]
    if as_json:
        arguments.append("--json")

    result = runner.invoke(app, arguments)
    rendered = f"{result.stdout}\n{result.stderr}"

    assert not report.ok
    assert result.exit_code == 3
    assert "SYNTHETICSECRETTOKEN" not in rendered
    assert "+SYNTHETIC" not in rendered
    assert "Codex version could not be determined." in rendered
    if as_json:
        payload = json.loads(result.stdout)
        assert payload["ok"] is False
        assert payload["codex"]["version"] is None
    else:
        assert "Codex: unavailable" in result.stdout
        assert "Environment ready: no" in result.stdout
