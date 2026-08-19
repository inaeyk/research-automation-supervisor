"""PA-4 integration for physics-enabled schema-version-2 substages."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar, cast

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, RootModel, ValidationError

from research_automation_supervisor.contract import (
    _format_validation_error,
    _UniqueKeySafeLoader,
)
from research_automation_supervisor.durable_state import (
    ZERO_HASH,
    append_hashed_journal_entry,
    atomic_write_bytes,
    commit_result_then_state,
    commit_state_then_result,
    read_hashed_journal,
    reconcile_model_snapshot,
)
from research_automation_supervisor.errors import (
    PhysicsAuditorError,
    PhysicsOracleError,
    PhysicsValidationError,
    WorkflowDependencyError,
    WorkflowInputError,
    WorkflowStateError,
)
from research_automation_supervisor.physics_auditor_execution import (
    PROOF_FILE as AUDITOR_PROOF_FILE,
)
from research_automation_supervisor.physics_auditor_execution import (
    PROVIDER_OBSERVATION_FILE,
    REPORT_FILE,
    ROUTING_FILE,
    PhysicsAuditorCodexInvoker,
    resume_physics_auditor,
    run_physics_auditor,
    verify_physics_auditor_action,
)
from research_automation_supervisor.physics_auditor_execution import (
    RESULT_FILE as AUDITOR_RESULT_FILE,
)
from research_automation_supervisor.physics_auditor_models import (
    PhysicsAuditorActionResultV1,
    PhysicsAuditorProviderObservationV1,
    load_physics_auditor_execution_config,
)
from research_automation_supervisor.physics_models import (
    DEFAULT_PHYSICS_AUDIT_POLICY_V1,
    PhysicsAuditReportV1,
    load_physics_task_contract,
)
from research_automation_supervisor.physics_oracle_execution import (
    PROOF_FILE as ORACLE_PROOF_FILE,
)
from research_automation_supervisor.physics_oracle_execution import (
    RESULT_FILE as ORACLE_RESULT_FILE,
)
from research_automation_supervisor.physics_oracle_execution import (
    resume_physics_oracle,
    run_physics_oracle,
    verify_physics_oracle_completion,
)
from research_automation_supervisor.physics_oracle_models import (
    PhysicsOracleExecutionResultV1,
    load_physics_oracle_catalog,
)
from research_automation_supervisor.physics_oracle_workspace import (
    collect_physics_oracle_workspace_identity,
)
from research_automation_supervisor.physics_workflow_models import (
    PHYSICS_ACTIVE_STATUSES_V2,
    PHYSICS_JOURNAL_SEMANTIC_FORMS_V2,
    PHYSICS_PAUSED_STATUSES_V2,
    PHYSICS_TERMINAL_STATUSES_V2,
    PhysicsHumanReviewPacketV1,
    PhysicsOracleEvidenceRecordV2,
    PhysicsReviewDecisionV1,
    PhysicsSubstageSpecificationV2,
    PhysicsWorkflowJournalEntryV2,
    PhysicsWorkflowResultV2,
    PhysicsWorkflowStateV2,
    PreparedPhysicsSubstageV2,
)
from research_automation_supervisor.workflow_engine import (
    DEFAULT_WORKFLOW_SERVICES,
    WorkflowPromptDecision,
    WorkflowPromptRequest,
    WorkflowPromptSource,
    WorkflowServices,
    _WorkflowLock,
    continue_substage,
    post_audit_prompt_source_boundary,
    resume_substage,
    run_substage,
    substage_status,
)
from research_automation_supervisor.workflow_integrity import sha256_regular_file
from research_automation_supervisor.workflow_models import (
    ACTIVE_STATUSES,
    PreparedSubstage,
    PreparedWorkflowTest,
    _absolute_locator,
    _git_baseline,
    _load_human_file,
    _read_utf8_file,
    _resolve_directory,
    _resolve_regular_file,
    _resolve_test_cwd,
    path_matches_any,
)

STATE_FILE = "state.json"
RESULT_FILE = "result.json"
JOURNAL_FILE = "physics-journal-v2.jsonl"
LOCK_FILE = "physics-workflow.lock"
SOFTWARE_SPEC_FILE = "software-substage-v1.yaml"
MAX_SPECIFICATION_BYTES = 2 * 1024 * 1024


OracleRunner = Callable[..., PhysicsOracleExecutionResultV1]
AuditorRunner = Callable[..., PhysicsAuditorActionResultV1]
Checkpoint = Callable[[str], None]
ModelT = TypeVar("ModelT", bound=BaseModel)


@dataclass(frozen=True)
class PhysicsWorkflowServices:
    """PA-4-only qualification seams; the frozen WorkflowServices stays unchanged."""

    oracle_runner: OracleRunner = run_physics_oracle
    oracle_resumer: OracleRunner = resume_physics_oracle
    oracle_verifier: OracleRunner = verify_physics_oracle_completion
    auditor_runner: AuditorRunner = run_physics_auditor
    auditor_resumer: AuditorRunner = resume_physics_auditor
    auditor_verifier: AuditorRunner = verify_physics_auditor_action
    physics_auditor_codex_invoker: PhysicsAuditorCodexInvoker | None = None
    checkpoint: Checkpoint = lambda _name: None
    utc_now: Callable[[], datetime] = lambda: datetime.now(UTC)


DEFAULT_PHYSICS_WORKFLOW_SERVICES = PhysicsWorkflowServices()


class _PhysicsSoftwarePromptSource:
    """Hold a verified Code Auditor result at the PA-4 handoff boundary."""

    def __init__(self, upstream: WorkflowPromptSource | None) -> None:
        self._upstream = upstream

    def __call__(self, request: WorkflowPromptRequest) -> WorkflowPromptDecision:
        if request.action in {"finish", "human_pause"}:
            return WorkflowPromptDecision(
                action="human_pause",
                prompt=None,
                summary="PA-4 retained the verified Code Auditor boundary.",
            )
        if self._upstream is not None:
            return self._upstream(request)
        return WorkflowPromptDecision(
            action=request.action,
            prompt=b"Continue under the complete engine-owned workflow authority.\n",
            summary="PA-4 software phase uses the unchanged v1 role and evidence policy.",
        )


@dataclass
class _PhysicsContext:
    prepared: PreparedPhysicsSubstageV2
    run_directory: Path
    state: PhysicsWorkflowStateV2
    software_services: WorkflowServices
    physics_services: PhysicsWorkflowServices


_MUTABLE_STATE_FIELDS = frozenset(
    {
        "status",
        "repair_round",
        "software_run_directory",
        "worker_thread_id",
        "latest_software_result_path",
        "tests_passed",
        "code_auditor_passed",
        "required_oracle_proofs_verified",
        "oracle_evidence",
        "historical_oracle_evidence",
        "invalidated_oracle_ids",
        "preserved_oracle_ids",
        "current_workspace_identity_sha256",
        "accepted_workspace_identity_sha256",
        "physics_auditor_action_directory",
        "physics_auditor_result_sha256",
        "physics_auditor_proof_sha256",
        "physics_report_sha256",
        "physics_routing_sha256",
        "physics_route",
        "physics_reason_codes",
        "prior_physics_auditor_thread_ids",
        "repair_prompt_path",
        "repair_prompt_sha256",
        "repair_prompt_consumed",
        "human_review_packet_path",
        "human_review_packet_sha256",
        "human_decision_path",
        "human_decision_sha256",
        "pause_reason",
        "summary",
    }
)


def validate_physics_substage(path: Path) -> PreparedPhysicsSubstageV2:
    """Validate schema-v2 authority and the clean workspace without writes."""
    return load_physics_substage_specification(path)


def load_physics_substage_specification(
    path: Path,
    *,
    sensitive_values: Sequence[str] = (),
    require_clean: bool = True,
) -> PreparedPhysicsSubstageV2:
    locator = _absolute_locator(path)
    resolved = _resolve_regular_file(path, "physics substage specification")
    raw = _read_utf8_file(
        resolved,
        "physics substage specification",
        limit=MAX_SPECIFICATION_BYTES,
    )
    try:
        value = yaml.load(raw.decode("utf-8"), Loader=_UniqueKeySafeLoader)
        specification = PhysicsSubstageSpecificationV2.model_validate(value)
    except (UnicodeDecodeError, yaml.YAMLError, ValidationError, ValueError) as exc:
        detail = (
            "; ".join(_format_validation_error(item) for item in exc.errors())
            if isinstance(exc, ValidationError)
            else str(exc)
        )
        raise WorkflowInputError(
            f"physics substage specification validation failed: {detail}"
        ) from exc

    parent = resolved.parent
    locator_parent = locator.parent
    workspace = _resolve_directory(parent, specification.workspace, "workspace")
    repository_root, baseline_commit, baseline_branch, clean_status = _git_baseline(workspace)
    if require_clean and clean_status:
        raise WorkflowInputError("workspace must be clean, including untracked files")

    software_specification = specification.software_specification()
    contract = _load_human_file(
        locator_parent,
        software_specification.contract_path,
        "software contract",
        workspace,
        software_specification.protected_paths,
    )
    worker_initial = _load_human_file(
        locator_parent,
        software_specification.worker_initial_prompt_path,
        "worker initial prompt",
        workspace,
        software_specification.protected_paths,
    )
    worker_repair = _load_human_file(
        locator_parent,
        software_specification.worker_repair_prompt_path,
        "worker repair prompt",
        workspace,
        software_specification.protected_paths,
    )
    code_auditor = _load_human_file(
        locator_parent,
        software_specification.auditor_prompt_path,
        "Code Auditor prompt",
        workspace,
        software_specification.protected_paths,
    )
    physics_contract_file = _load_human_file(
        locator_parent,
        specification.physics_contract_path,
        "physics contract",
        workspace,
        software_specification.protected_paths,
    )
    catalog_file = _load_human_file(
        locator_parent,
        specification.physics.trusted_oracle_catalog_path,
        "trusted physics oracle catalog",
        workspace,
        software_specification.protected_paths,
    )
    auditor_config_file = _load_human_file(
        locator_parent,
        specification.physics.auditor_execution_config_path,
        "Physics Auditor execution configuration",
        workspace,
        software_specification.protected_paths,
    )

    prepared_tests = tuple(
        PreparedWorkflowTest(
            specification=test,
            cwd=_resolve_test_cwd(parent, workspace, test.cwd),
        )
        for test in software_specification.acceptance_tests
    )
    software_bytes = yaml.safe_dump(
        software_specification.model_dump(mode="json"), sort_keys=False
    ).encode("utf-8")
    software_prepared = PreparedSubstage(
        specification_locator_path=locator,
        specification_path=resolved,
        specification_bytes=software_bytes,
        specification_sha256=hashlib.sha256(software_bytes).hexdigest(),
        specification=software_specification,
        workspace=workspace,
        repository_root=repository_root,
        baseline_commit=baseline_commit,
        baseline_branch=baseline_branch,
        contract=contract,
        worker_initial_prompt=worker_initial,
        worker_repair_prompt=worker_repair,
        auditor_prompt=code_auditor,
        acceptance_tests=prepared_tests,
    )

    try:
        physics_contract = load_physics_task_contract(physics_contract_file.path)
        catalog = load_physics_oracle_catalog(catalog_file.path)
        auditor_config = load_physics_auditor_execution_config(auditor_config_file.path)
    except (PhysicsValidationError, PhysicsOracleError, PhysicsAuditorError) as exc:
        raise WorkflowInputError(f"physics authority validation failed: {exc}") from exc
    expected_policy = physics_contract.audit_policy or DEFAULT_PHYSICS_AUDIT_POLICY_V1
    if specification.physics.routing_policy() != expected_policy:
        raise WorkflowInputError(
            "physics workflow finding/evidence policy must exactly match the PA-1 contract policy"
        )
    required_oracle_ids = {item.id for item in physics_contract.oracles if item.required}
    catalog_ids = {item.id for item in catalog.intents}
    missing = sorted(required_oracle_ids - catalog_ids)
    if missing:
        raise WorkflowInputError(
            "trusted catalog lacks contract-required oracle intents: " + ", ".join(missing)
        )
    for oracle_id in sorted(required_oracle_ids):
        program_path = catalog.intent(oracle_id).program.path
        if not path_matches_any(program_path, software_specification.protected_paths):
            raise WorkflowInputError(
                f"trusted oracle program {oracle_id} must match protected_paths"
            )
        if path_matches_any(program_path, software_specification.allowed_paths):
            raise WorkflowInputError(
                f"trusted oracle program {oracle_id} cannot match allowed_paths"
            )

    structural = (
        str(resolved),
        str(workspace),
        str(repository_root),
        baseline_commit,
        *(
            str(item.path)
            for item in (
                contract,
                worker_initial,
                worker_repair,
                code_auditor,
                physics_contract_file,
                catalog_file,
                auditor_config_file,
            )
        ),
        *(
            item.sha256
            for item in (
                contract,
                worker_initial,
                worker_repair,
                code_auditor,
                physics_contract_file,
                catalog_file,
                auditor_config_file,
            )
        ),
    )
    from research_automation_supervisor.redaction import would_redact_text

    if any(would_redact_text(item, sensitive_values) for item in structural):
        raise WorkflowInputError("physics substage has a structural redaction collision")

    return PreparedPhysicsSubstageV2(
        specification_locator_path=locator,
        specification_path=resolved,
        specification_bytes=raw,
        specification_sha256=hashlib.sha256(raw).hexdigest(),
        specification=specification,
        software_prepared=software_prepared,
        physics_contract_path=physics_contract_file.path,
        physics_contract_bytes=physics_contract_file.content,
        physics_contract_sha256=physics_contract.canonical_sha256(),
        physics_contract=physics_contract,
        oracle_catalog_path=catalog_file.path,
        oracle_catalog_bytes=catalog_file.content,
        oracle_catalog_sha256=catalog.canonical_sha256(),
        oracle_catalog=catalog,
        auditor_config_path=auditor_config_file.path,
        auditor_config_bytes=auditor_config_file.content,
        auditor_config_sha256=auditor_config.canonical_sha256(),
        auditor_config=auditor_config,
    )


def run_physics_substage(
    path: Path,
    *,
    runs_dir: Path = Path("runs/workflows"),
    software_services: WorkflowServices = DEFAULT_WORKFLOW_SERVICES,
    physics_services: PhysicsWorkflowServices = DEFAULT_PHYSICS_WORKFLOW_SERVICES,
) -> PhysicsWorkflowResultV2:
    """Create and synchronously drive one schema-v2 physics workflow."""
    prepared = load_physics_substage_specification(path)
    token = software_services.token_factory()
    if not token or len(token) > 80 or not token.replace("-", "").replace("_", "").isalnum():
        raise WorkflowInputError("physics workflow run token is invalid")
    root = runs_dir.resolve(strict=False)
    run_directory = root / f"{prepared.specification.substage_id}-{token}"
    try:
        root.mkdir(parents=True, exist_ok=True)
        run_directory.mkdir(exist_ok=False)
    except FileExistsError as exc:
        raise WorkflowInputError("exclusive physics workflow run directory already exists") from exc
    except OSError as exc:
        raise WorkflowInputError("physics workflow run directory could not be created") from exc

    _initialize_run(run_directory, prepared)
    now = _utc_string(software_services.utc_now())
    state = PhysicsWorkflowStateV2(
        substage_id=prepared.specification.substage_id,
        run_token=token,
        status="initialized",
        repair_round=0,
        max_repair_rounds=prepared.effective_max_repair_rounds,
        checkpoint_after=prepared.specification.checkpoint_after,
        specification_path=str(prepared.specification_path),
        specification_sha256=prepared.specification_sha256,
        software_specification_path=str(run_directory / "control" / SOFTWARE_SPEC_FILE),
        software_run_directory=None,
        physics_contract_path=str(prepared.physics_contract_path),
        physics_contract_sha256=prepared.physics_contract_sha256,
        oracle_catalog_path=str(prepared.oracle_catalog_path),
        oracle_catalog_sha256=prepared.oracle_catalog_sha256,
        auditor_config_path=str(prepared.auditor_config_path),
        auditor_config_sha256=prepared.auditor_config_sha256,
        workspace=str(prepared.workspace),
        repository_root=str(prepared.repository_root),
        baseline_commit=prepared.software_prepared.baseline_commit,
        baseline_branch=prepared.software_prepared.baseline_branch,
        worker_thread_id=None,
        latest_software_result_path=None,
        tests_passed=False,
        code_auditor_passed=False,
        required_oracle_proofs_verified=False,
        oracle_evidence=(),
        historical_oracle_evidence=(),
        invalidated_oracle_ids=(),
        preserved_oracle_ids=(),
        current_workspace_identity_sha256=None,
        accepted_workspace_identity_sha256=None,
        physics_auditor_action_directory=None,
        physics_auditor_result_sha256=None,
        physics_auditor_proof_sha256=None,
        physics_report_sha256=None,
        physics_routing_sha256=None,
        physics_route=None,
        physics_reason_codes=(),
        prior_physics_auditor_thread_ids=(),
        repair_prompt_path=None,
        repair_prompt_sha256=None,
        repair_prompt_consumed=False,
        human_review_packet_path=None,
        human_review_packet_sha256=None,
        human_decision_path=None,
        human_decision_sha256=None,
        pause_reason=None,
        summary="Physics workflow initialized.",
        artifact_directory=str(run_directory),
        journal_sequence=0,
        journal_hash=ZERO_HASH,
        started_at=now,
        updated_at=now,
    )
    commit_state_then_result(
        state_path=run_directory / STATE_FILE,
        state_value=state.model_dump(mode="json"),
        result_path=run_directory / RESULT_FILE,
        result_value=state.to_result().model_dump(mode="json"),
        checkpoint=lambda name: physics_services.checkpoint(f"initial_snapshot:{name}"),
        error_factory=WorkflowStateError,
        error_message="initial physics workflow snapshots could not be persisted",
    )
    state = _journal(
        run_directory,
        state,
        physics_services,
        event_type="transition",
        previous_state=None,
        new_state="initialized",
        action_id=None,
        action_kind=None,
        reason="physics_workflow_initialized",
        artifact_hashes=_frozen_hashes(run_directory, prepared),
        updates={},
    )
    with _WorkflowLock(run_directory, software_services.utc_now):
        context = _PhysicsContext(
            prepared=prepared,
            run_directory=run_directory,
            state=state,
            software_services=software_services,
            physics_services=physics_services,
        )
        context.state = _transition(
            context,
            "software_running",
            "software_workflow_requested",
            summary="Worker, visible tests, and Code Auditor requested.",
        )
        return _drive(context)


def resume_physics_substage(
    run_directory: Path,
    *,
    software_services: WorkflowServices = DEFAULT_WORKFLOW_SERVICES,
    physics_services: PhysicsWorkflowServices = DEFAULT_PHYSICS_WORKFLOW_SERVICES,
) -> PhysicsWorkflowResultV2:
    """Recover a schema-v2 workflow without repeating an ambiguous PA-2/PA-3 launch."""
    resolved = _resolve_run_directory(run_directory)
    with _WorkflowLock(resolved, software_services.utc_now):
        state = _load_reconciled_state(resolved, physics_services, persist=True)
        if (
            state.status in PHYSICS_TERMINAL_STATUSES_V2
            or state.status in PHYSICS_PAUSED_STATUSES_V2
        ):
            raise WorkflowInputError("physics workflow state cannot be resumed automatically")
        prepared = load_physics_substage_specification(
            Path(state.specification_path), require_clean=False
        )
        _verify_frozen_state(prepared, state)
        if state.journal_sequence == 0:
            state = _journal(
                resolved,
                state,
                physics_services,
                event_type="transition",
                previous_state=None,
                new_state="initialized",
                action_id=None,
                action_kind=None,
                reason="physics_workflow_initialized",
                artifact_hashes=_frozen_hashes(resolved, prepared),
                updates={},
            )
        context = _PhysicsContext(
            prepared=prepared,
            run_directory=resolved,
            state=state,
            software_services=software_services,
            physics_services=physics_services,
        )
        return _drive(context)


def review_physics_substage(
    run_directory: Path,
    decision_path: Path,
    *,
    software_services: WorkflowServices = DEFAULT_WORKFLOW_SERVICES,
    physics_services: PhysicsWorkflowServices = DEFAULT_PHYSICS_WORKFLOW_SERVICES,
) -> PhysicsWorkflowResultV2:
    """Apply one exact, immutable scientific-review decision."""
    resolved = _resolve_run_directory(run_directory)
    with _WorkflowLock(resolved, software_services.utc_now):
        state = _load_reconciled_state(resolved, physics_services, persist=True)
        if state.status not in PHYSICS_PAUSED_STATUSES_V2:
            raise WorkflowInputError("physics decisions are accepted only from a physics pause")
        prepared = load_physics_substage_specification(
            Path(state.specification_path), require_clean=False
        )
        _verify_frozen_state(prepared, state)
        context = _PhysicsContext(
            prepared=prepared,
            run_directory=resolved,
            state=state,
            software_services=software_services,
            physics_services=physics_services,
        )
        decision = _load_review_decision(decision_path)
        packet = _load_review_packet(state)
        if (
            decision.run_token != state.run_token
            or decision.review_packet_sha256 != packet.canonical_sha256()
        ):
            raise WorkflowInputError("human decision does not bind the current review packet")
        if not set(decision.acknowledged_finding_ids).issubset(packet.finding_ids):
            raise WorkflowInputError("human decision acknowledges unknown finding IDs")
        if not set(decision.acknowledged_question_ids).issubset(packet.unresolved_question_ids):
            raise WorkflowInputError("human decision acknowledges unknown question IDs")
        recovering_decision = (
            _read_journal(resolved)[-1].reason == "physics_human_decision_recorded"
        )
        if recovering_decision:
            if state.human_decision_path is None or state.human_decision_sha256 is None:
                raise WorkflowStateError("pending human decision recovery evidence is missing")
            accepted = _load_model(Path(state.human_decision_path), PhysicsReviewDecisionV1)
            if accepted != decision or accepted.canonical_sha256() != state.human_decision_sha256:
                raise WorkflowInputError(
                    "a different human decision is already pending deterministic recovery"
                )
        else:
            destination = (
                resolved / "human-decisions" / f"decision-{state.journal_sequence + 1:04d}.json"
            )
            _write_once(destination, decision.model_dump(mode="json"))
            context.state = _journal_event(
                context,
                event_type="human_decision",
                reason="physics_human_decision_recorded",
                action_id=None,
                action_kind=None,
                artifact_hashes={str(destination): sha256_regular_file(destination)},
                updates={
                    "human_decision_path": str(destination),
                    "human_decision_sha256": decision.canonical_sha256(),
                },
            )
        if decision.decision == "reject_candidate":
            context.state = _transition(
                context,
                "aborted",
                "human_rejected_candidate",
                pause_reason="human_rejected_candidate",
                summary="Human scientific review rejected the candidate.",
            )
            return context.state.to_result()
        if decision.decision == "revise_contract":
            context.state = _transition(
                context,
                "aborted",
                "human_revised_contract_new_run_required",
                pause_reason="revised_contract_requires_new_run",
                summary="A revised immutable physics contract requires a new run.",
            )
            return context.state.to_result()
        if decision.decision == "accept_with_caveat":
            context.state = _transition(
                context,
                "human_review_paused",
                "human_caveat_recorded",
                pause_reason="caveat_is_not_release_authority",
                summary=("The caveat was recorded, but the authoritative PA-1 route is not pass."),
            )
            return context.state.to_result()
        if context.state.repair_round >= context.state.max_repair_rounds:
            raise WorkflowInputError("the shared physics repair limit is exhausted")
        if context.state.worker_thread_id is None:
            raise WorkflowInputError("physics continuation requires the persistent Worker ID")
        prompt_path, prompt_hash = _write_repair_prompt(context)
        reason = (
            "human_approved_existing_contract_repair"
            if decision.decision == "approve_existing_contract"
            else "human_requested_additional_evidence"
        )
        context.state = _transition(
            context,
            "physics_repair_pending",
            reason,
            repair_prompt_path=str(prompt_path),
            repair_prompt_sha256=prompt_hash,
            repair_prompt_consumed=False,
            pause_reason=(
                "additional_evidence_requested"
                if decision.decision == "request_additional_evidence"
                else "existing_contract_repair_requested"
            ),
            summary="Human-gated continuation is ready for the persistent Worker.",
        )
        return _drive(context)


def physics_substage_status(run_directory: Path) -> PhysicsWorkflowResultV2:
    """Read and verify an exact schema-v2 state/result/journal without writes."""
    resolved = _resolve_run_directory(run_directory)
    state = _load_reconciled_state(resolved, DEFAULT_PHYSICS_WORKFLOW_SERVICES, persist=False)
    if state.journal_sequence != len(_read_journal(resolved)):
        raise WorkflowStateError("physics workflow state is behind its journal")
    result = _load_model(resolved / RESULT_FILE, PhysicsWorkflowResultV2)
    if result != state.to_result():
        raise WorkflowStateError("physics workflow state and result disagree")
    if state.status in {"completed", "checkpoint_paused"}:
        try:
            prepared = load_physics_substage_specification(
                Path(state.specification_path), require_clean=False
            )
            _verify_frozen_state(prepared, state)
            _verify_accepted_action_evidence(prepared, state)
        except (PhysicsAuditorError, PhysicsOracleError, WorkflowInputError) as exc:
            raise WorkflowStateError(
                "accepted physics workflow action evidence no longer verifies"
            ) from exc
        try:
            current_identity = collect_physics_oracle_workspace_identity(
                Path(state.workspace)
            ).canonical_sha256()
        except PhysicsOracleError as exc:
            raise WorkflowStateError(
                "completed physics workflow workspace identity is unavailable"
            ) from exc
        if current_identity != state.accepted_workspace_identity_sha256:
            raise WorkflowStateError(
                "completed physics workflow workspace no longer matches accepted evidence"
            )
    return result


def _verify_accepted_action_evidence(
    prepared: PreparedPhysicsSubstageV2,
    state: PhysicsWorkflowStateV2,
) -> None:
    if not state.oracle_evidence:
        raise WorkflowStateError("accepted physics workflow lacks oracle evidence")
    evidence_parents = {Path(item.output_directory).parent for item in state.oracle_evidence}
    if len(evidence_parents) != 1:
        raise WorkflowStateError("accepted oracle evidence roots are inconsistent")
    for record in state.oracle_evidence:
        verified_oracle = verify_physics_oracle_completion(Path(record.output_directory))
        identity_hash = verified_oracle.final_workspace_identity.canonical_sha256()
        if (
            verified_oracle.canonical_sha256() != record.result_sha256
            or verified_oracle.completion_proof_sha256 != record.completion_proof_sha256
            or verified_oracle.status != record.status
            or verified_oracle.request.task_id != state.substage_id
            or verified_oracle.request.oracle_id != record.oracle_id
            or verified_oracle.request.contract_sha256 != state.physics_contract_sha256
            or verified_oracle.initial_workspace_identity
            != verified_oracle.final_workspace_identity
            or identity_hash != record.workspace_identity_sha256
            or identity_hash != state.accepted_workspace_identity_sha256
        ):
            raise WorkflowStateError("accepted oracle evidence disagrees with durable state")
    if state.physics_auditor_action_directory is None:
        raise WorkflowStateError("accepted physics workflow lacks Physics Auditor evidence")
    verified_auditor = verify_physics_auditor_action(
        contract_path=prepared.physics_contract_path,
        execution_config_path=prepared.auditor_config_path,
        task_id=state.substage_id,
        workspace=prepared.workspace,
        oracle_evidence_root=next(iter(evidence_parents)),
        output_directory=Path(state.physics_auditor_action_directory),
        action_id=f"physics-auditor-r{state.repair_round:03d}",
        attempt_number=state.repair_round + 1,
    )
    if (
        verified_auditor.status != "routing_completed"
        or verified_auditor.routing_decision is None
        or verified_auditor.routing_decision.outcome != "pass"
        or verified_auditor.canonical_sha256() != state.physics_auditor_result_sha256
        or verified_auditor.action_proof_sha256 != state.physics_auditor_proof_sha256
        or verified_auditor.parsed_report_sha256 != state.physics_report_sha256
        or verified_auditor.routing_decision.canonical_sha256() != state.physics_routing_sha256
        or verified_auditor.final_workspace_identity.canonical_sha256()
        != state.accepted_workspace_identity_sha256
    ):
        raise WorkflowStateError("accepted Physics Auditor evidence disagrees with durable state")


def abort_physics_substage(
    run_directory: Path,
    reason: str,
    *,
    software_services: WorkflowServices = DEFAULT_WORKFLOW_SERVICES,
    physics_services: PhysicsWorkflowServices = DEFAULT_PHYSICS_WORKFLOW_SERVICES,
) -> PhysicsWorkflowResultV2:
    resolved = _resolve_run_directory(run_directory)
    with _WorkflowLock(resolved, software_services.utc_now):
        state = _load_reconciled_state(resolved, physics_services, persist=True)
        if state.status in PHYSICS_TERMINAL_STATUSES_V2:
            raise WorkflowInputError("terminal physics workflows cannot be aborted")
        if state.status in PHYSICS_ACTIVE_STATUSES_V2:
            raise WorkflowInputError("active physics workflows cannot be aborted")
        prepared = load_physics_substage_specification(
            Path(state.specification_path), require_clean=False
        )
        context = _PhysicsContext(
            prepared=prepared,
            run_directory=resolved,
            state=state,
            software_services=software_services,
            physics_services=physics_services,
        )
        summary = " ".join(reason.split()).strip()
        if not summary:
            raise WorkflowInputError("abort reason must not be empty")
        context.state = _transition(
            context,
            "aborted",
            "human_abort",
            pause_reason=summary[:16_384],
            summary="Physics workflow aborted by human request.",
        )
        return context.state.to_result()


def physics_workflow_exit_code(status: str) -> int:
    return {
        "completed": 0,
        "human_review_paused": 5,
        "evidence_paused": 9,
        "repair_limit_paused": 6,
        "checkpoint_paused": 7,
        "infrastructure_stopped": 3,
        "failed": 4,
        "aborted": 8,
        "initialized": 4,
        "software_running": 4,
        "physics_oracles_running": 4,
        "physics_auditor_running": 4,
        "physics_repair_pending": 4,
    }[status]


def _drive(context: _PhysicsContext) -> PhysicsWorkflowResultV2:
    while True:
        status = context.state.status
        if status in PHYSICS_TERMINAL_STATUSES_V2 or status in PHYSICS_PAUSED_STATUSES_V2:
            return context.state.to_result()
        if not _frozen_inputs_match(context):
            _pause_contract_weakening(
                context,
                "Frozen physics workflow authority changed; human scientific review is required.",
            )
            continue
        if status == "software_running":
            _handle_software(context)
        elif status == "physics_oracles_running":
            _handle_oracles(context)
        elif status == "physics_auditor_running":
            _handle_physics_auditor(context)
        elif status == "physics_repair_pending":
            if context.state.pause_reason == "additional_evidence_requested":
                reason = "additional_evidence_worker_resumed"
            elif context.state.pause_reason == "existing_contract_repair_requested":
                reason = "existing_contract_worker_resumed"
            else:
                reason = "physics_worker_repair_resumed"
            context.state = _transition(
                context,
                "software_running",
                reason,
                required_oracle_proofs_verified=False,
                code_auditor_passed=False,
                tests_passed=False,
                physics_route=None,
                physics_reason_codes=(),
                physics_auditor_action_directory=None,
                physics_auditor_result_sha256=None,
                physics_auditor_proof_sha256=None,
                physics_report_sha256=None,
                physics_routing_sha256=None,
                accepted_workspace_identity_sha256=None,
                pause_reason=None,
                summary="Persistent Worker repair and full software revalidation requested.",
            )
        elif status == "initialized":
            context.state = _transition(
                context,
                "software_running",
                "software_workflow_requested",
                summary="Worker, visible tests, and Code Auditor requested.",
            )
        else:
            raise WorkflowStateError("unsupported physics workflow state")


def _handle_software(context: _PhysicsContext) -> None:
    services = _software_services(context)
    if (
        context.state.tests_passed
        and context.state.code_auditor_passed
        and _read_journal(context.run_directory)[-1].reason == "software_gate_verified"
    ):
        context.state = _transition(
            context,
            "physics_oracles_running",
            "code_auditor_passed",
            pause_reason=None,
            summary="Code Auditor passed; trusted required physics oracles are running.",
        )
        return
    software_spec = Path(context.state.software_specification_path)
    software_runs = context.run_directory / "software-runs"
    expected_run = software_runs / (f"{context.state.substage_id}-sw-{context.state.run_token}")
    next_round = context.state.repair_round
    nested_exists = expected_run.is_dir()
    nested_result = None
    if nested_exists:
        nested_result = substage_status(expected_run)
        if nested_result.repair_round > context.state.repair_round:
            next_round = nested_result.repair_round
        elif (
            context.state.repair_prompt_path is not None
            and not context.state.repair_prompt_consumed
            and nested_result.repair_round == context.state.repair_round
            and nested_result.status in {"human_paused", "repair_limit_paused"}
        ):
            next_round += 1
    action_id = f"software-r{next_round:03d}"
    lifecycle = _action_lifecycle(context.run_directory, "software", action_id)
    if lifecycle is None:
        context.state = _journal_event(
            context,
            event_type="action_intent",
            reason="software_action_intent",
            action_id=action_id,
            action_kind="software",
            artifact_hashes={},
            updates={"software_run_directory": str(expected_run)},
        )
    try:
        if lifecycle == "completed":
            pass
        elif not nested_exists:
            nested_result = run_substage(
                software_spec,
                runs_dir=software_runs,
                services=services,
            )
        elif nested_result is not None and nested_result.status in ACTIVE_STATUSES:
            nested_result = resume_substage(expected_run, services=services)
        elif (
            nested_result is not None
            and context.state.repair_prompt_path is not None
            and not context.state.repair_prompt_consumed
            and nested_result.repair_round == context.state.repair_round
            and nested_result.status in {"human_paused", "repair_limit_paused"}
        ):
            if (
                sha256_regular_file(Path(context.state.repair_prompt_path))
                != context.state.repair_prompt_sha256
            ):
                raise WorkflowStateError("physics repair prompt changed before Worker resume")
            nested_result = continue_substage(
                expected_run,
                Path(context.state.repair_prompt_path),
                services=services,
            )
    except (WorkflowInputError, WorkflowDependencyError, WorkflowStateError):
        context.state = _transition(
            context,
            "infrastructure_stopped",
            "workflow_infrastructure_failure",
            pause_reason="software_workflow_infrastructure_failure",
            summary="The software workflow could not be completed or recovered safely.",
        )
        return
    if nested_result is None:
        raise WorkflowStateError("software workflow produced no durable result")
    legacy_result = nested_result
    software_result_path = expected_run / RESULT_FILE
    software_evidence_path = (
        context.run_directory
        / "physics"
        / "software-evidence"
        / f"round-{legacy_result.repair_round:03d}-result.json"
    )
    try:
        _write_once_bytes(software_evidence_path, software_result_path.read_bytes())
    except OSError as exc:
        raise WorkflowStateError("software result evidence is unreadable") from exc
    if lifecycle == "completed":
        if (
            context.state.repair_round != legacy_result.repair_round
            or context.state.worker_thread_id != legacy_result.worker_thread_id
            or context.state.latest_software_result_path != str(software_evidence_path)
        ):
            raise WorkflowStateError("recovered software completion disagrees with state")
    else:
        context.state = _journal_event(
            context,
            event_type="action_completion",
            reason="software_action_completed",
            action_id=action_id,
            action_kind="software",
            artifact_hashes={
                str(software_evidence_path): sha256_regular_file(software_evidence_path)
            },
            updates={
                "repair_round": legacy_result.repair_round,
                "worker_thread_id": legacy_result.worker_thread_id,
                "latest_software_result_path": str(software_evidence_path),
                "tests_passed": legacy_result.tests_passed,
                "code_auditor_passed": (
                    legacy_result.tests_passed
                    and legacy_result.scope_compliant
                    and legacy_result.contract_satisfied
                ),
                "repair_prompt_consumed": context.state.repair_prompt_path is not None,
            },
        )
    if not _frozen_inputs_match(context):
        _pause_contract_weakening(
            context,
            "The Worker attempted to change frozen physics authority.",
        )
        return
    boundary = None
    if legacy_result.status == "human_paused":
        try:
            boundary = post_audit_prompt_source_boundary(expected_run, services=services)
        except (WorkflowInputError, WorkflowStateError):
            boundary = None
    if boundary == "finish":
        if (
            context.state.oracle_evidence
            and _read_journal(context.run_directory)[-1].reason != "oracle_evidence_preserved"
        ):
            workspace_identity = collect_physics_oracle_workspace_identity(
                context.prepared.workspace
            ).canonical_sha256()
            if workspace_identity == context.state.current_workspace_identity_sha256:
                context.state = _journal_event(
                    context,
                    event_type="evidence",
                    reason="oracle_evidence_preserved",
                    action_id=None,
                    action_kind=None,
                    artifact_hashes={},
                    updates={
                        "preserved_oracle_ids": tuple(
                            sorted(item.oracle_id for item in context.state.oracle_evidence)
                        ),
                        "invalidated_oracle_ids": (),
                        "required_oracle_proofs_verified": True,
                    },
                )
            else:
                historical = (
                    *context.state.historical_oracle_evidence,
                    *context.state.oracle_evidence,
                )
                invalidated = tuple(
                    sorted(item.oracle_id for item in context.state.oracle_evidence)
                )
                context.state = _journal_event(
                    context,
                    event_type="evidence",
                    reason="stale_oracle_evidence_invalidated",
                    action_id=None,
                    action_kind=None,
                    artifact_hashes={},
                    updates={
                        "historical_oracle_evidence": historical,
                        "oracle_evidence": (),
                        "invalidated_oracle_ids": invalidated,
                        # PA-2 proofs bind the complete workspace identity. They are
                        # retained historically but cannot be accepted after mutation.
                        "preserved_oracle_ids": (),
                        "required_oracle_proofs_verified": False,
                    },
                )
        context.state = _journal_event(
            context,
            event_type="evidence",
            reason="software_gate_verified",
            action_id=None,
            action_kind=None,
            artifact_hashes={
                str(software_evidence_path): sha256_regular_file(software_evidence_path)
            },
            updates={
                "tests_passed": True,
                "code_auditor_passed": True,
                "repair_round": legacy_result.repair_round,
            },
        )
        context.state = _transition(
            context,
            "physics_oracles_running",
            "code_auditor_passed",
            pause_reason=None,
            summary="Code Auditor passed; trusted required physics oracles are running.",
        )
    elif legacy_result.status == "repair_limit_paused":
        context.state = _transition(
            context,
            "repair_limit_paused",
            "software_repair_limit_exhausted",
            pause_reason="software_repair_limit_exhausted",
            summary="The shared Worker repair limit was exhausted before physics auditing.",
        )
    else:
        context.state = _transition(
            context,
            "human_review_paused",
            "code_auditor_human_review",
            pause_reason=f"code_auditor_{legacy_result.pause_reason or legacy_result.status}",
            summary="The software gate did not produce a verified Code Auditor pass.",
        )


def _handle_oracles(context: _PhysicsContext) -> None:
    try:
        identity = collect_physics_oracle_workspace_identity(context.prepared.workspace)
    except PhysicsOracleError:
        _stop_infrastructure(context, "workspace_integrity_failure")
        return
    identity_hash = identity.canonical_sha256()
    if (
        context.state.current_workspace_identity_sha256 is not None
        and context.state.current_workspace_identity_sha256 != identity_hash
        and context.state.oracle_evidence
    ):
        context.state = _journal_event(
            context,
            event_type="evidence",
            reason="stale_oracle_evidence_invalidated",
            action_id=None,
            action_kind=None,
            artifact_hashes={},
            updates={
                "historical_oracle_evidence": (
                    *context.state.historical_oracle_evidence,
                    *context.state.oracle_evidence,
                ),
                "oracle_evidence": (),
                "invalidated_oracle_ids": tuple(
                    sorted(item.oracle_id for item in context.state.oracle_evidence)
                ),
                "preserved_oracle_ids": (),
                "required_oracle_proofs_verified": False,
            },
        )
    required = tuple(item.id for item in context.prepared.physics_contract.oracles if item.required)
    existing = {item.oracle_id: item for item in context.state.oracle_evidence}
    for oracle_id in required:
        if oracle_id in existing:
            continue
        action_id = f"oracle-r{context.state.repair_round:03d}-{oracle_id}"
        output = (
            context.run_directory / "physics" / "oracles" / f"workspace-{identity_hash}" / oracle_id
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        lifecycle = _action_lifecycle(context.run_directory, "physics_oracle", action_id)
        if lifecycle is None:
            context.state = _journal_event(
                context,
                event_type="action_intent",
                reason="physics_oracle_action_intent",
                action_id=action_id,
                action_kind="physics_oracle",
                artifact_hashes={},
                updates={"current_workspace_identity_sha256": identity_hash},
            )
        elif lifecycle == "completed":
            raise WorkflowStateError("completed oracle lifecycle lacks recovered state evidence")
        try:
            if output.exists():
                result = context.physics_services.oracle_resumer(
                    catalog_path=context.prepared.oracle_catalog_path,
                    contract_path=context.prepared.physics_contract_path,
                    workspace=context.prepared.workspace,
                    output_directory=output,
                    environ=context.software_services.environ,
                    checkpoint=_action_checkpoint(context, f"oracle_{oracle_id}"),
                )
            else:
                result = context.physics_services.oracle_runner(
                    catalog_path=context.prepared.oracle_catalog_path,
                    contract_path=context.prepared.physics_contract_path,
                    oracle_id=oracle_id,
                    task_id=context.state.substage_id,
                    workspace=context.prepared.workspace,
                    output_directory=output,
                    attempt_number=context.state.repair_round + 1,
                    environ=context.software_services.environ,
                    checkpoint=_action_checkpoint(context, f"oracle_{oracle_id}"),
                )
            verified = context.physics_services.oracle_verifier(output)
        except PhysicsOracleError:
            _stop_infrastructure(context, "recovery_indeterminate")
            return
        if result != verified:
            _stop_infrastructure(context, "recovery_indeterminate")
            return
        if (
            verified.request.task_id != context.state.substage_id
            or verified.request.oracle_id != oracle_id
            or verified.request.contract_sha256 != context.prepared.physics_contract_sha256
            or verified.initial_workspace_identity != identity
            or verified.final_workspace_identity != identity
        ):
            _stop_infrastructure(context, "workspace_integrity_failure")
            return
        record = PhysicsOracleEvidenceRecordV2(
            oracle_id=oracle_id,
            repair_round=context.state.repair_round,
            output_directory=str(output),
            result_sha256=verified.canonical_sha256(),
            completion_proof_sha256=verified.completion_proof_sha256,
            workspace_identity_sha256=identity_hash,
            status=verified.status,
        )
        result_path = output / ORACLE_RESULT_FILE
        proof_path = output / ORACLE_PROOF_FILE
        context.state = _journal_event(
            context,
            event_type="action_completion",
            reason="physics_oracle_action_completed",
            action_id=action_id,
            action_kind="physics_oracle",
            artifact_hashes={
                str(result_path): sha256_regular_file(result_path),
                str(proof_path): sha256_regular_file(proof_path),
            },
            updates={
                "oracle_evidence": (*context.state.oracle_evidence, record),
                "current_workspace_identity_sha256": identity_hash,
            },
        )
        existing[oracle_id] = record
        if verified.status == "workspace_integrity_failure":
            _stop_infrastructure(context, "workspace_integrity_failure")
            return
        if verified.status == "output_contract_failure":
            _pause_with_packet(
                context,
                "evidence_paused",
                "oracle_evidence_failure",
                "A required oracle could not produce valid bounded evidence.",
            )
            return
        if verified.status not in {"passed", "functional_failure"}:
            _stop_infrastructure(context, "workflow_infrastructure_failure")
            return
    if set(existing) != set(required):
        _pause_with_packet(
            context,
            "evidence_paused",
            "oracle_evidence_failure",
            "One or more contract-required oracle proofs are missing.",
        )
        return
    context.state = _journal_event(
        context,
        event_type="evidence",
        reason="oracle_evidence_refreshed",
        action_id=None,
        action_kind=None,
        artifact_hashes={
            str(Path(item.output_directory) / ORACLE_RESULT_FILE): sha256_regular_file(
                Path(item.output_directory) / ORACLE_RESULT_FILE
            )
            for item in context.state.oracle_evidence
        },
        updates={
            "required_oracle_proofs_verified": True,
            "current_workspace_identity_sha256": identity_hash,
        },
    )
    context.state = _transition(
        context,
        "physics_auditor_running",
        "required_oracle_proofs_verified",
        summary=(
            "Required current-workspace oracle proofs verified; fresh Physics Auditor requested."
        ),
    )


def _handle_physics_auditor(context: _PhysicsContext) -> None:
    round_id = context.state.repair_round
    action_id = f"physics-auditor-r{round_id:03d}"
    evidence_parents = {
        Path(item.output_directory).parent for item in context.state.oracle_evidence
    }
    if len(evidence_parents) != 1:
        _stop_infrastructure(context, "workflow_infrastructure_failure")
        return
    evidence_root = next(iter(evidence_parents))
    output = context.run_directory / "physics" / "audits" / f"round-{round_id:03d}"
    recovered_completion = context.state.physics_auditor_result_sha256 is not None
    if not recovered_completion:
        lifecycle = _action_lifecycle(context.run_directory, "physics_auditor", action_id)
        if lifecycle is None:
            context.state = _journal_event(
                context,
                event_type="action_intent",
                reason="physics_auditor_action_intent",
                action_id=action_id,
                action_kind="physics_auditor",
                artifact_hashes={},
                updates={"physics_auditor_action_directory": str(output)},
            )
        elif lifecycle == "completed":
            raise WorkflowStateError(
                "completed Physics Auditor lifecycle lacks recovered state evidence"
            )
    common: dict[str, object] = {
        "contract_path": context.prepared.physics_contract_path,
        "execution_config_path": context.prepared.auditor_config_path,
        "task_id": context.state.substage_id,
        "workspace": context.prepared.workspace,
        "oracle_evidence_root": evidence_root,
        "output_directory": output,
        "action_id": action_id,
        "attempt_number": round_id + 1,
    }
    model_common = {
        **common,
        "usage_campaign_id": (
            context.software_services.usage_campaign_id
            or context.state.substage_id
        ),
        "usage_task_id": (
            context.software_services.usage_task_id
            or context.state.substage_id
        ),
        "usage_ledger_root": (
            context.software_services.usage_ledger_root
            or context.run_directory
        ),
        "usage_ledger_path": (
            context.software_services.usage_ledger_path
            or (context.run_directory / "task-token-ledger.json")
        ),
    }
    try:
        if recovered_completion:
            result = context.physics_services.auditor_verifier(**common)
        elif output.exists():
            result = context.physics_services.auditor_resumer(
                **model_common,
                environ=context.software_services.environ,
                codex_invoker=context.physics_services.physics_auditor_codex_invoker,
                checkpoint=_action_checkpoint(context, "physics_auditor"),
            )
        else:
            result = context.physics_services.auditor_runner(
                **model_common,
                environ=context.software_services.environ,
                codex_invoker=context.physics_services.physics_auditor_codex_invoker,
                checkpoint=_action_checkpoint(context, "physics_auditor"),
            )
        verified = context.physics_services.auditor_verifier(**common)
    except PhysicsAuditorError:
        _stop_infrastructure(context, "recovery_indeterminate")
        return
    if result != verified:
        _stop_infrastructure(context, "recovery_indeterminate")
        return
    result_path = output / AUDITOR_RESULT_FILE
    proof_path = output / AUDITOR_PROOF_FILE
    report_path = output / REPORT_FILE
    routing_path = output / ROUTING_FILE
    provider_threads: tuple[str, ...] = ()
    provider_path = output / PROVIDER_OBSERVATION_FILE
    if provider_path.is_file():
        provider = _load_model(provider_path, PhysicsAuditorProviderObservationV1)
        provider_threads = provider.provider_thread_started_ids
        previous_threads = set(context.state.prior_physics_auditor_thread_ids)
        invalid_threads = (
            not set(provider_threads).issubset(previous_threads)
            if recovered_completion
            else bool(set(provider_threads) & previous_threads)
        )
        if invalid_threads:
            _stop_infrastructure(context, "recovery_indeterminate")
            return
    updates: dict[str, object] = {
        "physics_auditor_action_directory": str(output),
        "physics_auditor_result_sha256": verified.canonical_sha256(),
        "physics_auditor_proof_sha256": verified.action_proof_sha256,
        "prior_physics_auditor_thread_ids": context.state.prior_physics_auditor_thread_ids
        if recovered_completion
        else (*context.state.prior_physics_auditor_thread_ids, *provider_threads),
    }
    artifact_hashes = {
        str(result_path): sha256_regular_file(result_path),
        str(proof_path): sha256_regular_file(proof_path),
    }
    if verified.report_validated:
        updates["physics_report_sha256"] = cast(str, verified.parsed_report_sha256)
        artifact_hashes[str(report_path)] = sha256_regular_file(report_path)
    if verified.routing_decision is not None:
        updates.update(
            {
                "physics_routing_sha256": verified.routing_decision.canonical_sha256(),
                "physics_route": verified.routing_decision.outcome,
                "physics_reason_codes": tuple(
                    sorted({item.rule for item in verified.routing_decision.rules})
                ),
            }
        )
        artifact_hashes[str(routing_path)] = sha256_regular_file(routing_path)
    if recovered_completion:
        if (
            context.state.physics_auditor_action_directory != str(output)
            or context.state.physics_auditor_result_sha256 != verified.canonical_sha256()
            or context.state.physics_auditor_proof_sha256 != verified.action_proof_sha256
            or context.state.physics_report_sha256
            != (verified.parsed_report_sha256 if verified.report_validated else None)
            or context.state.physics_routing_sha256
            != (
                verified.routing_decision.canonical_sha256()
                if verified.routing_decision is not None
                else None
            )
        ):
            _stop_infrastructure(context, "recovery_indeterminate")
            return
    else:
        context.state = _journal_event(
            context,
            event_type="action_completion",
            reason="physics_auditor_action_completed",
            action_id=action_id,
            action_kind="physics_auditor",
            artifact_hashes=artifact_hashes,
            updates=updates,
        )
    if verified.status == "evidence_integrity_failure":
        _pause_with_packet(
            context,
            "evidence_paused",
            "physics_auditor_evidence_failure",
            "Physics Auditor evidence did not verify against the current workspace.",
        )
        return
    if verified.status != "routing_completed" or verified.routing_decision is None:
        _stop_infrastructure(context, "workflow_infrastructure_failure")
        return
    if (
        not context.state.tests_passed
        or not context.state.code_auditor_passed
        or not context.state.required_oracle_proofs_verified
    ):
        _stop_infrastructure(context, "workflow_infrastructure_failure")
        return
    if _read_journal(context.run_directory)[-1].reason != "physics_route_verified":
        context.state = _journal_event(
            context,
            event_type="evidence",
            reason="physics_route_verified",
            action_id=None,
            action_kind=None,
            artifact_hashes={str(routing_path): sha256_regular_file(routing_path)},
            updates={
                "physics_route": verified.routing_decision.outcome,
                "physics_routing_sha256": verified.routing_decision.canonical_sha256(),
            },
        )
    route = verified.routing_decision.outcome
    if route == "request_repair":
        if context.state.repair_round >= context.state.max_repair_rounds:
            _pause_with_packet(
                context,
                "repair_limit_paused",
                "physics_repair_limit_exhausted",
                "The shared physics repair limit is exhausted.",
            )
            return
        prompt_path, prompt_hash = _write_repair_prompt(context)
        context.state = _transition(
            context,
            "physics_repair_pending",
            "physics_repair_requested",
            repair_prompt_path=str(prompt_path),
            repair_prompt_sha256=prompt_hash,
            repair_prompt_consumed=False,
            summary="Validated physics findings are queued for the persistent Worker.",
        )
        return
    if route == "require_human_review":
        _pause_with_packet(
            context,
            "human_review_paused",
            "physics_human_review_required",
            "The authoritative physics route requires human scientific review.",
        )
        return
    if route == "block_insufficient_evidence":
        _pause_with_packet(
            context,
            "evidence_paused",
            "physics_evidence_insufficient",
            "Required scientific evidence is insufficient; Worker repair was not requested.",
        )
        return
    if route == "infrastructure_failure":
        _stop_infrastructure(context, "workflow_infrastructure_failure")
        return
    _complete_if_bound(context, verified)


def _complete_if_bound(context: _PhysicsContext, result: PhysicsAuditorActionResultV1) -> None:
    try:
        identity = collect_physics_oracle_workspace_identity(context.prepared.workspace)
    except PhysicsOracleError:
        _stop_infrastructure(context, "workspace_integrity_failure")
        return
    identity_hash = identity.canonical_sha256()
    if (
        result.routing_decision is None
        or result.routing_decision.outcome != "pass"
        or result.final_workspace_identity != identity
        or context.state.current_workspace_identity_sha256 != identity_hash
        or any(
            item.workspace_identity_sha256 != identity_hash
            for item in context.state.oracle_evidence
        )
        or not context.state.tests_passed
        or not context.state.code_auditor_passed
        or not context.state.required_oracle_proofs_verified
        or context.state.status in PHYSICS_PAUSED_STATUSES_V2
    ):
        _stop_infrastructure(context, "workspace_integrity_failure")
        return
    final_status = "checkpoint_paused" if context.state.checkpoint_after else "completed"
    reason = (
        "physics_completion_checkpoint"
        if context.state.checkpoint_after
        else "physics_completion_gate_passed"
    )
    context.state = _transition(
        context,
        final_status,
        reason,
        accepted_workspace_identity_sha256=identity_hash,
        pause_reason=(reason if context.state.checkpoint_after else None),
        summary=(
            "Physics completion gate verified all software, oracle, audit, and identity evidence."
        ),
    )


def _write_repair_prompt(context: _PhysicsContext) -> tuple[Path, str]:
    if context.state.physics_auditor_action_directory is None:
        raise WorkflowInputError("validated Physics Auditor evidence is unavailable")
    report = _load_model(
        Path(context.state.physics_auditor_action_directory) / REPORT_FILE,
        PhysicsAuditReportV1,
    )
    findings = [
        {
            "id": item.id,
            "summary": item.statement,
            "evidence_references": [
                reference.model_dump(mode="json") for reference in item.evidence
            ],
            "required_repair": item.required_action,
        }
        for item in report.findings
        if item.status == "open"
    ]
    if not findings:
        raise WorkflowInputError("physics continuation has no validated finding to communicate")
    # The continuation content set is closed: no route, round, decision metadata,
    # model reasoning, or engine prose may enter this Worker-facing artifact.
    content = (
        json.dumps(
            {"findings": findings},
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        + b"\n"
    )
    path = (
        context.run_directory
        / "physics"
        / "repair-prompts"
        / f"round-{context.state.repair_round + 1:03d}.txt"
    )
    _write_once_bytes(path, content)
    return path, hashlib.sha256(content).hexdigest()


def _pause_with_packet(
    context: _PhysicsContext,
    status: str,
    reason: str,
    summary: str,
) -> None:
    packet_path, packet_hash = _write_review_packet(context, (reason,))
    context.state = _transition(
        context,
        status,
        reason,
        human_review_packet_path=str(packet_path),
        human_review_packet_sha256=packet_hash,
        pause_reason=reason,
        summary=summary,
    )


def _pause_contract_weakening(context: _PhysicsContext, summary: str) -> None:
    if context.state.latest_software_result_path is None:
        _stop_infrastructure(context, "workspace_integrity_failure")
        return
    try:
        _pause_with_packet(
            context,
            "human_review_paused",
            "contract_weakening_attempt",
            summary,
        )
    except (PhysicsOracleError, WorkflowStateError):
        _stop_infrastructure(context, "workspace_integrity_failure")


def _write_review_packet(
    context: _PhysicsContext, extra_reasons: tuple[str, ...]
) -> tuple[Path, str]:
    identity = collect_physics_oracle_workspace_identity(context.prepared.workspace)
    software_result_path = context.state.latest_software_result_path
    if software_result_path is None:
        raise WorkflowStateError("scientific review lacks software evidence")
    report: PhysicsAuditReportV1 | None = None
    if context.state.physics_auditor_action_directory is not None:
        candidate = Path(context.state.physics_auditor_action_directory) / REPORT_FILE
        if candidate.is_file():
            report = _load_model(candidate, PhysicsAuditReportV1)
    packet = PhysicsHumanReviewPacketV1(
        run_token=context.state.run_token,
        substage_id=context.state.substage_id,
        repair_round=context.state.repair_round,
        reason_codes=tuple(sorted({*context.state.physics_reason_codes, *extra_reasons})),
        specification_sha256=context.state.specification_sha256,
        software_contract_sha256=context.prepared.software_prepared.contract.sha256,
        physics_contract_sha256=context.state.physics_contract_sha256,
        oracle_catalog_sha256=context.state.oracle_catalog_sha256,
        auditor_config_sha256=context.state.auditor_config_sha256,
        software_result_sha256=sha256_regular_file(Path(software_result_path)),
        workspace_identity_sha256=identity.canonical_sha256(),
        oracle_result_sha256={
            item.oracle_id: item.result_sha256 for item in context.state.oracle_evidence
        },
        oracle_completion_proof_sha256={
            item.oracle_id: item.completion_proof_sha256 for item in context.state.oracle_evidence
        },
        physics_auditor_result_sha256=context.state.physics_auditor_result_sha256,
        physics_auditor_proof_sha256=context.state.physics_auditor_proof_sha256,
        physics_report_sha256=context.state.physics_report_sha256,
        physics_routing_sha256=context.state.physics_routing_sha256,
        physics_route=context.state.physics_route,
        finding_ids=() if report is None else tuple(item.id for item in report.findings),
        unresolved_question_ids=(
            () if report is None else tuple(item.id for item in report.unresolved_questions)
        ),
    )
    path = (
        context.run_directory
        / "human-review"
        / f"round-{context.state.repair_round:03d}-{context.state.journal_sequence + 1:04d}.json"
    )
    _write_once(path, packet.model_dump(mode="json"))
    return path, packet.canonical_sha256()


def _stop_infrastructure(context: _PhysicsContext, reason: str) -> None:
    context.state = _transition(
        context,
        "infrastructure_stopped",
        reason,
        pause_reason=reason,
        summary=(
            "Physics workflow infrastructure or evidence integrity failed; candidate "
            "code was not blamed."
        ),
    )


def _software_services(context: _PhysicsContext) -> WorkflowServices:
    token = f"sw-{context.state.run_token}"
    return replace(
        context.software_services,
        token_factory=lambda: token,
        prompt_source=_PhysicsSoftwarePromptSource(context.software_services.prompt_source),
    )


def _action_checkpoint(context: _PhysicsContext, prefix: str) -> Checkpoint:
    def checkpoint(name: str) -> None:
        context.physics_services.checkpoint(f"{prefix}:{name}")

    return checkpoint


def _initialize_run(run_directory: Path, prepared: PreparedPhysicsSubstageV2) -> None:
    for name in (
        "control",
        "physics",
        "software-runs",
        "human-review",
        "human-decisions",
    ):
        (run_directory / name).mkdir()
    (run_directory / "physics" / "oracles").mkdir()
    (run_directory / "physics" / "audits").mkdir()
    (run_directory / "physics" / "repair-prompts").mkdir()
    software_value = prepared.specification.software_specification().model_dump(mode="json")
    software_value.update(
        {
            "workspace": str(prepared.workspace),
            "contract_path": str(prepared.software_prepared.contract.path),
            "worker_initial_prompt_path": str(
                prepared.software_prepared.worker_initial_prompt.path
            ),
            "worker_repair_prompt_path": str(prepared.software_prepared.worker_repair_prompt.path),
            "auditor_prompt_path": str(prepared.software_prepared.auditor_prompt.path),
            "acceptance_tests": [
                {
                    **item.specification.model_dump(mode="json"),
                    "cwd": str(item.cwd),
                }
                for item in prepared.software_prepared.acceptance_tests
            ],
        }
    )
    software_bytes = yaml.safe_dump(software_value, sort_keys=False).encode("utf-8")
    _write_once_bytes(run_directory / "control" / SOFTWARE_SPEC_FILE, software_bytes)
    normalized = {
        "schema_version": 2,
        "specification_path": str(prepared.specification_path),
        "specification_sha256": prepared.specification_sha256,
        "workspace": str(prepared.workspace),
        "repository_root": str(prepared.repository_root),
        "physics_contract_path": str(prepared.physics_contract_path),
        "physics_contract_sha256": prepared.physics_contract_sha256,
        "oracle_catalog_path": str(prepared.oracle_catalog_path),
        "oracle_catalog_sha256": prepared.oracle_catalog_sha256,
        "auditor_config_path": str(prepared.auditor_config_path),
        "auditor_config_sha256": prepared.auditor_config_sha256,
        "software_specification_sha256": hashlib.sha256(software_bytes).hexdigest(),
    }
    _write_once(run_directory / "control" / "authority.json", normalized)
    _write_once_bytes(run_directory / JOURNAL_FILE, b"")


def _frozen_hashes(run_directory: Path, prepared: PreparedPhysicsSubstageV2) -> dict[str, str]:
    paths = (
        prepared.specification_path,
        prepared.physics_contract_path,
        prepared.oracle_catalog_path,
        prepared.auditor_config_path,
        prepared.software_prepared.contract.path,
        prepared.software_prepared.worker_initial_prompt.path,
        prepared.software_prepared.worker_repair_prompt.path,
        prepared.software_prepared.auditor_prompt.path,
        run_directory / "control" / SOFTWARE_SPEC_FILE,
        run_directory / "control" / "authority.json",
    )
    return {str(item): sha256_regular_file(item) for item in paths}


def _frozen_inputs_match(context: _PhysicsContext) -> bool:
    try:
        prepared = load_physics_substage_specification(
            Path(context.state.specification_path), require_clean=False
        )
        _verify_frozen_state(prepared, context.state)
        authority = _load_json(context.run_directory / "control" / "authority.json")
        if authority.get("software_specification_sha256") != sha256_regular_file(
            Path(context.state.software_specification_path)
        ):
            return False
        current_root, current_head, current_branch, _ = _git_baseline(prepared.workspace)
        return (
            str(current_root) == context.state.repository_root
            and current_head == context.state.baseline_commit
            and current_branch == context.state.baseline_branch
        )
    except (
        PhysicsAuditorError,
        PhysicsOracleError,
        PhysicsValidationError,
        WorkflowInputError,
        WorkflowStateError,
        OSError,
    ):
        return False


def _verify_frozen_state(
    prepared: PreparedPhysicsSubstageV2, state: PhysicsWorkflowStateV2
) -> None:
    if (
        prepared.specification_sha256 != state.specification_sha256
        or prepared.physics_contract_sha256 != state.physics_contract_sha256
        or prepared.oracle_catalog_sha256 != state.oracle_catalog_sha256
        or prepared.auditor_config_sha256 != state.auditor_config_sha256
        or str(prepared.workspace) != state.workspace
        or str(prepared.repository_root) != state.repository_root
        or prepared.software_prepared.baseline_commit != state.baseline_commit
        or prepared.software_prepared.baseline_branch != state.baseline_branch
    ):
        raise WorkflowInputError("frozen physics workflow authority changed")


def _transition(
    context: _PhysicsContext,
    new_status: str,
    reason: str,
    **updates: object,
) -> PhysicsWorkflowStateV2:
    return _journal(
        context.run_directory,
        context.state,
        context.physics_services,
        event_type="transition",
        previous_state=context.state.status,
        new_state=cast(Any, new_status),
        action_id=None,
        action_kind=None,
        reason=reason,
        artifact_hashes=_hash_updates(updates),
        updates={"status": new_status, **updates},
    )


def _journal_event(
    context: _PhysicsContext,
    *,
    event_type: str,
    reason: str,
    action_id: str | None,
    action_kind: str | None,
    artifact_hashes: Mapping[str, str],
    updates: Mapping[str, object],
) -> PhysicsWorkflowStateV2:
    return _journal(
        context.run_directory,
        context.state,
        context.physics_services,
        event_type=cast(Any, event_type),
        previous_state=context.state.status,
        new_state=context.state.status,
        action_id=action_id,
        action_kind=cast(Any, action_kind),
        reason=reason,
        artifact_hashes=artifact_hashes,
        updates=updates,
    )


def _journal(
    run_directory: Path,
    state: PhysicsWorkflowStateV2,
    services: PhysicsWorkflowServices,
    *,
    event_type: str,
    previous_state: str | None,
    new_state: str,
    action_id: str | None,
    action_kind: str | None,
    reason: str,
    artifact_hashes: Mapping[str, str],
    updates: Mapping[str, object],
) -> PhysicsWorkflowStateV2:
    if not set(updates).issubset(_MUTABLE_STATE_FIELDS):
        raise WorkflowStateError("physics journal updates an immutable state field")
    form = (event_type, previous_state, new_state, action_kind, reason)
    if form not in PHYSICS_JOURNAL_SEMANTIC_FORMS_V2:
        raise WorkflowStateError(f"unsupported physics journal semantic form: {form!r}")
    for path, digest in artifact_hashes.items():
        if not Path(path).is_absolute() or sha256_regular_file(Path(path)) != digest:
            raise WorkflowStateError("physics journal artifact mapping is invalid")
    body: dict[str, object] = {
        "schema_version": 2,
        "sequence": state.journal_sequence + 1,
        "event_type": event_type,
        "previous_state": previous_state,
        "new_state": new_state,
        "action_id": action_id,
        "action_kind": action_kind,
        "timestamp": _utc_string(services.utc_now()),
        "reason": reason,
        "artifact_hashes": dict(artifact_hashes),
        "state_updates": _json_value(dict(updates)),
        "previous_hash": state.journal_hash,
    }

    def validate(value: Mapping[str, object]) -> None:
        PhysicsWorkflowJournalEntryV2.model_validate(value)

    raw_entry, entry_hash = append_hashed_journal_entry(
        run_directory / JOURNAL_FILE,
        body,
        validate=validate,
        error_factory=WorkflowStateError,
        error_message="physics workflow journal could not be appended",
    )
    values = state.model_dump(mode="json")
    raw_updates = raw_entry["state_updates"]
    if not isinstance(raw_updates, dict):
        raise WorkflowStateError("physics journal state updates are invalid")
    values.update(raw_updates)
    values.update(
        {
            "updated_at": raw_entry["timestamp"],
            "journal_sequence": raw_entry["sequence"],
            "journal_hash": entry_hash,
        }
    )
    try:
        next_state = PhysicsWorkflowStateV2.model_validate(values)
    except ValidationError as exc:
        raise WorkflowStateError("physics journal produced an invalid state") from exc
    _persist_snapshots(run_directory, next_state, services)
    services.checkpoint(f"journal:{reason}")
    return next_state


def _persist_snapshots(
    run_directory: Path,
    state: PhysicsWorkflowStateV2,
    services: PhysicsWorkflowServices,
) -> None:
    commit_result_then_state(
        result_path=run_directory / RESULT_FILE,
        result_value=state.to_result().model_dump(mode="json"),
        state_path=run_directory / STATE_FILE,
        state_value=state.model_dump(mode="json"),
        checkpoint=lambda name: services.checkpoint(f"snapshot:{name}"),
        error_factory=WorkflowStateError,
        error_message="physics workflow snapshots could not be persisted",
    )


def _read_journal(run_directory: Path) -> list[PhysicsWorkflowJournalEntryV2]:
    values = read_hashed_journal(
        run_directory / JOURNAL_FILE,
        error_factory=WorkflowStateError,
        malformed_message="physics workflow journal is malformed",
    )
    entries: list[PhysicsWorkflowJournalEntryV2] = []
    action_lifecycles: dict[tuple[str, str], str] = {}
    current = None
    for value in values:
        try:
            entry = PhysicsWorkflowJournalEntryV2.model_validate(value)
        except ValidationError as exc:
            raise WorkflowStateError("physics workflow journal entry is invalid") from exc
        form = (
            entry.event_type,
            entry.previous_state,
            entry.new_state,
            entry.action_kind,
            entry.reason,
        )
        if form not in PHYSICS_JOURNAL_SEMANTIC_FORMS_V2:
            raise WorkflowStateError("physics journal semantic form is unsupported")
        if entry.previous_state != current:
            raise WorkflowStateError("physics journal state history is discontinuous")
        if not set(entry.state_updates).issubset(_MUTABLE_STATE_FIELDS):
            raise WorkflowStateError("physics journal mutates frozen state")
        for path, digest in entry.artifact_hashes.items():
            if sha256_regular_file(Path(path)) != digest:
                raise WorkflowStateError("physics journal evidence was replaced")
        if entry.action_id is not None and entry.action_kind is not None:
            key = (entry.action_kind, entry.action_id)
            lifecycle = action_lifecycles.get(key)
            if entry.event_type == "action_intent":
                if lifecycle is not None:
                    raise WorkflowStateError("physics journal action intent is duplicated")
                action_lifecycles[key] = "intent"
            elif lifecycle != "intent":
                raise WorkflowStateError("physics journal action completion lacks one intent")
            else:
                action_lifecycles[key] = "completed"
        current = entry.new_state
        entries.append(entry)
    return entries


def _action_lifecycle(run_directory: Path, action_kind: str, action_id: str) -> str | None:
    lifecycle = None
    for entry in _read_journal(run_directory):
        if entry.action_kind == action_kind and entry.action_id == action_id:
            lifecycle = "intent" if entry.event_type == "action_intent" else "completed"
    return lifecycle


def _load_reconciled_state(
    run_directory: Path,
    services: PhysicsWorkflowServices,
    *,
    persist: bool,
) -> PhysicsWorkflowStateV2:
    state = _load_model(run_directory / STATE_FILE, PhysicsWorkflowStateV2)
    entries = _read_journal(run_directory)
    reconciled = reconcile_model_snapshot(
        state,
        [item.model_dump(mode="json") for item in entries],
        model=PhysicsWorkflowStateV2,
        error_factory=WorkflowStateError,
        error_message="physics workflow snapshot cannot be reconciled",
    )
    if persist and reconciled != state:
        _persist_snapshots(run_directory, reconciled, services)
    return reconciled


def _load_review_decision(path: Path) -> PhysicsReviewDecisionV1:
    raw = _read_utf8_file(path, "physics review decision", limit=MAX_SPECIFICATION_BYTES)
    try:
        value = yaml.load(raw.decode("utf-8"), Loader=_UniqueKeySafeLoader)
        return PhysicsReviewDecisionV1.model_validate(value)
    except (UnicodeDecodeError, yaml.YAMLError, ValidationError, ValueError) as exc:
        raise WorkflowInputError("physics review decision is malformed or invalid") from exc


def _load_review_packet(state: PhysicsWorkflowStateV2) -> PhysicsHumanReviewPacketV1:
    if state.human_review_packet_path is None or state.human_review_packet_sha256 is None:
        raise WorkflowInputError("physics pause has no durable review packet")
    packet = _load_model(Path(state.human_review_packet_path), PhysicsHumanReviewPacketV1)
    if packet.canonical_sha256() != state.human_review_packet_sha256:
        raise WorkflowStateError("physics review packet was replaced")
    return packet


def _load_model(path: Path, model: type[ModelT]) -> ModelT:
    try:
        value = json.loads(path.read_text(encoding="ascii"))
        return model.model_validate(value)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValidationError) as exc:
        raise WorkflowStateError(f"durable physics model is invalid: {path.name}") from exc


def _load_json(path: Path) -> dict[str, object]:
    value = _load_model(path, _StrictMapping)
    return value.root


class _StrictMapping(RootModel[dict[str, object]]):
    pass


def _write_once(path: Path, value: object) -> None:
    data = (
        json.dumps(
            _json_value(value), ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode("ascii")
        + b"\n"
    )
    _write_once_bytes(path, data)


def _write_once_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            if path.read_bytes() != data:
                raise WorkflowStateError("create-once physics artifact changed")
        except OSError as exc:
            raise WorkflowStateError("create-once physics artifact is unreadable") from exc
        return
    atomic_write_bytes(
        path,
        data,
        error_factory=WorkflowStateError,
        error_message="physics workflow artifact could not be written",
    )


def _hash_updates(updates: Mapping[str, object]) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for key, value in updates.items():
        if not key.endswith("_path") or value is None:
            continue
        path = Path(cast(str, value))
        if path.is_file():
            hashes[str(path)] = sha256_regular_file(path)
    return hashes


def _json_value(value: object) -> object:
    if hasattr(value, "model_dump"):
        return cast(Any, value).model_dump(mode="json")
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value


def _resolve_run_directory(path: Path) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise WorkflowInputError("physics workflow run directory is unavailable") from exc
    if not resolved.is_dir():
        raise WorkflowInputError("physics workflow run path is not a directory")
    return resolved


def _utc_string(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
