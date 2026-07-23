"""Command-line interface for deterministic supervisor foundations."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Never

import typer

from research_automation_supervisor import __version__
from research_automation_supervisor.codex_adapter import (
    build_subprocess_environment,
    execute_codex_request,
    validate_locator_confidentiality,
    validate_request_confidentiality,
)
from research_automation_supervisor.codex_models import (
    CodexRunResult,
    RunStatus,
    load_codex_request,
)
from research_automation_supervisor.contract import load_contract
from research_automation_supervisor.doctor import DoctorReport, run_doctor
from research_automation_supervisor.errors import (
    CodexConfidentialityError,
    CodexDependencyError,
    CodexRequestError,
    ContractError,
    WorkflowDependencyError,
    WorkflowInputError,
    WorkflowLockError,
    WorkflowStateError,
)
from research_automation_supervisor.redaction import redact_text
from research_automation_supervisor.workflow_engine import (
    abort_substage as abort_workflow,
)
from research_automation_supervisor.workflow_engine import (
    continue_substage as continue_workflow,
)
from research_automation_supervisor.workflow_engine import (
    resume_substage as resume_workflow,
)
from research_automation_supervisor.workflow_engine import (
    run_substage as run_workflow,
)
from research_automation_supervisor.workflow_engine import (
    substage_status as read_workflow_status,
)
from research_automation_supervisor.workflow_engine import (
    validate_substage as validate_workflow,
)
from research_automation_supervisor.workflow_engine import workflow_exit_code
from research_automation_supervisor.workflow_models import WorkflowResult

app = typer.Typer(
    add_completion=False,
    help="Validate supervisor inputs, inspect the environment, and run Codex deterministically.",
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


@app.command("validate-codex-request")
def validate_codex_request(
    path: Annotated[Path, typer.Argument(help="YAML Codex run request to validate.")],
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit stable machine-readable JSON.")
    ] = False,
) -> None:
    """Load and validate a Codex request without writing files or launching Codex."""
    try:
        _, _, sensitive_values = build_subprocess_environment()
        validate_locator_confidentiality((path,), sensitive_values)
        prepared = load_codex_request(path)
        validate_request_confidentiality(
            prepared,
            sensitive_values,
            request_locator=path,
        )
    except CodexDependencyError as exc:
        _render_codex_input_error(str(exc), path, as_json, dependency=True)
    except CodexConfidentialityError as exc:
        _render_codex_input_error(
            str(exc),
            path,
            as_json,
            dependency=False,
            hide_locator=True,
        )
    except CodexRequestError as exc:
        _render_codex_input_error(str(exc), path, as_json, dependency=False)
    except Exception:
        _render_internal_error(as_json)

    result = {"ok": True, "path": str(path), "request": prepared.normalized_dict()}
    if as_json:
        typer.echo(_stable_json(result))
    else:
        typer.echo(
            f"Valid Codex request {prepared.request.run_id} "
            f"({prepared.request.role}): {path}"
        )


@app.command("run-codex")
def run_codex(
    path: Annotated[Path, typer.Argument(help="YAML Codex run request to execute.")],
    runs_dir: Annotated[
        Path,
        typer.Option(
            "--runs-dir",
            help="Directory under which the exclusive run directory is created.",
        ),
    ] = Path("runs/codex"),
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit stable machine-readable JSON.")
    ] = False,
) -> None:
    """Validate and execute one exact prompt through the deterministic Codex adapter."""
    try:
        result = execute_codex_request(path, runs_dir=runs_dir)
    except CodexDependencyError as exc:
        _render_codex_input_error(str(exc), path, as_json, dependency=True)
    except CodexConfidentialityError as exc:
        _render_codex_input_error(
            str(exc),
            path,
            as_json,
            dependency=False,
            hide_locator=True,
        )
    except CodexRequestError as exc:
        _render_codex_input_error(str(exc), path, as_json, dependency=False)
    except Exception:
        _render_internal_error(as_json)

    if as_json:
        typer.echo(_stable_json(result.to_dict()))
    else:
        typer.echo(_format_codex_result(result))
    exit_code = _codex_status_exit_code(result.status)
    if exit_code:
        raise typer.Exit(code=exit_code)


@app.command("validate-substage")
def validate_substage_command(
    path: Annotated[Path, typer.Argument(help="YAML substage specification to validate.")],
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit stable machine-readable JSON.")
    ] = False,
) -> None:
    """Validate a Stage 2 substage without writes or process launches."""
    try:
        prepared = validate_workflow(path)
    except WorkflowDependencyError as exc:
        _render_workflow_error(str(exc), as_json, 3)
    except WorkflowInputError as exc:
        _render_workflow_error(str(exc), as_json, 2)
    except Exception:
        _render_workflow_internal_error(as_json)
    result = {
        "ok": True,
        "path": str(path),
        "substage_id": prepared.specification.substage_id,
        "workspace": str(prepared.workspace),
        "acceptance_test_ids": [
            test.specification.id for test in prepared.acceptance_tests
        ],
    }
    if as_json:
        typer.echo(_stable_json(result))
    else:
        typer.echo(
            f"Valid substage {prepared.specification.substage_id}: {path}\n"
            f"Workspace: {prepared.workspace}\n"
            f"Fixed tests: {len(prepared.acceptance_tests)}"
        )


@app.command("run-substage")
def run_substage_command(
    path: Annotated[Path, typer.Argument(help="YAML substage specification to execute.")],
    runs_dir: Annotated[
        Path,
        typer.Option(
            "--runs-dir",
            help="Directory under which the exclusive workflow run is created.",
        ),
    ] = Path("runs/workflows"),
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit stable machine-readable JSON.")
    ] = False,
) -> None:
    """Run one deterministic single-substage workflow synchronously."""
    try:
        result = run_workflow(path, runs_dir=runs_dir)
    except WorkflowDependencyError as exc:
        _render_workflow_error(str(exc), as_json, 3)
    except WorkflowInputError as exc:
        _render_workflow_error(str(exc), as_json, 2)
    except (WorkflowLockError, WorkflowStateError) as exc:
        _render_workflow_error(str(exc), as_json, 4)
    except Exception:
        _render_workflow_internal_error(as_json)
    _render_workflow_result_and_exit(result, as_json)


@app.command("resume-substage")
def resume_substage_command(
    run_directory: Annotated[Path, typer.Argument(help="Existing workflow run directory.")],
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit stable machine-readable JSON.")
    ] = False,
) -> None:
    """Continue an interrupted nonterminal workflow from its last safe state."""
    try:
        result = resume_workflow(run_directory)
    except WorkflowDependencyError as exc:
        _render_workflow_error(str(exc), as_json, 3)
    except WorkflowInputError as exc:
        _render_workflow_error(str(exc), as_json, 2)
    except (WorkflowLockError, WorkflowStateError) as exc:
        _render_workflow_error(str(exc), as_json, 4)
    except Exception:
        _render_workflow_internal_error(as_json)
    _render_workflow_result_and_exit(result, as_json)


@app.command("continue-substage")
def continue_substage_command(
    run_directory: Annotated[Path, typer.Argument(help="Paused workflow run directory.")],
    instruction: Annotated[
        Path,
        typer.Option("--instruction", help="Exact human-written continuation file."),
    ],
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit stable machine-readable JSON.")
    ] = False,
) -> None:
    """Append one exact human instruction to the persistent worker session."""
    try:
        result = continue_workflow(run_directory, instruction)
    except WorkflowDependencyError as exc:
        _render_workflow_error(str(exc), as_json, 3)
    except WorkflowInputError as exc:
        _render_workflow_error(str(exc), as_json, 2)
    except (WorkflowLockError, WorkflowStateError) as exc:
        _render_workflow_error(str(exc), as_json, 4)
    except Exception:
        _render_workflow_internal_error(as_json)
    _render_workflow_result_and_exit(result, as_json)


@app.command("substage-status")
def substage_status_command(
    run_directory: Annotated[Path, typer.Argument(help="Workflow run directory to inspect.")],
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit stable machine-readable JSON.")
    ] = False,
) -> None:
    """Read durable workflow state without writes or process launches."""
    try:
        result = read_workflow_status(run_directory)
    except WorkflowInputError as exc:
        _render_workflow_error(str(exc), as_json, 2)
    except (WorkflowLockError, WorkflowStateError) as exc:
        _render_workflow_error(str(exc), as_json, 4)
    except Exception:
        _render_workflow_internal_error(as_json)
    if as_json:
        typer.echo(_stable_json(result.to_dict()))
    else:
        typer.echo(_format_workflow_result(result))


@app.command("abort-substage")
def abort_substage_command(
    run_directory: Annotated[Path, typer.Argument(help="Workflow run directory to abort.")],
    reason: Annotated[str, typer.Option("--reason", help="Human abort reason.")],
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit stable machine-readable JSON.")
    ] = False,
) -> None:
    """Atomically abort a nonterminal workflow that is not actively running."""
    try:
        result = abort_workflow(run_directory, reason)
    except WorkflowInputError as exc:
        _render_workflow_error(str(exc), as_json, 2)
    except WorkflowDependencyError as exc:
        _render_workflow_error(str(exc), as_json, 3)
    except (WorkflowLockError, WorkflowStateError) as exc:
        _render_workflow_error(str(exc), as_json, 4)
    except Exception:
        _render_workflow_internal_error(as_json)
    _render_workflow_result_and_exit(result, as_json)


def _stable_json(value: object) -> str:
    return json.dumps(value, indent=2, sort_keys=True)


def _render_workflow_result_and_exit(result: WorkflowResult, as_json: bool) -> None:
    if as_json:
        typer.echo(_stable_json(result.to_dict()))
    else:
        typer.echo(_format_workflow_result(result))
    exit_code = workflow_exit_code(result.status)
    if exit_code:
        raise typer.Exit(code=exit_code)


def _format_workflow_result(result: WorkflowResult) -> str:
    return "\n".join(
        (
            f"Substage: {result.substage_id}",
            f"Status: {result.status}",
            f"Summary: {result.summary}",
            f"Repair round: {result.repair_round}/{result.max_repair_rounds}",
            f"Worker thread: {result.worker_thread_id or 'not available'}",
            f"Scope compliant: {'yes' if result.scope_compliant else 'no'}",
            f"Fixed tests passed: {'yes' if result.tests_passed else 'no'}",
            f"Contract satisfied: {'yes' if result.contract_satisfied else 'no'}",
            f"Pause reason: {result.pause_reason or 'none'}",
            f"Artifacts: {result.artifact_directory}",
        )
    )


def _render_workflow_error(error: str, as_json: bool, exit_code: int) -> Never:
    _, _, sensitive_values = build_subprocess_environment()
    sanitized = redact_text(error, sensitive_values)
    if as_json:
        typer.echo(
            _stable_json(
                {
                    "error": sanitized,
                    "error_kind": (
                        "input"
                        if exit_code == 2
                        else "dependency"
                        if exit_code == 3
                        else "workflow"
                    ),
                    "ok": False,
                }
            )
        )
    else:
        typer.echo(f"Workflow error: {sanitized}", err=True)
    raise typer.Exit(code=exit_code)


def _render_workflow_internal_error(as_json: bool) -> Never:
    message = "Unexpected internal workflow engine failure."
    if as_json:
        typer.echo(_stable_json({"error": message, "error_kind": "internal", "ok": False}))
    else:
        typer.echo(message, err=True)
    raise typer.Exit(code=1)


def _render_codex_input_error(
    error: str,
    path: Path,
    as_json: bool,
    *,
    dependency: bool,
    hide_locator: bool = False,
) -> Never:
    _, _, sensitive_values = build_subprocess_environment()
    sanitized_error = redact_text(error, sensitive_values)
    sanitized_path = "<REDACTED>" if hide_locator else redact_text(str(path), sensitive_values)
    kind = "dependency" if dependency else "input"
    if as_json:
        typer.echo(
            _stable_json(
                {
                    "error": sanitized_error,
                    "error_kind": kind,
                    "ok": False,
                    "path": sanitized_path,
                }
            )
        )
    else:
        prefix = "Missing dependency" if dependency else "Invalid Codex request"
        typer.echo(f"{prefix}: {sanitized_error}", err=True)
    raise typer.Exit(code=3 if dependency else 2)


def _render_internal_error(as_json: bool) -> Never:
    message = "Unexpected internal Codex adapter failure."
    if as_json:
        typer.echo(_stable_json({"error": message, "error_kind": "internal", "ok": False}))
    else:
        typer.echo(message, err=True)
    raise typer.Exit(code=1)


def _format_codex_result(result: CodexRunResult) -> str:
    return "\n".join(
        [
            f"Run: {result.run_id}",
            f"Status: {result.status}",
            f"Summary: {result.summary}",
            f"Exit code: {result.exit_code if result.exit_code is not None else 'not available'}",
            f"Events: {result.event_count} valid, "
            f"{result.malformed_event_count} malformed",
            f"Final message: {'present' if result.final_message_present else 'missing'}",
            f"Artifacts: {result.artifact_directory}",
        ]
    )


def _codex_status_exit_code(status: RunStatus) -> int:
    return {
        "succeeded": 0,
        "launch_failed": 4,
        "timed_out": 5,
        "output_limit_exceeded": 7,
        "permission_blocked": 6,
        "malformed_event_stream": 7,
        "process_failed": 4,
        "missing_final_message": 7,
    }[status]


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
