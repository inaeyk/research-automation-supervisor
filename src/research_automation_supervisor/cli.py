"""Command-line interface for deterministic supervisor foundations."""

from __future__ import annotations

import json
from contextlib import suppress
from pathlib import Path
from typing import Annotated, Never, cast

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
    LiveShadowDependencyError,
    LiveShadowInputError,
    LiveShadowLockError,
    LiveShadowStateError,
    PhysicsAuditError,
    PhysicsAuditorDependencyError,
    PhysicsAuditorInputError,
    PhysicsAuditorIntegrityError,
    PhysicsAuditorStateError,
    PhysicsContractError,
    PhysicsOracleDependencyError,
    PhysicsOracleInputError,
    PhysicsOracleIntegrityError,
    PhysicsOracleStateError,
    ReplayCampaignDependencyError,
    ReplayCampaignInputError,
    ReplayCampaignLockError,
    ReplayCampaignStateError,
    ShadowDependencyError,
    ShadowInputError,
    ShadowLockError,
    ShadowStateError,
    WorkflowDependencyError,
    WorkflowInputError,
    WorkflowLockError,
    WorkflowStateError,
)
from research_automation_supervisor.example_bundle import (
    ExampleBundleError,
    materialize_synthetic_example,
)
from research_automation_supervisor.live_shadow_engine import (
    DEFAULT_LIVE_SHADOW_RUNS_DIRECTORY,
    abort_live_shadow,
    live_shadow_exit_code,
    live_shadow_report,
    live_shadow_status,
    record_live_shadow_review,
    resume_live_shadow,
    run_live_shadow,
    validate_live_shadow_spec,
)
from research_automation_supervisor.live_shadow_isolation import (
    resolve_authentication_confidentiality,
)
from research_automation_supervisor.live_shadow_models import LiveShadowResult
from research_automation_supervisor.physics_auditor_execution import (
    resume_physics_auditor,
    run_physics_auditor,
    validate_physics_auditor_action,
)
from research_automation_supervisor.physics_models import (
    DEFAULT_PHYSICS_AUDIT_POLICY_V1,
    load_physics_audit_report,
    load_physics_task_contract,
)
from research_automation_supervisor.physics_oracle_execution import (
    run_physics_oracle,
)
from research_automation_supervisor.physics_routing import (
    derive_physics_audit_decision,
)
from research_automation_supervisor.redaction import redact_json, redact_text
from research_automation_supervisor.replay_campaign_engine import (
    DEFAULT_REPLAY_RUNS_DIRECTORY,
    replay_campaign_exit_code,
    replay_campaign_status,
    resume_replay_campaign,
    run_replay_campaign,
)
from research_automation_supervisor.replay_campaign_models import ReplayCampaignState
from research_automation_supervisor.shadow_confidentiality import (
    preflight_shadow_confidentiality,
)
from research_automation_supervisor.shadow_engine import (
    abort_shadow_calibration as abort_shadow,
)
from research_automation_supervisor.shadow_engine import (
    record_shadow_review as record_review,
)
from research_automation_supervisor.shadow_engine import (
    resume_shadow_calibration as resume_shadow,
)
from research_automation_supervisor.shadow_engine import (
    run_shadow_calibration as run_shadow,
)
from research_automation_supervisor.shadow_engine import (
    shadow_calibration_exit_code,
)
from research_automation_supervisor.shadow_engine import (
    shadow_calibration_report as read_shadow_report,
)
from research_automation_supervisor.shadow_engine import (
    shadow_calibration_status as read_shadow_status,
)
from research_automation_supervisor.shadow_engine import (
    validate_shadow_spec as validate_shadow,
)
from research_automation_supervisor.shadow_models import ShadowResult
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
from research_automation_supervisor.workflow_recovery import (
    RecoveryExecutionV1,
    RecoverySelectionError,
    build_recovery_plan,
    discover_workflow_runs,
    execute_recovery_plan,
    latest_incomplete_run,
)
from research_automation_supervisor.workflow_recovery_models import (
    RecoveryPlanV1,
    RunIndexV1,
)

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


@app.command("init-example")
def init_example_command(
    output: Annotated[
        Path,
        typer.Option("--output", help="New directory for the synthetic quick start."),
    ],
) -> None:
    """Materialize the bundled non-model synthetic workflow example."""
    try:
        destination = materialize_synthetic_example(output)
    except ExampleBundleError as exc:
        typer.echo(f"Could not create example: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(f"Synthetic example created: {destination}")
    typer.echo(f"Next: read {destination / 'README.md'}")


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


@app.command("validate-physics-contract")
def validate_physics_contract_command(
    path: Annotated[
        Path,
        typer.Argument(help="Standalone Physics Task Contract v1 YAML/JSON."),
    ],
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit stable machine-readable JSON.")
    ] = False,
) -> None:
    """Validate a physics contract without model execution or repository mutation."""
    try:
        contract = load_physics_task_contract(path)
    except PhysicsContractError as exc:
        _render_physics_error(str(exc), as_json)
    except Exception:
        _render_physics_internal_error(as_json)
    result = {
        "canonical_sha256": contract.canonical_sha256(),
        "check_count": len(contract.required_identities) + len(contract.limiting_cases),
        "ok": True,
        "oracle_count": len(contract.oracles),
        "path": str(path),
        "profile": contract.profile,
        "schema_version": contract.schema_version,
    }
    if as_json:
        typer.echo(_stable_json(result))
    else:
        typer.echo(
            f"Valid Physics Task Contract v1 ({contract.profile}): {path}\n"
            f"Required checks: {result['check_count']}\n"
            f"Declared oracles: {result['oracle_count']}\n"
            "Model execution: unavailable in PA-1"
        )


@app.command("validate-physics-audit")
def validate_physics_audit_command(
    contract_path: Annotated[
        Path,
        typer.Option("--contract", help="Validated Physics Task Contract v1."),
    ],
    report_path: Annotated[
        Path,
        typer.Option("--report", help="Physics Audit Report v1 YAML/JSON."),
    ],
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit stable machine-readable JSON.")
    ] = False,
) -> None:
    """Validate and deterministically route a report without invoking a model."""
    try:
        contract = load_physics_task_contract(contract_path)
        report = load_physics_audit_report(report_path, contract)
        policy = contract.audit_policy or DEFAULT_PHYSICS_AUDIT_POLICY_V1
        decision = derive_physics_audit_decision(contract, policy, report)
    except (PhysicsContractError, PhysicsAuditError) as exc:
        _render_physics_error(str(exc), as_json)
    except Exception:
        _render_physics_internal_error(as_json)
    result = {
        "contract": str(contract_path),
        "decision": decision.to_canonical_dict(),
        "ok": True,
        "report": str(report_path),
    }
    if as_json:
        typer.echo(_stable_json(result))
    else:
        typer.echo(
            f"Valid Physics Audit Report v1: {report_path}\n"
            f"Deterministic route: {decision.outcome}\n"
            f"Rules fired: {len(decision.rules)}\n"
            "Model execution: unavailable in PA-1"
        )


@app.command("run-physics-oracle")
def run_physics_oracle_command(
    catalog_path: Annotated[
        Path,
        typer.Option("--catalog", help="Trusted operator-owned Physics Oracle catalog."),
    ],
    contract_path: Annotated[
        Path,
        typer.Option("--contract", help="Validated Physics Task Contract v1."),
    ],
    oracle_id: Annotated[
        str,
        typer.Option("--oracle-id", help="Declared oracle ID selected from the catalog."),
    ],
    task_id: Annotated[
        str,
        typer.Option("--task-id", help="Stable task identifier bound into the proof."),
    ],
    workspace: Annotated[
        Path,
        typer.Option("--workspace", help="Canonical Git worktree root mounted read-only."),
    ],
    output: Annotated[
        Path,
        typer.Option("--output", help="New explicit directory for the durable action."),
    ],
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit the bounded canonical result as JSON.")
    ] = False,
) -> None:
    """Run one trusted fixed oracle without invoking Codex or another model."""
    try:
        result = run_physics_oracle(
            catalog_path=catalog_path,
            contract_path=contract_path,
            oracle_id=oracle_id,
            task_id=task_id,
            workspace=workspace,
            output_directory=output,
        )
    except PhysicsOracleDependencyError as exc:
        _render_physics_oracle_error(str(exc), as_json, 3)
    except PhysicsOracleInputError as exc:
        _render_physics_oracle_error(str(exc), as_json, 2)
    except (PhysicsOracleIntegrityError, PhysicsOracleStateError) as exc:
        _render_physics_oracle_error(str(exc), as_json, 4)
    except Exception:
        _render_physics_oracle_internal_error(as_json)
    if as_json:
        typer.echo(_stable_json(result.to_canonical_dict()))
    else:
        typer.echo(
            "\n".join(
                (
                    f"Physics oracle: {result.request.oracle_id}",
                    f"Status: {result.status}",
                    f"Workspace integrity: {result.integrity_verdict}",
                    f"Network enforcement: {result.network_enforcement.capability}",
                    f"Completion proof: {result.completion_proof_sha256}",
                    f"Artifacts: {output}",
                    "Model invocation: none",
                )
            )
        )
    if result.status != "passed":
        raise typer.Exit(code=5)


@app.command("audit-physics")
def audit_physics_command(
    contract_path: Annotated[
        Path,
        typer.Option("--contract", help="Validated Physics Task Contract v1."),
    ],
    execution_config_path: Annotated[
        Path,
        typer.Option(
            "--execution-config",
            help="Trusted Codex Physics Auditor execution configuration v1.",
        ),
    ],
    task_id: Annotated[
        str,
        typer.Option("--task-id", help="Stable task identifier bound into the proof."),
    ],
    workspace: Annotated[
        Path,
        typer.Option("--workspace", help="Canonical Git worktree inspected read-only."),
    ],
    oracle_evidence: Annotated[
        Path,
        typer.Option(
            "--oracle-evidence",
            help="Root containing completed and verified PA-2 oracle actions.",
        ),
    ],
    output: Annotated[
        Path,
        typer.Option("--output", help="New standalone action directory, or existing on resume."),
    ],
    action_id: Annotated[
        str | None,
        typer.Option("--action-id", help="Optional stable action ID; otherwise engine-derived."),
    ] = None,
    attempt_number: Annotated[
        int,
        typer.Option("--attempt", min=1, max=1000, help="Bounded standalone attempt number."),
    ] = 1,
    validate_only: Annotated[
        bool,
        typer.Option(
            "--validate-only",
            help="Verify authority and render the prompt in memory without Codex or writes.",
        ),
    ] = False,
    resume: Annotated[
        bool,
        typer.Option(
            "--resume",
            help="Recover an existing action without resuming or rerunning a Codex session.",
        ),
    ] = False,
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Emit a safe machine-readable summary."),
    ] = False,
) -> None:
    """Run one standalone fresh read-only Physics Auditor; no repair is performed."""
    if validate_only and resume:
        _render_physics_auditor_error(
            "--validate-only and --resume are mutually exclusive",
            as_json,
            2,
        )
    try:
        if validate_only:
            summary = validate_physics_auditor_action(
                contract_path=contract_path,
                execution_config_path=execution_config_path,
                task_id=task_id,
                workspace=workspace,
                oracle_evidence_root=oracle_evidence,
                action_id=action_id,
                attempt_number=attempt_number,
            )
            payload = summary.to_dict()
        else:
            operation = resume_physics_auditor if resume else run_physics_auditor
            result = operation(
                contract_path=contract_path,
                execution_config_path=execution_config_path,
                task_id=task_id,
                workspace=workspace,
                oracle_evidence_root=oracle_evidence,
                output_directory=output,
                action_id=action_id,
                attempt_number=attempt_number,
            )
            payload = result.to_canonical_dict()
    except PhysicsAuditorDependencyError as exc:
        _render_physics_auditor_error(str(exc), as_json, 3)
    except PhysicsAuditorInputError as exc:
        _render_physics_auditor_error(str(exc), as_json, 2)
    except (PhysicsAuditorIntegrityError, PhysicsAuditorStateError) as exc:
        _render_physics_auditor_error(str(exc), as_json, 4)
    except Exception:
        _render_physics_auditor_internal_error(as_json)
    if as_json:
        typer.echo(_stable_json(payload))
    elif validate_only:
        typer.echo(
            "\n".join(
                (
                    "Standalone Physics Auditor inputs are valid.",
                    f"Prompt SHA-256: {payload['prompt_sha256']}",
                    "Codex launched: no",
                    "Warning: this standalone action does not repair code or mutate "
                    "workflow state.",
                )
            )
        )
    else:
        decision = cast(dict[str, object] | None, payload.get("routing_decision"))
        route = decision.get("outcome") if decision is not None else "not_available"
        typer.echo(
            "\n".join(
                (
                    f"Physics Auditor status: {payload['status']}",
                    f"Report validated: {str(payload['report_validated']).lower()}",
                    f"Deterministic route: {route}",
                    f"Workspace integrity: {payload['integrity_verdict']}",
                    f"Action proof: {payload['action_proof_sha256']}",
                    "Warning: standalone PA-3 reports routing only; it performs no repair.",
                )
            )
        )
    if not validate_only:
        status = cast(str, payload["status"])
        if status != "routing_completed":
            raise typer.Exit(code=4)
        route_value = cast(dict[str, object], payload["routing_decision"])["outcome"]
        if route_value != "pass":
            raise typer.Exit(code=5)


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
            f"Valid Codex request {prepared.request.run_id} ({prepared.request.role}): {path}"
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
        "acceptance_test_ids": [test.specification.id for test in prepared.acceptance_tests],
    }
    if getattr(prepared.specification, "schema_version", 1) == 2:
        result["physics"] = {
            "enabled": True,
            "required": True,
            "required_oracle_ids": [
                item.id
                for item in prepared.physics_contract.oracles  # type: ignore[attr-defined]
                if item.required
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


@app.command("status")
def workflow_recovery_status_command(
    run_directory: Annotated[
        Path | None,
        typer.Argument(help="Explicit workflow run directory to inspect."),
    ] = None,
    runs_dir: Annotated[
        Path,
        typer.Option("--runs-dir", help="Directory containing workflow runs."),
    ] = Path("runs/workflows"),
    latest: Annotated[
        bool,
        typer.Option("--latest", help="Inspect the unique latest incomplete run."),
    ] = False,
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit stable machine-readable JSON.")
    ] = False,
) -> None:
    """Discover runs or build one read-only deterministic recovery plan."""
    if run_directory is not None and latest:
        _render_recovery_error(
            "conflicting_run_selection",
            "Pass either an explicit run directory or --latest, not both.",
            as_json,
            2,
        )
    try:
        if run_directory is None and not latest:
            index = discover_workflow_runs(runs_dir)
            _render_recovery_index(index, as_json)
            if index.issues:
                raise typer.Exit(code=4)
            return
        selected = run_directory
        if latest:
            selected = Path(latest_incomplete_run(discover_workflow_runs(runs_dir)).run_directory)
        if selected is None:
            raise RecoverySelectionError(
                "run_selection_missing", "Pass an explicit run directory or --latest."
            )
        plan = build_recovery_plan(selected)
    except RecoverySelectionError as exc:
        _render_recovery_error(exc.reason_code, exc.next_step, as_json, 4)
    except (WorkflowInputError, WorkflowStateError):
        _render_recovery_error(
            "run_record_integrity_failed",
            "Restore the exact durable run records and retry status.",
            as_json,
            4,
        )
    except Exception:
        _render_recovery_internal_error(as_json)
    _render_recovery_plan(plan, as_json)
    if plan.disposition == "blocked":
        raise typer.Exit(code=4)


@app.command("latest-incomplete")
def latest_incomplete_command(
    runs_dir: Annotated[
        Path,
        typer.Option("--runs-dir", help="Directory containing workflow runs."),
    ] = Path("runs/workflows"),
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit stable machine-readable JSON.")
    ] = False,
) -> None:
    """Locate the unique latest incomplete run from authoritative journals."""
    try:
        entry = latest_incomplete_run(discover_workflow_runs(runs_dir))
    except RecoverySelectionError as exc:
        _render_recovery_error(exc.reason_code, exc.next_step, as_json, 4)
    except (WorkflowInputError, WorkflowStateError):
        _render_recovery_error(
            "run_discovery_integrity_failed",
            "Inspect the runs directory and restore exact durable records before retrying.",
            as_json,
            4,
        )
    except Exception:
        _render_recovery_internal_error(as_json)
    value = entry.model_dump(mode="json")
    if as_json:
        _emit_recovery_payload(value, _stable_json(value))
    else:
        _emit_recovery_payload(value, entry.run_directory)


@app.command("resume")
def workflow_recovery_resume_command(
    run_directory: Annotated[
        Path | None,
        typer.Argument(help="Explicit workflow run directory to recover."),
    ] = None,
    runs_dir: Annotated[
        Path,
        typer.Option("--runs-dir", help="Directory containing workflow runs."),
    ] = Path("runs/workflows"),
    latest: Annotated[
        bool,
        typer.Option("--latest", help="Recover the unique latest incomplete run."),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Build and verify the plan without writes or launches."),
    ] = False,
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit stable machine-readable JSON.")
    ] = False,
) -> None:
    """Safely recover one journal-proven workflow without duplicate actions."""
    if (run_directory is None) == (not latest):
        _render_recovery_error(
            "run_selection_invalid",
            "Pass exactly one explicit run directory or --latest.",
            as_json,
            2,
        )
    try:
        selected = run_directory
        if latest:
            selected = Path(
                latest_incomplete_run(
                    discover_workflow_runs(runs_dir, persist_cache=not dry_run)
                ).run_directory
            )
        if selected is None:
            raise RecoverySelectionError(
                "run_selection_missing", "Pass an explicit run directory or --latest."
            )
        plan = build_recovery_plan(selected)
        if dry_run:
            _render_recovery_plan(plan, as_json)
            if plan.disposition == "blocked":
                raise typer.Exit(code=4)
            return
        execution = execute_recovery_plan(plan)
    except RecoverySelectionError as exc:
        _render_recovery_error(exc.reason_code, exc.next_step, as_json, 4)
    except (WorkflowInputError, WorkflowStateError):
        _render_recovery_error(
            "recovery_integrity_failed",
            "Run status again and restore the exact durable evidence before retrying.",
            as_json,
            4,
        )
    except Exception:
        _render_recovery_internal_error(as_json)
    _render_recovery_execution(execution, as_json)
    if execution.outcome.status in {"blocked", "failed"}:
        raise typer.Exit(code=4)
    if execution.outcome.result_status is not None:
        exit_code = workflow_exit_code(execution.outcome.result_status)
        if exit_code:
            raise typer.Exit(code=exit_code)


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


@app.command("review-physics-substage")
def review_physics_substage_command(
    run_directory: Annotated[
        Path, typer.Argument(help="Paused schema-version-2 physics workflow directory.")
    ],
    decision: Annotated[
        Path,
        typer.Option(
            "--decision",
            help="Exact PhysicsReviewDecisionV1 YAML/JSON file.",
        ),
    ],
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit stable machine-readable JSON.")
    ] = False,
) -> None:
    """Apply one hash-bound human scientific decision to a physics pause."""
    try:
        result = continue_workflow(run_directory, decision)
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


@app.command("run-visible-campaign")
def run_replay_campaign_command(
    path: Annotated[Path, typer.Argument(help="Visible campaign specification.")],
    runs_dir: Annotated[
        Path,
        typer.Option(
            "--runs-dir",
            help="Directory under which the exclusive campaign run is created.",
        ),
    ] = DEFAULT_REPLAY_RUNS_DIRECTORY,
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit stable machine-readable JSON.")
    ] = False,
) -> None:
    """Run an ordered visible-only campaign and export its candidate."""
    try:
        result = run_replay_campaign(path, runs_dir=runs_dir)
    except ReplayCampaignInputError as exc:
        _render_replay_error(str(exc), as_json, 2)
    except ReplayCampaignDependencyError as exc:
        _render_replay_error(str(exc), as_json, 3)
    except (ReplayCampaignLockError, ReplayCampaignStateError) as exc:
        _render_replay_error(str(exc), as_json, 4)
    except Exception:
        _render_replay_internal_error(as_json)
    _render_replay_result_and_exit(result, as_json)


@app.command("resume-visible-campaign")
def resume_replay_campaign_command(
    run_directory: Annotated[Path, typer.Argument(help="Existing campaign run directory.")],
    decision: Annotated[
        Path | None,
        typer.Option(
            "--decision",
            help="Exact decision YAML for a human pause; omit for running recovery.",
        ),
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit stable machine-readable JSON.")
    ] = False,
) -> None:
    """Apply one immutable decision and resume exact campaign sessions."""
    try:
        result = resume_replay_campaign(run_directory, decision_path=decision)
    except ReplayCampaignInputError as exc:
        _render_replay_error(str(exc), as_json, 2)
    except ReplayCampaignDependencyError as exc:
        _render_replay_error(str(exc), as_json, 3)
    except (ReplayCampaignLockError, ReplayCampaignStateError) as exc:
        _render_replay_error(str(exc), as_json, 4)
    except Exception:
        _render_replay_internal_error(as_json)
    _render_replay_result_and_exit(result, as_json)


@app.command("visible-campaign-status")
def replay_campaign_status_command(
    run_directory: Annotated[Path, typer.Argument(help="Existing campaign run directory.")],
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit stable machine-readable JSON.")
    ] = False,
) -> None:
    """Read visible campaign status without mutation."""
    try:
        result = replay_campaign_status(run_directory)
    except ReplayCampaignInputError as exc:
        _render_replay_error(str(exc), as_json, 2)
    except (ReplayCampaignLockError, ReplayCampaignStateError) as exc:
        _render_replay_error(str(exc), as_json, 4)
    except Exception:
        _render_replay_internal_error(as_json)
    _render_replay_result_and_exit(result, as_json)


@app.command("validate-shadow-spec")
def validate_shadow_spec_command(
    path: Annotated[
        Path,
        typer.Argument(help="YAML shadow-calibration specification."),
    ],
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit stable machine-readable JSON.")
    ] = False,
) -> None:
    """Validate frozen Stage 3 inputs without writes or launches."""
    try:
        prepared = validate_shadow(path)
    except (ShadowInputError, WorkflowInputError) as exc:
        _render_shadow_error(str(exc), as_json, 2)
    except (ShadowDependencyError, WorkflowDependencyError) as exc:
        _render_shadow_error(str(exc), as_json, 3)
    except (ShadowLockError, ShadowStateError, WorkflowStateError) as exc:
        _render_shadow_error(str(exc), as_json, 4)
    except Exception:
        _render_shadow_internal_error(as_json)
    result = {
        "ok": True,
        "path": str(path),
        "calibration_id": prepared.specification.calibration_id,
        "source_stage2_run": str(prepared.source.run_directory),
        "decision_count": len(prepared.source.decisions),
    }
    if as_json:
        _emit_shadow_payload(result, _stable_json(result), as_json)
    else:
        rendered = (
            f"Valid shadow calibration "
            f"{prepared.specification.calibration_id}: {path}\n"
            f"Source Stage 2 run: {prepared.source.run_directory}\n"
            f"Decision points: {len(prepared.source.decisions)}"
        )
        _emit_shadow_payload(result, rendered, as_json)


@app.command("run-shadow-calibration")
def run_shadow_calibration_command(
    path: Annotated[
        Path,
        typer.Argument(help="YAML shadow-calibration specification."),
    ],
    runs_dir: Annotated[
        Path,
        typer.Option(
            "--runs-dir",
            help="Directory under which the exclusive shadow run is created.",
        ),
    ] = Path("runs/shadow"),
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit stable machine-readable JSON.")
    ] = False,
) -> None:
    """Run only the persistent blind supervisor retrospectively."""
    try:
        result = run_shadow(path, runs_dir=runs_dir)
    except (ShadowInputError, WorkflowInputError) as exc:
        _render_shadow_error(str(exc), as_json, 2)
    except (ShadowDependencyError, WorkflowDependencyError) as exc:
        _render_shadow_error(str(exc), as_json, 3)
    except (ShadowLockError, ShadowStateError, WorkflowStateError) as exc:
        _render_shadow_error(str(exc), as_json, 4)
    except Exception:
        _render_shadow_internal_error(as_json)
    _render_shadow_result_and_exit(result, as_json)


@app.command("resume-shadow-calibration")
def resume_shadow_calibration_command(
    run_directory: Annotated[Path, typer.Argument(help="Existing shadow-calibration run.")],
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit stable machine-readable JSON.")
    ] = False,
) -> None:
    """Recover an interrupted shadow run without duplicate launch."""
    try:
        result = resume_shadow(run_directory)
    except ShadowInputError as exc:
        _render_shadow_error(str(exc), as_json, 2)
    except ShadowDependencyError as exc:
        _render_shadow_error(str(exc), as_json, 3)
    except (ShadowLockError, ShadowStateError) as exc:
        _render_shadow_error(str(exc), as_json, 4)
    except Exception:
        _render_shadow_internal_error(as_json)
    _render_shadow_result_and_exit(result, as_json)


@app.command("shadow-calibration-status")
def shadow_calibration_status_command(
    run_directory: Annotated[Path, typer.Argument(help="Shadow-calibration run to inspect.")],
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit stable machine-readable JSON.")
    ] = False,
) -> None:
    """Read Stage 3 status without writes or launches."""
    try:
        result = read_shadow_status(run_directory)
    except ShadowInputError as exc:
        _render_shadow_error(str(exc), as_json, 2)
    except ShadowDependencyError as exc:
        _render_shadow_error(str(exc), as_json, 3)
    except (ShadowLockError, ShadowStateError) as exc:
        _render_shadow_error(str(exc), as_json, 4)
    except Exception:
        _render_shadow_internal_error(as_json)
    if as_json:
        value = result.to_dict()
        _emit_shadow_payload(value, _stable_json(value), as_json)
    else:
        _emit_shadow_payload(result.to_dict(), _format_shadow_result(result), as_json)


@app.command("record-shadow-review")
def record_shadow_review_command(
    run_directory: Annotated[Path, typer.Argument(help="Shadow-calibration run.")],
    proposal_id: Annotated[str, typer.Argument(help="Exact proposal ID to review.")],
    review_path: Annotated[Path, typer.Argument(help="Strict human-review YAML file.")],
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit stable machine-readable JSON.")
    ] = False,
) -> None:
    """Record one immutable structured human semantic review."""
    try:
        result = record_review(run_directory, proposal_id, review_path)
    except ShadowInputError as exc:
        _render_shadow_error(str(exc), as_json, 2)
    except ShadowDependencyError as exc:
        _render_shadow_error(str(exc), as_json, 3)
    except (ShadowLockError, ShadowStateError) as exc:
        _render_shadow_error(str(exc), as_json, 4)
    except Exception:
        _render_shadow_internal_error(as_json)
    _render_shadow_result_and_exit(result, as_json)


@app.command("shadow-calibration-report")
def shadow_calibration_report_command(
    run_directory: Annotated[Path, typer.Argument(help="Shadow-calibration run to report.")],
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit stable machine-readable JSON.")
    ] = False,
) -> None:
    """Build a read-only deterministic calibration/readiness report."""
    try:
        report = read_shadow_report(run_directory)
    except ShadowInputError as exc:
        _render_shadow_error(str(exc), as_json, 2)
    except ShadowDependencyError as exc:
        _render_shadow_error(str(exc), as_json, 3)
    except (ShadowLockError, ShadowStateError) as exc:
        _render_shadow_error(str(exc), as_json, 4)
    except Exception:
        _render_shadow_internal_error(as_json)
    if as_json:
        _emit_shadow_payload(report, _stable_json(report), as_json)
    else:
        readiness = cast(dict[str, object], report["readiness"])
        rendered = "\n".join(
            (
                f"Calibration: {report['calibration_id']}",
                f"Status: {report['status']}",
                f"Readiness: {readiness['status']}",
                "Readiness is informational only; automation remains disabled.",
            )
        )
        _emit_shadow_payload(report, rendered, as_json)


@app.command("abort-shadow-calibration")
def abort_shadow_calibration_command(
    run_directory: Annotated[Path, typer.Argument(help="Shadow-calibration run to abort.")],
    reason: Annotated[str, typer.Option("--reason", help="Human abort reason.")],
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit stable machine-readable JSON.")
    ] = False,
) -> None:
    """Abort a non-running, nonterminal Stage 3 calibration."""
    try:
        result = abort_shadow(run_directory, reason)
    except ShadowInputError as exc:
        _render_shadow_error(str(exc), as_json, 2)
    except ShadowDependencyError as exc:
        _render_shadow_error(str(exc), as_json, 3)
    except (ShadowLockError, ShadowStateError) as exc:
        _render_shadow_error(str(exc), as_json, 4)
    except Exception:
        _render_shadow_internal_error(as_json)
    _render_shadow_result_and_exit(result, as_json)


@app.command("validate-live-shadow-spec")
def validate_live_shadow_spec_command(
    path: Annotated[Path, typer.Argument(help="YAML live-shadow specification.")],
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit stable machine-readable JSON.")
    ] = False,
) -> None:
    """Validate frozen Stage 4 inputs without writes or launches."""
    try:
        prepared = validate_live_shadow_spec(path)
    except LiveShadowInputError as exc:
        _render_live_shadow_error(str(exc), as_json, 2)
    except LiveShadowDependencyError as exc:
        _render_live_shadow_error(str(exc), as_json, 3)
    except (LiveShadowLockError, LiveShadowStateError) as exc:
        _render_live_shadow_error(str(exc), as_json, 4)
    except Exception:
        _render_live_shadow_internal_error(as_json)
    value = {
        "ok": True,
        "path": str(path),
        "live_shadow_id": prepared.specification.live_shadow_id,
        "stage2_specification": str(prepared.stage2.specification_path),
        "substage_id": prepared.stage2.specification.substage_id,
    }
    rendered = (
        _stable_json(value)
        if as_json
        else "\n".join(
            (
                f"Valid live shadow {prepared.specification.live_shadow_id}: {path}",
                f"Stage 2 specification: {prepared.stage2.specification_path}",
                f"Substage: {prepared.stage2.specification.substage_id}",
            )
        )
    )
    _emit_live_shadow_payload(value, rendered, as_json)


@app.command("run-live-shadow")
def run_live_shadow_command(
    path: Annotated[Path, typer.Argument(help="YAML live-shadow specification.")],
    runs_dir: Annotated[
        Path,
        typer.Option(
            "--runs-dir",
            help="Directory under which the exclusive Stage 4 run is created.",
        ),
    ] = DEFAULT_LIVE_SHADOW_RUNS_DIRECTORY,
    stage2_runs_dir: Annotated[
        Path,
        typer.Option(
            "--stage2-runs-dir",
            help="Directory under which the authoritative Stage 2 run is created.",
        ),
    ] = Path("runs/workflows"),
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit stable machine-readable JSON.")
    ] = False,
) -> None:
    """Run one independent Stage 2 child plus quarantined observer."""
    try:
        result = run_live_shadow(
            path,
            runs_dir=runs_dir,
            stage2_runs_dir=stage2_runs_dir,
        )
    except LiveShadowInputError as exc:
        _render_live_shadow_error(str(exc), as_json, 2)
    except LiveShadowDependencyError as exc:
        _render_live_shadow_error(str(exc), as_json, 3)
    except (LiveShadowLockError, LiveShadowStateError) as exc:
        _render_live_shadow_error(str(exc), as_json, 4)
    except Exception:
        _render_live_shadow_internal_error(as_json)
    _render_live_shadow_result_and_exit(result, as_json)


@app.command("resume-live-shadow")
def resume_live_shadow_command(
    run_directory: Annotated[Path, typer.Argument(help="Existing live-shadow run.")],
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit stable machine-readable JSON.")
    ] = False,
) -> None:
    """Reattach observation without relaunching Stage 2."""
    try:
        result = resume_live_shadow(run_directory)
    except LiveShadowInputError as exc:
        _render_live_shadow_error(str(exc), as_json, 2)
    except LiveShadowDependencyError as exc:
        _render_live_shadow_error(str(exc), as_json, 3)
    except (LiveShadowLockError, LiveShadowStateError) as exc:
        _render_live_shadow_error(str(exc), as_json, 4)
    except Exception:
        _render_live_shadow_internal_error(as_json)
    _render_live_shadow_result_and_exit(result, as_json)


@app.command("live-shadow-status")
def live_shadow_status_command(
    run_directory: Annotated[Path, typer.Argument(help="Live-shadow run to inspect.")],
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit stable machine-readable JSON.")
    ] = False,
) -> None:
    """Read Stage 4 status without writes or launches."""
    try:
        result = live_shadow_status(run_directory)
    except LiveShadowInputError as exc:
        _render_live_shadow_error(str(exc), as_json, 2)
    except LiveShadowDependencyError as exc:
        _render_live_shadow_error(str(exc), as_json, 3)
    except (LiveShadowLockError, LiveShadowStateError) as exc:
        _render_live_shadow_error(str(exc), as_json, 4)
    except Exception:
        _render_live_shadow_internal_error(as_json)
    value = result.to_dict()
    _emit_live_shadow_payload(
        value,
        _stable_json(value) if as_json else _format_live_shadow_result(result),
        as_json,
    )


@app.command("record-live-shadow-review")
def record_live_shadow_review_command(
    run_directory: Annotated[Path, typer.Argument(help="Live-shadow run.")],
    proposal_id: Annotated[str, typer.Argument(help="Exact proposal ID to review.")],
    review_path: Annotated[Path, typer.Argument(help="Strict human-review YAML file.")],
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit stable machine-readable JSON.")
    ] = False,
) -> None:
    """Record one immutable live-shadow human review."""
    try:
        result = record_live_shadow_review(
            run_directory,
            proposal_id,
            review_path,
        )
    except LiveShadowInputError as exc:
        _render_live_shadow_error(str(exc), as_json, 2)
    except LiveShadowDependencyError as exc:
        _render_live_shadow_error(str(exc), as_json, 3)
    except (LiveShadowLockError, LiveShadowStateError) as exc:
        _render_live_shadow_error(str(exc), as_json, 4)
    except Exception:
        _render_live_shadow_internal_error(as_json)
    _render_live_shadow_result_and_exit(result, as_json)


@app.command("live-shadow-report")
def live_shadow_report_command(
    run_directory: Annotated[Path, typer.Argument(help="Live-shadow run to report.")],
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit stable machine-readable JSON.")
    ] = False,
) -> None:
    """Build a read-only live comparison/readiness report."""
    try:
        report = live_shadow_report(run_directory)
    except LiveShadowInputError as exc:
        _render_live_shadow_error(str(exc), as_json, 2)
    except LiveShadowDependencyError as exc:
        _render_live_shadow_error(str(exc), as_json, 3)
    except (LiveShadowLockError, LiveShadowStateError) as exc:
        _render_live_shadow_error(str(exc), as_json, 4)
    except Exception:
        _render_live_shadow_internal_error(as_json)
    if as_json:
        rendered = _stable_json(report)
    else:
        readiness = cast(dict[str, object], report["readiness"])
        rendered = "\n".join(
            (
                f"Live shadow: {report['live_shadow_id']}",
                f"Status: {report['status']}",
                (
                    "Authoritative Stage 2: "
                    f"{cast(dict[str, object], report['authoritative'])['status']}"
                ),
                f"Readiness: {readiness['status']}",
                "Readiness is informational only; automation remains disabled.",
            )
        )
    _emit_live_shadow_payload(report, rendered, as_json)


@app.command("abort-live-shadow")
def abort_live_shadow_command(
    run_directory: Annotated[Path, typer.Argument(help="Live-shadow run to abort.")],
    reason: Annotated[str, typer.Option("--reason", help="Human abort reason.")],
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit stable machine-readable JSON.")
    ] = False,
) -> None:
    """Abort only Stage 4 observation; never signal Stage 2."""
    try:
        result = abort_live_shadow(run_directory, reason)
    except LiveShadowInputError as exc:
        _render_live_shadow_error(str(exc), as_json, 2)
    except LiveShadowDependencyError as exc:
        _render_live_shadow_error(str(exc), as_json, 3)
    except (LiveShadowLockError, LiveShadowStateError) as exc:
        _render_live_shadow_error(str(exc), as_json, 4)
    except Exception:
        _render_live_shadow_internal_error(as_json)
    _render_live_shadow_result_and_exit(result, as_json)


def _render_recovery_index(index: RunIndexV1, as_json: bool) -> None:
    value = index.model_dump(mode="json")
    if as_json:
        _emit_recovery_payload(value, _stable_json(value))
        return
    lines = [
        f"Runs directory: {index.runs_directory}",
        f"Verified runs: {len(index.entries)}",
        f"Integrity issues: {len(index.issues)}",
    ]
    lines.extend(
        f"{item.status}: {item.run_directory}"
        for item in sorted(index.entries, key=lambda entry: entry.updated_at, reverse=True)
    )
    lines.extend(f"CORRUPT: {item.run_directory}" for item in index.issues)
    _emit_recovery_payload(value, "\n".join(lines))


def _render_recovery_plan(plan: RecoveryPlanV1, as_json: bool) -> None:
    value = {
        "ok": plan.disposition != "blocked",
        "plan": plan.model_dump(mode="json"),
        "plan_sha256": plan.canonical_sha256(),
    }
    if as_json:
        _emit_recovery_payload(value, _stable_json(value))
        return
    rendered = "\n".join(
        (
            f"Run: {plan.run_directory}",
            f"State: {plan.observed_status} (journal {plan.journal_sequence})",
            f"Recovery: {plan.disposition} / {plan.operation}",
            f"Reason: {plan.reason_code}",
            f"Next: {plan.next_step}",
        )
    )
    _emit_recovery_payload(value, rendered)


def _render_recovery_execution(execution: RecoveryExecutionV1, as_json: bool) -> None:
    value = execution.to_dict()
    if as_json:
        _emit_recovery_payload(value, _stable_json(value))
        return
    outcome = execution.outcome
    rendered = "\n".join(
        (
            f"Run: {outcome.run_directory}",
            f"Recovery outcome: {outcome.status}",
            f"Workflow state: {outcome.result_status or execution.plan.observed_status}",
            f"Reason: {outcome.reason_code}",
            f"Next: {outcome.next_step}",
            f"Plan receipt: {execution.plan_receipt_path}",
            f"Outcome receipt: {execution.outcome_receipt_path}",
        )
    )
    _emit_recovery_payload(value, rendered)


def _emit_recovery_payload(value: object, rendered: str) -> None:
    _, _, sensitive_values = build_subprocess_environment()
    safe_value = redact_json(value, sensitive_values)
    safe_rendered = redact_text(rendered, sensitive_values)
    typer.echo(_stable_json(safe_value) if rendered.startswith("{") else safe_rendered)


def _render_recovery_error(
    reason_code: str,
    next_step: str,
    as_json: bool,
    exit_code: int,
) -> Never:
    value = {
        "ok": False,
        "reason_code": reason_code,
        "next_step": next_step,
    }
    if as_json:
        _emit_recovery_payload(value, _stable_json(value))
    else:
        _emit_recovery_payload(
            value,
            f"Recovery blocked: {reason_code}\nNext: {next_step}",
        )
    raise typer.Exit(code=exit_code)


def _render_recovery_internal_error(as_json: bool) -> Never:
    _render_recovery_error(
        "unexpected_recovery_failure",
        "Run status again; if the failure repeats, inspect the durable records manually.",
        as_json,
        1,
    )


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


def _render_shadow_result_and_exit(result: ShadowResult, as_json: bool) -> None:
    value = result.to_dict()
    rendered = _stable_json(value) if as_json else _format_shadow_result(result)
    _emit_shadow_payload(value, rendered, as_json)
    exit_code = shadow_calibration_exit_code(result.status)
    if exit_code:
        raise typer.Exit(code=exit_code)


def _render_live_shadow_result_and_exit(
    result: LiveShadowResult,
    as_json: bool,
) -> None:
    value = result.to_dict()
    rendered = _stable_json(result.to_dict()) if as_json else _format_live_shadow_result(result)
    _emit_live_shadow_payload(value, rendered, as_json)
    exit_code = live_shadow_exit_code(result.status)
    if exit_code:
        raise typer.Exit(code=exit_code)


def _format_live_shadow_result(result: LiveShadowResult) -> str:
    return "\n".join(
        (
            f"Live shadow: {result.live_shadow_id}",
            f"Status: {result.status}",
            f"Authoritative Stage 2: {result.authoritative_stage2_status}",
            f"Summary: {result.summary}",
            f"Observed decisions: {result.observed_decision_count}",
            f"Proposals/comparisons/reviews: "
            f"{result.proposal_count}/{result.comparison_count}/{result.review_count}",
            f"Readiness: {result.readiness}",
            "Automation enabled: no",
            f"Artifacts: {result.artifact_directory}",
        )
    )


def _render_live_shadow_error(
    error: str,
    as_json: bool,
    exit_code: int,
) -> Never:
    _, _, sensitive_values = build_subprocess_environment()
    sanitized = redact_text(
        error,
        (*sensitive_values, *_cli_authentication_fragments()),
    )
    if as_json:
        typer.echo(_stable_json({"error": sanitized, "ok": False}))
    else:
        typer.echo(f"Live-shadow error: {sanitized}", err=True)
    raise typer.Exit(code=exit_code)


def _render_live_shadow_internal_error(as_json: bool) -> Never:
    message = "unexpected internal live-shadow failure"
    if as_json:
        typer.echo(_stable_json({"error": message, "ok": False}))
    else:
        typer.echo(f"Live-shadow error: {message}", err=True)
    raise typer.Exit(code=1)


def _emit_live_shadow_payload(
    value: object,
    rendered: str,
    as_json: bool,
) -> None:
    _, _, sensitive_values = build_subprocess_environment()
    protected_values = (
        *sensitive_values,
        *_cli_authentication_fragments(),
    )
    try:
        preflight_shadow_confidentiality(
            value,
            protected_values,
            label="live-shadow CLI payload",
            integrity=True,
        )
    except ShadowStateError as exc:
        _render_live_shadow_error(str(exc), as_json, 4)
    typer.echo(rendered)


def _cli_authentication_fragments() -> tuple[str, ...]:
    fragments: tuple[str, ...] = ()
    with suppress(LiveShadowDependencyError, LiveShadowStateError):
        fragments = resolve_authentication_confidentiality(
            authentication_file=None,
            environ=None,
            forbidden_roots=(),
        ).text_fragments()
    return fragments


def _emit_shadow_payload(
    value: object,
    rendered: str,
    as_json: bool,
) -> None:
    _, _, sensitive_values = build_subprocess_environment()
    try:
        preflight_shadow_confidentiality(
            value,
            sensitive_values,
            label="shadow CLI payload",
            integrity=True,
        )
    except ShadowStateError as exc:
        _render_shadow_error(str(exc), as_json, 4)
    typer.echo(rendered)


def _format_shadow_result(result: ShadowResult) -> str:
    return "\n".join(
        (
            f"Calibration: {result.calibration_id}",
            f"Status: {result.status}",
            f"Summary: {result.summary}",
            f"Supervisor session: {result.supervisor_session_id or 'not available'}",
            f"Proposals: {result.proposal_count}",
            f"Comparisons: {result.comparison_count}",
            f"Reviews: {result.review_count}",
            f"Disqualifications: {result.disqualification_count}",
            f"Readiness: {result.readiness} (informational only)",
            f"Pause reason: {result.pause_reason or 'none'}",
            f"Artifacts: {result.artifact_directory}",
        )
    )


def _render_shadow_error(
    error: str,
    as_json: bool,
    exit_code: int,
) -> Never:
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
                        else "integrity"
                    ),
                    "ok": False,
                }
            )
        )
    else:
        typer.echo(f"Shadow calibration error: {sanitized}", err=True)
    raise typer.Exit(code=exit_code)


def _render_shadow_internal_error(as_json: bool) -> Never:
    message = "Unexpected internal shadow-calibration failure."
    if as_json:
        typer.echo(
            _stable_json(
                {
                    "error": message,
                    "error_kind": "internal",
                    "ok": False,
                }
            )
        )
    else:
        typer.echo(message, err=True)
    raise typer.Exit(code=1)


def _format_workflow_result(result: WorkflowResult) -> str:
    if getattr(result, "schema_version", 1) == 2:
        return "\n".join(
            (
                f"Substage: {result.substage_id}",
                f"Status: {result.status}",
                f"Summary: {result.summary}",
                f"Repair round: {result.repair_round}/{result.max_repair_rounds}",
                f"Worker thread: {result.worker_thread_id or 'not available'}",
                f"Fixed tests passed: {'yes' if result.tests_passed else 'no'}",
                "Code Auditor passed: "
                f"{'yes' if getattr(result, 'code_auditor_passed', False) else 'no'}",
                "Required oracle proofs: "
                + (
                    "verified"
                    if getattr(result, "required_oracle_proofs_verified", False)
                    else "not verified"
                ),
                f"Physics route: {getattr(result, 'physics_route', None) or 'not available'}",
                f"Pause reason: {result.pause_reason or 'none'}",
                f"Artifacts: {result.artifact_directory}",
            )
        )
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


def _render_physics_error(error: str, as_json: bool) -> Never:
    _, _, sensitive_values = build_subprocess_environment()
    sanitized = redact_text(error, sensitive_values)
    if as_json:
        typer.echo(_stable_json({"error": sanitized, "error_kind": "input", "ok": False}))
    else:
        typer.echo(f"Physics validation error: {sanitized}", err=True)
    raise typer.Exit(code=2)


def _render_physics_internal_error(as_json: bool) -> Never:
    message = "Unexpected internal physics validation failure."
    if as_json:
        typer.echo(_stable_json({"error": message, "error_kind": "internal", "ok": False}))
    else:
        typer.echo(message, err=True)
    raise typer.Exit(code=1)


def _render_physics_auditor_error(
    error: str,
    as_json: bool,
    exit_code: int,
) -> Never:
    _, _, sensitive_values = build_subprocess_environment()
    sanitized = redact_text(error, sensitive_values)
    kind = "input" if exit_code == 2 else "dependency" if exit_code == 3 else "integrity"
    if as_json:
        typer.echo(
            _stable_json(
                {
                    "error": sanitized,
                    "error_kind": kind,
                    "ok": False,
                }
            )
        )
    else:
        typer.echo(f"Physics Auditor error: {sanitized}", err=True)
    raise typer.Exit(code=exit_code)


def _render_physics_auditor_internal_error(as_json: bool) -> Never:
    message = "Unexpected internal standalone Physics Auditor failure."
    if as_json:
        typer.echo(_stable_json({"error": message, "error_kind": "internal", "ok": False}))
    else:
        typer.echo(message, err=True)
    raise typer.Exit(code=1)


def _render_physics_oracle_error(error: str, as_json: bool, exit_code: int) -> Never:
    kind = "input" if exit_code == 2 else "dependency" if exit_code == 3 else "integrity"
    if as_json:
        typer.echo(
            _stable_json(
                {
                    "error": error,
                    "error_kind": kind,
                    "ok": False,
                }
            )
        )
    else:
        typer.echo(f"Physics oracle error: {error}", err=True)
    raise typer.Exit(code=exit_code)


def _render_physics_oracle_internal_error(as_json: bool) -> Never:
    message = "Unexpected internal Physics Oracle execution failure."
    if as_json:
        typer.echo(_stable_json({"error": message, "error_kind": "internal", "ok": False}))
    else:
        typer.echo(message, err=True)
    raise typer.Exit(code=1)


def _render_replay_result_and_exit(
    result: ReplayCampaignState,
    as_json: bool,
) -> None:
    if as_json:
        typer.echo(_stable_json(result.to_dict()))
    else:
        typer.echo(
            "\n".join(
                (
                    f"Campaign: {result.campaign_id}",
                    f"Status: {result.status}",
                    f"Current task index: {result.current_task_index}",
                    f"Completed tasks: {len(result.completed_task_ids)}",
                    f"Supervisor session: {result.supervisor_session_id or 'not available'}",
                    f"Human assisted tasks: {', '.join(result.human_assisted_task_ids) or 'none'}",
                    f"Pause reason: {result.pause_reason or 'none'}",
                )
            )
        )
    code = replay_campaign_exit_code(result.status)
    if code:
        raise typer.Exit(code=code)


def _render_replay_error(error: str, as_json: bool, exit_code: int) -> Never:
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
                        else "campaign"
                    ),
                    "ok": False,
                }
            )
        )
    else:
        typer.echo(f"Replay campaign error: {sanitized}", err=True)
    raise typer.Exit(code=exit_code)


def _render_replay_internal_error(as_json: bool) -> Never:
    message = "Unexpected internal replay campaign failure."
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
            f"Events: {result.event_count} valid, {result.malformed_event_count} malformed",
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
