"""Sequential qualified PA-2/PA-3 execution for the bounded GL pilot."""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError

from research_automation_supervisor.durable_state import atomic_write_bytes, render_json_bytes
from research_automation_supervisor.errors import (
    PhysicsBenchmarkInputError,
    PhysicsBenchmarkIntegrityError,
    PhysicsBenchmarkStateError,
)
from research_automation_supervisor.physics_auditor_execution import (
    CONTROL_DIRECTORY,
    PROMPT_FILE,
    PROOF_FILE,
    PROVIDER_OBSERVATION_FILE,
    REPORT_FILE,
    PhysicsAuditorCodexInvoker,
    resume_physics_auditor,
    run_physics_auditor,
    verify_physics_auditor_action,
)
from research_automation_supervisor.physics_auditor_models import (
    PhysicsAuditorActionProofV1,
    PhysicsAuditorProjectionManifestV1,
    PhysicsAuditorProviderObservationV1,
)
from research_automation_supervisor.physics_auditor_projection import (
    PROJECTION_DIRECTORY,
    PROJECTION_MANIFEST_FILE,
)
from research_automation_supervisor.physics_benchmark_models import (
    PhysicsBenchmarkFindingObservationV1,
    PhysicsBenchmarkRecoveryProofV1,
    PhysicsBenchmarkUsageV1,
)
from research_automation_supervisor.physics_gl_pilot import (
    PhysicsGLPilotConfigV1,
    PhysicsGLPilotReportV1,
    PhysicsGLPilotRunV1,
    PhysicsGLPilotTaskV1,
    aggregate_physics_gl_pilot,
    finalize_physics_gl_pilot_report,
    gl_pilot_scoring_observation,
    locked_authority_sha256,
    read_gl_source_blob,
    validate_physics_gl_pilot,
    verify_gl_auditor_visible_blindness,
)
from research_automation_supervisor.physics_models import (
    PhysicsAuditReportV1,
    load_physics_task_contract,
)
from research_automation_supervisor.physics_oracle_execution import (
    resume_physics_oracle,
    run_physics_oracle,
    verify_physics_oracle_completion,
)
from research_automation_supervisor.physics_oracle_models import (
    PhysicsOracleCatalogV1,
    PhysicsOracleEnvironmentProfileV1,
    PhysicsOracleExecutableV1,
    PhysicsOracleExecutionPolicyV1,
    PhysicsOracleIntentV1,
    PhysicsOracleTrustedProgramV1,
)

GLPilotCheckpoint = Callable[[str], None]


def run_bounded_physics_gl_pilot(
    *,
    config: PhysicsGLPilotConfigV1,
    config_path: Path,
    execution_config_path: Path,
    repository_root: Path,
    source_repository_root: Path,
    output_directory: Path,
    codex_invokers: dict[str, PhysicsAuditorCodexInvoker] | None = None,
    checkpoint: GLPilotCheckpoint | None = None,
) -> PhysicsGLPilotReportV1:
    """Run or safely resume all ten one-shot pilot tasks and finalize a bounded report."""
    root = repository_root.resolve(strict=True)
    fixture_hashes = validate_physics_gl_pilot(
        config,
        repository_root=root,
        config_path=config_path,
        source_repository_root=source_repository_root,
    )
    _checkpoint(checkpoint, "gl_authority_and_source_verified")
    output_directory.mkdir(parents=True, exist_ok=True)
    session_hashes: set[str] = set()
    records: list[PhysicsGLPilotRunV1] = []
    for task in config.tasks:
        task_output = output_directory / task.task_id
        contract_path = root / task.contract_path
        prepared_workspace = _prepare_exact_gl_workspace(
            config=config,
            task=task,
            benchmark_root=root,
            source_repository_root=source_repository_root.resolve(strict=True),
            destination=task_output / "prepared-workspace",
        )
        _checkpoint(checkpoint, f"{task.task_id}:exact_source_workspace_verified")
        evidence_root, oracle_result = _pilot_oracle_evidence(
            task=task,
            contract_path=contract_path,
            repository_root=prepared_workspace,
            task_output=task_output,
            checkpoint=checkpoint,
        )
        action_output = task_output / "action"
        action_output.parent.mkdir(parents=True, exist_ok=True)
        resumed = action_output.exists()
        kwargs: dict[str, Any] = {
            "contract_path": contract_path,
            "execution_config_path": execution_config_path,
            "task_id": task.task_id,
            "workspace": prepared_workspace,
            "oracle_evidence_root": evidence_root,
            "output_directory": action_output,
            "attempt_number": 1,
        }
        invoker = (codex_invokers or {}).get(task.task_id)
        if invoker is not None:
            kwargs["codex_invoker"] = invoker
        operation = resume_physics_auditor if resumed else run_physics_auditor
        _checkpoint(checkpoint, f"{task.task_id}:before_pa3_resume_or_launch")
        operation(**kwargs)
        _checkpoint(checkpoint, f"{task.task_id}:after_pa3_completion")
        verified = verify_physics_auditor_action(
            **{key: item for key, item in kwargs.items() if key != "codex_invoker"}
        )
        _checkpoint(checkpoint, f"{task.task_id}:after_pa3_proof_reverification")
        record = _pilot_record(
            config=config,
            config_path=config_path,
            task=task,
            fixture_hash=fixture_hashes[task.task_id],
            contract_path=contract_path,
            repository_root=root,
            prepared_workspace=prepared_workspace,
            action_output=action_output,
            result=verified,
            oracle_result=oracle_result,
            resumed=resumed,
            known_session_hashes=session_hashes,
            checkpoint=checkpoint,
        )
        _write_once_or_verify(
            action_output / "gl-pilot-record.json",
            render_json_bytes(record.model_dump(mode="json")),
            "GL pilot run record",
        )
        records.append(record)
        _checkpoint(checkpoint, f"{task.task_id}:after_record_finalization")
        if task.deliberately_seeded and record.actual_route == "pass":
            raise PhysicsBenchmarkIntegrityError(
                f"hard stop: seeded GL pilot task {task.task_id} routed pass"
            )
        if (
            record.workspace_integrity == "changed"
            or record.answer_key_or_oracle_exposure_detected
            or record.session_reused
            or record.yolo_inheritance_detected
        ):
            raise PhysicsBenchmarkIntegrityError(
                f"hard stop: GL pilot safety boundary failed for {task.task_id}"
            )
    report = aggregate_physics_gl_pilot(config, tuple(records))
    finalize_physics_gl_pilot_report(output_directory / "aggregate", report)
    return report


def _pilot_oracle_evidence(
    *,
    task: PhysicsGLPilotTaskV1,
    contract_path: Path,
    repository_root: Path,
    task_output: Path,
    checkpoint: GLPilotCheckpoint | None,
) -> tuple[Path, Any]:
    evidence_root = task_output / "oracle-evidence"
    evidence_root.mkdir(parents=True, exist_ok=True)
    contract = load_physics_task_contract(contract_path)
    required = tuple(item for item in contract.oracles if item.required)
    if len(required) != 1:
        raise PhysicsBenchmarkInputError("GL pilot tasks require exactly one oracle")
    oracle = required[0]
    catalog = _pilot_oracle_catalog(task, oracle.id, repository_root)
    catalog_path = task_output / "control/oracle-catalog.json"
    _write_once_or_verify(
        catalog_path,
        render_json_bytes(catalog.model_dump(mode="json")),
        "GL pilot oracle catalog",
    )
    output = evidence_root / oracle.id
    _checkpoint(checkpoint, f"{task.task_id}:{oracle.id}:before_pa2_resume_or_launch")
    if output.exists():
        result = resume_physics_oracle(
            catalog_path=catalog_path,
            contract_path=contract_path,
            workspace=repository_root,
            output_directory=output,
        )
    else:
        result = run_physics_oracle(
            catalog_path=catalog_path,
            contract_path=contract_path,
            oracle_id=oracle.id,
            task_id=task.task_id,
            workspace=repository_root,
            output_directory=output,
        )
    verified = verify_physics_oracle_completion(output)
    if verified != result:
        raise PhysicsBenchmarkIntegrityError("GL pilot PA-2 evidence changed")
    _checkpoint(checkpoint, f"{task.task_id}:{oracle.id}:after_pa2_proof_reverification")
    return evidence_root, verified


def _pilot_oracle_catalog(
    task: PhysicsGLPilotTaskV1,
    oracle_id: str,
    repository_root: Path,
) -> PhysicsOracleCatalogV1:
    executable_path = Path("/usr/bin/python3").resolve(strict=True)
    program = repository_root / task.oracle_program_path
    executable = PhysicsOracleExecutableV1(
        schema_version=1,
        policy="isolated_system_python_v1",
        path=str(executable_path),
        sha256=_sha256_file(executable_path),
    )
    intent = PhysicsOracleIntentV1(
        schema_version=1,
        id=oracle_id,
        executable=executable,
        program=PhysicsOracleTrustedProgramV1(
            path=task.oracle_program_path,
            sha256=_sha256_file(program),
        ),
        argv=(
            str(executable_path),
            "-I",
            "-S",
            "-B",
            task.oracle_program_path,
            task.task_id,
            oracle_id,
        ),
        execution_policy=PhysicsOracleExecutionPolicyV1(
            schema_version=1,
            policy_id=f"{task.task_id}-{oracle_id}",
            isolation_backend="bubblewrap_unshare_all_v1",
            working_directory="workspace_root",
            workspace_access="read_only",
            scratch_output="scratch_only",
            network="disabled",
            environment_profile_id="minimal-python",
            timeout_seconds=30,
            max_stdout_bytes=65_536,
            max_stderr_bytes=65_536,
            accepted_exit_codes=(0,),
            structured_output_schema="physics_oracle_result_v1",
            required_artifacts=(),
        ),
    )
    return PhysicsOracleCatalogV1(
        schema_version=1,
        catalog_id=f"{task.task_id}-catalog",
        environment_profiles=(
            PhysicsOracleEnvironmentProfileV1(
                schema_version=1,
                id="minimal-python",
                profile="minimal_python_v1",
            ),
        ),
        intents=(intent,),
    )


def _pilot_record(
    *,
    config: PhysicsGLPilotConfigV1,
    config_path: Path,
    task: PhysicsGLPilotTaskV1,
    fixture_hash: str,
    contract_path: Path,
    repository_root: Path,
    prepared_workspace: Path,
    action_output: Path,
    result: Any,
    oracle_result: Any,
    resumed: bool,
    known_session_hashes: set[str],
    checkpoint: GLPilotCheckpoint | None,
) -> PhysicsGLPilotRunV1:
    proof = _load_model(action_output / PROOF_FILE, PhysicsAuditorActionProofV1)
    projection = _load_model(
        action_output / CONTROL_DIRECTORY / PROJECTION_MANIFEST_FILE,
        PhysicsAuditorProjectionManifestV1,
    )
    visible = {item.path for item in projection.objects}
    forbidden = {
        config_path.resolve(strict=True).relative_to(repository_root).as_posix(),
        task.oracle_program_path,
    }
    exposure = bool(visible & forbidden)
    visible_root = action_output / PROJECTION_DIRECTORY
    visible_files = {
        item.path: (visible_root / item.path).read_bytes()
        for item in projection.objects
        if item.kind == "regular"
    }
    verify_gl_auditor_visible_blindness(
        visible_files,
        prompt=(action_output / CONTROL_DIRECTORY / PROMPT_FILE).read_bytes(),
        task=task,
    )
    provider_path = action_output / PROVIDER_OBSERVATION_FILE
    provider = (
        _load_model(provider_path, PhysicsAuditorProviderObservationV1)
        if provider_path.exists()
        else None
    )
    report_path = action_output / REPORT_FILE
    report = _load_model(report_path, PhysicsAuditReportV1) if report_path.exists() else None
    findings = (
        tuple(
            PhysicsBenchmarkFindingObservationV1(
                finding_id=item.id,
                category=item.category,
                severity=item.severity,
                status=item.status,
            )
            for item in report.findings
        )
        if report is not None
        else ()
    )
    session = None
    if provider is not None:
        raw = provider.provider_session_id
        if raw is None and len(provider.provider_thread_started_ids) == 1:
            raw = provider.provider_thread_started_ids[0]
        if raw is not None:
            session = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    reused = session in known_session_hashes if session is not None else False
    if session is not None:
        known_session_hashes.add(session)
    actual_route = result.routing_decision.outcome if result.routing_decision else None
    status: Literal["routing_completed", "malformed_report", "infrastructure_failure"] = (
        "routing_completed"
        if result.status == "routing_completed"
        else "malformed_report"
        if result.status == "report_invalid"
        else "infrastructure_failure"
    )
    recovery_path = action_output / "gl-pilot-recovery-proof.json"
    action_records = tuple(sorted((action_output / "action-records").glob("*.json")))
    action_record_hashes = tuple(sorted(_sha256_file(path) for path in action_records))
    if recovery_path.exists():
        recovery = _load_model(recovery_path, PhysicsBenchmarkRecoveryProofV1)
        if (
            recovery.benchmark_id != config.pilot_id
            or recovery.case_id != task.task_id
            or recovery.repetition != 1
            or set(recovery.pa2_action_ids) != {oracle_result.request.action_id}
            or set(recovery.pa2_completion_proof_sha256s) != {oracle_result.completion_proof_sha256}
            or recovery.pa3_action_id != result.request.action_id
            or recovery.pa3_action_proof_sha256 != result.action_proof_sha256
            or recovery.pa3_record_count != len(action_records)
            or recovery.pa3_action_record_sha256s != action_record_hashes
            or not recovery.proofs_reverified
        ):
            raise PhysicsBenchmarkIntegrityError(
                "persisted GL pilot recovery proof was substituted"
            )
    else:
        recovery = PhysicsBenchmarkRecoveryProofV1(
            benchmark_id=config.pilot_id,
            case_id=task.task_id,
            repetition=1,
            pa2_action_ids=(oracle_result.request.action_id,),
            pa2_completion_proof_sha256s=(oracle_result.completion_proof_sha256,),
            pa3_action_id=result.request.action_id,
            pa3_action_proof_sha256=result.action_proof_sha256,
            pa3_record_count=len(action_records),
            pa3_action_record_sha256s=action_record_hashes,
            resumed_existing_action=resumed,
            duplicate_action_detected=False,
        )
        _write_once_or_verify(
            recovery_path,
            render_json_bytes(recovery.model_dump(mode="json")),
            "GL pilot recovery proof",
        )
    _checkpoint(checkpoint, f"{task.task_id}:after_recovery_proof_finalization")
    score = gl_pilot_scoring_observation(
        task,
        findings,
        actual_route,
        evidence_valid=report is not None,
    )
    return PhysicsGLPilotRunV1(
        pilot_id=config.pilot_id,
        task_id=task.task_id,
        topic=task.topic,
        fixture_sha256=fixture_hash,
        contract_sha256=load_physics_task_contract(contract_path).canonical_sha256(),
        locked_authority_sha256=locked_authority_sha256(task),
        expected_route=task.expected_route,
        actual_report_verdict=report.verdict if report is not None else None,
        actual_route=actual_route,
        required_finding_categories=task.required_finding_categories,
        acceptable_alternative_categories=task.acceptable_alternative_categories,
        forbidden_finding_categories=task.forbidden_finding_categories,
        minimum_severity=task.minimum_severity,
        acceptable_alternative_routes=task.acceptable_alternative_routes,
        findings=findings,
        human_review_mandatory=task.human_review_mandatory,
        route_matched=score["route_matched"],
        category_recognized=score["category_recognized"],
        severity_matched=score["severity_matched"],
        evidence_valid=score["evidence_valid"],
        required_categories_satisfied=score["required_categories_satisfied"],
        acceptable_alternative_satisfied=score["acceptable_alternative_satisfied"],
        forbidden_category_observed=score["forbidden_category_observed"],
        forbidden_route_observed=score["forbidden_route_observed"],
        run_status=status,
        fresh_session_identity_sha256=session,
        prompt_sha256=proof.canonical_prompt_sha256,
        projection_sha256=proof.projection_manifest_sha256,
        oracle_proof_manifest_sha256=proof.oracle_completion_proof_manifest_sha256,
        action_proof_sha256=result.action_proof_sha256,
        recovery_proof_sha256=recovery.canonical_sha256(),
        workspace_integrity=result.integrity_verdict,
        answer_key_or_oracle_exposure_detected=exposure or result.oracle_execution_detected,
        session_reused=reused,
        yolo_inheritance_detected=False,
        pa2_pa3_proofs_verified=True,
        source_contract_projection_verified=(
            task.contract_sha256 == load_physics_task_contract(contract_path).canonical_sha256()
            and result.projected_workspace_integrity == "unchanged"
            and _prepared_workspace_verified(prepared_workspace)
        ),
        duration_seconds=provider.adapter_result.duration_seconds if provider else 0.0,
        usage=_provider_usage(provider),
    )


def _provider_usage(
    provider: PhysicsAuditorProviderObservationV1 | None,
) -> PhysicsBenchmarkUsageV1:
    if provider is None:
        return PhysicsBenchmarkUsageV1(
            availability="unavailable", input_tokens=None, output_tokens=None
        )
    events = Path(provider.adapter_result.artifact_directory) / "events.jsonl"
    totals: dict[str, int] = {}
    try:
        for line in events.read_text(encoding="utf-8").splitlines():
            value = json.loads(line)
            _collect_usage(value, totals)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return PhysicsBenchmarkUsageV1(
            availability="unavailable", input_tokens=None, output_tokens=None
        )
    if "input_tokens" not in totals or "output_tokens" not in totals:
        return PhysicsBenchmarkUsageV1(
            availability="unavailable", input_tokens=None, output_tokens=None
        )
    return PhysicsBenchmarkUsageV1(
        availability="provider_reported",
        input_tokens=totals["input_tokens"],
        output_tokens=totals["output_tokens"],
        cached_input_tokens=totals.get("cached_input_tokens"),
    )


def _collect_usage(value: object, totals: dict[str, int]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if (
                key in {"input_tokens", "output_tokens", "cached_input_tokens"}
                and isinstance(item, int)
                and item >= 0
            ):
                totals[key] = max(totals.get(key, 0), item)
            else:
                _collect_usage(item, totals)
    elif isinstance(value, list):
        for item in value:
            _collect_usage(item, totals)


def _load_model(path: Path, model: type[Any]) -> Any:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return model.model_validate(value)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValidationError, ValueError) as exc:
        raise PhysicsBenchmarkIntegrityError("GL pilot PA artifact is invalid") from exc


def _write_once_or_verify(path: Path, value: bytes, label: str) -> None:
    if path.exists():
        if path.read_bytes() != value:
            raise PhysicsBenchmarkStateError(f"existing {label} contradicts recovery")
        return
    atomic_write_bytes(
        path,
        value,
        error_factory=PhysicsBenchmarkStateError,
        error_message=f"could not persist {label}",
    )


def _sha256_file(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise PhysicsBenchmarkInputError("GL pilot executable authority is unavailable") from exc


def _prepare_exact_gl_workspace(
    *,
    config: PhysicsGLPilotConfigV1,
    task: PhysicsGLPilotTaskV1,
    benchmark_root: Path,
    source_repository_root: Path,
    destination: Path,
) -> Path:
    expected: dict[str, bytes] = {
        f"source/{item.path}": read_gl_source_blob(
            source_repository_root,
            config.source_commit,
            item.path,
        )
        for item in task.source_refs
    }
    contract = load_physics_task_contract(benchmark_root / task.contract_path)
    for evidence in contract.evidence:
        if evidence.path is None or evidence.path.startswith("source/"):
            continue
        expected[evidence.path] = (benchmark_root / evidence.path).read_bytes()
    expected[task.oracle_program_path] = (benchmark_root / task.oracle_program_path).read_bytes()
    if destination.exists():
        _verify_prepared_files(destination, expected)
        return destination.resolve(strict=True)
    destination.mkdir(parents=True)
    for relative, content in sorted(expected.items()):
        target = destination / relative
        atomic_write_bytes(
            target,
            content,
            error_factory=PhysicsBenchmarkStateError,
            error_message="could not prepare exact GL source projection",
        )
    commands = (
        ("/usr/bin/git", "-C", str(destination), "init", "-q"),
        ("/usr/bin/git", "-C", str(destination), "add", "."),
        (
            "/usr/bin/git",
            "-C",
            str(destination),
            "-c",
            "user.name=PA5C GL Source Projector",
            "-c",
            "user.email=pa5c@example.invalid",
            "commit",
            "-qm",
            f"exact source projection {config.source_commit} {task.task_id}",
        ),
    )
    for command in commands:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=30.0,
        )
        if completed.returncode != 0:
            raise PhysicsBenchmarkStateError("could not seal exact GL source projection")
    _verify_prepared_files(destination, expected)
    return destination.resolve(strict=True)


def _verify_prepared_files(root: Path, expected: dict[str, bytes]) -> None:
    tracked = subprocess.run(
        ("/usr/bin/git", "-C", str(root), "ls-files", "-z"),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=15.0,
    )
    observed = set(tracked.stdout.decode("utf-8").split("\0")) - {""}
    status = subprocess.run(
        ("/usr/bin/git", "-C", str(root), "status", "--porcelain=v1"),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=15.0,
    )
    if tracked.returncode != 0 or status.returncode != 0 or status.stdout:
        raise PhysicsBenchmarkIntegrityError("prepared GL source workspace changed")
    if observed != set(expected):
        raise PhysicsBenchmarkIntegrityError("prepared GL source object set changed")
    for relative, content in expected.items():
        if (root / relative).read_bytes() != content:
            raise PhysicsBenchmarkIntegrityError("prepared GL source content changed")


def _prepared_workspace_verified(root: Path) -> bool:
    status = subprocess.run(
        ("/usr/bin/git", "-C", str(root), "status", "--porcelain=v1"),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=15.0,
    )
    return status.returncode == 0 and not status.stdout


def _checkpoint(checkpoint: GLPilotCheckpoint | None, name: str) -> None:
    if checkpoint is not None:
        checkpoint(name)
