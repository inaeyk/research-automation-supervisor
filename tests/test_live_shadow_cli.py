from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from research_automation_supervisor.cli import app
from research_automation_supervisor.live_shadow_engine import live_shadow_exit_code
from tests.live_shadow_helpers import create_live_shadow_tree

runner = CliRunner()


def test_validate_live_shadow_cli_is_read_only(tmp_path: Path) -> None:
    spec, _, _, _, _ = create_live_shadow_tree(tmp_path)
    before = tuple(sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*")))
    result = runner.invoke(app, ["validate-live-shadow-spec", str(spec), "--json"])
    after = tuple(sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*")))
    assert result.exit_code == 0
    assert '"live_shadow_id": "minimal-live-shadow"' in result.stdout
    assert before == after


def test_all_stage4_commands_are_registered() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in (
        "validate-live-shadow-spec",
        "run-live-shadow",
        "resume-live-shadow",
        "live-shadow-status",
        "record-live-shadow-review",
        "live-shadow-report",
        "abort-live-shadow",
    ):
        assert command in result.stdout


def test_live_shadow_exit_codes_are_frozen() -> None:
    assert live_shadow_exit_code("completed") == 0
    assert live_shadow_exit_code("awaiting_reviews") == 5
    assert live_shadow_exit_code("shadow_degraded") == 5
    assert live_shadow_exit_code("human_paused") == 5
    assert live_shadow_exit_code("failed") == 4
    assert live_shadow_exit_code("aborted") == 8
