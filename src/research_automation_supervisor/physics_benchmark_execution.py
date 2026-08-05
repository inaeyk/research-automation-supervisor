"""Bounded PA-2/PA-3 execution path for the public PA-5B benchmark."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError

from research_automation_supervisor.durable_state import render_json_bytes
from research_automation_supervisor.errors import (
    PhysicsBenchmarkInputError,
    PhysicsBenchmarkIntegrityError,
    PhysicsBenchmarkStateError,
)
from research_automation_supervisor.physics_auditor_execution import (
    CONTROL_DIRECTORY,
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
    PROJECTION_MANIFEST_FILE,
)
from research_automation_supervisor.physics_benchmark import (
    seeded_authority_sha256,
    validate_benchmark_authority_separation,
    validate_physics_benchmark_records,
    verify_projection_excludes_benchmark_authority,
)
from research_automation_supervisor.physics_benchmark_models import (
    PhysicsBenchmarkCaseAuthorityV1,
    PhysicsBenchmarkCatalogV1,
    PhysicsBenchmarkFindingObservationV1,
    PhysicsBenchmarkRecoveryProofV1,
    PhysicsBenchmarkRunRecordV1,
    PhysicsBenchmarkStatusV1,
    PhysicsBenchmarkUsageV1,
    PhysicsBenchmarkWorkerRepairV1,
    load_physics_benchmark_run_record,
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

CodexInvokerFactory = Callable[
    [PhysicsBenchmarkCaseAuthorityV1, int], PhysicsAuditorCodexInvoker | None
]
RepairResults = Mapping[str, PhysicsBenchmarkWorkerRepairV1]


def physics_benchmark_status(
    *,
    catalog: PhysicsBenchmarkCatalogV1,
    output_directory: Path,
    case_ids: Sequence[str] = (),
) -> PhysicsBenchmarkStatusV1:
    """Inspect the next safe sequential action without writes or process launches."""
    selected = _selected_cases(catalog, case_ids)
    expected = tuple(
        (case, repetition)
        for case in selected
        for repetition in range(1, case.repetitions + 1)
    )
    expected_record_paths = {
        output_directory
        / case.case_id
        / "actions"
        / f"repetition-{repetition:03d}"
        / "benchmark-record.json"
        for case, repetition in expected
    }
    observed_paths = (
        set(output_directory.glob("*/actions/repetition-*/benchmark-record.json"))
        if output_directory.exists()
        else set()
    )
    unexpected = observed_paths - expected_record_paths
    if unexpected:
        raise PhysicsBenchmarkIntegrityError(
            "benchmark output contains a record outside the predeclared design"
        )
    records: list[PhysicsBenchmarkRunRecordV1] = []
    partial = 0
    next_action: tuple[PhysicsBenchmarkCaseAuthorityV1, int] | None = None
    for case, repetition in expected:
        action_output = (
            output_directory
            / case.case_id
            / "actions"
            / f"repetition-{repetition:03d}"
        )
        record_path = action_output / "benchmark-record.json"
        if record_path.is_file():
            record = load_physics_benchmark_run_record(record_path)
            recovery = _load_model(
                action_output / "benchmark-recovery-proof.json",
                PhysicsBenchmarkRecoveryProofV1,
            )
            if record.recovery_proof_sha256 != recovery.canonical_sha256():
                raise PhysicsBenchmarkIntegrityError(
                    "benchmark record does not bind its recovery proof"
                )
            records.append(record)
            continue
        if action_output.exists():
            partial += 1
        if next_action is None:
            next_action = (case, repetition)
    validated = validate_physics_benchmark_records(catalog, records)
    pending = len(expected) - len(validated)
    return PhysicsBenchmarkStatusV1(
        benchmark_id=catalog.benchmark_id,
        expected_run_count=len(expected),
        completed_run_count=len(validated),
        pending_run_count=pending,
        partial_action_count=partial,
        next_case_id=next_action[0].case_id if next_action is not None else None,
        next_repetition=next_action[1] if next_action is not None else None,
        records_verified=True,
        safe_resume=True,
        model_or_oracle_launched=False,
    )


def run_public_physics_benchmark(
    *,
    catalog: PhysicsBenchmarkCatalogV1,
    catalog_path: Path,
    execution_config_path: Path,
    repository_root: Path,
    output_directory: Path,
    case_ids: Sequence[str] = (),
    codex_invoker_factory: CodexInvokerFactory | None = None,
    repair_results: RepairResults | None = None,
) -> tuple[PhysicsBenchmarkRunRecordV1, ...]:
    """Execute or safely resume the fixed sequential public benchmark design."""
    root = repository_root.resolve(strict=True)
    fixture_hashes = validate_benchmark_authority_separation(
        catalog,
        repository_root=root,
        catalog_path=catalog_path,
    )
    selected = _selected_cases(catalog, case_ids)
    repairs = _validated_repair_results(catalog, repair_results or {})
    output_directory.mkdir(parents=True, exist_ok=True)
    records: list[PhysicsBenchmarkRunRecordV1] = []
    session_hashes: set[str] = set()
    for case in selected:
        contract_path = root / case.contract_path
        case_output = output_directory / case.case_id
        evidence_root, oracle_results = _prepare_oracle_evidence(
            case=case,
            contract_path=contract_path,
            repository_root=root,
            case_output=case_output,
        )
        for repetition in range(1, case.repetitions + 1):
            action_output = case_output / "actions" / f"repetition-{repetition:03d}"
            action_output.parent.mkdir(parents=True, exist_ok=True)
            resumed = action_output.exists()
            invoker = (
                codex_invoker_factory(case, repetition)
                if codex_invoker_factory is not None
                else None
            )
            operation = resume_physics_auditor if resumed else run_physics_auditor
            kwargs: dict[str, Any] = {
                "contract_path": contract_path,
                "execution_config_path": execution_config_path,
                "task_id": case.case_id,
                "workspace": root,
                "oracle_evidence_root": evidence_root,
                "output_directory": action_output,
                "attempt_number": repetition,
            }
            if invoker is not None:
                kwargs["codex_invoker"] = invoker
            operation(**kwargs)
            result = verify_physics_auditor_action(**kwargs_without_invoker(kwargs))
            record = _record_from_action(
                catalog=catalog,
                catalog_path=catalog_path,
                case=case,
                repetition=repetition,
                fixture_hash=fixture_hashes[case.case_id],
                contract_path=contract_path,
                repository_root=root,
                action_output=action_output,
                result=result,
                oracle_results=oracle_results,
                resumed=resumed,
                known_session_hashes=session_hashes,
                worker_repair=_repair_observation(case, repetition, repairs),
            )
            _write_once_or_verify(
                action_output / "benchmark-record.json",
                render_json_bytes(record.model_dump(mode="json")),
                "benchmark run record",
            )
            records.append(record)
            if case.critical_seeded_defect and record.actual_route == "pass":
                raise PhysicsBenchmarkIntegrityError(
                    f"hard stop: critical seeded defect {case.case_id} routed pass"
                )
            if (
                record.answer_key_exposure_detected
                or record.oracle_program_access_detected
                or record.session_reused
                or record.yolo_inheritance_detected
                or record.workspace_integrity == "changed"
            ):
                raise PhysicsBenchmarkIntegrityError(
                    f"hard stop: PA-5B safety gate failed for {case.case_id}"
                )
    return tuple(records)


def _validated_repair_results(
    catalog: PhysicsBenchmarkCatalogV1,
    repair_results: RepairResults,
) -> RepairResults:
    for case_id, result in repair_results.items():
        try:
            case = catalog.case(case_id)
        except KeyError as exc:
            raise PhysicsBenchmarkInputError(
                "repair calibration names an unknown benchmark case"
            ) from exc
        if (
            not case.worker_repair_appropriate
            or not result.applicable
            or not result.attempted
        ):
            raise PhysicsBenchmarkInputError(
                "repair calibration contradicts benchmark repair authority"
            )
    return repair_results


def _repair_observation(
    case: PhysicsBenchmarkCaseAuthorityV1,
    repetition: int,
    repair_results: RepairResults,
) -> PhysicsBenchmarkWorkerRepairV1:
    calibrated = repair_results.get(case.case_id) if repetition == 1 else None
    if calibrated is not None:
        return calibrated
    return PhysicsBenchmarkWorkerRepairV1(
        applicable=case.worker_repair_appropriate,
        attempted=False,
        same_worker_session=None,
        repair_round_count=0,
        stale_evidence_invalidated=None,
        fresh_auditor_enforced=None,
        final_route=None,
        success=None,
        final_workspace_integrity="not_available",
        final_proof_integrity="not_available",
    )


def kwargs_without_invoker(value: Mapping[str, Any]) -> dict[str, Any]:
    """Remove the test-only invoker before independent proof verification."""
    return {key: item for key, item in value.items() if key != "codex_invoker"}


def _selected_cases(
    catalog: PhysicsBenchmarkCatalogV1,
    case_ids: Sequence[str],
) -> tuple[PhysicsBenchmarkCaseAuthorityV1, ...]:
    if not case_ids:
        return catalog.cases
    if len(case_ids) != len(set(case_ids)):
        raise PhysicsBenchmarkInputError("benchmark case selection contains duplicates")
    try:
        return tuple(catalog.case(case_id) for case_id in case_ids)
    except KeyError as exc:
        raise PhysicsBenchmarkInputError("benchmark case selection is unknown") from exc


def _prepare_oracle_evidence(
    *,
    case: PhysicsBenchmarkCaseAuthorityV1,
    contract_path: Path,
    repository_root: Path,
    case_output: Path,
) -> tuple[Path, tuple[Any, ...]]:
    evidence_root = case_output / "oracle-evidence"
    evidence_root.mkdir(parents=True, exist_ok=True)
    if case.seed_kind == "insufficient_evidence":
        return evidence_root, ()
    contract = load_physics_task_contract(contract_path)
    catalog = _oracle_catalog(case, contract.oracles, repository_root)
    catalog_path = case_output / "control" / "oracle-catalog.json"
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    _write_once_or_verify(
        catalog_path,
        render_json_bytes(catalog.model_dump(mode="json")),
        "generated oracle catalog",
    )
    results: list[Any] = []
    for oracle in contract.oracles:
        if not oracle.required:
            continue
        output = evidence_root / oracle.id
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
                task_id=case.case_id,
                workspace=repository_root,
                output_directory=output,
            )
        verified = verify_physics_oracle_completion(output)
        if result != verified:
            raise PhysicsBenchmarkIntegrityError("PA-2 result changed during verification")
        results.append(verified)
    return evidence_root, tuple(results)


def _oracle_catalog(
    case: PhysicsBenchmarkCaseAuthorityV1,
    oracles: Sequence[Any],
    repository_root: Path,
) -> PhysicsOracleCatalogV1:
    executable_path = Path("/usr/bin/python3").resolve(strict=True)
    program_relative = case.oracle_program_paths[0]
    program = repository_root / program_relative
    executable = PhysicsOracleExecutableV1(
        schema_version=1,
        policy="isolated_system_python_v1",
        path=str(executable_path),
        sha256=_sha256_file(executable_path),
    )
    intents = tuple(
        PhysicsOracleIntentV1(
            schema_version=1,
            id=oracle.id,
            executable=executable,
            program=PhysicsOracleTrustedProgramV1(
                path=program_relative,
                sha256=_sha256_file(program),
            ),
            argv=(
                str(executable_path),
                "-I",
                "-S",
                "-B",
                program_relative,
                case.seed_kind,
                oracle.id,
            ),
            execution_policy=PhysicsOracleExecutionPolicyV1(
                schema_version=1,
                policy_id=f"{case.case_id}-{oracle.id}",
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
        for oracle in oracles
        if oracle.required
    )
    return PhysicsOracleCatalogV1(
        schema_version=1,
        catalog_id=f"{case.case_id}-catalog",
        environment_profiles=(
            PhysicsOracleEnvironmentProfileV1(
                schema_version=1,
                id="minimal-python",
                profile="minimal_python_v1",
            ),
        ),
        intents=intents,
    )


def _record_from_action(
    *,
    catalog: PhysicsBenchmarkCatalogV1,
    catalog_path: Path,
    case: PhysicsBenchmarkCaseAuthorityV1,
    repetition: int,
    fixture_hash: str,
    contract_path: Path,
    repository_root: Path,
    action_output: Path,
    result: Any,
    oracle_results: Sequence[Any],
    resumed: bool,
    known_session_hashes: set[str],
    worker_repair: PhysicsBenchmarkWorkerRepairV1 | None,
) -> PhysicsBenchmarkRunRecordV1:
    proof = _load_model(action_output / PROOF_FILE, PhysicsAuditorActionProofV1)
    projection = _load_model(
        action_output / CONTROL_DIRECTORY / PROJECTION_MANIFEST_FILE,
        PhysicsAuditorProjectionManifestV1,
    )
    try:
        catalog_relative = catalog_path.resolve(strict=True).relative_to(
            repository_root
        ).as_posix()
    except ValueError as exc:
        raise PhysicsBenchmarkIntegrityError(
            "benchmark answer-key authority escaped the repository"
        ) from exc
    verify_projection_excludes_benchmark_authority(
        (item.path for item in projection.objects),
        case=case,
        catalog_relative_path=catalog_relative,
    )
    provider_path = action_output / PROVIDER_OBSERVATION_FILE
    provider = (
        _load_model(provider_path, PhysicsAuditorProviderObservationV1)
        if provider_path.exists()
        else None
    )
    report_path = action_output / REPORT_FILE
    report = _load_model(report_path, PhysicsAuditReportV1) if report_path.exists() else None
    findings = tuple(
        PhysicsBenchmarkFindingObservationV1(
            finding_id=item.id,
            category=item.category,
            severity=item.severity,
            status=item.status,
        )
        for item in report.findings
    ) if report is not None else ()
    actual_route = result.routing_decision.outcome if result.routing_decision is not None else None
    actual_verdict = report.verdict if report is not None else None
    status: Literal[
        "routing_completed", "malformed_report", "infrastructure_failure"
    ] = (
        "routing_completed"
        if result.status == "routing_completed"
        else "malformed_report"
        if result.status == "report_invalid"
        else "infrastructure_failure"
    )
    session = None
    if provider is not None:
        raw_session = provider.provider_session_id
        if raw_session is None and len(provider.provider_thread_started_ids) == 1:
            raw_session = provider.provider_thread_started_ids[0]
        if raw_session is not None:
            session = hashlib.sha256(raw_session.encode("utf-8")).hexdigest()
    reused = session in known_session_hashes if session is not None else False
    if session is not None:
        known_session_hashes.add(session)
    false_positive = tuple(
        item.finding_id
        for item in findings
        if (
            (case.clean_case and item.status == "open")
            or (not case.clean_case and item.category not in case.required_finding_categories)
        )
    )
    critical_detected = case.critical_seeded_defect and actual_route != "pass" and any(
        item.status == "open"
        and item.category in case.required_finding_categories
        and item.severity in {"critical", "high"}
        for item in findings
    )
    records = tuple((action_output / "action-records").glob("*.json"))
    recovery_path = action_output / "benchmark-recovery-proof.json"
    if recovery_path.exists():
        recovery = _load_model(recovery_path, PhysicsBenchmarkRecoveryProofV1)
        if (
            recovery.benchmark_id != catalog.benchmark_id
            or recovery.case_id != case.case_id
            or recovery.repetition != repetition
            or set(recovery.pa2_action_ids)
            != {item.request.action_id for item in oracle_results}
            or set(recovery.pa2_completion_proof_sha256s)
            != {item.completion_proof_sha256 for item in oracle_results}
            or recovery.pa3_action_id != result.request.action_id
            or recovery.pa3_action_proof_sha256 != result.action_proof_sha256
        ):
            raise PhysicsBenchmarkIntegrityError(
                "persisted benchmark recovery proof was substituted"
            )
    else:
        recovery = PhysicsBenchmarkRecoveryProofV1(
            benchmark_id=catalog.benchmark_id,
            case_id=case.case_id,
            repetition=repetition,
            pa2_action_ids=tuple(item.request.action_id for item in oracle_results),
            pa2_completion_proof_sha256s=tuple(
                item.completion_proof_sha256 for item in oracle_results
            ),
            pa3_action_id=result.request.action_id,
            pa3_action_proof_sha256=result.action_proof_sha256,
            pa3_record_count=len(records),
            resumed_existing_action=resumed,
            duplicate_action_detected=False,
        )
        _write_once_or_verify(
            recovery_path,
            render_json_bytes(recovery.model_dump(mode="json")),
            "benchmark recovery proof",
        )
    usage = _provider_usage(provider)
    return PhysicsBenchmarkRunRecordV1(
        benchmark_id=catalog.benchmark_id,
        case_id=case.case_id,
        category=case.category,
        repetition=repetition,
        fixture_sha256=fixture_hash,
        contract_sha256=load_physics_task_contract(contract_path).canonical_sha256(),
        seeded_defect_authority_sha256=seeded_authority_sha256(case),
        expected_route=case.expected_route,
        actual_report_verdict=actual_verdict,
        actual_route=actual_route,
        required_finding_categories=case.required_finding_categories,
        findings=findings,
        critical_defect_detected=critical_detected,
        false_positive_finding_ids=false_positive,
        run_status=status,
        malformed_report=status == "malformed_report",
        infrastructure_failure=status == "infrastructure_failure",
        infrastructure_reason=(
            result.failure_reason if status == "infrastructure_failure" else None
        ),
        worker_repair=worker_repair,
        fresh_session_identity_sha256=session,
        session_reused=reused,
        prompt_template_sha256=proof.prompt_template_sha256,
        prompt_sha256=proof.canonical_prompt_sha256,
        projection_sha256=proof.projection_manifest_sha256,
        oracle_proof_manifest_sha256=proof.oracle_completion_proof_manifest_sha256,
        action_proof_sha256=result.action_proof_sha256,
        recovery_proof_sha256=recovery.canonical_sha256(),
        duplicate_recovery_action_detected=False,
        workspace_integrity=result.integrity_verdict,
        projection_integrity=result.projected_workspace_integrity,
        oracle_program_access_detected=result.oracle_execution_detected,
        answer_key_exposure_detected=False,
        yolo_inheritance_detected=False,
        pa2_proofs_verified=True,
        pa3_proof_verified=True,
        duration_seconds=(provider.adapter_result.duration_seconds if provider else 0.0),
        usage=usage,
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
            if key in {"input_tokens", "output_tokens", "cached_input_tokens"} and isinstance(
                item, int
            ) and item >= 0:
                totals[key] = max(totals.get(key, 0), item)
            else:
                _collect_usage(item, totals)
    elif isinstance(value, list):
        for item in value:
            _collect_usage(item, totals)


def _load_model(path: Path, model: type[Any]) -> Any:
    try:
        value = json.loads(path.read_text(encoding="utf-8"), parse_constant=_reject_constant)
        return model.model_validate(value)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValidationError, ValueError) as exc:
        raise PhysicsBenchmarkIntegrityError("benchmark-bound PA artifact is invalid") from exc


def _write_once_or_verify(path: Path, value: bytes, label: str) -> None:
    if path.exists():
        try:
            current = path.read_bytes()
        except OSError as exc:
            raise PhysicsBenchmarkStateError(f"could not verify existing {label}") from exc
        if current != value:
            raise PhysicsBenchmarkStateError(f"existing {label} contradicts recovery")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_bytes(value)
    except OSError as exc:
        raise PhysicsBenchmarkStateError(f"could not persist {label}") from exc


def _sha256_file(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise PhysicsBenchmarkInputError("benchmark executable authority is unavailable") from exc


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is forbidden: {value}")
