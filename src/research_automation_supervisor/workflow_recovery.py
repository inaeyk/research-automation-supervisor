"""PA-5A model-free workflow discovery, planning, and safe recovery."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import socket
import stat
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import ValidationError

from research_automation_supervisor.codex_adapter import build_subprocess_environment
from research_automation_supervisor.durable_state import (
    ZERO_HASH,
    atomic_write_json,
    canonical_json,
    commit_result_then_state,
    reconcile_model_snapshot,
    render_json_bytes,
)
from research_automation_supervisor.errors import (
    PhysicsOracleError,
    SupervisorError,
    WorkflowDependencyError,
    WorkflowInputError,
    WorkflowStateError,
)
from research_automation_supervisor.git_evidence import (
    GitBaseline,
    GitEvidence,
    collect_git_evidence,
    record_git_baseline,
)
from research_automation_supervisor.physics_auditor_execution import (
    _load_records as _load_physics_auditor_records,
)
from research_automation_supervisor.physics_auditor_execution import (
    verify_physics_auditor_action,
)
from research_automation_supervisor.physics_oracle_execution import (
    _load_records as _load_physics_oracle_records,
)
from research_automation_supervisor.physics_oracle_execution import (
    verify_physics_oracle_completion,
)
from research_automation_supervisor.physics_oracle_workspace import (
    collect_physics_oracle_workspace_identity,
)
from research_automation_supervisor.physics_workflow import (
    DEFAULT_PHYSICS_WORKFLOW_SERVICES,
    PhysicsWorkflowServices,
    _verify_frozen_state,
    load_physics_substage_specification,
    physics_substage_status,
    review_physics_substage,
)
from research_automation_supervisor.physics_workflow import (
    JOURNAL_FILE as PHYSICS_JOURNAL_FILE,
)
from research_automation_supervisor.physics_workflow import (
    RESULT_FILE as PHYSICS_RESULT_FILE,
)
from research_automation_supervisor.physics_workflow import (
    _load_model as _load_physics_model,
)
from research_automation_supervisor.physics_workflow import (
    _load_reconciled_state as _load_reconciled_physics_state,
)
from research_automation_supervisor.physics_workflow import (
    _read_journal as _read_physics_journal,
)
from research_automation_supervisor.physics_workflow_models import (
    PHYSICS_PAUSED_STATUSES_V2,
    PHYSICS_TERMINAL_STATUSES_V2,
    PhysicsReviewDecisionV1,
    PhysicsWorkflowJournalEntryV2,
    PhysicsWorkflowResultV2,
    PhysicsWorkflowStateV2,
    PreparedPhysicsSubstageV2,
)
from research_automation_supervisor.workflow_engine import (
    DEFAULT_WORKFLOW_SERVICES,
    WorkflowServices,
    _load_result,
    _load_state,
    _raw_frozen_inputs_match,
    _raw_repository_matches,
    _read_valid_journal,
    _reconcile_state_with_journal,
    _validate_normalized_action_intents,
    _WorkflowLock,
    resume_substage,
)
from research_automation_supervisor.workflow_engine import (
    JOURNAL_FILE as WORKFLOW_JOURNAL_FILE,
)
from research_automation_supervisor.workflow_engine import (
    LOCK_FILE as WORKFLOW_LOCK_FILE,
)
from research_automation_supervisor.workflow_integrity import (
    sha256_regular_file,
    verify_codex_artifacts,
    verify_test_artifacts,
)
from research_automation_supervisor.workflow_models import (
    PAUSED_STATUSES,
    TERMINAL_STATUSES,
    WorkflowResult,
    WorkflowState,
    load_substage_specification,
)
from research_automation_supervisor.workflow_recovery_models import (
    ObservedWorkflowStatus,
    ProcessReconciliationV1,
    ProofReconciliationV1,
    RecoveryAttemptReceiptV1,
    RecoveryOutcomeStatusV1,
    RecoveryOutcomeV1,
    RecoveryPlanV1,
    RecoveryProcessObservationV1,
    RunIndexEntryV1,
    RunIndexIssueV1,
    RunIndexV1,
)

RUN_INDEX_FILE = ".workflow-run-index-v1.json"
RECEIPT_ROOT = ".workflow-recovery-v1"
_TERMINAL_V1 = frozenset(TERMINAL_STATUSES)
_TERMINAL_V2 = frozenset(PHYSICS_TERMINAL_STATUSES_V2)
_PAUSED_V1 = frozenset(PAUSED_STATUSES)
_PAUSED_V2 = frozenset(PHYSICS_PAUSED_STATUSES_V2)
_PROCESS_PRIORITY: dict[ProcessReconciliationV1, int] = {
    "not_applicable": 0,
    "no_process": 1,
    "exited": 2,
    "active_matching": 7,
    "stale_identity": 6,
    "reused_identity": 8,
    "ambiguous_identity": 9,
    "foreign_host": 10,
}


class RecoverySelectionError(WorkflowInputError):
    """Discovery or selection failed with one stable operator-facing reason."""

    def __init__(self, reason_code: str, next_step: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.next_step = next_step


@dataclass(frozen=True)
class RecoveryExecutionV1:
    """The public plan/outcome pair and immutable receipt locators."""

    plan: RecoveryPlanV1
    outcome: RecoveryOutcomeV1
    plan_receipt_path: Path
    outcome_receipt_path: Path

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "plan": self.plan.model_dump(mode="json"),
            "outcome": self.outcome.model_dump(mode="json"),
            "plan_receipt_path": str(self.plan_receipt_path),
            "outcome_receipt_path": str(self.outcome_receipt_path),
            "outcome_sha256": self.outcome.canonical_sha256(),
        }


@dataclass(frozen=True)
class RecoveryServices:
    workflow_services: WorkflowServices = DEFAULT_WORKFLOW_SERVICES
    physics_services: PhysicsWorkflowServices = DEFAULT_PHYSICS_WORKFLOW_SERVICES
    utc_now: Callable[[], datetime] = lambda: datetime.now(UTC)
    attempt_token: Callable[[], str] = lambda: secrets.token_hex(12)


DEFAULT_RECOVERY_SERVICES = RecoveryServices()


def discover_workflow_runs(
    runs_directory: Path,
    *,
    persist_cache: bool = True,
    utc_now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> RunIndexV1:
    """Rebuild the replaceable index from direct authoritative run directories."""
    root = _resolve_runs_directory(runs_directory)
    entries: list[RunIndexEntryV1] = []
    issues: list[RunIndexIssueV1] = []
    try:
        candidates = sorted(
            (item for item in root.iterdir() if _is_direct_run_directory(item)),
            key=lambda item: item.name,
        )
    except OSError as exc:
        raise WorkflowInputError("workflow runs directory could not be enumerated") from exc
    for candidate in candidates:
        try:
            entries.append(_discover_run(candidate))
        except (OSError, ValidationError, WorkflowInputError, WorkflowStateError, ValueError):
            issues.append(
                RunIndexIssueV1(
                    run_directory=str(candidate),
                    reason_code="run_record_integrity_failed",
                    next_step=(
                        "Inspect this run explicitly and restore its exact durable records; "
                        "do not relaunch any action."
                    ),
                )
            )
    entries.sort(key=lambda item: item.run_directory)
    issues.sort(key=lambda item: item.run_directory)
    source = {
        "runs_directory": str(root),
        "entries": [item.model_dump(mode="json") for item in entries],
        "issues": [item.model_dump(mode="json") for item in issues],
    }
    index = RunIndexV1(
        runs_directory=str(root),
        generated_at=_utc_string(utc_now()),
        entries=tuple(entries),
        issues=tuple(issues),
        source_sha256=hashlib.sha256(canonical_json(source)).hexdigest(),
    )
    if persist_cache:
        _persist_run_index(root, index)
    return index


def load_run_index(path: Path) -> RunIndexV1:
    """Strictly load a cache for diagnostics; selection never trusts this function."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"), parse_constant=_reject_constant)
        return RunIndexV1.model_validate(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValidationError, ValueError) as exc:
        raise WorkflowStateError("workflow run index is invalid") from exc


def latest_incomplete_run(index: RunIndexV1) -> RunIndexEntryV1:
    """Select one unique latest incomplete run or fail with an actionable reason."""
    if index.issues:
        raise RecoverySelectionError(
            "run_discovery_integrity_failed",
            "Inspect the reported corrupt run directories or select a verified run explicitly.",
        )
    incomplete = tuple(item for item in index.entries if item.completion == "incomplete")
    if not incomplete:
        raise RecoverySelectionError(
            "no_incomplete_run",
            "Start a new workflow or pass an explicit run directory to inspect a terminal run.",
        )
    ordered = sorted(incomplete, key=lambda item: (_parse_utc(item.updated_at), item.run_directory))
    latest_time = _parse_utc(ordered[-1].updated_at)
    tied = tuple(item for item in ordered if _parse_utc(item.updated_at) == latest_time)
    if len(tied) != 1:
        raise RecoverySelectionError(
            "multiple_latest_runs",
            "Pass one explicit run directory; tied journal timestamps are not guessed.",
        )
    return tied[0]


def build_recovery_plan(run_directory: Path) -> RecoveryPlanV1:
    """Read and reconcile one exact run without writes or process launches."""
    resolved = _resolve_run_directory(run_directory)
    version = _run_schema_version(resolved)
    try:
        if version == 2:
            return _build_physics_plan(resolved)
        return _build_workflow_plan(resolved)
    except RecoverySelectionError:
        raise
    except (OSError, ValidationError, WorkflowInputError, WorkflowStateError, ValueError) as exc:
        raise RecoverySelectionError(
            "run_record_integrity_failed",
            "Restore the exact state, journal, authorities, and evidence before retrying recovery.",
        ) from exc


def execute_recovery_plan(
    plan: RecoveryPlanV1,
    *,
    services: RecoveryServices = DEFAULT_RECOVERY_SERVICES,
) -> RecoveryExecutionV1:
    """Receipt and execute one already-built plan, never broadening its authority."""
    current = build_recovery_plan(Path(plan.run_directory))
    if current.canonical_sha256() != plan.canonical_sha256():
        plan = _blocked_copy(
            current,
            "recovery_plan_stale",
            "Run status again and review the new recovery plan before retrying.",
        )
    attempt_id = _attempt_id(services.attempt_token())
    started = _utc_string(services.utc_now())
    plan_path, outcome_path = _receipt_paths(Path(plan.run_directory), attempt_id)
    receipt = RecoveryAttemptReceiptV1(
        attempt_id=attempt_id,
        created_at=started,
        plan_sha256=plan.canonical_sha256(),
        plan=plan,
    )
    _write_once_json(plan_path, receipt.model_dump(mode="json"))

    result_status: ObservedWorkflowStatus | None = None
    outcome_status: RecoveryOutcomeStatusV1
    reason = plan.reason_code
    next_step = plan.next_step
    try:
        if plan.disposition == "blocked":
            outcome_status = "blocked"
        elif plan.disposition == "reopen_pause":
            outcome_status = "reopened"
            result_status = plan.observed_status
        elif plan.disposition == "already_terminal":
            outcome_status = "already_terminal"
            result_status = plan.observed_status
        elif plan.operation == "finalize_snapshots":
            result = _finalize_snapshots(
                Path(plan.run_directory),
                plan.workflow_schema_version,
                physics_services=services.physics_services,
            )
            result_status = result.status
            outcome_status = "finalized"
            reason = "snapshot_finalization_completed"
            next_step = _next_step_for_status(result_status)
        elif plan.operation == "replay_human_decision":
            state = _load_reconciled_physics_state(
                Path(plan.run_directory), DEFAULT_PHYSICS_WORKFLOW_SERVICES, persist=False
            )
            if state.human_decision_path is None:
                raise WorkflowStateError("durable physics human decision is unavailable")
            result = review_physics_substage(
                Path(plan.run_directory),
                Path(state.human_decision_path),
                software_services=services.workflow_services,
                physics_services=services.physics_services,
            )
            result_status = result.status
            outcome_status = "resumed"
            reason = "durable_human_decision_continued"
            next_step = _next_step_for_status(result_status)
        else:
            result = resume_substage(
                Path(plan.run_directory),
                services=services.workflow_services,
                physics_services=services.physics_services,
            )
            result_status = result.status
            outcome_status = "resumed"
            reason = "safe_recovery_completed"
            next_step = _next_step_for_status(result_status)
    except SupervisorError as exc:
        outcome_status = "failed"
        if plan.operation == "finalize_snapshots":
            reason = "snapshot_finalization_integrity_failed"
            next_step = (
                "Restore the exact authoritative state, journal, and proof records; "
                "the corrupt public result was not trusted."
            )
        else:
            reason = "recovery_execution_failed"
            next_step = (
                "Run status again; inspect the new durable head before taking manual action."
            )
        del exc
    finished = _utc_string(services.utc_now())
    outcome = RecoveryOutcomeV1(
        attempt_id=attempt_id,
        plan_sha256=plan.canonical_sha256(),
        status=outcome_status,
        run_directory=plan.run_directory,
        result_status=result_status,
        reason_code=reason,
        next_step=next_step,
        started_at=started,
        finished_at=finished,
        plan_receipt_path=str(plan_path),
    )
    _write_once_json(outcome_path, outcome.model_dump(mode="json"))
    return RecoveryExecutionV1(
        plan=plan,
        outcome=outcome,
        plan_receipt_path=plan_path,
        outcome_receipt_path=outcome_path,
    )


def _discover_run(run_directory: Path) -> RunIndexEntryV1:
    version = _run_schema_version(run_directory)
    state_path = run_directory / "state.json"
    if version == 1:
        workflow_state = _load_reconciled_workflow_state(run_directory)
        journal_path = run_directory / WORKFLOW_JOURNAL_FILE
        terminal = workflow_state.status in _TERMINAL_V1
        state: WorkflowState | PhysicsWorkflowStateV2 = workflow_state
    else:
        physics_state = _load_reconciled_physics_state(
            run_directory, DEFAULT_PHYSICS_WORKFLOW_SERVICES, persist=False
        )
        physics_entries = _read_physics_journal(run_directory)
        _verify_physics_state_reconstruction(physics_state, physics_entries)
        journal_path = run_directory / PHYSICS_JOURNAL_FILE
        terminal = physics_state.status in _TERMINAL_V2
        state = physics_state
    return RunIndexEntryV1(
        run_directory=str(run_directory),
        workflow_schema_version=version,
        substage_id=state.substage_id,
        run_token=state.run_token,
        status=state.status,
        completion="terminal" if terminal else "incomplete",
        journal_sequence=state.journal_sequence,
        journal_hash=state.journal_hash,
        updated_at=state.updated_at,
        state_sha256=sha256_regular_file(state_path),
        journal_sha256=sha256_regular_file(journal_path),
    )


def _build_workflow_plan(run_directory: Path) -> RecoveryPlanV1:
    state = _load_reconciled_workflow_state(run_directory)
    state_sha = sha256_regular_file(run_directory / "state.json")
    journal_sha = sha256_regular_file(run_directory / WORKFLOW_JOURNAL_FILE)
    snapshots = _workflow_snapshots_synchronized(run_directory, state)
    policy_sha = _workflow_policy_sha256(state)
    lock = _inspect_workflow_lock(run_directory / WORKFLOW_LOCK_FILE)
    observations = (lock,)
    aggregate = _aggregate_process(observations)
    common = _plan_common(
        run_directory,
        state,
        1,
        state_sha,
        journal_sha,
        policy_sha,
        snapshots,
        observations,
        aggregate,
    )
    if aggregate in {"active_matching", "ambiguous_identity", "foreign_host"}:
        return _make_plan(
            common,
            workspace_reconciliation="invalid",
            proof_reconciliation="not_applicable",
            pending_action_id=None,
            pending_action_kind=None,
            disposition="blocked",
            operation="none",
            auto_resume_safe=False,
            reason_code=_process_reason(aggregate),
            next_step=_process_next_step(aggregate),
        )
    if not _raw_frozen_inputs_match(run_directory, state):
        return _blocked_plan(
            common,
            state,
            "frozen_authority_changed",
            "Restore the exact specification, contract, and prompt bytes; then retry status.",
            workspace="changed",
        )
    if not _raw_repository_matches(state, DEFAULT_WORKFLOW_SERVICES):
        return _blocked_plan(
            common,
            state,
            "workspace_identity_changed",
            "Restore the original repository root, branch, and baseline commit manually.",
            workspace="changed",
        )
    if not _workflow_workspace_matches_evidence(run_directory, state):
        return _blocked_plan(
            common,
            state,
            "workspace_content_changed",
            "Restore the exact journal-accepted workspace content manually; no reset is automatic.",
            workspace="changed",
        )
    if state.status in _TERMINAL_V1:
        disposition = "already_terminal" if snapshots else "finish_finalization"
        return _make_plan(
            common,
            workspace_reconciliation="verified",
            proof_reconciliation="not_applicable",
            pending_action_id=None,
            pending_action_kind=None,
            disposition=disposition,
            operation="none" if snapshots else "finalize_snapshots",
            auto_resume_safe=not snapshots,
            reason_code="terminal_state_verified" if snapshots else "snapshot_finalization_safe",
            next_step=(
                "No recovery action is needed."
                if snapshots
                else "Finalize the derived state/result snapshots from the journal head."
            ),
        )
    if state.status in _PAUSED_V1:
        return _make_plan(
            common,
            workspace_reconciliation="verified",
            proof_reconciliation="not_applicable",
            pending_action_id=None,
            pending_action_kind=None,
            disposition="reopen_pause",
            operation="reopen_pause",
            auto_resume_safe=False,
            reason_code="human_pause_reopened",
            next_step=(
                "Review the durable pause reason and provide the required human continuation."
            ),
        )
    pending = state.pending_action
    if pending is None:
        if state.status == "repair_pending" and state.worker_thread_id is None:
            return _blocked_plan(
                common,
                state,
                "worker_session_missing",
                "Provide human direction; the exact persistent Worker session cannot be proven.",
            )
        return _make_plan(
            common,
            workspace_reconciliation="verified",
            proof_reconciliation="before_launch",
            pending_action_id=None,
            pending_action_kind=None,
            disposition="auto_resume",
            operation="resume_workflow",
            auto_resume_safe=True,
            reason_code="safe_before_launch",
            next_step="Continue from the journal-proven pre-launch workflow phase.",
        )
    try:
        if pending.kind in {"worker", "auditor"}:
            verify_codex_artifacts(pending, known_worker_thread_id=state.worker_thread_id)
        else:
            verify_test_artifacts(pending)
    except (WorkflowInputError, WorkflowStateError):
        proof = "missing" if not Path(pending.artifact_path).exists() else "invalid"
        return _make_plan(
            common,
            workspace_reconciliation="verified",
            proof_reconciliation=proof,
            pending_action_id=pending.action_id,
            pending_action_kind=pending.kind,
            disposition="blocked",
            operation="none",
            auto_resume_safe=False,
            reason_code=(
                "ambiguous_post_launch_state" if proof == "missing" else "action_proof_invalid"
            ),
            next_step=(
                "Inspect the intended action externally; do not relaunch it without complete proof."
                if proof == "missing"
                else "Restore the exact action artifacts or abandon the run; do not relaunch it."
            ),
        )
    return _make_plan(
        common,
        workspace_reconciliation="verified",
        proof_reconciliation="completed_valid",
        pending_action_id=pending.action_id,
        pending_action_kind=pending.kind,
        disposition="auto_resume",
        operation="resume_workflow",
        auto_resume_safe=True,
        reason_code="completed_output_verified",
        next_step=(
            "Capture the verified completed output and continue without relaunching the action."
        ),
    )


def _build_physics_plan(run_directory: Path) -> RecoveryPlanV1:
    state = _load_reconciled_physics_state(
        run_directory, DEFAULT_PHYSICS_WORKFLOW_SERVICES, persist=False
    )
    entries = _read_physics_journal(run_directory)
    _verify_physics_state_reconstruction(state, entries)
    prepared = load_physics_substage_specification(
        Path(state.specification_path), require_clean=False
    )
    _verify_frozen_state(prepared, state)
    state_sha = sha256_regular_file(run_directory / "state.json")
    journal_sha = sha256_regular_file(run_directory / PHYSICS_JOURNAL_FILE)
    snapshots = _physics_snapshots_synchronized(run_directory, state)
    policy_sha = _physics_policy_sha256(state)
    # PA-4 deliberately reuses the frozen Stage-2 lock implementation.
    lock = _inspect_workflow_lock(run_directory / WORKFLOW_LOCK_FILE)
    child_observations, proof, pending_id, pending_kind = _physics_action_reconciliation(
        run_directory, state, prepared, entries
    )
    observations = (lock, *child_observations)
    aggregate = _aggregate_process(observations)
    common = _plan_common(
        run_directory,
        state,
        2,
        state_sha,
        journal_sha,
        policy_sha,
        snapshots,
        observations,
        aggregate,
    )
    if aggregate in {
        "active_matching",
        "stale_identity",
        "reused_identity",
        "ambiguous_identity",
        "foreign_host",
    }:
        return _make_plan(
            common,
            workspace_reconciliation="invalid",
            proof_reconciliation=proof,
            pending_action_id=pending_id,
            pending_action_kind=pending_kind,
            disposition="blocked",
            operation="none",
            auto_resume_safe=False,
            reason_code=_process_reason(aggregate),
            next_step=_process_next_step(aggregate),
        )
    workspace = _physics_workspace_and_authority_reconciliation(
        run_directory, state, prepared, entries
    )
    if workspace != "verified":
        reason_code = {
            "changed": "workspace_integrity_failure",
            "missing": "workspace_identity_authority_missing",
            "invalid": "workspace_identity_authority_invalid",
        }[workspace]
        next_step = {
            "changed": (
                "Restore the exact journal-accepted workspace content manually; "
                "recovery will not reset it."
            ),
            "missing": (
                "Restore the authoritative persisted workspace evidence or abandon the run; "
                "the observed workspace is never adopted as authority."
            ),
            "invalid": (
                "Restore the exact workspace/configuration/contract/evidence records manually; "
                "recovery will not rewrite them."
            ),
        }[workspace]
        return _make_plan(
            common,
            workspace_reconciliation="changed" if workspace == "changed" else "invalid",
            proof_reconciliation=proof,
            pending_action_id=pending_id,
            pending_action_kind=pending_kind,
            disposition="blocked",
            operation="none",
            auto_resume_safe=False,
            reason_code=reason_code,
            next_step=next_step,
        )
    if proof in {"missing", "invalid"}:
        return _make_plan(
            common,
            workspace_reconciliation="verified",
            proof_reconciliation=proof,
            pending_action_id=pending_id,
            pending_action_kind=pending_kind,
            disposition="blocked",
            operation="none",
            auto_resume_safe=False,
            reason_code=(
                "ambiguous_post_launch_state" if proof == "missing" else "action_proof_invalid"
            ),
            next_step=(
                "Inspect the intended action externally; do not relaunch it without proof."
                if proof == "missing"
                else "Restore the exact proof/evidence or abandon the run; do not relaunch it."
            ),
        )
    if state.status in _TERMINAL_V2:
        if state.status in {"completed", "checkpoint_paused"}:
            if snapshots:
                physics_substage_status(run_directory)
            else:
                current_identity = collect_physics_oracle_workspace_identity(
                    prepared.workspace
                ).canonical_sha256()
                if current_identity != state.accepted_workspace_identity_sha256:
                    raise WorkflowStateError(
                        "terminal physics workspace no longer matches accepted evidence"
                    )
        disposition = "already_terminal" if snapshots else "finish_finalization"
        return _make_plan(
            common,
            workspace_reconciliation="verified",
            proof_reconciliation=proof,
            pending_action_id=pending_id,
            pending_action_kind=pending_kind,
            disposition=disposition,
            operation="none" if snapshots else "finalize_snapshots",
            auto_resume_safe=not snapshots,
            reason_code="terminal_state_verified" if snapshots else "snapshot_finalization_safe",
            next_step=(
                "No recovery action is needed."
                if snapshots
                else "Finalize the derived state/result snapshots from the journal head."
            ),
        )
    if state.status in _PAUSED_V2:
        if entries[-1].reason == "physics_human_decision_recorded":
            _verify_pending_human_decision(state)
            return _make_plan(
                common,
                workspace_reconciliation="verified",
                proof_reconciliation=proof,
                pending_action_id=pending_id,
                pending_action_kind=pending_kind,
                disposition="auto_resume",
                operation="replay_human_decision",
                auto_resume_safe=True,
                reason_code="durable_human_decision_verified",
                next_step="Continue the one already-recorded human decision exactly once.",
            )
        return _make_plan(
            common,
            workspace_reconciliation="verified",
            proof_reconciliation=proof,
            pending_action_id=pending_id,
            pending_action_kind=pending_kind,
            disposition="reopen_pause",
            operation="reopen_pause",
            auto_resume_safe=False,
            reason_code="human_or_evidence_pause_reopened",
            next_step=(
                "Review the durable packet and supply the required human decision or evidence."
            ),
        )
    if state.status == "physics_repair_pending" and state.worker_thread_id is None:
        return _make_plan(
            common,
            workspace_reconciliation="verified",
            proof_reconciliation=proof,
            pending_action_id=pending_id,
            pending_action_kind=pending_kind,
            disposition="blocked",
            operation="none",
            auto_resume_safe=False,
            reason_code="worker_session_missing",
            next_step=(
                "Provide human direction; the exact persistent Worker session is unavailable."
            ),
        )
    reason = {
        "before_launch": "safe_before_launch",
        "completed_valid": "completed_output_verified",
        "finalized_valid": "finalized_proof_verified",
        "not_applicable": "safe_deterministic_transition",
    }.get(proof, "safe_deterministic_transition")
    return _make_plan(
        common,
        workspace_reconciliation="verified",
        proof_reconciliation=proof,
        pending_action_id=pending_id,
        pending_action_kind=pending_kind,
        disposition="auto_resume",
        operation="resume_workflow",
        auto_resume_safe=True,
        reason_code=reason,
        next_step="Continue the verified phase without duplicating any completed external action.",
    )


def _physics_action_reconciliation(
    run_directory: Path,
    state: PhysicsWorkflowStateV2,
    prepared: PreparedPhysicsSubstageV2,
    entries: Sequence[Any],
) -> tuple[
    tuple[RecoveryProcessObservationV1, ...],
    ProofReconciliationV1,
    str | None,
    str | None,
]:
    incomplete: tuple[str, str] | None = None
    lifecycles: dict[tuple[str, str], str] = {}
    for entry in entries:
        if entry.action_id is None or entry.action_kind is None:
            continue
        key = (entry.action_kind, entry.action_id)
        lifecycles[key] = "intent" if entry.event_type == "action_intent" else "completed"
    intents = tuple(key for key, lifecycle in lifecycles.items() if lifecycle == "intent")
    if len(intents) > 1:
        raise WorkflowStateError("physics workflow has multiple incomplete actions")
    if intents:
        incomplete = intents[0]
    if incomplete is None:
        _verify_current_physics_evidence(state, prepared)
        return (), "not_applicable", None, None
    action_kind, action_id = incomplete
    if action_kind == "software":
        nested = Path(state.software_run_directory or "")
        if not state.software_run_directory or not nested.is_dir():
            return (), "missing", action_id, action_kind
        nested_plan = _build_workflow_plan(nested)
        if nested_plan.disposition == "blocked":
            nested_proof = nested_plan.proof_reconciliation
            if nested_plan.process_reconciliation in {
                "not_applicable",
                "no_process",
                "exited",
            }:
                nested_proof = "invalid"
            return (
                nested_plan.process_observations,
                nested_proof,
                action_id,
                action_kind,
            )
        proof: ProofReconciliationV1 = (
            "completed_valid"
            if nested_plan.disposition in {"already_terminal", "reopen_pause"}
            else nested_plan.proof_reconciliation
        )
        return nested_plan.process_observations, proof, action_id, action_kind
    if action_kind == "physics_oracle":
        oracle_id = _oracle_id(action_id)
        identity = state.current_workspace_identity_sha256
        if identity is None:
            return (), "missing", action_id, action_kind
        output = run_directory / "physics" / "oracles" / f"workspace-{identity}" / oracle_id
        if not output.is_dir():
            return (), "missing", action_id, action_kind
        records = _load_physics_oracle_records(output)
        current = records[-1]
        observation = _child_process_observation(
            "physics_oracle", current.process_identity, current.phase
        )
        if current.phase == "completion_proof_finalized":
            result = verify_physics_oracle_completion(output)
            if result.canonical_sha256() != current.result_sha256:
                raise WorkflowStateError("finalized oracle result contradicts its record")
            proof = "finalized_valid"
        elif current.phase in {"intent_accepted", "execution_prepared"}:
            proof = "before_launch"
        elif current.phase in {"process_exit_observed", "output_captured", "workspace_rechecked"}:
            proof = "completed_valid"
        else:
            proof = "missing"
        return (observation,), cast(ProofReconciliationV1, proof), action_id, action_kind
    if action_kind == "physics_auditor":
        if state.physics_auditor_action_directory is None:
            return (), "missing", action_id, action_kind
        output = Path(state.physics_auditor_action_directory)
        if not output.is_dir():
            return (), "missing", action_id, action_kind
        auditor_records = _load_physics_auditor_records(output)
        current_auditor = auditor_records[-1]
        observation = _child_process_observation(
            "physics_auditor", current_auditor.process_identity, current_auditor.phase
        )
        if current_auditor.phase == "action_proof_finalized":
            _verify_physics_auditor_output(state, prepared, output, action_id)
            proof = "finalized_valid"
        elif current_auditor.phase in {
            "action_accepted",
            "evidence_verified",
            "prompt_finalized",
        }:
            proof = "before_launch"
        elif current_auditor.phase in {
            "model_exit_observed",
            "output_captured",
            "report_validated",
            "workspace_rechecked",
            "routing_completed",
        }:
            proof = "completed_valid"
        else:
            proof = "missing"
        return (observation,), cast(ProofReconciliationV1, proof), action_id, action_kind
    raise WorkflowStateError("unsupported incomplete physics action kind")


def _verify_current_physics_evidence(
    state: PhysicsWorkflowStateV2, prepared: PreparedPhysicsSubstageV2
) -> None:
    for evidence in state.oracle_evidence:
        result = verify_physics_oracle_completion(Path(evidence.output_directory))
        if (
            result.canonical_sha256() != evidence.result_sha256
            or result.completion_proof_sha256 != evidence.completion_proof_sha256
            or result.final_workspace_identity.canonical_sha256()
            != evidence.workspace_identity_sha256
        ):
            raise WorkflowStateError("current oracle proof contradicts physics state")
    if state.physics_auditor_result_sha256 is not None:
        if state.physics_auditor_action_directory is None:
            raise WorkflowStateError("Physics Auditor proof has no action directory")
        _verify_physics_auditor_output(
            state,
            prepared,
            Path(state.physics_auditor_action_directory),
            f"physics-auditor-r{state.repair_round:03d}",
        )


def _verify_physics_auditor_output(
    state: PhysicsWorkflowStateV2,
    prepared: PreparedPhysicsSubstageV2,
    output: Path,
    action_id: str,
) -> None:
    evidence_parents = {Path(item.output_directory).parent for item in state.oracle_evidence}
    if len(evidence_parents) != 1:
        raise WorkflowStateError("Physics Auditor evidence root is unavailable")
    result = verify_physics_auditor_action(
        contract_path=prepared.physics_contract_path,
        execution_config_path=prepared.auditor_config_path,
        task_id=state.substage_id,
        workspace=prepared.workspace,
        oracle_evidence_root=next(iter(evidence_parents)),
        output_directory=output,
        action_id=action_id,
        attempt_number=state.repair_round + 1,
    )
    records = _load_physics_auditor_records(output)
    provider_threads = set(records[-1].provider_thread_started_ids)
    prior_threads = set(state.prior_physics_auditor_thread_ids)
    if state.physics_auditor_result_sha256 is None:
        if provider_threads & prior_threads:
            raise WorkflowStateError("fresh Physics Auditor provider session was reused")
    elif not provider_threads.issubset(prior_threads):
        raise WorkflowStateError("accepted Physics Auditor provider session is not durable")
    if state.physics_auditor_result_sha256 is not None and (
        result.canonical_sha256() != state.physics_auditor_result_sha256
        or result.action_proof_sha256 != state.physics_auditor_proof_sha256
    ):
        raise WorkflowStateError("Physics Auditor proof contradicts physics state")


def _physics_workspace_and_authority_reconciliation(
    run_directory: Path,
    state: PhysicsWorkflowStateV2,
    prepared: PreparedPhysicsSubstageV2,
    entries: Sequence[PhysicsWorkflowJournalEntryV2],
) -> Literal["verified", "changed", "missing", "invalid"]:
    try:
        authority = json.loads(
            (run_directory / "control" / "authority.json").read_text(encoding="utf-8")
        )
        software_sha = sha256_regular_file(Path(state.software_specification_path))
        if authority.get("software_specification_sha256") != software_sha:
            return "invalid"
        baseline = record_git_baseline(prepared.workspace)
        if (
            str(baseline.repository_root) != state.repository_root
            or baseline.head != state.baseline_commit
            or baseline.branch != state.baseline_branch
        ):
            return "changed"

        journal_current_identity: str | None = None
        journal_accepted_identity: str | None = None
        for entry in entries:
            if "current_workspace_identity_sha256" in entry.state_updates:
                value = entry.state_updates["current_workspace_identity_sha256"]
                if value is not None and not isinstance(value, str):
                    return "invalid"
                journal_current_identity = value
            if "accepted_workspace_identity_sha256" in entry.state_updates:
                value = entry.state_updates["accepted_workspace_identity_sha256"]
                if value is not None and not isinstance(value, str):
                    return "invalid"
                journal_accepted_identity = value
        if (
            journal_current_identity != state.current_workspace_identity_sha256
            or journal_accepted_identity != state.accepted_workspace_identity_sha256
        ):
            return "invalid"

        nested = _physics_software_workspace_reconciliation(
            run_directory, state, prepared, entries, baseline.clean
        )
        if nested != "verified":
            return nested

        expected_identity = journal_current_identity
        if state.status in {"completed", "checkpoint_paused"}:
            if journal_accepted_identity is None:
                return "missing"
            if expected_identity is not None and expected_identity != journal_accepted_identity:
                return "invalid"
            expected_identity = journal_accepted_identity
        if state.status == "physics_auditor_running" and expected_identity is None:
            return "missing"
        if expected_identity is not None:
            current = collect_physics_oracle_workspace_identity(prepared.workspace)
            if current.canonical_sha256() != expected_identity:
                return "changed"
        return "verified"
    except (
        OSError,
        ValidationError,
        ValueError,
        PhysicsOracleError,
        WorkflowDependencyError,
        WorkflowInputError,
        WorkflowStateError,
    ):
        return "invalid"


def _physics_software_workspace_reconciliation(
    run_directory: Path,
    state: PhysicsWorkflowStateV2,
    prepared: PreparedPhysicsSubstageV2,
    entries: Sequence[PhysicsWorkflowJournalEntryV2],
    baseline_clean: bool,
) -> Literal["verified", "changed", "missing", "invalid"]:
    expected_run = run_directory / "software-runs" / f"{state.substage_id}-sw-{state.run_token}"
    if state.software_run_directory is None:
        if state.status not in {"initialized", "software_running"}:
            return "missing"
        return "verified" if baseline_clean else "changed"
    nested = Path(state.software_run_directory)
    try:
        metadata = nested.lstat()
    except OSError:
        return "missing"
    if nested != expected_run or nested.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
        return "invalid"
    nested_state = _load_reconciled_workflow_state(nested)
    if (
        nested_state.workspace != state.workspace
        or nested_state.repository_root != state.repository_root
        or nested_state.baseline_commit != state.baseline_commit
        or nested_state.baseline_branch != state.baseline_branch
    ):
        return "invalid"

    lifecycle: str | None = None
    for entry in entries:
        if entry.action_kind == "software":
            lifecycle = "intent" if entry.event_type == "action_intent" else "completed"
    if lifecycle == "completed":
        if state.latest_software_result_path is None:
            return "missing"
        try:
            evidence = _load_physics_model(Path(state.latest_software_result_path), WorkflowResult)
        except WorkflowStateError:
            return "invalid"
        if evidence != nested_state.to_result():
            return "invalid"
    software_gate_accepted = state.code_auditor_passed or state.status in {
        "physics_oracles_running",
        "physics_auditor_running",
        "physics_repair_pending",
        "completed",
        "checkpoint_paused",
    }
    if software_gate_accepted and nested_state.latest_git_evidence_path is None:
        return "missing"
    return "verified" if _workflow_workspace_matches_evidence(nested, nested_state) else "changed"


def _verify_pending_human_decision(state: PhysicsWorkflowStateV2) -> None:
    if state.human_decision_path is None or state.human_decision_sha256 is None:
        raise WorkflowStateError("durable human decision evidence is missing")
    decision = _load_physics_model(Path(state.human_decision_path), PhysicsReviewDecisionV1)
    if decision.canonical_sha256() != state.human_decision_sha256:
        raise WorkflowStateError("durable human decision was replaced")


def _verify_physics_state_reconstruction(
    state: PhysicsWorkflowStateV2,
    entries: Sequence[PhysicsWorkflowJournalEntryV2],
) -> None:
    """Rebuild every mutable v2 field from the qualified sequence-zero form."""
    initial_values = state.model_dump(mode="python")
    initial_values.update(
        {
            "status": "initialized",
            "repair_round": 0,
            "software_run_directory": None,
            "worker_thread_id": None,
            "latest_software_result_path": None,
            "tests_passed": False,
            "code_auditor_passed": False,
            "required_oracle_proofs_verified": False,
            "oracle_evidence": (),
            "historical_oracle_evidence": (),
            "invalidated_oracle_ids": (),
            "preserved_oracle_ids": (),
            "current_workspace_identity_sha256": None,
            "accepted_workspace_identity_sha256": None,
            "physics_auditor_action_directory": None,
            "physics_auditor_result_sha256": None,
            "physics_auditor_proof_sha256": None,
            "physics_report_sha256": None,
            "physics_routing_sha256": None,
            "physics_route": None,
            "physics_reason_codes": (),
            "prior_physics_auditor_thread_ids": (),
            "repair_prompt_path": None,
            "repair_prompt_sha256": None,
            "repair_prompt_consumed": False,
            "human_review_packet_path": None,
            "human_review_packet_sha256": None,
            "human_decision_path": None,
            "human_decision_sha256": None,
            "pause_reason": None,
            "summary": "Physics workflow initialized.",
            "journal_sequence": 0,
            "journal_hash": ZERO_HASH,
            "updated_at": state.started_at,
        }
    )
    try:
        initial = PhysicsWorkflowStateV2.model_validate(initial_values)
    except ValidationError as exc:
        raise WorkflowStateError("physics sequence-zero snapshot is invalid") from exc
    reconstructed = reconcile_model_snapshot(
        initial,
        [item.model_dump(mode="json") for item in entries],
        model=PhysicsWorkflowStateV2,
        error_factory=WorkflowStateError,
        error_message="physics journal cannot reconstruct its authoritative state",
    )
    if reconstructed != state:
        raise WorkflowStateError("physics state contradicts its journal-derived authority")


def _load_reconciled_workflow_state(run_directory: Path) -> WorkflowState:
    state = _load_state(run_directory)
    entries = _read_valid_journal(run_directory)
    reconciled = reconcile_model_snapshot(
        state,
        [item.model_dump(mode="json") for item in entries],
        model=WorkflowState,
        error_factory=WorkflowStateError,
        error_message="workflow journal recovery state is invalid",
    )
    if (
        not entries
        or reconciled.journal_sequence != len(entries)
        or reconciled.journal_hash != entries[-1].entry_hash
        or reconciled.updated_at != entries[-1].timestamp
    ):
        raise WorkflowStateError("workflow state does not reconcile to its journal head")
    _validate_normalized_action_intents(run_directory, reconciled, entries=entries)
    return reconciled


def _workflow_snapshots_synchronized(run_directory: Path, state: WorkflowState) -> bool:
    try:
        return (
            _load_state(run_directory) == state and _load_result(run_directory) == state.to_result()
        )
    except WorkflowStateError:
        return False


def _workflow_workspace_matches_evidence(run_directory: Path, state: WorkflowState) -> bool:
    """Compare current Git-scope evidence without modifying the workspace or run."""
    if state.latest_git_evidence_path is None:
        # A completed-but-not-yet-captured Worker action legitimately precedes the first
        # Stage-2 Git snapshot. Its exact adapter proof is checked separately below.
        if state.pending_action is not None and state.pending_action.kind == "worker":
            return True
        if state.status not in {"initialized", "worker_running"}:
            return True
        try:
            return record_git_baseline(Path(state.workspace)).clean
        except (WorkflowDependencyError, WorkflowInputError):
            return False
    try:
        prepared = load_substage_specification(Path(state.specification_path), require_clean=False)
        baseline = GitBaseline.model_validate(
            json.loads((run_directory / "baseline.json").read_text(encoding="utf-8"))
        )
        expected = GitEvidence.model_validate(
            json.loads(Path(state.latest_git_evidence_path).read_text(encoding="utf-8"))
        )
        _, _, sensitive_values = build_subprocess_environment()
        with tempfile.TemporaryDirectory(prefix="pa5-workspace-evidence-") as temporary:
            observed = collect_git_evidence(
                prepared.workspace,
                baseline,
                prepared.specification.allowed_paths,
                prepared.specification.protected_paths,
                Path(temporary) / "git",
                sensitive_values=sensitive_values,
            )
        return observed.model_dump(exclude={"patch_artifact"}) == expected.model_dump(
            exclude={"patch_artifact"}
        )
    except (
        OSError,
        ValidationError,
        WorkflowDependencyError,
        WorkflowInputError,
        WorkflowStateError,
        ValueError,
    ):
        return False


def _physics_snapshots_synchronized(run_directory: Path, state: PhysicsWorkflowStateV2) -> bool:
    try:
        raw_state = _load_physics_model(run_directory / "state.json", PhysicsWorkflowStateV2)
        result = _load_physics_model(run_directory / PHYSICS_RESULT_FILE, PhysicsWorkflowResultV2)
        return raw_state == state and result == state.to_result()
    except WorkflowStateError:
        return False


def _finalize_snapshots(
    run_directory: Path,
    version: Literal[1, 2],
    *,
    physics_services: PhysicsWorkflowServices = DEFAULT_PHYSICS_WORKFLOW_SERVICES,
) -> WorkflowResult | PhysicsWorkflowResultV2:
    if version == 1:
        with _WorkflowLock(run_directory, lambda: datetime.now(UTC)):
            state = _reconcile_state_with_journal(run_directory, _load_state(run_directory))
            if state.status not in _TERMINAL_V1:
                raise WorkflowStateError("snapshot finalization no longer has a terminal head")
            return state.to_result()
    with _WorkflowLock(run_directory, lambda: datetime.now(UTC)):
        state_v2 = _load_reconciled_physics_state(
            run_directory, DEFAULT_PHYSICS_WORKFLOW_SERVICES, persist=False
        )
        if state_v2.status not in _TERMINAL_V2:
            raise WorkflowStateError("snapshot finalization no longer has a terminal head")
        entries = _read_physics_journal(run_directory)
        _verify_physics_state_reconstruction(state_v2, entries)
        prepared = load_physics_substage_specification(
            Path(state_v2.specification_path), require_clean=False
        )
        _verify_frozen_state(prepared, state_v2)
        observations, proof, pending_id, pending_kind = _physics_action_reconciliation(
            run_directory, state_v2, prepared, entries
        )
        if (
            observations
            or pending_id is not None
            or pending_kind is not None
            or proof
            in {
                "missing",
                "invalid",
            }
        ):
            raise WorkflowStateError("terminal snapshot has unresolved external action evidence")
        workspace = _physics_workspace_and_authority_reconciliation(
            run_directory, state_v2, prepared, entries
        )
        if workspace != "verified":
            raise WorkflowStateError("terminal snapshot workspace authority does not verify")
        expected = state_v2.to_result()
        commit_result_then_state(
            result_path=run_directory / PHYSICS_RESULT_FILE,
            result_value=expected.model_dump(mode="json"),
            state_path=run_directory / "state.json",
            state_value=state_v2.model_dump(mode="json"),
            checkpoint=lambda name: physics_services.checkpoint(f"recovery_finalization:{name}"),
            error_factory=WorkflowStateError,
            error_message="physics recovery snapshots could not be finalized",
        )
        verified = physics_substage_status(run_directory)
        if verified != expected:
            raise WorkflowStateError("finalized public result contradicts authoritative state")
        return verified


def _inspect_workflow_lock(path: Path) -> RecoveryProcessObservationV1:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return _process_observation("workflow_lock", "no_process")
    except OSError:
        return _process_observation("workflow_lock", "ambiguous_identity")
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            return _process_observation("workflow_lock", "ambiguous_identity")
        locked = False
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except BlockingIOError:
            pass
        content = os.read(descriptor, 16 * 1024)
        if locked:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        metadata = _parse_lock_metadata(content)
        if not locked:
            return _process_observation("workflow_lock", "active_matching", pid=metadata.get("pid"))
        if not content.strip():
            return _process_observation("workflow_lock", "no_process")
        host = metadata.get("host")
        pid = metadata.get("pid")
        if not isinstance(host, str) or not isinstance(pid, int):
            return _process_observation("workflow_lock", "ambiguous_identity")
        if host != socket.gethostname():
            return _process_observation("workflow_lock", "foreign_host", pid=pid)
        observed = _process_start_ticks(pid)
        if observed is None:
            return _process_observation("workflow_lock", "exited", pid=pid)
        return _process_observation(
            "workflow_lock", "ambiguous_identity", pid=pid, observed=observed
        )
    except (OSError, ValueError):
        return _process_observation("workflow_lock", "ambiguous_identity")
    finally:
        os.close(descriptor)


def _child_process_observation(
    scope: Literal["physics_oracle", "physics_auditor"],
    identity: Any,
    phase: str,
) -> RecoveryProcessObservationV1:
    launch_phase = (
        "process_launch_attempted" if scope == "physics_oracle" else "model_launch_attempted"
    )
    running_phase = "process_running" if scope == "physics_oracle" else "model_running"
    if phase == launch_phase:
        return _process_observation(scope, "ambiguous_identity")
    if phase != running_phase:
        return _process_observation(scope, "no_process")
    if identity is None:
        return _process_observation(scope, "ambiguous_identity")
    observed = _process_start_ticks(identity.pid)
    if observed is None:
        return _process_observation(
            scope, "stale_identity", pid=identity.pid, expected=identity.start_ticks
        )
    if observed != identity.start_ticks:
        return _process_observation(
            scope,
            "reused_identity",
            pid=identity.pid,
            expected=identity.start_ticks,
            observed=observed,
        )
    return _process_observation(
        scope,
        "active_matching",
        pid=identity.pid,
        expected=identity.start_ticks,
        observed=observed,
    )


def _process_observation(
    scope: Literal["workflow_lock", "physics_oracle", "physics_auditor"],
    reconciliation: ProcessReconciliationV1,
    *,
    pid: int | None = None,
    expected: int | None = None,
    observed: int | None = None,
) -> RecoveryProcessObservationV1:
    return RecoveryProcessObservationV1(
        scope=scope,
        reconciliation=reconciliation,
        pid=pid,
        expected_start_ticks=expected,
        observed_start_ticks=observed,
    )


def _aggregate_process(
    observations: Sequence[RecoveryProcessObservationV1],
) -> ProcessReconciliationV1:
    if not observations:
        return "not_applicable"
    return max(
        (item.reconciliation for item in observations),
        key=lambda item: _PROCESS_PRIORITY[item],
    )


def _plan_common(
    run_directory: Path,
    state: WorkflowState | PhysicsWorkflowStateV2,
    version: Literal[1, 2],
    state_sha: str,
    journal_sha: str,
    policy_sha: str,
    snapshots: bool,
    observations: tuple[RecoveryProcessObservationV1, ...],
    aggregate: ProcessReconciliationV1,
) -> dict[str, object]:
    return {
        "run_directory": str(run_directory),
        "workflow_schema_version": version,
        "substage_id": state.substage_id,
        "run_token": state.run_token,
        "observed_status": state.status,
        "journal_sequence": state.journal_sequence,
        "journal_hash": state.journal_hash,
        "state_sha256": state_sha,
        "journal_sha256": journal_sha,
        "policy_sha256": policy_sha,
        "process_reconciliation": aggregate,
        "process_observations": observations,
        "worker_session_id": state.worker_thread_id,
        "snapshots_synchronized": snapshots,
    }


def _blocked_plan(
    common: Mapping[str, object],
    state: WorkflowState,
    reason: str,
    next_step: str,
    *,
    workspace: Literal["verified", "changed", "invalid"] = "verified",
) -> RecoveryPlanV1:
    return _make_plan(
        common,
        workspace_reconciliation=workspace,
        proof_reconciliation="not_applicable",
        pending_action_id=(state.pending_action.action_id if state.pending_action else None),
        pending_action_kind=(state.pending_action.kind if state.pending_action else None),
        disposition="blocked",
        operation="none",
        auto_resume_safe=False,
        reason_code=reason,
        next_step=next_step,
    )


def _make_plan(common: Mapping[str, object], **updates: object) -> RecoveryPlanV1:
    values = dict(common)
    values.update(updates)
    return RecoveryPlanV1.model_validate(values)


def _blocked_copy(plan: RecoveryPlanV1, reason: str, next_step: str) -> RecoveryPlanV1:
    values = plan.model_dump(mode="python")
    values.update(
        {
            "disposition": "blocked",
            "operation": "none",
            "auto_resume_safe": False,
            "reason_code": reason,
            "next_step": next_step,
        }
    )
    return RecoveryPlanV1.model_validate(values)


def _workflow_policy_sha256(state: WorkflowState) -> str:
    pending = state.pending_action
    policy = {
        "specification_sha256": state.specification_sha256,
        "contract_sha256": state.contract_sha256,
        "prompts_sha256": state.prompts_sha256,
        "max_repair_rounds": state.max_repair_rounds,
        "checkpoint_after": state.checkpoint_after,
        "pending_policy": (
            None
            if pending is None
            else {
                "kind": pending.kind,
                "model": pending.model,
                "reasoning_effort": pending.reasoning_effort,
                "sandbox": pending.sandbox,
                "approval_policy": pending.approval_policy,
                "ephemeral": pending.ephemeral,
                "network_policy": pending.network_policy,
                "resume_thread_id": pending.resume_thread_id,
            }
        ),
    }
    return hashlib.sha256(canonical_json(policy)).hexdigest()


def _physics_policy_sha256(state: PhysicsWorkflowStateV2) -> str:
    policy = {
        "specification_sha256": state.specification_sha256,
        "physics_contract_sha256": state.physics_contract_sha256,
        "oracle_catalog_sha256": state.oracle_catalog_sha256,
        "auditor_config_sha256": state.auditor_config_sha256,
        "max_repair_rounds": state.max_repair_rounds,
        "checkpoint_after": state.checkpoint_after,
        "worker_thread_id": state.worker_thread_id,
    }
    return hashlib.sha256(canonical_json(policy)).hexdigest()


def _receipt_paths(run_directory: Path, attempt_id: str) -> tuple[Path, Path]:
    root = run_directory.parent / RECEIPT_ROOT
    run_key = hashlib.sha256(str(run_directory).encode("utf-8")).hexdigest()[:32]
    directory = root / run_key
    _ensure_private_directory(root)
    _ensure_private_directory(directory)
    return directory / f"{attempt_id}.plan.json", directory / f"{attempt_id}.outcome.json"


def _ensure_private_directory(path: Path) -> None:
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        metadata = path.lstat()
        if path.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
            raise WorkflowStateError("recovery receipt directory is unsafe") from None
    except OSError as exc:
        raise WorkflowStateError("recovery receipt directory could not be created") from exc


def _write_once_json(path: Path, value: object) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
        try:
            content = render_json_bytes(value)
            offset = 0
            while offset < len(content):
                offset += os.write(descriptor, content[offset:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as exc:
        raise WorkflowStateError("immutable recovery receipt could not be written") from exc


def _persist_run_index(root: Path, index: RunIndexV1) -> None:
    path = root / RUN_INDEX_FILE
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        pass
    except OSError:
        return
    else:
        if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
            return
    atomic_write_json(
        path,
        index.model_dump(mode="json"),
        error_factory=WorkflowStateError,
        error_message="workflow run index cache could not be written",
    )


def _resolve_runs_directory(path: Path) -> Path:
    try:
        resolved = path.resolve(strict=True)
        metadata = resolved.lstat()
    except (OSError, RuntimeError, ValueError) as exc:
        raise WorkflowInputError("workflow runs directory is unavailable") from exc
    if resolved.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
        raise WorkflowInputError("workflow runs path is not a canonical directory")
    return resolved


def _resolve_run_directory(path: Path) -> Path:
    try:
        resolved = path.resolve(strict=True)
        metadata = resolved.lstat()
    except (OSError, RuntimeError, ValueError) as exc:
        raise WorkflowInputError("workflow run directory is unavailable") from exc
    if resolved.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
        raise WorkflowInputError("workflow run path is not a canonical directory")
    return resolved


def _is_direct_run_directory(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    if path.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
        return False
    return (path / WORKFLOW_JOURNAL_FILE).is_file() or (path / PHYSICS_JOURNAL_FILE).is_file()


def _run_schema_version(run_directory: Path) -> Literal[1, 2]:
    physics = run_directory / PHYSICS_JOURNAL_FILE
    legacy = run_directory / WORKFLOW_JOURNAL_FILE
    if physics.is_file() and not physics.is_symlink() and not legacy.exists():
        return 2
    if legacy.is_file() and not legacy.is_symlink() and not physics.exists():
        return 1
    raise WorkflowStateError("workflow run journal discriminator is ambiguous")


def _parse_lock_metadata(content: bytes) -> dict[str, Any]:
    if not content.strip():
        return {}
    value = json.loads(content.decode("utf-8"), parse_constant=_reject_constant)
    if (
        not isinstance(value, dict)
        or set(value) != {"schema_version", "pid", "host", "started_at"}
        or value.get("schema_version") != 1
    ):
        raise ValueError("lock metadata is invalid")
    return cast(dict[str, Any], value)


def _process_start_ticks(pid: int) -> int | None:
    try:
        value = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        close = value.rfind(")")
        fields = value[close + 2 :].split()
        if fields[0] == "Z":
            return None
        ticks = int(fields[19])
    except (OSError, ValueError, IndexError):
        return None
    return ticks if ticks > 0 else None


def _process_reason(value: ProcessReconciliationV1) -> str:
    return {
        "active_matching": "active_matching_process",
        "stale_identity": "stale_pid_ambiguity",
        "reused_identity": "reused_pid_ambiguity",
        "ambiguous_identity": "process_identity_ambiguous",
        "foreign_host": "foreign_host_process_ambiguity",
    }.get(value, "process_identity_ambiguous")


def _process_next_step(value: ProcessReconciliationV1) -> str:
    return {
        "active_matching": "Wait for the matching process to exit, then run status again.",
        "stale_identity": "Inspect the stale child process record; do not relaunch the action.",
        "reused_identity": "Inspect the reused PID record; do not signal or relaunch that process.",
        "foreign_host": "Confirm the original host is stopped and resolve the lock manually.",
    }.get(value, "Inspect the recorded process identity; do not guess or relaunch the action.")


def _next_step_for_status(status: ObservedWorkflowStatus) -> str:
    if status == "completed":
        return "Inspect the completed result; no further recovery action is needed."
    if status in {"human_paused", "human_review_paused", "repair_limit_paused"}:
        return "Review the durable pause packet and provide the required human decision."
    if status == "evidence_paused":
        return "Provide the missing evidence through the documented human review path."
    if status == "checkpoint_paused":
        return "Review and accept the durable checkpoint before downstream work."
    if status in {"failed", "aborted", "infrastructure_stopped"}:
        return "Inspect the terminal reason and start a new run if further work is required."
    return "Run status again before any further operator action."


def _attempt_id(value: str) -> str:
    normalized = value.strip().replace("_", "-")
    if not normalized or len(normalized) > 64 or not normalized.replace("-", "").isalnum():
        raise WorkflowStateError("recovery attempt token is invalid")
    return f"recovery-{normalized}"


def _oracle_id(action_id: str) -> str:
    match = re.fullmatch(r"oracle-r\d{3}-(.+)", action_id)
    if match is None:
        raise WorkflowStateError("physics oracle action ID is invalid")
    return match.group(1)


def _parse_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise WorkflowStateError("workflow timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise WorkflowStateError("workflow timestamp is not UTC")
    return parsed


def _utc_string(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")
