from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from research_automation_supervisor.cli import app

FIXTURES = Path(__file__).parent / "fixtures/physics"
RUNNER = CliRunner()


def test_physics_validation_commands_are_in_help_without_runtime_dependencies() -> None:
    root = RUNNER.invoke(app, ["--help"], env={"PATH": "/nonexistent"})
    contract = RUNNER.invoke(
        app, ["validate-physics-contract", "--help"], env={"PATH": "/nonexistent"}
    )
    audit = RUNNER.invoke(
        app, ["validate-physics-audit", "--help"], env={"PATH": "/nonexistent"}
    )

    assert root.exit_code == contract.exit_code == audit.exit_code == 0
    assert "validate-physics-contract" in root.stdout
    assert "validate-physics-audit" in root.stdout
    assert "without model execution" in contract.stdout
    assert "without invoking a model" in audit.stdout


def test_validate_physics_contract_is_read_only_and_machine_stable() -> None:
    before = {
        path: (path.stat().st_mtime_ns, path.stat().st_size)
        for path in FIXTURES.rglob("*")
        if path.is_file()
    }

    result = RUNNER.invoke(
        app,
        [
            "validate-physics-contract",
            str(FIXTURES / "full_contract.yaml"),
            "--json",
        ],
        env={"PATH": "/nonexistent"},
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["profile"] == "physics_implementation"
    assert len(payload["canonical_sha256"]) == 64
    after = {
        path: (path.stat().st_mtime_ns, path.stat().st_size)
        for path in FIXTURES.rglob("*")
        if path.is_file()
    }
    assert after == before


def test_validate_physics_audit_prints_authoritative_model_free_route() -> None:
    result = RUNNER.invoke(
        app,
        [
            "validate-physics-audit",
            "--contract",
            str(FIXTURES / "full_contract.yaml"),
            "--report",
            str(FIXTURES / "repairable_report.json"),
            "--json",
        ],
        env={"PATH": "/nonexistent"},
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["decision"]["authoritative"] is True
    assert payload["decision"]["outcome"] == "request_repair"


def test_malformed_physics_inputs_use_safe_exit_two_without_traceback() -> None:
    contract = RUNNER.invoke(
        app,
        [
            "validate-physics-contract",
            str(FIXTURES / "malformed_contract.yaml"),
            "--json",
        ],
    )
    report = RUNNER.invoke(
        app,
        [
            "validate-physics-audit",
            "--contract",
            str(FIXTURES / "full_contract.yaml"),
            "--report",
            str(FIXTURES / "malformed_report.json"),
            "--json",
        ],
    )

    for result in (contract, report):
        assert result.exit_code == 2
        payload = json.loads(result.stdout)
        assert payload["ok"] is False
        assert payload["error_kind"] == "input"
        assert "Traceback" not in result.stdout
