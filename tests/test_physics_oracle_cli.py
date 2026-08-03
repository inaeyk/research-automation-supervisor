from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from research_automation_supervisor.cli import app
from tests.test_physics_oracle_execution import (
    BWRAP,
    CONTRACT,
    ORACLE_ID,
    _catalog,
    _passing_source,
    _workspace,
)

RUNNER = CliRunner()


def test_installed_help_exposes_only_catalog_selected_oracle_execution() -> None:
    result = RUNNER.invoke(app, ["run-physics-oracle", "--help"])

    assert result.exit_code == 0
    assert "--catalog" in result.stdout
    assert "--contract" in result.stdout
    assert "--oracle-id" in result.stdout
    assert "--workspace" in result.stdout
    assert "--output" in result.stdout
    assert "--argv" not in result.stdout


@pytest.mark.skipif(not BWRAP.is_file(), reason="Bubblewrap production path unavailable")
def test_model_free_cli_execution_emits_safe_bounded_json(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, _passing_source())
    catalog = _catalog(tmp_path, workspace)
    output = tmp_path / "oracle-output"

    result = RUNNER.invoke(
        app,
        [
            "run-physics-oracle",
            "--catalog",
            str(catalog),
            "--contract",
            str(CONTRACT),
            "--oracle-id",
            ORACLE_ID,
            "--task-id",
            "synthetic-task",
            "--workspace",
            str(workspace),
            "--output",
            str(output),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["status"] == "passed"
    assert payload["network_enforcement"]["capability"] == "enforced"
    assert "stdout.log" not in result.stdout
    assert "oracle-output" not in result.stdout
    assert (output / "completion-proof.json").is_file()


def test_cli_fails_closed_before_launch_on_executable_hash_mismatch(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, _passing_source())
    catalog = _catalog(tmp_path, workspace)
    value = json.loads(catalog.read_text())
    value["intents"][0]["executable"]["sha256"] = "0" * 64
    catalog.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")

    result = RUNNER.invoke(
        app,
        [
            "run-physics-oracle",
            "--catalog",
            str(catalog),
            "--contract",
            str(CONTRACT),
            "--oracle-id",
            ORACLE_ID,
            "--task-id",
            "synthetic-task",
            "--workspace",
            str(workspace),
            "--output",
            str(tmp_path / "oracle-output"),
            "--json",
        ],
    )

    assert result.exit_code == 3
    payload = json.loads(result.stdout)
    assert payload["error_kind"] == "dependency"
    assert "0" * 64 not in result.stdout
