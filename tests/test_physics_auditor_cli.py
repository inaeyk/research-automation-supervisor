from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from research_automation_supervisor.cli import app
from tests.test_physics_auditor_execution import (
    BWRAP,
    SYNTHETIC,
    _evidence,
    _pinned_fake_config,
    _workspace,
)

RUNNER = CliRunner()


def test_installed_help_is_dependency_free_and_exposes_no_prompt_or_command() -> None:
    result = RUNNER.invoke(app, ["audit-physics", "--help"], env={"PATH": "/nonexistent"})

    assert result.exit_code == 0
    assert "--contract" in result.stdout
    assert "--execution-config" in result.stdout
    assert "--oracle-evidence" in result.stdout
    assert "--validate-only" in result.stdout
    assert "--prompt" not in result.stdout
    assert "--command" not in result.stdout
    assert "no repair" in result.stdout


def test_validate_only_does_not_locate_codex_create_output_or_mutate_workspace(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    evidence = tmp_path / "empty-evidence"
    evidence.mkdir()
    output = tmp_path / "must-not-exist"
    before = {path.name: path.read_bytes() for path in workspace.iterdir() if path.is_file()}

    result = RUNNER.invoke(
        app,
        [
            "audit-physics",
            "--contract",
            str(SYNTHETIC / "contract.yaml"),
            "--execution-config",
            str(SYNTHETIC / "execution-config.yaml"),
            "--task-id",
            "synthetic-task",
            "--workspace",
            str(workspace),
            "--oracle-evidence",
            str(evidence),
            "--output",
            str(output),
            "--validate-only",
            "--json",
        ],
        env={"PATH": "/nonexistent", "HOME": "/nonexistent"},
    )

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["validate_only"] is True
    assert payload["model_launched"] is False
    assert payload["missing_required_oracle_ids"] == ["force_oracle"]
    assert not output.exists()
    after = {path.name: path.read_bytes() for path in workspace.iterdir() if path.is_file()}
    assert after == before


def test_missing_codex_is_reported_only_when_execution_is_requested(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    evidence = tmp_path / "empty-evidence"
    evidence.mkdir()

    result = RUNNER.invoke(
        app,
        [
            "audit-physics",
            "--contract",
            str(SYNTHETIC / "contract.yaml"),
            "--execution-config",
            str(SYNTHETIC / "execution-config.yaml"),
            "--task-id",
            "synthetic-task",
            "--workspace",
            str(workspace),
            "--oracle-evidence",
            str(evidence),
            "--output",
            str(tmp_path / "audit-output"),
            "--json",
        ],
        env={"PATH": "/nonexistent", "HOME": "/nonexistent"},
    )

    assert result.exit_code == 3
    payload = json.loads(result.stdout)
    assert payload["error_kind"] == "dependency"
    assert "Codex executable is required" in payload["error"]


@pytest.mark.skipif(not BWRAP.is_file(), reason="Bubblewrap PA-2 verification unavailable")
def test_standalone_cli_emits_safe_summary_and_never_repairs(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    observation = tmp_path / "fake-observation.json"
    (workspace / ".fake-codex.json").write_text(
        json.dumps(
            {
                "require_stage2_policy": True,
                "expected_sandbox": "read-only",
                "expected_ephemeral": True,
                "observation_path": str(observation),
                "stdout_lines": [
                    '{"thread_id":"fresh-cli-physics-thread","type":"thread.started"}'
                ],
                "final": (SYNTHETIC / "reports/clean.json").read_text(),
            }
        )
    )
    evidence = _evidence(tmp_path, workspace)
    output = tmp_path / "audit-output"

    result = RUNNER.invoke(
        app,
        [
            "audit-physics",
            "--contract",
            str(SYNTHETIC / "contract.yaml"),
            "--execution-config",
            str(_pinned_fake_config(tmp_path)),
            "--task-id",
            "synthetic-task",
            "--workspace",
            str(workspace),
            "--oracle-evidence",
            str(evidence),
            "--output",
            str(output),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["status"] == "routing_completed"
    assert payload["routing_decision"]["outcome"] == "pass"
    assert payload["integrity_verdict"] == "unchanged"
    assert str(tmp_path) not in result.stdout
    assert (output / "action-proof.json").is_file()
