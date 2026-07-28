"""Strict Stage 5A replay campaign, action, state, and decision models."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from research_automation_supervisor.codex_models import ModelName, ReasoningEffort
from research_automation_supervisor.shadow_models import canonical_supervisor_uuid
from research_automation_supervisor.workflow_models import (
    Identifier,
    WorkflowTest,
    _freeze_sequence,
    normalize_path_pattern,
)

BoundedText = Annotated[str, Field(min_length=1, max_length=16_384)]
StringTuple = Annotated[tuple[str, ...], BeforeValidator(_freeze_sequence)]
ReplayCampaignStatus = Literal[
    "initialized",
    "running",
    "human_paused",
    "completed",
    "failed",
    "aborted",
]
ReplayPausedBoundary = Literal[
    "supervisor_worker_prompt",
    "supervisor_auditor_prompt",
    "supervisor_repair_prompt",
    "supervisor_finish",
    "worker_continuation",
    "auditor_escalation",
    "repair_limit",
]
ReplayTaskVerdict = Literal[
    "autonomous",
    "human_assisted",
    "passed",
    "gold_mismatch",
    "failed",
]
SupervisorActionName = Literal[
    "worker_prompt",
    "auditor_prompt",
    "repair_prompt",
    "finish",
    "human_pause",
]


class ProductionProfile(BaseModel):
    """Manifest-owned classification of production and validation paths."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    hot_path: StringTuple
    post_update: StringTuple
    validation_only: StringTuple

    @field_validator("hot_path", "post_update", "validation_only")
    @classmethod
    def normalize_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(normalize_path_pattern(item) for item in value)
        if len(set(normalized)) != len(normalized):
            raise ValueError("production-profile paths must be unique")
        return normalized


class ReplayTaskSpecification(BaseModel):
    """One fixed historical replay task in campaign order."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        str_strip_whitespace=True,
    )

    task_id: Identifier
    title: Annotated[str, Field(min_length=1, max_length=1024)]
    stage2_specification_path: Annotated[str, Field(min_length=1)]
    project_context_paths: Annotated[
        tuple[Annotated[str, Field(min_length=1)], ...],
        BeforeValidator(_freeze_sequence),
        Field(max_length=100),
    ] = ()
    gold_evaluations: Annotated[
        tuple[WorkflowTest, ...],
        BeforeValidator(_freeze_sequence),
        Field(min_length=1, max_length=100),
    ]
    gold_artifact_roots: Annotated[
        tuple[Annotated[str, Field(min_length=1)], ...],
        BeforeValidator(_freeze_sequence),
        Field(min_length=1, max_length=100),
    ]
    production_profile: ProductionProfile

    @model_validator(mode="after")
    def validate_unique_values(self) -> ReplayTaskSpecification:
        ids = [test.id for test in self.gold_evaluations]
        if len(ids) != len(set(ids)):
            raise ValueError("gold evaluation IDs must be unique")
        if len(self.project_context_paths) != len(set(self.project_context_paths)):
            raise ValueError("project-context paths must be unique")
        if len(self.gold_artifact_roots) != len(set(self.gold_artifact_roots)):
            raise ValueError("gold artifact roots must be unique")
        return self


class ReplayCampaignSpecification(BaseModel):
    """The exact schema-version-1 Stage 5A campaign manifest."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        str_strip_whitespace=True,
    )

    schema_version: Literal[1]
    campaign_id: Identifier
    title: Annotated[str, Field(min_length=1, max_length=1024)]
    supervisor_policy_path: Annotated[str, Field(min_length=1)]
    supervisor_model: ModelName
    supervisor_reasoning_effort: ReasoningEffort
    supervisor_timeout_seconds: Annotated[int, Field(ge=30, le=14_400)]
    tasks: Annotated[
        tuple[ReplayTaskSpecification, ...],
        BeforeValidator(_freeze_sequence),
        Field(min_length=1, max_length=100),
    ]

    @model_validator(mode="after")
    def validate_task_ids(self) -> ReplayCampaignSpecification:
        identifiers = [task.task_id for task in self.tasks]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("replay task IDs must be unique")
        return self


class SupervisorAction(BaseModel):
    """Strict supervisor output at every Stage 2 prompt/action boundary."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        str_strip_whitespace=True,
    )

    schema_version: Literal[1]
    action: SupervisorActionName
    prompt: str
    summary: BoundedText
    referenced_paths: Annotated[
        tuple[str, ...], BeforeValidator(_freeze_sequence), Field(max_length=200)
    ]
    required_checks: Annotated[
        tuple[BoundedText, ...], BeforeValidator(_freeze_sequence), Field(max_length=200)
    ]
    assumptions: Annotated[
        tuple[BoundedText, ...], BeforeValidator(_freeze_sequence), Field(max_length=200)
    ]
    questions: Annotated[
        tuple[BoundedText, ...], BeforeValidator(_freeze_sequence), Field(max_length=200)
    ]
    contract_change_requested: bool
    scope_expansion_requested: bool
    permission_change_requested: bool
    acceptance_change_requested: bool
    convention_change_requested: bool

    @field_validator("referenced_paths")
    @classmethod
    def normalize_referenced_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(normalize_path_pattern(item) for item in value)
        if len(normalized) != len(set(normalized)):
            raise ValueError("referenced paths must be unique")
        return normalized

    @model_validator(mode="after")
    def validate_prompt_shape(self) -> SupervisorAction:
        prompt_action = self.action in {
            "worker_prompt",
            "auditor_prompt",
            "repair_prompt",
        }
        if prompt_action != bool(self.prompt.strip()):
            raise ValueError("only prompt actions must contain nonempty prompt text")
        return self

    @property
    def requests_authority_change(self) -> bool:
        return any(
            (
                self.contract_change_requested,
                self.scope_expansion_requested,
                self.permission_change_requested,
                self.acceptance_change_requested,
                self.convention_change_requested,
            )
        )


class HumanReplayDecision(BaseModel):
    """One immutable operator decision for a paused campaign."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        str_strip_whitespace=True,
    )

    schema_version: Literal[1]
    decision: Literal["resume", "abort"]
    note: Annotated[str, Field(max_length=16_384)]


class PendingHumanDecision(BaseModel):
    """Prepared human-decision identity between durable intent and completion."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    index: Annotated[int, Field(ge=0)]
    task_id: Identifier
    decision: Literal["resume", "abort"]
    prepared_path: str
    destination_path: str
    note_path: str
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    note_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class ReplayCampaignState(BaseModel):
    """Thin durable Stage 5A campaign snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    campaign_id: Identifier
    run_token: Identifier
    specification_path: str
    specification_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    current_task_index: Annotated[int, Field(ge=0)]
    completed_task_ids: Annotated[
        tuple[Identifier, ...], BeforeValidator(_freeze_sequence)
    ]
    current_task_run: str | None
    supervisor_session_id: Annotated[str, Field(max_length=256)] | None
    task_worker_session_ids: dict[str, str]
    human_assisted_task_ids: Annotated[
        tuple[Identifier, ...], BeforeValidator(_freeze_sequence)
    ]
    human_decision_count: Annotated[int, Field(ge=0)] = 0
    pending_human_decision: PendingHumanDecision | None = None
    continuation_note_path: str | None = None
    paused_boundary: ReplayPausedBoundary | None = None
    model_terminal_task_ids: Annotated[
        tuple[Identifier, ...], BeforeValidator(_freeze_sequence)
    ] = ()
    gold_evaluated_task_ids: Annotated[
        tuple[Identifier, ...], BeforeValidator(_freeze_sequence)
    ] = ()
    gold_reveal_model_turn_count: Annotated[int, Field(ge=0)] | None = None
    post_gold_model_turn_count: Annotated[int, Field(ge=0)] | None = None
    status: ReplayCampaignStatus
    pause_reason: str | None
    journal_sequence: Annotated[int, Field(ge=0)]
    journal_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    started_at: str
    updated_at: str

    @field_validator("supervisor_session_id")
    @classmethod
    def validate_supervisor_uuid(cls, value: str | None) -> str | None:
        return None if value is None else canonical_supervisor_uuid(value)

    @field_validator("task_worker_session_ids")
    @classmethod
    def validate_worker_uuids(cls, value: dict[str, str]) -> dict[str, str]:
        for identifier in value.values():
            canonical_supervisor_uuid(identifier)
        return value

    def to_dict(self) -> dict[str, object]:
        return self.model_dump(mode="json")
