"""Strict immutable models for retrospective Stage 3 shadow calibration."""

from __future__ import annotations

import posixpath
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from research_automation_supervisor.codex_models import (
    CodexRunResult,
    ModelName,
    ReasoningEffort,
)
from research_automation_supervisor.workflow_models import (
    Identifier,
    _freeze_sequence,
    normalize_relative_path,
)

MAX_SHADOW_STRING_BYTES = 16 * 1024
MAX_PROPOSAL_BYTES = 2 * 1024 * 1024
MIN_PROPOSAL_BYTES = 1024
MAX_LIST_ITEMS = 200

ProposalKind = Literal[
    "worker_initial",
    "worker_scope_repair",
    "worker_test_repair",
    "worker_audit_repair",
    "worker_human_continuation",
    "auditor",
]
ShadowStatus = Literal[
    "initialized",
    "reconstructing",
    "supervisor_running",
    "proposal_validating",
    "awaiting_reviews",
    "completed",
    "human_paused",
    "failed",
    "aborted",
]
ReadinessStatus = Literal[
    "insufficient_data",
    "not_ready",
    "candidate_ready_for_live_shadow",
]


def _sanitize_string(value: str) -> str:
    normalized = "".join(
        character
        for character in value
        if character in {"\n", "\t"} or ord(character) >= 32
    ).strip()
    if not normalized:
        raise ValueError("structured strings must not be empty after sanitization")
    if len(normalized.encode("utf-8")) > MAX_SHADOW_STRING_BYTES:
        raise ValueError("structured string exceeds its UTF-8 byte limit")
    return normalized


def _sanitize_optional_string(value: str) -> str:
    return _sanitize_string(value)


def _sanitize_prompt(value: str) -> str:
    normalized = "".join(
        character
        for character in value
        if character in {"\n", "\t"} or ord(character) >= 32
    ).strip()
    if not normalized:
        raise ValueError(
            "proposal prompt must not be empty after sanitization"
        )
    if len(normalized.encode("utf-8")) > MAX_PROPOSAL_BYTES:
        raise ValueError("proposal prompt exceeds its UTF-8 byte limit")
    return normalized


def _sanitize_text(value: str) -> str:
    normalized = "".join(
        character
        for character in value
        if character in {"\n", "\t"} or ord(character) >= 32
    ).strip()
    if len(normalized.encode("utf-8")) > MAX_SHADOW_STRING_BYTES:
        raise ValueError("structured text exceeds its UTF-8 byte limit")
    return normalized


def canonical_supervisor_uuid(value: str) -> str:
    """Require exact lowercase hyphenated non-nil UUID text."""
    try:
        parsed = UUID(value)
    except (AttributeError, ValueError) as exc:
        raise ValueError(
            "supervisor session identity must be a canonical UUID"
        ) from exc
    if parsed.int == 0 or str(parsed) != value:
        raise ValueError(
            "supervisor session identity must be a canonical non-nil UUID"
        )
    return value


BoundedString = Annotated[
    str,
    AfterValidator(_sanitize_string),
    Field(min_length=1, max_length=MAX_SHADOW_STRING_BYTES),
]
SanitizedText = Annotated[
    str,
    AfterValidator(_sanitize_text),
    Field(max_length=MAX_SHADOW_STRING_BYTES),
]
OptionalBoundedString = Annotated[
    str,
    AfterValidator(_sanitize_optional_string),
    Field(min_length=1, max_length=MAX_SHADOW_STRING_BYTES),
]
StringTuple = Annotated[
    tuple[BoundedString, ...],
    BeforeValidator(_freeze_sequence),
    Field(max_length=MAX_LIST_ITEMS),
]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
SupervisorUUID = Annotated[str, AfterValidator(canonical_supervisor_uuid)]


class ShadowSpecification(BaseModel):
    """The exact frozen schema-version-1 Stage 3 input."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        str_strip_whitespace=True,
    )

    schema_version: Literal[1]
    calibration_id: Identifier
    title: Annotated[str, Field(min_length=1, max_length=1024)]
    source_stage2_run: Annotated[str, Field(min_length=1)]
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
        int,
        Field(ge=MIN_PROPOSAL_BYTES, le=MAX_PROPOSAL_BYTES),
    ]
    minimum_reviewed_proposals: Annotated[int, Field(ge=1, le=100)]
    required_consecutive_acceptable: Annotated[int, Field(ge=1, le=100)]

    @model_validator(mode="after")
    def validate_thresholds_and_contexts(self) -> ShadowSpecification:
        if (
            self.required_consecutive_acceptable
            > self.minimum_reviewed_proposals
        ):
            raise ValueError(
                "required_consecutive_acceptable must not exceed "
                "minimum_reviewed_proposals"
            )
        if len(set(self.project_context_paths)) != len(
            self.project_context_paths
        ):
            raise ValueError("project_context_paths contains a duplicate")
        return self


class FrozenFileHash(BaseModel):
    """One ordered frozen human-written input."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    path: str
    sha256: Sha256
    byte_count: Annotated[int, Field(ge=1)]


class DecisionPoint(BaseModel):
    """One verified Stage 2 Codex decision point, without comparison material."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    decision_id: Identifier
    proposal_kind: ProposalKind
    source_action_id: Identifier
    repair_round: Annotated[int, Field(ge=0)]
    ordinal: Annotated[int, Field(ge=1)]
    journal_sequence: Annotated[int, Field(ge=1)]
    evidence_sha256: Sha256
    comparison_available: bool
    comparison_unavailable_reason: str | None

    @model_validator(mode="after")
    def validate_availability(self) -> DecisionPoint:
        if self.comparison_available == (
            self.comparison_unavailable_reason is not None
        ):
            raise ValueError(
                "comparison availability and unavailable reason contradict"
            )
        return self


class SupervisorProposal(BaseModel):
    """Strict structured advisory output from the blind supervisor."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        str_strip_whitespace=True,
    )

    schema_version: Literal[1]
    proposal_kind: ProposalKind
    disposition: Literal["propose", "recommend_human_pause"]
    prompt: Annotated[
        str,
        AfterValidator(_sanitize_prompt),
        Field(min_length=1, max_length=MAX_PROPOSAL_BYTES),
    ] | None
    summary: BoundedString
    referenced_paths: Annotated[
        tuple[str, ...],
        BeforeValidator(_freeze_sequence),
        Field(max_length=MAX_LIST_ITEMS),
    ]
    required_checks: StringTuple
    assumptions: StringTuple
    questions: StringTuple
    contract_change_requested: bool
    scope_expansion_requested: bool
    permission_change_requested: bool
    acceptance_change_requested: bool
    convention_change_requested: bool

    @field_validator("referenced_paths")
    @classmethod
    def normalize_referenced_paths(
        cls, value: tuple[str, ...]
    ) -> tuple[str, ...]:
        normalized = tuple(normalize_relative_path(item) for item in value)
        if any(
            len(item.encode("utf-8")) > MAX_SHADOW_STRING_BYTES
            for item in normalized
        ):
            raise ValueError(
                "referenced_paths contains a path over the byte limit"
            )
        if len(set(normalized)) != len(normalized):
            raise ValueError(
                "referenced_paths contains duplicate normalized paths"
            )
        return normalized

    @field_validator("prompt")
    @classmethod
    def validate_prompt_bytes(cls, value: str | None) -> str | None:
        if (
            value is not None
            and len(value.encode("utf-8")) > MAX_PROPOSAL_BYTES
        ):
            raise ValueError("prompt exceeds its UTF-8 byte limit")
        return value

    @field_validator("required_checks")
    @classmethod
    def validate_required_checks(
        cls, value: tuple[str, ...]
    ) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("required_checks contains duplicates")
        return value

    @model_validator(mode="after")
    def validate_disposition(self) -> SupervisorProposal:
        if self.disposition == "propose" and self.prompt is None:
            raise ValueError("propose requires a nonempty prompt")
        if self.disposition == "recommend_human_pause" and (
            self.prompt is not None or not self.questions
        ):
            raise ValueError(
                "recommend_human_pause requires a null prompt and a question"
            )
        return self


class BlindInputManifest(BaseModel):
    """Hash-only proof for one in-memory blind input."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    proposal_id: Identifier
    proposal_kind: ProposalKind
    policy_sha256: Sha256
    context_files: Annotated[
        tuple[FrozenFileHash, ...],
        BeforeValidator(_freeze_sequence),
    ]
    contract_sha256: Sha256
    source_summary_sha256: Sha256
    evidence_sha256: Sha256
    output_schema_sha256: Sha256
    rendered_blind_input_sha256: Sha256
    rendered_blind_input_byte_count: Annotated[int, Field(ge=1)]
    authoritative_sentinel_absent: Literal[True]
    shadow_only: Literal[True]
    automatic_send_disabled: Literal[True]


class PathScopeFinding(BaseModel):
    """One deterministic Stage 2 scope finding in a proposal path."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    path: str
    reason: Literal["outside_allowed_paths", "protected_path"]


class RequiredCheckCoverage(BaseModel):
    """Exact-ID coverage against frozen Stage 2 acceptance tests."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    required_test_ids: Annotated[
        tuple[str, ...], BeforeValidator(_freeze_sequence)
    ]
    covered_test_ids: Annotated[
        tuple[str, ...], BeforeValidator(_freeze_sequence)
    ]
    missing_test_ids: Annotated[
        tuple[str, ...], BeforeValidator(_freeze_sequence)
    ]


class DeterministicAssessment(BaseModel):
    """Non-semantic deterministic assessment of one supervisor result."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    proposal_id: Identifier
    proposal_kind: ProposalKind
    schema_integrity: bool
    blind_input_integrity: bool
    session_integrity: bool
    size_compliant: bool
    proposal_byte_count: Annotated[int, Field(ge=0)]
    change_flags: dict[str, bool]
    path_scope_findings: Annotated[
        tuple[PathScopeFinding, ...],
        BeforeValidator(_freeze_sequence),
    ]
    required_check_coverage: RequiredCheckCoverage
    disposition: Literal[
        "propose", "recommend_human_pause", "malformed"
    ]
    disqualified: bool
    disqualification_reasons: Annotated[
        tuple[str, ...], BeforeValidator(_freeze_sequence)
    ]
    candidate_sha256: Sha256 | None
    candidate_byte_count: Annotated[int, Field(ge=0)]
    authoritative_source_sha256: Sha256 | None
    authoritative_source_byte_count: Annotated[int, Field(ge=0)] | None
    authoritative_rendered_sha256: Sha256 | None
    authoritative_rendered_byte_count: Annotated[int, Field(ge=0)] | None
    comparison_available: bool
    review_status: Literal["unreviewed"]


class ProposalComparison(BaseModel):
    """Hash and availability record written only after proposal finalization."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    proposal_id: Identifier
    proposal_kind: ProposalKind
    source_stage2_action_id: Identifier
    candidate_sha256: Sha256 | None
    candidate_byte_count: Annotated[int, Field(ge=0)]
    authoritative_source_sha256: Sha256 | None
    authoritative_source_byte_count: Annotated[int, Field(ge=0)] | None
    authoritative_rendered_sha256: Sha256 | None
    authoritative_rendered_byte_count: Annotated[int, Field(ge=0)] | None
    comparison_available: bool
    comparison_unavailable_reason: str | None


Score = Annotated[int, Field(ge=1, le=5)]


class HumanReview(BaseModel):
    """One immutable structured semantic human review."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        str_strip_whitespace=True,
    )

    schema_version: Literal[1]
    proposal_id: Identifier
    verdict: Literal["better", "equivalent", "worse", "unsafe"]
    objective_fidelity: Score
    scope_discipline: Score
    technical_completeness: Score
    evidence_use: Score
    actionability: Score
    concision: Score
    unsupported_assumptions: StringTuple
    blocking_issues: StringTuple
    notes: SanitizedText

    @model_validator(mode="after")
    def validate_unsafe(self) -> HumanReview:
        if self.verdict == "unsafe" and not self.blocking_issues:
            raise ValueError("unsafe requires at least one blocking issue")
        return self


class ReviewEvaluation(BaseModel):
    """Deterministic acceptability derived from an immutable human review."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    proposal_id: Identifier
    verdict: Literal["better", "equivalent", "worse", "unsafe"]
    deterministic_disqualification: bool
    acceptable: bool


class ReadinessReport(BaseModel):
    """Informational-only Stage 3 readiness result."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    calibration_id: Identifier
    status: ReadinessStatus
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
    minimum_reviewed_proposals: Annotated[int, Field(ge=1, le=100)]
    required_consecutive_acceptable: Annotated[int, Field(ge=1, le=100)]
    reasons: Annotated[
        tuple[str, ...], BeforeValidator(_freeze_sequence)
    ]


class PendingSupervisorAction(BaseModel):
    """Durable exact-ID intent preceding one supervisor launch."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    action_id: Identifier
    proposal_id: Identifier
    proposal_kind: ProposalKind
    decision_index: Annotated[int, Field(ge=0)]
    stage1_artifact_directory: str
    workspace: str
    role: Literal["supervisor"]
    model: ModelName
    reasoning_effort: ReasoningEffort
    timeout_seconds: Annotated[int, Field(ge=30, le=14_400)]
    sandbox: Literal["read-only"]
    approval_policy: Literal["never"]
    ephemeral: Literal[False]
    network_policy: Literal["disabled"]
    codex_executable: str
    prompt_sha256: Sha256
    prompt_byte_count: Annotated[int, Field(ge=1)]
    output_schema_path: str
    output_schema_sha256: Sha256
    blind_manifest_path: str
    blind_manifest_sha256: Sha256
    resume_session_id: SupervisorUUID | None
    removed_environment_variable_names: Annotated[
        tuple[str, ...], BeforeValidator(_freeze_sequence)
    ]
    started_at: str

    @model_validator(mode="after")
    def validate_identity(self) -> PendingSupervisorAction:
        if self.action_id != f"supervisor-{self.proposal_id}":
            raise ValueError("supervisor action ID is not deterministic")
        return self


class SupervisorActionRecord(BaseModel):
    """Final proof record for a completed external supervisor action."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    action_id: Identifier
    proposal_id: Identifier
    proposal_kind: ProposalKind
    complete: Literal[True]
    stage1_artifact_directory: str
    adapter_result: CodexRunResult
    session_ids: Annotated[
        tuple[str, ...], BeforeValidator(_freeze_sequence)
    ]
    structured_result_valid: bool
    artifact_hashes: dict[str, Sha256]


class ShadowJournalEntry(BaseModel):
    """One strict hash-chained Stage 3 journal record."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    sequence: Annotated[int, Field(ge=1)]
    event_type: Literal[
        "transition", "action_intent", "action_completion", "review"
    ]
    previous_state: ShadowStatus | None
    new_state: ShadowStatus
    action_id: str | None
    proposal_id: str | None
    timestamp: str
    reason: Annotated[
        str, Field(pattern=r"^[a-z0-9][a-z0-9_]{0,127}$")
    ]
    artifact_hashes: dict[str, Sha256]
    state_updates: dict[str, Any]
    previous_hash: Sha256
    entry_hash: Sha256


class ShadowState(BaseModel):
    """Strict durable Stage 3 state snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    calibration_id: Identifier
    run_token: Identifier
    status: ShadowStatus
    shadow_specification_path: str
    shadow_specification_sha256: Sha256
    policy_path: str
    policy_sha256: Sha256
    context_hashes: Annotated[
        tuple[FrozenFileHash, ...],
        BeforeValidator(_freeze_sequence),
    ]
    source_stage2_run: str
    source_stage2_state_sha256: Sha256
    source_stage2_journal_sha256: Sha256
    source_substage_id: Identifier
    supervisor_model: ModelName
    supervisor_reasoning_effort: ReasoningEffort
    supervisor_session_id: SupervisorUUID | None
    decision_count: Annotated[int, Field(ge=0)]
    current_decision_index: Annotated[int, Field(ge=0)]
    completed_action_ids: Annotated[
        tuple[str, ...], BeforeValidator(_freeze_sequence)
    ]
    proposal_ids: Annotated[
        tuple[str, ...], BeforeValidator(_freeze_sequence)
    ]
    reviewed_proposal_ids: Annotated[
        tuple[str, ...], BeforeValidator(_freeze_sequence)
    ]
    pending_action: PendingSupervisorAction | None
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

    def to_result(
        self,
        *,
        comparison_count: int,
        review_count: int,
        disqualification_count: int,
        readiness: ReadinessStatus,
    ) -> ShadowResult:
        return ShadowResult(
            calibration_id=self.calibration_id,
            source_stage2_run=self.source_stage2_run,
            source_substage_id=self.source_substage_id,
            status=self.status,
            supervisor_session_id=self.supervisor_session_id,
            supervisor_model=self.supervisor_model,
            proposal_count=len(self.proposal_ids),
            comparison_count=comparison_count,
            review_count=review_count,
            disqualification_count=disqualification_count,
            readiness=readiness,
            artifact_directory=self.artifact_directory,
            pause_reason=self.pause_reason,
            summary=self.summary,
            started_at=self.started_at,
            updated_at=self.updated_at,
        )


class ShadowResult(BaseModel):
    """Stable public Stage 3 result mirrored by state and CLI output."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    calibration_id: Identifier
    source_stage2_run: str
    source_substage_id: Identifier
    status: ShadowStatus
    supervisor_session_id: SupervisorUUID | None
    supervisor_model: ModelName
    proposal_count: Annotated[int, Field(ge=0)]
    comparison_count: Annotated[int, Field(ge=0)]
    review_count: Annotated[int, Field(ge=0)]
    disqualification_count: Annotated[int, Field(ge=0)]
    readiness: ReadinessStatus
    artifact_directory: str
    pause_reason: str | None
    summary: BoundedString
    started_at: str
    updated_at: str

    def to_dict(self) -> dict[str, object]:
        return self.model_dump(mode="json")


def normalize_source_locator(value: str) -> str:
    """Normalize a relative YAML locator without accepting traversal."""
    stripped = value.strip().replace("\\", "/")
    if not stripped:
        raise ValueError("locator must not be empty")
    normalized = posixpath.normpath(stripped)
    if normalized in {"", "."}:
        raise ValueError("locator must identify a path")
    return normalized
