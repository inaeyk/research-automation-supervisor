"""Command-line interface for deterministic supervisor foundations."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from research_automation_supervisor import __version__
from research_automation_supervisor.contract import load_contract
from research_automation_supervisor.doctor import DoctorReport, run_doctor
from research_automation_supervisor.errors import ContractError

app = typer.Typer(
    add_completion=False,
    help="Validate stage contracts and inspect the local execution environment.",
    no_args_is_help=True,
)


def version_callback(value: bool) -> None:
    """Print the package version for the eager root option."""
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=version_callback,
            is_eager=True,
            help="Show the package version and exit.",
        ),
    ] = False,
) -> None:
    """Research Automation Supervisor."""


@app.command()
def doctor(
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit stable machine-readable JSON.")
    ] = False,
) -> None:
    """Run read-only environment diagnostics."""
    report = run_doctor()
    if as_json:
        typer.echo(_stable_json(report.to_dict()))
    else:
        typer.echo(_format_doctor(report))
    if not report.ok:
        raise typer.Exit(code=3)


@app.command("validate-contract")
def validate_contract(
    path: Annotated[Path, typer.Argument(help="YAML stage contract to validate.")],
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit stable machine-readable JSON.")
    ] = False,
) -> None:
    """Load and validate a stage contract without writing files."""
    try:
        contract = load_contract(path)
    except ContractError as exc:
        if as_json:
            typer.echo(_stable_json({"error": str(exc), "ok": False, "path": str(path)}))
        else:
            typer.echo(f"Invalid contract: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    result = {"ok": True, "path": str(path), "stage_id": contract.stage_id}
    if as_json:
        typer.echo(_stable_json(result))
    else:
        typer.echo(f"Valid contract {contract.stage_id}: {path}")


def _stable_json(value: object) -> str:
    return json.dumps(value, indent=2, sort_keys=True)


def _format_doctor(report: DoctorReport) -> str:
    git_repository = _format_repository_state(report.git.inside_repository)
    git_clean = _format_cleanliness(report.git.clean)
    root = report.git.repository_root or "not available"
    git_version = _format_version(report.git.present, report.git.version)
    codex_version = _format_version(report.codex.present, report.codex.version)

    lines = [
        f"Python: {report.python.version} "
        f"({'supported' if report.python.supported else 'unsupported'}; >= "
        f"{report.python.minimum_version})",
        f"Git: {git_version}",
        f"Inside Git repository: {git_repository}",
        f"Repository root: {root}",
        f"Repository clean: {git_clean}",
        f"Codex: {codex_version} "
        f"({'supported' if report.codex.supported else 'unsupported'}; >= "
        f"{report.codex.minimum_version})",
        f"Codex login: {report.codex.login_status}",
        f"Environment ready: {'yes' if report.ok else 'no'}",
    ]
    lines.extend(f"ERROR: {error}" for error in report.dependency_errors)
    return "\n".join(lines)


def _format_cleanliness(clean: bool | None) -> str:
    if clean is None:
        return "not available"
    return "yes" if clean else "no"


def _format_repository_state(inside_repository: bool | None) -> str:
    if inside_repository is None:
        return "indeterminate"
    return "yes" if inside_repository else "no"


def _format_version(present: bool, version: str | None) -> str:
    if not present:
        return "not found"
    return version or "unavailable"
