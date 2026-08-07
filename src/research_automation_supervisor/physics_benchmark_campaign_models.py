"""Strict PA-5C3 benchmark campaign authority and durable-state models."""

from __future__ import annotations

import hashlib
from typing import Annotated, Any, Literal, TypeAlias

from pydantic import BeforeValidator, Field, field_validator, model_validator

from research_automation_supervisor.durable_state import canonical_json
from research_automation_supervisor.physics_benchmark_scoring import (
    ExactBenchmarkRunIdentityV1,
    ExactBenchmarkScoreReportV1,
)
from research_automation_supervisor.physics_models import PhysicsCanonicalModel
from research_automation_supervisor.physics_oracle_models import Sha256
from research_automation_supervisor.workflow_models import Identifier, _freeze_sequence
from research_automation_supervisor.workflow_recovery_models import ObservedWorkflowStatus

MAX_CAMPAIGN_CHILDREN = 10_000

PhysicsBenchmarkCampaignStatusV1: TypeAlias = Literal[
    "running",
    "resumable",
    "human_review_required",
    "insufficient_evidence",
    "infrastructure_blocked",
    "child_failed",
    "ready_to_aggregate",
    "completed",
]

CampaignEventTypeV1: TypeAlias = Literal[
    "campaign_initialized",
    "child_registered",
    "child_launch_intended",
    "child_terminal_observed",
    "campaign_status_routed",
    "campaign_ready_to_aggregate",
    "campaign_scorer_action_started",
    "campaign_scorer_result_persisted",
    "campaign_aggregate_persisted",
    "campaign_action_tree_persisted",
    "campaign_completion_receipt_persisted",
    "campaign_completed",
]


class CampaignChildAuthorityV1(PhysicsCanonicalModel):
    """Frozen PA-4 authority and deterministic campaign coordinates for one child."""

    schema_version: Literal[1] = 1
    campaign_id: Identifier
    child_run_id: Identifier
    case_id: Annotated[str, Field(pattern=r"^case_[0-9]{3}$")]
    variant_id: Literal["variant_001", "variant_002"]
    repetition_id: Annotated[int, Field(ge=1, le=1000)]
    substage_id: Identifier
    run_token: Identifier
    workflow_run_directory: str
    specification_path: str
    specification_sha256: Sha256
    workspace: str
    repository_root: str
    baseline_commit: Annotated[str, Field(pattern=r"^[0-9a-fA-F]{40,64}$")]
    physics_contract_path: str
    physics_contract_sha256: Sha256
    oracle_catalog_path: str
    oracle_catalog_sha256: Sha256
    auditor_config_path: str
    auditor_config_sha256: Sha256

    @model_validator(mode="after")
    def validate_child_coordinates(self) -> CampaignChildAuthorityV1:
        if self.substage_id != self.case_id:
            raise ValueError("benchmark PA-4 substage ID must exactly equal its case ID")
        expected_directory = f"{self.substage_id}-{self.run_token}"
        if self.workflow_run_directory.rstrip("/").rsplit("/", 1)[-1] != expected_directory:
            raise ValueError("child workflow directory contradicts its PA-4 identity")
        return self


class PhysicsBenchmarkCampaignManifestV1(PhysicsCanonicalModel):
    """Self-hashed complete expected child-run set frozen before any registration."""

    schema_version: Literal[1] = 1
    campaign_id: Identifier
    manifest_sha256: Sha256
    repository_root: str
    child_runs_directory: str
    scorer_catalog_path: str
    catalog_id: Literal["physics_benchmark_blind_authority_v1"]
    catalog_sha256: Sha256
    children: Annotated[
        tuple[CampaignChildAuthorityV1, ...],
        BeforeValidator(_freeze_sequence),
        Field(min_length=1, max_length=MAX_CAMPAIGN_CHILDREN),
    ]

    @field_validator("children")
    @classmethod
    def canonicalize_children(
        cls, value: tuple[CampaignChildAuthorityV1, ...]
    ) -> tuple[CampaignChildAuthorityV1, ...]:
        return tuple(
            sorted(value, key=lambda item: (item.case_id, item.variant_id, item.repetition_id))
        )

    @model_validator(mode="after")
    def validate_manifest(self) -> PhysicsBenchmarkCampaignManifestV1:
        keys = tuple((item.case_id, item.variant_id, item.repetition_id) for item in self.children)
        if len(keys) != len(set(keys)):
            raise ValueError("campaign manifest contains duplicate repetition keys")
        child_ids = tuple(item.child_run_id for item in self.children)
        run_tokens = tuple(item.run_token for item in self.children)
        run_directories = tuple(item.workflow_run_directory for item in self.children)
        workspaces = tuple(item.workspace for item in self.children)
        if any(
            len(values) != len(set(values))
            for values in (child_ids, run_tokens, run_directories, workspaces)
        ):
            raise ValueError("campaign children must have unique IDs, runs, and workspaces")
        if any(item.campaign_id != self.campaign_id for item in self.children):
            raise ValueError("child authority contradicts the campaign ID")
        payload = self.model_dump(mode="json", exclude={"manifest_sha256"})
        if self.manifest_sha256 != hashlib.sha256(canonical_json(payload)).hexdigest():
            raise ValueError("campaign manifest digest is invalid")
        return self


class CampaignScoringArtifactsV1(PhysicsCanonicalModel):
    """Durable paths passed unchanged to the PA-5C2 read-only verifier."""

    contract_path: str
    execution_config_path: str
    workspace: str
    oracle_evidence_root: str
    output_directory: str
    attempt_number: Annotated[int, Field(ge=1, le=11)]


class CampaignTerminalChildV1(PhysicsCanonicalModel):
    """One terminal PA-4 head with its independently PA-5C2-bound identity."""

    schema_version: Literal[1] = 1
    child_run_id: Identifier
    child_authority_sha256: Sha256
    workflow_status: ObservedWorkflowStatus
    state_sha256: Sha256
    journal_sha256: Sha256
    journal_hash: Sha256
    scoring_identity: ExactBenchmarkRunIdentityV1
    scoring_artifacts: CampaignScoringArtifactsV1


class CampaignScorerActionStartV1(PhysicsCanonicalModel):
    """Durable exactly-once intent for one complete PA-5C2 scoring action."""

    schema_version: Literal[1] = 1
    action_id: Identifier
    campaign_id: Identifier
    campaign_manifest_sha256: Sha256
    repository_root: str
    expected_child_set_sha256: Sha256
    expected_child_authority_sha256: Annotated[
        tuple[Sha256, ...], BeforeValidator(_freeze_sequence), Field(min_length=1)
    ]
    expected_pa5c2_input_identity_sha256: Annotated[
        tuple[Sha256, ...], BeforeValidator(_freeze_sequence), Field(min_length=1)
    ]
    expected_run_manifest_sha256: Sha256
    catalog_id: Literal["physics_benchmark_blind_authority_v1"]
    catalog_sha256: Sha256
    scorer_authority_sha256: Sha256

    @model_validator(mode="after")
    def validate_action_identity(self) -> CampaignScorerActionStartV1:
        child_hashes = tuple(sorted(self.expected_child_authority_sha256))
        input_hashes = tuple(sorted(self.expected_pa5c2_input_identity_sha256))
        object.__setattr__(self, "expected_child_authority_sha256", child_hashes)
        object.__setattr__(self, "expected_pa5c2_input_identity_sha256", input_hashes)
        if len(child_hashes) != len(set(child_hashes)):
            raise ValueError("scorer action contains duplicate child authorities")
        if len(input_hashes) != len(set(input_hashes)):
            raise ValueError("scorer action contains duplicate PA-5C2 input identities")
        payload = self.model_dump(mode="json", exclude={"action_id"})
        expected = f"score-{hashlib.sha256(canonical_json(payload)).hexdigest()[:48]}"
        if self.action_id != expected:
            raise ValueError("scorer action ID is invalid")
        return self


class CampaignScorerResultReceiptV1(PhysicsCanonicalModel):
    """Atomically persisted exact scorer result and its action binding."""

    schema_version: Literal[1] = 1
    action_id: Identifier
    campaign_id: Identifier
    campaign_manifest_sha256: Sha256
    scorer_action_start_sha256: Sha256
    expected_run_manifest_sha256: Sha256
    scorer_authority_sha256: Sha256
    result_sha256: Sha256
    result: ExactBenchmarkScoreReportV1

    @model_validator(mode="after")
    def validate_result_identity(self) -> CampaignScorerResultReceiptV1:
        if self.result.expected_run_manifest_sha256 != self.expected_run_manifest_sha256:
            raise ValueError("scorer result contradicts its expected-run manifest")
        if self.result_sha256 != self.result.canonical_sha256():
            raise ValueError("scorer result digest is invalid")
        return self


class CampaignAggregateReceiptV1(PhysicsCanonicalModel):
    """Campaign-bound publication of the exact stored PA-5C2 result."""

    schema_version: Literal[1] = 1
    action_id: Identifier
    campaign_id: Identifier
    campaign_manifest_sha256: Sha256
    scorer_action_id: Identifier
    scorer_result_receipt_sha256: Sha256
    scorer_result_semantic_sha256: Sha256
    expected_run_manifest_sha256: Sha256
    result: ExactBenchmarkScoreReportV1

    @model_validator(mode="after")
    def validate_aggregate_identity(self) -> CampaignAggregateReceiptV1:
        if (
            self.result.canonical_sha256() != self.scorer_result_semantic_sha256
            or self.result.expected_run_manifest_sha256
            != self.expected_run_manifest_sha256
        ):
            raise ValueError("campaign aggregate contradicts its scorer result")
        return self


class PhysicsBenchmarkCampaignStateV1(PhysicsCanonicalModel):
    """Journal-reconcilable campaign snapshot; completion is the final commit marker."""

    schema_version: Literal[1] = 1
    campaign_id: Identifier
    manifest_sha256: Sha256
    repository_root: str
    scorer_catalog_path: str
    catalog_id: Literal["physics_benchmark_blind_authority_v1"]
    catalog_sha256: Sha256
    expected_child_set_sha256: Sha256
    scorer_authority_sha256: Sha256
    status: PhysicsBenchmarkCampaignStatusV1
    registered_child_run_ids: Annotated[
        tuple[Identifier, ...], BeforeValidator(_freeze_sequence)
    ] = ()
    launch_intent_child_run_ids: Annotated[
        tuple[Identifier, ...], BeforeValidator(_freeze_sequence)
    ] = ()
    terminal_children: Annotated[
        tuple[CampaignTerminalChildV1, ...], BeforeValidator(_freeze_sequence)
    ] = ()
    blocking_child_run_id: Identifier | None = None
    reason_code: Identifier
    expected_run_manifest_path: str | None = None
    expected_run_manifest_sha256: Sha256 | None = None
    scorer_action_id: Identifier | None = None
    scorer_action_path: str | None = None
    scorer_action_sha256: Sha256 | None = None
    scorer_result_path: str | None = None
    scorer_result_sha256: Sha256 | None = None
    scorer_result_semantic_sha256: Sha256 | None = None
    aggregate_action_id: Identifier | None = None
    aggregate_result_path: str | None = None
    aggregate_result_sha256: Sha256 | None = None
    action_tree_path: str | None = None
    action_tree_sha256: Sha256 | None = None
    completion_receipt_path: str | None = None
    completion_receipt_sha256: Sha256 | None = None
    journal_sequence: Annotated[int, Field(ge=0)]
    journal_hash: Sha256
    started_at: str
    updated_at: str

    @field_validator("registered_child_run_ids", "launch_intent_child_run_ids")
    @classmethod
    def validate_unique_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("campaign child progress contains duplicates")
        return value

    @field_validator("terminal_children")
    @classmethod
    def canonicalize_terminal_children(
        cls, value: tuple[CampaignTerminalChildV1, ...]
    ) -> tuple[CampaignTerminalChildV1, ...]:
        items = tuple(sorted(value, key=lambda item: item.child_run_id))
        if len({item.child_run_id for item in items}) != len(items):
            raise ValueError("campaign terminal observations contain duplicate children")
        keys = {
            (
                item.scoring_identity.case_id,
                item.scoring_identity.variant_id,
                item.scoring_identity.repetition_id,
            )
            for item in items
        }
        if len(keys) != len(items):
            raise ValueError("campaign terminal observations contain duplicate repetitions")
        return items

    @model_validator(mode="after")
    def validate_completion_shape(self) -> PhysicsBenchmarkCampaignStateV1:
        scorer_action = (
            self.expected_run_manifest_path,
            self.expected_run_manifest_sha256,
            self.scorer_action_id,
            self.scorer_action_path,
            self.scorer_action_sha256,
        )
        scorer_result = (
            self.scorer_result_path,
            self.scorer_result_sha256,
            self.scorer_result_semantic_sha256,
        )
        aggregate = (
            self.aggregate_action_id,
            self.aggregate_result_path,
            self.aggregate_result_sha256,
        )
        action_tree = (
            self.action_tree_path,
            self.action_tree_sha256,
        )
        completion = (
            self.completion_receipt_path,
            self.completion_receipt_sha256,
        )
        for label, values in (
            ("scorer action", scorer_action),
            ("scorer result", scorer_result),
            ("aggregate", aggregate),
            ("action tree", action_tree),
            ("completion receipt", completion),
        ):
            if any(value is not None for value in values) and not all(
                value is not None for value in values
            ):
                raise ValueError(f"campaign {label} identity must be atomic")
        if all(value is not None for value in scorer_result) and not all(
            value is not None for value in scorer_action
        ):
            raise ValueError("campaign scorer result lacks its action intent")
        if all(value is not None for value in aggregate) and not all(
            value is not None for value in scorer_result
        ):
            raise ValueError("campaign aggregate lacks its scorer result")
        if all(value is not None for value in action_tree) and not all(
            value is not None for value in aggregate
        ):
            raise ValueError("campaign action tree lacks its aggregate")
        if all(value is not None for value in completion) and not all(
            value is not None for value in action_tree
        ):
            raise ValueError("campaign completion receipt lacks its action tree")
        if self.status == "completed" and not (
            all(value is not None for value in scorer_action)
            and all(value is not None for value in scorer_result)
            and all(value is not None for value in aggregate)
            and all(value is not None for value in action_tree)
            and all(value is not None for value in completion)
        ):
            raise ValueError("completed campaign lacks its aggregate or completion receipts")
        if self.status == "ready_to_aggregate" and self.blocking_child_run_id is not None:
            raise ValueError("aggregation-ready campaign cannot be blocked on a child")
        return self


class PhysicsBenchmarkCampaignJournalEntryV1(PhysicsCanonicalModel):
    """One immutable hash-chain transition in the campaign action tree."""

    schema_version: Literal[1] = 1
    sequence: Annotated[int, Field(ge=1)]
    event_type: CampaignEventTypeV1
    action_id: Identifier
    timestamp: str
    state_updates: dict[str, Any]
    artifact_hashes: dict[str, Sha256]
    previous_hash: Sha256
    entry_hash: Sha256

    @model_validator(mode="after")
    def validate_hash(self) -> PhysicsBenchmarkCampaignJournalEntryV1:
        payload = self.model_dump(mode="json", exclude={"entry_hash"})
        if self.entry_hash != hashlib.sha256(canonical_json(payload)).hexdigest():
            raise ValueError("campaign journal entry digest is invalid")
        return self
