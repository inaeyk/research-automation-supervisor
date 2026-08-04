"""Versioned PA-4 models for physics-enabled single-substage workflows.

These models are intentionally disjoint from the frozen 0.2.0 workflow models.
Schema-version-1 specifications, states, results, pending actions, and journals are
never parsed through this module.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal, TypeAlias, cast

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from research_automation_supervisor.durable_state import canonical_json
from research_automation_supervisor.physics_auditor_models import (
    PhysicsAuditorExecutionConfigV1,
)
from research_automation_supervisor.physics_models import (
    PhysicsAuditPolicyV1,
    PhysicsTaskContractV1,
)
from research_automation_supervisor.physics_oracle_models import PhysicsOracleCatalogV1
from research_automation_supervisor.workflow_models import (
    BoundedString,
    Identifier,
    PreparedSubstage,
    RequiredString,
    SubstageSpecification,
    _freeze_sequence,
)

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]

PhysicsWorkflowStatusV2: TypeAlias = Literal[
    "initialized",
    "software_running",
    "physics_oracles_running",
    "physics_auditor_running",
    "physics_repair_pending",
    "human_review_paused",
    "evidence_paused",
    "infrastructure_stopped",
    "repair_limit_paused",
    "checkpoint_paused",
    "completed",
    "failed",
    "aborted",
]

PHYSICS_TERMINAL_STATUSES_V2 = frozenset(
    {"infrastructure_stopped", "checkpoint_paused", "completed", "failed", "aborted"}
)
PHYSICS_PAUSED_STATUSES_V2 = frozenset(
    {"human_review_paused", "evidence_paused", "repair_limit_paused"}
)
PHYSICS_ACTIVE_STATUSES_V2 = frozenset(
    {
        "initialized",
        "software_running",
        "physics_oracles_running",
        "physics_auditor_running",
        "physics_repair_pending",
    }
)

HumanReviewTriggerV1: TypeAlias = Literal[
    "convention_change",
    "unresolved_gauge_constraint_ambiguity",
    "new_physical_interpretation",
    "conflicting_evidence",
    "contract_weakening_attempt",
]

MANDATORY_HUMAN_REVIEW_TRIGGERS_V1 = (
    "conflicting_evidence",
    "contract_weakening_attempt",
    "convention_change",
    "new_physical_interpretation",
    "unresolved_gauge_constraint_ambiguity",
)


class PhysicsWorkflowConfigurationV1(BaseModel):
    """Operator-owned PA-4 workflow policy, separate from PA-1/2/3 proofs."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, str_strip_whitespace=True)

    schema_version: Literal[1]
    enabled: bool
    required: bool
    trusted_oracle_catalog_path: RequiredString
    auditor_execution_config_path: RequiredString
    max_repair_rounds: Annotated[int, Field(ge=0, le=10)] | None = None
    human_review_triggers: Annotated[
        tuple[HumanReviewTriggerV1, ...],
        BeforeValidator(_freeze_sequence),
        Field(min_length=5, max_length=5),
    ] = cast(tuple[HumanReviewTriggerV1, ...], MANDATORY_HUMAN_REVIEW_TRIGGERS_V1)
    insufficient_evidence_policy: Literal["block", "human_review"] = "block"
    conflicting_evidence_policy: Literal["block", "human_review"] = "human_review"
    medium_finding_policy: Literal["request_repair", "require_human_review", "allow_pass"] = (
        "request_repair"
    )
    low_finding_policy: Literal["request_repair", "require_human_review", "allow_pass"] = (
        "allow_pass"
    )

    @field_validator("human_review_triggers")
    @classmethod
    def validate_human_triggers(
        cls, value: tuple[HumanReviewTriggerV1, ...]
    ) -> tuple[HumanReviewTriggerV1, ...]:
        if len(set(value)) != len(value) or set(value) != set(MANDATORY_HUMAN_REVIEW_TRIGGERS_V1):
            raise ValueError("all five PA-4 human-review triggers are mandatory")
        return tuple(sorted(value))

    @model_validator(mode="after")
    def validate_enablement(self) -> PhysicsWorkflowConfigurationV1:
        if self.required and not self.enabled:
            raise ValueError("required physics auditing cannot be disabled")
        return self

    def routing_policy(self) -> PhysicsAuditPolicyV1:
        """Translate workflow spelling to the unchanged PA-1 policy model."""
        return PhysicsAuditPolicyV1(
            schema_version=1,
            insufficient_required_evidence=self.insufficient_evidence_policy,
            conflicting_required_evidence=self.conflicting_evidence_policy,
            medium_severity=self.medium_finding_policy,
            low_severity=self.low_finding_policy,
            informational_severity="allow_pass",
        )


class PhysicsSubstageSpecificationV2(SubstageSpecification):
    """Explicit physics opt-in without changing the frozen v1 specification."""

    schema_version: Literal[2]  # type: ignore[assignment]
    physics_contract_path: RequiredString
    physics: PhysicsWorkflowConfigurationV1

    @model_validator(mode="after")
    def validate_physics_policy(self) -> PhysicsSubstageSpecificationV2:
        if not self.physics.enabled or not self.physics.required:
            raise ValueError(
                "schema-version-2 substages require enabled and required physics auditing"
            )
        if (
            self.physics.max_repair_rounds is not None
            and self.physics.max_repair_rounds > self.max_repair_rounds
        ):
            raise ValueError("physics max_repair_rounds cannot exceed the shared workflow bound")
        return self

    def software_specification(self) -> SubstageSpecification:
        value = self.model_dump(mode="json", exclude={"physics_contract_path", "physics"})
        value["schema_version"] = 1
        return SubstageSpecification.model_validate(value)


@dataclass(frozen=True)
class PreparedPhysicsSubstageV2:
    specification_locator_path: Path
    specification_path: Path
    specification_bytes: bytes
    specification_sha256: str
    specification: PhysicsSubstageSpecificationV2
    software_prepared: PreparedSubstage
    physics_contract_path: Path
    physics_contract_bytes: bytes
    physics_contract_sha256: str
    physics_contract: PhysicsTaskContractV1
    oracle_catalog_path: Path
    oracle_catalog_bytes: bytes
    oracle_catalog_sha256: str
    oracle_catalog: PhysicsOracleCatalogV1
    auditor_config_path: Path
    auditor_config_bytes: bytes
    auditor_config_sha256: str
    auditor_config: PhysicsAuditorExecutionConfigV1

    @property
    def workspace(self) -> Path:
        return self.software_prepared.workspace

    @property
    def repository_root(self) -> Path:
        return self.software_prepared.repository_root

    @property
    def acceptance_tests(self) -> tuple[Any, ...]:
        return self.software_prepared.acceptance_tests

    @property
    def effective_max_repair_rounds(self) -> int:
        configured = self.specification.physics.max_repair_rounds
        return self.specification.max_repair_rounds if configured is None else configured


class PhysicsOracleEvidenceRecordV2(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    oracle_id: Identifier
    repair_round: Annotated[int, Field(ge=0, le=10)]
    output_directory: RequiredString
    result_sha256: Sha256
    completion_proof_sha256: Sha256
    workspace_identity_sha256: Sha256
    status: Literal[
        "passed",
        "functional_failure",
        "timed_out",
        "infrastructure_failure",
        "workspace_integrity_failure",
        "output_contract_failure",
        "cancelled",
        "indeterminate_recovery",
    ]


class PhysicsWorkflowResultV2(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[2] = 2
    substage_id: Identifier
    run_token: Identifier
    status: PhysicsWorkflowStatusV2
    repair_round: Annotated[int, Field(ge=0, le=10)]
    max_repair_rounds: Annotated[int, Field(ge=0, le=10)]
    checkpoint_after: bool
    workspace: RequiredString
    baseline_commit: RequiredString
    worker_thread_id: Annotated[str, Field(max_length=512)] | None
    tests_passed: bool
    code_auditor_passed: bool
    required_oracle_proofs_verified: bool
    physics_route: (
        Literal[
            "pass",
            "request_repair",
            "require_human_review",
            "block_insufficient_evidence",
            "infrastructure_failure",
        ]
        | None
    )
    final_workspace_identity_sha256: Sha256 | None
    artifact_directory: RequiredString
    pause_reason: Annotated[str, Field(max_length=16_384)] | None
    summary: BoundedString
    started_at: RequiredString
    updated_at: RequiredString

    def to_dict(self) -> dict[str, object]:
        return self.model_dump(mode="json")


class PhysicsWorkflowStateV2(BaseModel):
    """Strict schema-v2 state snapshot; no v1 status is reinterpreted."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[2] = 2
    substage_id: Identifier
    run_token: Identifier
    status: PhysicsWorkflowStatusV2
    repair_round: Annotated[int, Field(ge=0, le=10)]
    max_repair_rounds: Annotated[int, Field(ge=0, le=10)]
    checkpoint_after: bool
    specification_path: RequiredString
    specification_sha256: Sha256
    software_specification_path: RequiredString
    software_run_directory: RequiredString | None
    physics_contract_path: RequiredString
    physics_contract_sha256: Sha256
    oracle_catalog_path: RequiredString
    oracle_catalog_sha256: Sha256
    auditor_config_path: RequiredString
    auditor_config_sha256: Sha256
    workspace: RequiredString
    repository_root: RequiredString
    baseline_commit: RequiredString
    baseline_branch: str | None
    worker_thread_id: Annotated[str, Field(max_length=512)] | None
    latest_software_result_path: RequiredString | None
    tests_passed: bool
    code_auditor_passed: bool
    required_oracle_proofs_verified: bool
    oracle_evidence: tuple[PhysicsOracleEvidenceRecordV2, ...]
    historical_oracle_evidence: tuple[PhysicsOracleEvidenceRecordV2, ...]
    invalidated_oracle_ids: tuple[Identifier, ...]
    preserved_oracle_ids: tuple[Identifier, ...]
    current_workspace_identity_sha256: Sha256 | None
    accepted_workspace_identity_sha256: Sha256 | None
    physics_auditor_action_directory: RequiredString | None
    physics_auditor_result_sha256: Sha256 | None
    physics_auditor_proof_sha256: Sha256 | None
    physics_report_sha256: Sha256 | None
    physics_routing_sha256: Sha256 | None
    physics_route: (
        Literal[
            "pass",
            "request_repair",
            "require_human_review",
            "block_insufficient_evidence",
            "infrastructure_failure",
        ]
        | None
    )
    physics_reason_codes: tuple[Identifier, ...]
    prior_physics_auditor_thread_ids: tuple[str, ...]
    repair_prompt_path: RequiredString | None
    repair_prompt_sha256: Sha256 | None
    repair_prompt_consumed: bool
    human_review_packet_path: RequiredString | None
    human_review_packet_sha256: Sha256 | None
    human_decision_path: RequiredString | None
    human_decision_sha256: Sha256 | None
    pause_reason: Annotated[str, Field(max_length=16_384)] | None
    summary: BoundedString
    artifact_directory: RequiredString
    journal_sequence: Annotated[int, Field(ge=0)]
    journal_hash: Sha256
    started_at: RequiredString
    updated_at: RequiredString

    @field_validator(
        "oracle_evidence",
        "historical_oracle_evidence",
        "invalidated_oracle_ids",
        "preserved_oracle_ids",
        "physics_reason_codes",
        "prior_physics_auditor_thread_ids",
        mode="before",
    )
    @classmethod
    def freeze_state_collections(cls, value: Any) -> Any:
        return _freeze_sequence(value)

    def to_result(self) -> PhysicsWorkflowResultV2:
        return PhysicsWorkflowResultV2(
            substage_id=self.substage_id,
            run_token=self.run_token,
            status=self.status,
            repair_round=self.repair_round,
            max_repair_rounds=self.max_repair_rounds,
            checkpoint_after=self.checkpoint_after,
            workspace=self.workspace,
            baseline_commit=self.baseline_commit,
            worker_thread_id=self.worker_thread_id,
            tests_passed=self.tests_passed,
            code_auditor_passed=self.code_auditor_passed,
            required_oracle_proofs_verified=self.required_oracle_proofs_verified,
            physics_route=self.physics_route,
            final_workspace_identity_sha256=self.accepted_workspace_identity_sha256,
            artifact_directory=self.artifact_directory,
            pause_reason=self.pause_reason,
            summary=self.summary,
            started_at=self.started_at,
            updated_at=self.updated_at,
        )


PhysicsJournalEventTypeV2: TypeAlias = Literal[
    "transition", "action_intent", "action_completion", "evidence", "human_decision"
]


class PhysicsWorkflowJournalEntryV2(BaseModel):
    """One immutable, self-hashed schema-v2 orchestration record."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[2] = 2
    sequence: Annotated[int, Field(ge=1)]
    event_type: PhysicsJournalEventTypeV2
    previous_state: PhysicsWorkflowStatusV2 | None
    new_state: PhysicsWorkflowStatusV2
    action_id: Identifier | None
    action_kind: Literal["software", "physics_oracle", "physics_auditor"] | None
    timestamp: RequiredString
    reason: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_]{0,127}$")]
    artifact_hashes: dict[str, Sha256]
    state_updates: dict[str, Any]
    previous_hash: Sha256
    entry_hash: Sha256

    @model_validator(mode="after")
    def validate_entry(self) -> PhysicsWorkflowJournalEntryV2:
        is_action = self.event_type in {"action_intent", "action_completion"}
        if is_action != (self.action_id is not None and self.action_kind is not None):
            raise ValueError("physics workflow action journal identity is invalid")
        payload = self.model_dump(mode="json", exclude={"entry_hash"})
        expected = hashlib.sha256(canonical_json(payload)).hexdigest()
        if self.entry_hash != expected:
            raise ValueError("physics workflow journal entry hash is invalid")
        return self


PhysicsReviewDecisionNameV1: TypeAlias = Literal[
    "approve_existing_contract",
    "revise_contract",
    "request_additional_evidence",
    "accept_with_caveat",
    "reject_candidate",
]


class PhysicsHumanReviewPacketV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    run_token: Identifier
    substage_id: Identifier
    repair_round: Annotated[int, Field(ge=0, le=10)]
    reason_codes: tuple[Identifier, ...]
    specification_sha256: Sha256
    software_contract_sha256: Sha256
    physics_contract_sha256: Sha256
    oracle_catalog_sha256: Sha256
    auditor_config_sha256: Sha256
    software_result_sha256: Sha256
    workspace_identity_sha256: Sha256
    oracle_result_sha256: Mapping[Identifier, Sha256]
    oracle_completion_proof_sha256: Mapping[Identifier, Sha256]
    physics_auditor_result_sha256: Sha256 | None
    physics_auditor_proof_sha256: Sha256 | None
    physics_report_sha256: Sha256 | None
    physics_routing_sha256: Sha256 | None
    physics_route: (
        Literal[
            "pass",
            "request_repair",
            "require_human_review",
            "block_insufficient_evidence",
            "infrastructure_failure",
        ]
        | None
    )
    finding_ids: tuple[Identifier, ...]
    unresolved_question_ids: tuple[Identifier, ...]

    @field_validator("reason_codes", "finding_ids", "unresolved_question_ids", mode="before")
    @classmethod
    def freeze_packet_collections(cls, value: Any) -> Any:
        return _freeze_sequence(value)

    def canonical_sha256(self) -> str:
        return hashlib.sha256(canonical_json(self.model_dump(mode="json"))).hexdigest()


class PhysicsReviewDecisionV1(BaseModel):
    """Exact immutable human input; it cannot modify frozen authority."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, str_strip_whitespace=True)

    schema_version: Literal[1]
    run_token: Identifier
    review_packet_sha256: Sha256
    decision: PhysicsReviewDecisionNameV1
    reason: BoundedString
    acknowledged_finding_ids: Annotated[
        tuple[Identifier, ...], BeforeValidator(_freeze_sequence), Field(max_length=200)
    ] = ()
    acknowledged_question_ids: Annotated[
        tuple[Identifier, ...], BeforeValidator(_freeze_sequence), Field(max_length=200)
    ] = ()
    evidence_references: Annotated[
        tuple[BoundedString, ...], BeforeValidator(_freeze_sequence), Field(max_length=200)
    ] = ()

    @field_validator("acknowledged_finding_ids", "acknowledged_question_ids", "evidence_references")
    @classmethod
    def canonicalize_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("human decision collections must be unique")
        return tuple(sorted(value))

    def canonical_sha256(self) -> str:
        return hashlib.sha256(canonical_json(self.model_dump(mode="json"))).hexdigest()


# Exact semantic forms are append-only and are never mixed with JOURNAL_SEMANTIC_FORMS.
PhysicsJournalSemanticFormV2: TypeAlias = tuple[
    PhysicsJournalEventTypeV2,
    PhysicsWorkflowStatusV2 | None,
    PhysicsWorkflowStatusV2,
    Literal["software", "physics_oracle", "physics_auditor"] | None,
    str,
]


def _build_physics_journal_semantic_forms_v2() -> frozenset[PhysicsJournalSemanticFormV2]:
    forms: set[PhysicsJournalSemanticFormV2] = set()

    def add(
        event: PhysicsJournalEventTypeV2,
        previous: PhysicsWorkflowStatusV2 | None,
        new: PhysicsWorkflowStatusV2,
        kind: Literal["software", "physics_oracle", "physics_auditor"] | None,
        *reasons: str,
    ) -> None:
        forms.update((event, previous, new, kind, reason) for reason in reasons)

    add("transition", None, "initialized", None, "physics_workflow_initialized")
    add("transition", "initialized", "software_running", None, "software_workflow_requested")
    add(
        "action_intent",
        "software_running",
        "software_running",
        "software",
        "software_action_intent",
    )
    add(
        "action_completion",
        "software_running",
        "software_running",
        "software",
        "software_action_completed",
    )
    add(
        "evidence",
        "software_running",
        "software_running",
        None,
        "software_gate_verified",
        "stale_oracle_evidence_invalidated",
        "oracle_evidence_preserved",
    )
    add(
        "transition",
        "software_running",
        "physics_oracles_running",
        None,
        "code_auditor_passed",
    )
    add(
        "transition",
        "software_running",
        "human_review_paused",
        None,
        "code_auditor_human_review",
        "contract_weakening_attempt",
    )
    for status in (
        "physics_oracles_running",
        "physics_auditor_running",
        "physics_repair_pending",
    ):
        add(
            "transition",
            status,
            "human_review_paused",
            None,
            "contract_weakening_attempt",
        )
    add(
        "transition",
        "software_running",
        "repair_limit_paused",
        None,
        "software_repair_limit_exhausted",
    )
    infrastructure_sources: tuple[PhysicsWorkflowStatusV2, ...] = (
        "initialized",
        "software_running",
        "physics_oracles_running",
        "physics_auditor_running",
        "physics_repair_pending",
    )
    for status in infrastructure_sources:
        add(
            "transition",
            status,
            "infrastructure_stopped",
            None,
            "workflow_infrastructure_failure",
            "workspace_integrity_failure",
            "recovery_indeterminate",
        )
    add(
        "action_intent",
        "physics_oracles_running",
        "physics_oracles_running",
        "physics_oracle",
        "physics_oracle_action_intent",
    )
    add(
        "action_completion",
        "physics_oracles_running",
        "physics_oracles_running",
        "physics_oracle",
        "physics_oracle_action_completed",
    )
    add(
        "evidence",
        "physics_oracles_running",
        "physics_oracles_running",
        None,
        "oracle_evidence_refreshed",
        "stale_oracle_evidence_invalidated",
    )
    add(
        "transition",
        "physics_oracles_running",
        "physics_auditor_running",
        None,
        "required_oracle_proofs_verified",
    )
    add(
        "transition",
        "physics_oracles_running",
        "evidence_paused",
        None,
        "oracle_evidence_failure",
    )
    add(
        "action_intent",
        "physics_auditor_running",
        "physics_auditor_running",
        "physics_auditor",
        "physics_auditor_action_intent",
    )
    add(
        "action_completion",
        "physics_auditor_running",
        "physics_auditor_running",
        "physics_auditor",
        "physics_auditor_action_completed",
    )
    add(
        "evidence",
        "physics_auditor_running",
        "physics_auditor_running",
        None,
        "physics_route_verified",
    )
    add(
        "transition",
        "physics_auditor_running",
        "physics_repair_pending",
        None,
        "physics_repair_requested",
    )
    add(
        "transition",
        "physics_auditor_running",
        "repair_limit_paused",
        None,
        "physics_repair_limit_exhausted",
    )
    add(
        "transition",
        "physics_auditor_running",
        "human_review_paused",
        None,
        "physics_human_review_required",
    )
    add(
        "transition",
        "physics_auditor_running",
        "evidence_paused",
        None,
        "physics_evidence_insufficient",
        "physics_auditor_evidence_failure",
    )
    add(
        "transition",
        "physics_auditor_running",
        "completed",
        None,
        "physics_completion_gate_passed",
    )
    add(
        "transition",
        "physics_auditor_running",
        "checkpoint_paused",
        None,
        "physics_completion_checkpoint",
    )
    add(
        "transition",
        "physics_repair_pending",
        "software_running",
        None,
        "physics_worker_repair_resumed",
        "additional_evidence_worker_resumed",
        "existing_contract_worker_resumed",
    )
    for paused in ("human_review_paused", "evidence_paused", "repair_limit_paused"):
        add(
            "human_decision",
            paused,
            paused,
            None,
            "physics_human_decision_recorded",
        )
        add(
            "transition",
            paused,
            "physics_repair_pending",
            None,
            "human_approved_existing_contract_repair",
            "human_requested_additional_evidence",
        )
        add(
            "transition",
            paused,
            "human_review_paused",
            None,
            "human_caveat_recorded",
        )
        add(
            "transition",
            paused,
            "aborted",
            None,
            "human_revised_contract_new_run_required",
            "human_rejected_candidate",
            "human_abort",
        )
    return frozenset(forms)


PHYSICS_JOURNAL_SEMANTIC_FORMS_V2 = _build_physics_journal_semantic_forms_v2()
