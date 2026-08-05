"""Strict PA-5A models for workflow discovery and operator recovery."""

from __future__ import annotations

import hashlib
from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

from research_automation_supervisor.durable_state import canonical_json
from research_automation_supervisor.workflow_models import (
    BoundedString,
    Identifier,
    _freeze_sequence,
)

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]

ObservedWorkflowStatus: TypeAlias = Literal[
    "initialized",
    "worker_running",
    "scope_checking",
    "tests_running",
    "auditor_running",
    "repair_pending",
    "human_paused",
    "repair_limit_paused",
    "checkpoint_paused",
    "completed",
    "failed",
    "aborted",
    "software_running",
    "physics_oracles_running",
    "physics_auditor_running",
    "physics_repair_pending",
    "human_review_paused",
    "evidence_paused",
    "infrastructure_stopped",
]

RunCompletionV1: TypeAlias = Literal["incomplete", "terminal"]
RunIntegrityV1: TypeAlias = Literal["verified", "corrupt"]
RecoveryDispositionV1: TypeAlias = Literal[
    "auto_resume", "reopen_pause", "finish_finalization", "already_terminal", "blocked"
]
RecoveryOperationV1: TypeAlias = Literal[
    "resume_workflow",
    "replay_human_decision",
    "reopen_pause",
    "finalize_snapshots",
    "none",
]
ProcessReconciliationV1: TypeAlias = Literal[
    "not_applicable",
    "no_process",
    "exited",
    "active_matching",
    "stale_identity",
    "reused_identity",
    "ambiguous_identity",
    "foreign_host",
]
ProofReconciliationV1: TypeAlias = Literal[
    "not_applicable",
    "before_launch",
    "completed_valid",
    "finalized_valid",
    "missing",
    "invalid",
]
RecoveryOutcomeStatusV1: TypeAlias = Literal[
    "resumed", "reopened", "finalized", "already_terminal", "blocked", "failed"
]


class RunIndexEntryV1(BaseModel):
    """One derived pointer to an authoritative workflow journal head."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    run_directory: str
    workflow_schema_version: Literal[1, 2]
    substage_id: Identifier
    run_token: Identifier
    status: ObservedWorkflowStatus
    completion: RunCompletionV1
    journal_sequence: Annotated[int, Field(ge=0)]
    journal_hash: Sha256
    updated_at: str
    state_sha256: Sha256
    journal_sha256: Sha256

    @model_validator(mode="after")
    def validate_journal_sequence(self) -> RunIndexEntryV1:
        if self.workflow_schema_version == 1 and self.journal_sequence == 0:
            raise ValueError("schema-version-1 run-index entries require a journal head")
        return self


class RunIndexIssueV1(BaseModel):
    """A run-shaped directory that could not be trusted for selection."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    run_directory: str
    integrity: Literal["corrupt"] = "corrupt"
    reason_code: Identifier
    next_step: BoundedString


class RunIndexV1(BaseModel):
    """Replaceable cache rebuilt from run-owned manifests and journals."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    runs_directory: str
    generated_at: str
    entries: Annotated[tuple[RunIndexEntryV1, ...], BeforeValidator(_freeze_sequence)]
    issues: Annotated[tuple[RunIndexIssueV1, ...], BeforeValidator(_freeze_sequence)]
    source_sha256: Sha256

    @model_validator(mode="after")
    def validate_cache(self) -> RunIndexV1:
        if tuple(sorted(self.entries, key=lambda item: item.run_directory)) != self.entries:
            raise ValueError("run-index entries must be sorted by directory")
        if tuple(sorted(self.issues, key=lambda item: item.run_directory)) != self.issues:
            raise ValueError("run-index issues must be sorted by directory")
        entry_directories = {item.run_directory for item in self.entries}
        issue_directories = {item.run_directory for item in self.issues}
        if entry_directories & issue_directories:
            raise ValueError("run-index directories must be unique")
        payload = {
            "runs_directory": self.runs_directory,
            "entries": [item.model_dump(mode="json") for item in self.entries],
            "issues": [item.model_dump(mode="json") for item in self.issues],
        }
        if self.source_sha256 != hashlib.sha256(canonical_json(payload)).hexdigest():
            raise ValueError("run-index source digest is invalid")
        return self


class RecoveryProcessObservationV1(BaseModel):
    """One reconciled engine or child-process identity."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    scope: Literal["workflow_lock", "physics_oracle", "physics_auditor"]
    reconciliation: ProcessReconciliationV1
    pid: Annotated[int, Field(gt=0)] | None
    expected_start_ticks: Annotated[int, Field(gt=0)] | None
    observed_start_ticks: Annotated[int, Field(gt=0)] | None

    @model_validator(mode="after")
    def validate_identity(self) -> RecoveryProcessObservationV1:
        if self.scope == "workflow_lock" and self.expected_start_ticks is not None:
            raise ValueError("legacy workflow locks do not contain process start ticks")
        if self.reconciliation in {"stale_identity", "reused_identity", "active_matching"} and (
            self.pid is None
        ):
            raise ValueError("process reconciliation requires a PID")
        if (
            self.scope != "workflow_lock"
            and self.reconciliation in {"reused_identity", "active_matching"}
            and (self.expected_start_ticks is None or self.observed_start_ticks is None)
        ):
            raise ValueError("matching or reused identity requires both start ticks")
        return self


class RecoveryPlanV1(BaseModel):
    """A deterministic plan bound to one exact authoritative run head."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    run_directory: str
    workflow_schema_version: Literal[1, 2]
    substage_id: Identifier
    run_token: Identifier
    observed_status: ObservedWorkflowStatus
    journal_sequence: Annotated[int, Field(ge=0)]
    journal_hash: Sha256
    state_sha256: Sha256
    journal_sha256: Sha256
    policy_sha256: Sha256
    workspace_reconciliation: Literal["verified", "changed", "invalid"]
    process_reconciliation: ProcessReconciliationV1
    process_observations: Annotated[
        tuple[RecoveryProcessObservationV1, ...], BeforeValidator(_freeze_sequence)
    ]
    proof_reconciliation: ProofReconciliationV1
    pending_action_id: Identifier | None
    pending_action_kind: Identifier | None
    worker_session_id: str | None
    snapshots_synchronized: bool
    disposition: RecoveryDispositionV1
    operation: RecoveryOperationV1
    auto_resume_safe: bool
    reason_code: Identifier
    next_step: BoundedString

    @model_validator(mode="after")
    def validate_disposition(self) -> RecoveryPlanV1:
        if self.workflow_schema_version == 1 and self.journal_sequence == 0:
            raise ValueError("schema-version-1 recovery plans require a journal head")
        if self.auto_resume_safe != (self.disposition in {"auto_resume", "finish_finalization"}):
            raise ValueError("recovery plan safety flag contradicts its disposition")
        allowed = {
            "auto_resume": {"resume_workflow", "replay_human_decision"},
            "reopen_pause": {"reopen_pause"},
            "finish_finalization": {"finalize_snapshots"},
            "already_terminal": {"none"},
            "blocked": {"none"},
        }
        if self.operation not in allowed[self.disposition]:
            raise ValueError("recovery operation contradicts its disposition")
        worst = {item.reconciliation for item in self.process_observations}
        if self.process_reconciliation not in worst and self.process_observations:
            raise ValueError("aggregate process reconciliation lacks a matching observation")
        return self

    def canonical_sha256(self) -> str:
        return hashlib.sha256(canonical_json(self.model_dump(mode="json"))).hexdigest()


class RecoveryAttemptReceiptV1(BaseModel):
    """Create-once evidence that an exact recovery plan was accepted for execution."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    attempt_id: Identifier
    created_at: str
    plan_sha256: Sha256
    plan: RecoveryPlanV1

    @model_validator(mode="after")
    def validate_plan_hash(self) -> RecoveryAttemptReceiptV1:
        if self.plan_sha256 != self.plan.canonical_sha256():
            raise ValueError("recovery-attempt plan digest is invalid")
        return self


class RecoveryOutcomeV1(BaseModel):
    """Create-once result for one previously receipted recovery plan."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    attempt_id: Identifier
    plan_sha256: Sha256
    status: RecoveryOutcomeStatusV1
    run_directory: str
    result_status: ObservedWorkflowStatus | None
    reason_code: Identifier
    next_step: BoundedString
    started_at: str
    finished_at: str
    plan_receipt_path: str

    def canonical_sha256(self) -> str:
        return hashlib.sha256(canonical_json(self.model_dump(mode="json"))).hexdigest()
