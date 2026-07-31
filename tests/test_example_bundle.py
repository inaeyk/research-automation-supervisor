from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from typer.testing import CliRunner

from research_automation_supervisor.cli import app
from research_automation_supervisor.example_bundle import (
    materialize_synthetic_example,
)

runner = CliRunner()


def _git(repository: Path, *arguments: str) -> None:
    subprocess.run(
        ("git", "-C", str(repository), *arguments),
        env={
            **os.environ,
            "GIT_AUTHOR_EMAIL": "synthetic@example.invalid",
            "GIT_AUTHOR_NAME": "Synthetic Quick Start",
            "GIT_COMMITTER_EMAIL": "synthetic@example.invalid",
            "GIT_COMMITTER_NAME": "Synthetic Quick Start",
            "LC_ALL": "C",
        },
        check=True,
        capture_output=True,
    )


def test_init_example_materializes_installed_resource_tree(tmp_path: Path) -> None:
    output = tmp_path / "example"

    result = runner.invoke(app, ["init-example", "--output", str(output)])

    assert result.exit_code == 0, result.output
    assert (output / "config/substage.yaml").is_file()
    assert (output / "project/tools/codex").stat().st_mode & 0o111
    assert "Synthetic example created" in result.output


def test_synthetic_quick_start_runs_complete_workflow(tmp_path: Path) -> None:
    example = materialize_synthetic_example(tmp_path / "example")
    project = example / "project"
    specification = example / "config/substage.yaml"
    _git(project, "init", "-q")
    _git(project, "add", ".")
    _git(project, "commit", "-q", "-m", "synthetic baseline")

    validation = runner.invoke(app, ["validate-substage", str(specification)])
    run = runner.invoke(
        app,
        [
            "run-substage",
            str(specification),
            "--runs-dir",
            str(example / "runs"),
            "--json",
        ],
        env={"PATH": f"{project / 'tools'}:{os.environ['PATH']}"},
    )

    assert validation.exit_code == 0, validation.output
    assert run.exit_code == 0, run.output
    result = json.loads(run.stdout)
    assert result["status"] == "completed"
    assert result["tests_passed"] is True
    assert result["scope_compliant"] is True
    assert result["contract_satisfied"] is True
    assert (project / "src/ready.txt").read_bytes() == b"ready\n"
