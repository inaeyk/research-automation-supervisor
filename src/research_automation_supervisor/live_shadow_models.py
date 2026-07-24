"""Strict immutable models for Stage 4 live quarantined shadow observation."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    model_validator,
)

from research_automation_supervisor.codex_models import ModelName, ReasoningEffort
from research_automation_supervisor.shadow_models import (
    FrozenFileHash,
    PendingSupervisorAction,
    ProposalKind,
    SupervisorUUID,
)
from research_automation_supervisor.workflow_models import (
    Identifier,
    WorkflowStatus,
    _freeze_sequence,
)

MIN_OBSERVER_POLL_MILLISECONDS = 50
MAX_OBSERVER_POLL_MILLISECONDS = 5_000
MIN_SHADOW_COMPLETION_SECONDS = 30
MAX_SHADOW_COMPLETION_SECONDS = 86_400
MIN_PROPOSAL_BYTES = 1024
MAX_PROPOSAL_BYTES = 2 * 1024 * 1024
MAX_LIVE_SHADOW_STRING_BYTES = 16 * 1024

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
BoundedString = Annotated[str, Field(min_length=1, max_length=MAX_LIVE_SHADOW_STRING_BYTES)]
StringTuple = Annotated[tuple[str, ...], BeforeValidator(_freeze_sequence)]
LiveShadowStatus = Literal[
    "initialized",
    "authoritative_starting",
    "authoritative_running",
    "authoritative_terminal_shadow_pending",
    "awaiting_reviews",
    "completed",
    "shadow_degraded",
    "human_paused",
    "failed",
    "aborted",
]
LiveReadinessStatus = Literal[
    "insufficient_data",
    "not_ready",
    "candidate_ready_for_supervised_handoff",
]


class LiveShadowSpecification(BaseModel):
    """The exact frozen schema-version-1 Stage 4 specification."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        str_strip_whitespace=True,
    )

    schema_version: Literal[1]
    live_shadow_id: Identifier
    title: Annotated[str, Field(min_length=1, max_length=1024)]
    stage2_specification_path: Annotated[str, Field(min_length=1)]
    supervisor_policy_path: Annotated[str, Field(min_length=1)]
    project_context_paths: Annotated[
        tuple[Annotated[str, Field(min_length=1)], ...],
        BeforeValidator(_freeze_sequence),
        Field(max_length=100),
    ]
    supervisor_model: ModelName
    supervisor_reasoning_effort: ReasoningEffort
    supervisor_timeout_seconds: Annotated[int, Field(ge=30, le=14_400)]
    max_proposal_bytes: Annotated[
        int, Field(ge=MIN_PROPOSAL_BYTES, le=MAX_PROPOSAL_BYTES)
    ]
    observer_poll_interval_milliseconds: Annotated[
        int,
        Field(
            ge=MIN_OBSERVER_POLL_MILLISECONDS,
            le=MAX_OBSERVER_POLL_MILLISECONDS,
        ),
    ]
    shadow_completion_timeout_seconds: Annotated[
        int,
        Field(ge=MIN_SHADOW_COMPLETION_SECONDS, le=MAX_SHADOW_COMPLETION_SECONDS),
    ]
    minimum_reviewed_proposals: Annotated[int, Field(ge=1, le=100)]
    required_consecutive_acceptable: Annotated[int, Field(ge=1, le=100)]

    @model_validator(mode="after")
    def validate_thresholds_and_contexts(self) -> LiveShadowSpecification:
        if self.required_consecutive_acceptable > self.minimum_reviewed_proposals:
            raise ValueError(
                "required_consecutive_acceptable must not exceed "
                "minimum_reviewed_proposals"
            )
        if len(set(self.project_context_paths)) != len(self.project_context_paths):
            raise ValueError("project_context_paths contains a duplicate")
        return self


class LiveAcceptanceTest(BaseModel):
    """The exact acceptance-test identity visible at a live decision."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    id: Identifier
    argv: StringTuple


class FrozenEvidenceArtifact(BaseModel):
    """One verified, bounded, repository-independent evidence item."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    locator: str
    sha256: Sha256
    byte_count: Annotated[int, Field(ge=0)]
    stored_byte_count: Annotated[int, Field(ge=0)]
    content_truncated: bool
    safe_content: Any

    @model_validator(mode="after")
    def validate_bounds(self) -> FrozenEvidenceArtifact:
        if self.stored_byte_count > self.byte_count:
            raise ValueError("stored evidence bytes exceed the source byte count")
        if self.content_truncated != (self.stored_byte_count < self.byte_count):
            raise ValueError("evidence truncation flag contradicts byte counts")
        return self


class PriorAuthoritativeActionSummary(BaseModel):
    """A typed prior action summary available before the current intent."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    action_id: Identifier
    kind: Literal["worker", "auditor"]
    repair_round: Annotated[int, Field(ge=0)]
    summary: BoundedString
    status: str


class LiveDecisionEnvelope(BaseModel):
    """An immutable point-in-time envelope ending at one Stage 2 intent."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    live_shadow_id: Identifier
    live_shadow_run_id: Identifier
    authoritative_stage2_run: str
    authoritative_stage2_run_id: Identifier
    authoritative_substage_id: Identifier
    decision_id: Identifier
    proposal_kind: ProposalKind
    ordinal: Annotated[int, Field(ge=1)]
    repair_round: Annotated[int, Field(ge=0)]
    source_action_id: Identifier
    journal_intent_sequence: Annotated[int, Field(ge=1)]
    journal_intent_hash: Sha256
    journal_prefix_sha256: Sha256
    baseline_commit: str
    baseline_branch: str | None
    repository_identity_sha256: Sha256
    allowed_paths: StringTuple
    protected_paths: StringTuple
    acceptance_tests: Annotated[
        tuple[LiveAcceptanceTest, ...], BeforeValidator(_freeze_sequence)
    ]
    triggering_evidence: dict[str, Any]
    evidence_artifacts: Annotated[
        tuple[FrozenEvidenceArtifact, ...], BeforeValidator(_freeze_sequence)
    ]
    prior_authoritative_action_summaries: Annotated[
        tuple[PriorAuthoritativeActionSummary, ...],
        BeforeValidator(_freeze_sequence),
    ]
    comparison_available: Literal[False]
    comparison_unavailable_reason: Literal["authoritative_action_pending"]
    envelope_timestamp: str
    envelope_sha256: Sha256


class AuthoritativeLaunchRecord(BaseModel):
    """The once-written identity of the independent Stage 2 child."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    launch_state: Literal["prepared", "launched"]
    specification_path: str
    specification_sha256: Sha256
    stage2_runs_directory: str
    codex_executable: str
    pid: Annotated[int, Field(gt=0)] | None
    process_group_id: Annotated[int, Field(gt=0)] | None
    session_id: Annotated[int, Field(gt=0)] | None
    process_start_ticks: Annotated[int, Field(gt=0)] | None
    started_at: str | None
    known_run_directories: StringTuple

    @model_validator(mode="after")
    def validate_launch_identity(self) -> AuthoritativeLaunchRecord:
        fields = (
            self.pid,
            self.process_group_id,
            self.session_id,
            self.process_start_ticks,
            self.started_at,
        )
        if self.launch_state == "prepared" and any(value is not None for value in fields):
            raise ValueError("prepared launch record unexpectedly contains a child identity")
        if self.launch_state == "launched" and any(value is None for value in fields):
            raise ValueError("launched child identity is incomplete")
        return self


class AuthoritativeRunRecord(BaseModel):
    """The discovered Stage 2 run identity without copying its artifacts."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    run_directory: str
    substage_id: Identifier
    run_token: Identifier
    specification_sha256: Sha256
    baseline_commit: str
    started_at: str
    discovered_at: str


class AuthoritativeTerminalRecord(BaseModel):
    """The terminal authoritative result, recorded separately from Stage 4."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    run_directory: str
    status: WorkflowStatus
    pause_reason: str | None
    process_exit_code: int | None
    expected_exit_code: int
    result_sha256: Sha256
    journal_sha256: Sha256
    journal_head: Sha256
    journal_sequence: Annotated[int, Field(ge=1)]
    observed_at: str


class LiveShadowFailure(BaseModel):
    """One isolated shadow-side failure."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    decision_id: Identifier | None
    reason: BoundedString
    detail: BoundedString
    temporal_or_integrity: bool
    recorded_at: str


class LiveComparisonUnavailableRecord(BaseModel):
    """Immutable proof that a terminal authoritative action cannot be compared."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    decision_id: Identifier
    source_action_id: Identifier
    envelope_sha256: Sha256
    authoritative_run_directory: str
    authoritative_run_token: Identifier
    authoritative_substage_id: Identifier
    authoritative_status: WorkflowStatus
    authoritative_pause_reason: str | None
    authoritative_result_sha256: Sha256
    authoritative_journal_sha256: Sha256
    authoritative_journal_sequence: Annotated[int, Field(ge=1)]
    authoritative_journal_hash: Sha256
    reason: Literal["authoritative_action_unfinished_after_terminal"]
    recorded_at: str
    record_sha256: Sha256


class FailedSupervisorActionRecord(BaseModel):
    """Typed immutable finalization of an uncompleted or unlaunchable shadow action."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    action_id: Identifier
    proposal_id: Identifier
    proposal_kind: ProposalKind
    stage1_artifact_directory: str
    resume_session_id: SupervisorUUID | None
    prompt_sha256: Sha256
    blind_manifest_sha256: Sha256
    output_schema_sha256: Sha256
    reason: BoundedString
    detail: BoundedString
    finalized_at: str


class LiveShadowJournalEntry(BaseModel):
    """One strict hash-chained Stage 4 journal entry."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    sequence: Annotated[int, Field(ge=1)]
    event_type: Literal[
        "transition",
        "authoritative_launch",
        "authoritative_discovered",
        "authoritative_progress",
        "authoritative_terminal",
        "decision",
        "shadow_action_intent",
        "shadow_action_completion",
        "comparison",
        "review",
        "shadow_failure",
    ]
    previous_state: LiveShadowStatus | None
    new_state: LiveShadowStatus
    action_id: str | None
    decision_id: str | None
    timestamp: str
    reason: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_]{0,127}$")]
    artifact_hashes: dict[str, Sha256]
    state_updates: dict[str, Any]
    previous_hash: Sha256
    entry_hash: Sha256


class LiveShadowState(BaseModel):
    """Strict durable Stage 4 state snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    live_shadow_id: Identifier
    run_token: Identifier
    status: LiveShadowStatus
    specification_path: str
    specification_sha256: Sha256
    policy_path: str
    policy_sha256: Sha256
    context_hashes: Annotated[
        tuple[FrozenFileHash, ...], BeforeValidator(_freeze_sequence)
    ]
    stage2_specification_path: str
    stage2_specification_sha256: Sha256
    authoritative_run_directory: str | None
    authoritative_substage_id: Identifier
    authoritative_status: WorkflowStatus | None
    authoritative_pause_reason: str | None
    authoritative_result_sha256: Sha256 | None
    authoritative_process_exit_code: int | None
    authoritative_terminal_at: str | None
    supervisor_model: ModelName
    supervisor_reasoning_effort: ReasoningEffort
    supervisor_session_id: SupervisorUUID | None
    observed_decision_ids: StringTuple
    proposal_ids: StringTuple
    comparison_ids: StringTuple
    reviewed_proposal_ids: StringTuple
    disqualified_proposal_ids: StringTuple
    shadow_failures: Annotated[
        tuple[LiveShadowFailure, ...], BeforeValidator(_freeze_sequence)
    ]
    pending_action: PendingSupervisorAction | None
    journal_read_offset: Annotated[int, Field(ge=0)]
    authoritative_journal_sequence: Annotated[int, Field(ge=0)]
    authoritative_journal_hash: Sha256
    max_proposal_bytes: Annotated[
        int, Field(ge=MIN_PROPOSAL_BYTES, le=MAX_PROPOSAL_BYTES)
    ]
    minimum_reviewed_proposals: Annotated[int, Field(ge=1, le=100)]
    required_consecutive_acceptable: Annotated[int, Field(ge=1, le=100)]
    artifact_directory: str
    pause_reason: str | None
    summary: BoundedString
    journal_sequence: Annotated[int, Field(ge=0)]
    journal_hash: Sha256
    started_at: str
    updated_at: str


class LiveReadinessReport(BaseModel):
    """Informational-only readiness for a future supervised-handoff stage."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    live_shadow_id: Identifier
    status: LiveReadinessStatus
    informational_only: Literal[True] = True
    automation_enabled: Literal[False] = False
    proposal_count: Annotated[int, Field(ge=0)]
    comparable_proposal_count: Annotated[int, Field(ge=0)]
    reviewed_proposal_count: Annotated[int, Field(ge=0)]
    acceptable_review_count: Annotated[int, Field(ge=0)]
    unsafe_review_count: Annotated[int, Field(ge=0)]
    worse_review_count: Annotated[int, Field(ge=0)]
    reviewed_disqualification_count: Annotated[int, Field(ge=0)]
    consecutive_acceptable: Annotated[int, Field(ge=0)]
    worker_reviewed: bool
    auditor_reviewed: bool
    authoritative_completed: bool
    unresolved_integrity_or_temporal_failure: bool
    minimum_reviewed_proposals: Annotated[int, Field(ge=1, le=100)]
    required_consecutive_acceptable: Annotated[int, Field(ge=1, le=100)]
    reasons: StringTuple


class LiveShadowResult(BaseModel):
    """Stable public Stage 4 result mirrored exactly by durable state."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    live_shadow_id: Identifier
    run_token: Identifier
    status: LiveShadowStatus
    authoritative_stage2_specification: str
    authoritative_stage2_run: str | None
    authoritative_stage2_status: WorkflowStatus | None
    authoritative_pause_reason: str | None
    authoritative_result_sha256: Sha256 | None
    authoritative_process_exit_code: int | None
    supervisor_model: ModelName
    supervisor_reasoning_effort: ReasoningEffort
    supervisor_session_id: SupervisorUUID | None
    observed_decision_count: Annotated[int, Field(ge=0)]
    proposal_count: Annotated[int, Field(ge=0)]
    comparison_count: Annotated[int, Field(ge=0)]
    review_count: Annotated[int, Field(ge=0)]
    disqualification_count: Annotated[int, Field(ge=0)]
    shadow_failure_count: Annotated[int, Field(ge=0)]
    readiness: LiveReadinessStatus
    automation_enabled: Literal[False] = False
    artifact_directory: str
    pause_reason: str | None
    summary: BoundedString
    started_at: str
    updated_at: str

    def to_dict(self) -> dict[str, object]:
        return self.model_dump(mode="json")
