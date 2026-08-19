"""Strict PA-5D0 prospective calibration authority.

This module is deliberately incapable of launching PA-2, PA-3, a benchmark campaign,
or a GL pilot.  It reads qualified authority, constructs canonical review candidates,
and refuses to finalize while an explicit human decision is absent.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal, TypeAlias, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from research_automation_supervisor.durable_state import canonical_json
from research_automation_supervisor.physics_auditor_models import (
    PHYSICS_AUDITOR_BUBBLEWRAP_POLICY_V1,
    PHYSICS_AUDITOR_CODEX_ROLE_POLICY_V1,
    PhysicsAuditorExecutionConfigV1,
    load_physics_auditor_execution_config,
)
from research_automation_supervisor.physics_auditor_prompts import (
    PHYSICS_AUDITOR_PROMPT_TEMPLATE_SHA256,
)
from research_automation_supervisor.physics_benchmark_blindness import (
    FixtureReviewPacketV1,
    GLFixtureAuthorityV1,
    PhysicsBlindFixtureCatalogV1,
    build_gl_visible_manifest,
    build_paired_visible_manifest,
    load_blind_fixture_catalog,
    qualify_fixture_authority,
)
from research_automation_supervisor.physics_models import PHYSICS_AUDIT_REPORT_OUTPUT_SCHEMA

HUMAN_AUTHORITY_REQUIRED = "HUMAN_AUTHORITY_REQUIRED"
BASE_COMMIT = "aeaef976c5990245c3d72f0b0cf41bc76fd8d415"
CATALOG_RELATIVE = "examples/physics_auditor/benchmark_v1/scorer_only/catalog.json"
CONFIG_RELATIVE = "examples/physics_auditor/synthetic/execution-config.yaml"
GL_SOURCE_RELATIVE = "../GL-with-AI"
PA5D_ROOT_SEED = "pa5d0-calibration-authority-v1-fresh-namespace"
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Commit = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
ScheduleId: TypeAlias = Literal[
    "schedule_maximum_variant_coverage_v1",
    "schedule_balanced_single_repeat_v1",
]


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha_text(value: str) -> str:
    return _sha_bytes(value.encode("utf-8"))


def _file_sha256(path: Path) -> str:
    return _sha_bytes(path.read_bytes())


def _canonical_file_sha256(path: Path) -> str:
    return _sha_bytes(canonical_json(json.loads(path.read_bytes())))


class CanonicalModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    def to_canonical_json(self) -> bytes:
        return canonical_json(self.model_dump(mode="json"))

    def canonical_sha256(self) -> str:
        return _sha_bytes(self.to_canonical_json())


class FileAuthorityV1(CanonicalModel):
    path: str
    sha256: Sha256


class QualifiedAuthoritySourcesV1(CanonicalModel):
    base_commit: Commit
    pa5a_commit: Commit
    pa5c1_commit: Commit
    pa5c2_commit: Commit
    pa5c3_commit: Commit
    pa5c4_commit: Commit
    catalog_file: FileAuthorityV1
    catalog_id: Literal["physics_benchmark_blind_authority_v1"]
    catalog_canonical_sha256: Sha256
    review_packet_sha256: Sha256
    fixture_qualification_sha256: Sha256
    scorer_root_manifest_sha256: Sha256
    frozen_pa5c_tree_sha256: Sha256
    blindness_authority: FileAuthorityV1
    scoring_authority: FileAuthorityV1
    campaign_authority: FileAuthorityV1
    recovery_authority: FileAuthorityV1
    custodian_authority: FileAuthorityV1
    pa3_qualification: FileAuthorityV1
    roadmap_pre_outcome_authority: FileAuthorityV1


class BenchmarkVariantAuthorityV1(CanonicalModel):
    case_id: Annotated[str, Field(pattern=r"^case_[0-9]{3}$")]
    pair_id: Annotated[str, Field(pattern=r"^pair_[0-9]{3}$")]
    variant_id: Literal["variant_001", "variant_002"]
    fixture_label: Literal["clean", "defective"]
    expected_route: Literal[
        "pass", "request_repair", "require_human_review", "block_insufficient_evidence"
    ]
    minimum_severity: Literal["high"] | None
    required_categories: tuple[str, ...]
    acceptable_alternative_categories: tuple[str, ...]
    forbidden_categories: tuple[str, ...]
    visible_root: str
    visible_manifest_sha256: Sha256
    paired_visible_manifest_sha256: Sha256
    scorer_authority_sha256: Sha256
    review_receipt: FileAuthorityV1
    contract_sha256: Sha256


class ExecutionCoordinateV1(CanonicalModel):
    ordinal: Annotated[int, Field(ge=1)]
    execution_id: Annotated[str, Field(pattern=r"^benchmark-[a-f0-9]{32}$")]
    case_id: Annotated[str, Field(pattern=r"^case_[0-9]{3}$")]
    pair_id: Annotated[str, Field(pattern=r"^pair_[0-9]{3}$")]
    variant_id: Literal["variant_001", "variant_002"]
    repetition_id: Annotated[int, Field(ge=1, le=2)]
    child_run_id: Annotated[str, Field(pattern=r"^pa5d-child-[a-f0-9]{32}$")]
    child_run_token: Annotated[str, Field(pattern=r"^pa5d-[a-f0-9]{32}$")]
    child_authority_path: str
    child_authority_sha256: Sha256
    workflow_run_root: str
    pa3_action_root: str
    prompt_authority_sha256: Sha256
    session_policy: Literal["one_fresh_ephemeral_session_no_resume"]


class BenchmarkScheduleAuthorityV1(CanonicalModel):
    schedule_id: ScheduleId
    status: Literal["candidate_not_selected", "human_selected"]
    unit_of_execution: Literal["one_case_variant_repetition_per_fresh_pa3_session"]
    neutral_rule: str
    scientific_tradeoff: str
    catalog_variant_count: Literal[42]
    catalog_sha256: Sha256
    distinct_variant_count: Annotated[int, Field(ge=40, le=41)]
    repeated_execution_count: Annotated[int, Field(ge=0, le=1)]
    omitted_variant_keys: tuple[str, ...]
    ordering_policy: Literal["lexicographic_case_variant_repetition"]
    action_root_derivation: str
    prior_action_or_session_reuse_allowed: Literal[False]
    campaign_root: str
    expected_child_set_sha256: Sha256
    executions: Annotated[tuple[ExecutionCoordinateV1, ...], Field(min_length=41, max_length=41)]

    @model_validator(mode="after")
    def validate_schedule(self) -> BenchmarkScheduleAuthorityV1:
        keys = tuple(
            (item.case_id, item.variant_id, item.repetition_id) for item in self.executions
        )
        if len(keys) != len(set(keys)):
            raise ValueError("schedule execution identities must be unique")
        if tuple(item.ordinal for item in self.executions) != tuple(range(1, 42)):
            raise ValueError("schedule ordinals must be exact and contiguous")
        ordered = tuple(sorted(keys))
        if keys != ordered:
            raise ValueError("schedule contradicts its deterministic ordering policy")
        if len({item.case_id for item in self.executions}) != 21:
            raise ValueError("schedule must cover all 21 qualified subjects")
        if len({item.execution_id for item in self.executions}) != 41:
            raise ValueError("schedule execution IDs must be unique")
        if len({item.child_run_id for item in self.executions}) != 41:
            raise ValueError("schedule child IDs must be unique")
        if len({item.pa3_action_root for item in self.executions}) != 41:
            raise ValueError("schedule PA-3 action roots must be unique")
        namespace = _sha_text(
            f"{PA5D_ROOT_SEED}|{BASE_COMMIT}|{self.catalog_sha256}|{self.schedule_id}"
        )[:24]
        expected_root = f"runs/pa5d1-preregistered-v1/benchmark-{namespace}"
        if self.campaign_root != expected_root:
            raise ValueError("benchmark campaign root contradicts deterministic derivation")
        for item in self.executions:
            key = f"{item.case_id}|{item.variant_id}|{item.repetition_id}"
            digest = _sha_text(f"{PA5D_ROOT_SEED}|{BASE_COMMIT}|{self.schedule_id}|{key}")
            execution_id = f"benchmark-{digest[:32]}"
            child_run_id = f"pa5d-child-{digest[:32]}"
            run_token = f"pa5d-{digest[32:64]}"
            child_path = f"{expected_root}/child-authority/{execution_id}.json"
            workflow_root = f"{expected_root}/children/{item.case_id}-{run_token}"
            action_root = f"{workflow_root}/physics-auditor/{execution_id}"
            child_payload = {
                "case_id": item.case_id,
                "child_run_id": child_run_id,
                "pair_id": item.pair_id,
                "repetition_id": item.repetition_id,
                "run_token": run_token,
                "schedule_id": self.schedule_id,
                "variant_id": item.variant_id,
                "workflow_run_root": workflow_root,
            }
            observed = (
                item.execution_id,
                item.child_run_id,
                item.child_run_token,
                item.child_authority_path,
                item.child_authority_sha256,
                item.workflow_run_root,
                item.pa3_action_root,
            )
            exact = (
                execution_id,
                child_run_id,
                run_token,
                child_path,
                _sha_bytes(canonical_json(child_payload)),
                workflow_root,
                action_root,
            )
            if observed != exact:
                raise ValueError("benchmark execution root or identity derivation changed")
        expected = _sha_bytes(
            canonical_json([item.model_dump(mode="json") for item in self.executions])
        )
        if self.expected_child_set_sha256 != expected:
            raise ValueError("expected benchmark child-set digest is invalid")
        return self


class ModelConfigurationAuthorityV1(CanonicalModel):
    status: Literal["HUMAN_AUTHORITY_REQUIRED", "HUMAN_APPROVED"]
    decision_id: Literal["approve_pa3_qualified_model_configuration_v1"]
    qualified_source: FileAuthorityV1
    execution_config_sha256: Sha256
    config: dict[str, Any]
    codex_cli_version: Literal["codex-cli 0.146.0"]
    codex_executable_sha256: Literal[
        "2e863156ed35ecc5253b1e2f907a9143077b9f7cb51942070c61996471ff6e04"
    ]
    bubblewrap_version: Literal["bubblewrap 0.11.1"]
    bubblewrap_backend_identity_sha256: Literal[
        "e7ffcfcfc7611c7cc6bb5913841e6385684429b8ab9b0c4c7b7c506fd94eaedc"
    ]
    runtime_identity_drift_allowed: Literal[False]
    role_policy_sha256: Sha256
    bubblewrap_policy_sha256: Sha256
    prompt_template_version: Literal["physics_auditor_prompt_v2"]
    prompt_template_sha256: Sha256
    prompt_renderer: FileAuthorityV1
    rendered_prompt_policy: Literal[
        "qualified_renderer_over_exact_run_inputs_then_bind_rendered_sha256_before_launch"
    ]
    output_schema_id: Literal["physics_audit_report_v1"]
    output_schema_sha256: Sha256
    fresh_session_required: Literal[True]
    resume_allowed: Literal[False]
    yolo_or_full_access_allowed: Literal[False]
    blindness_certificate_required_before_launch: Literal[True]
    scorer_namespace_present_in_auditor_sandbox: Literal[False]
    same_configuration_for_benchmark_and_gl: Literal[True]


class MetricDefinitionV1(CanonicalModel):
    metric_id: str
    kind: Literal["rate", "count", "consistency", "duration", "token_usage"]
    numerator: str
    denominator: str
    exact_rule: str
    zero_denominator: Literal["not_applicable", "zero"]
    aggregation: Literal["benchmark", "benchmark_and_per_category", "gl", "both"]
    separate_metric: Literal[True] = True


GateKind: TypeAlias = Literal["structural_hard_gate", "performance_gate", "descriptive"]


class ThresholdAuthorityV1(CanonicalModel):
    threshold_id: str
    metric_id: str
    operator: Literal["==", "<=", ">=", "NO_GATE"]
    value: bool | int | float | str | None
    gate_kind: GateKind
    authority_status: Literal[
        "QUALIFIED_PRE_OUTCOME",
        "HUMAN_AUTHORITY_REQUIRED",
        "HUMAN_APPROVED",
        "NOT_AN_ACCEPTANCE_GATE",
    ]
    application_scope: Literal[
        "global",
        "aggregate_and_each_nonzero_declared_category",
        "not_applicable_descriptive",
    ]
    rationale: str
    decision_id: str | None

    @model_validator(mode="after")
    def validate_authority(self) -> ThresholdAuthorityV1:
        if (self.authority_status == HUMAN_AUTHORITY_REQUIRED) != (self.decision_id is not None):
            raise ValueError("human-required thresholds need exactly one decision ID")
        if self.gate_kind == "descriptive" and self.operator != "NO_GATE":
            raise ValueError("descriptive metrics cannot silently become gates")
        if (self.operator == "NO_GATE") != (self.value is None):
            raise ValueError("NO_GATE and null value must occur together")
        return self


class GLSourceBlobAuthorityV1(CanonicalModel):
    path: str
    role: str
    byte_length: Annotated[int, Field(ge=1)]
    sha256: Sha256


class GLTaskExecutionAuthorityV1(CanonicalModel):
    ordinal: Annotated[int, Field(ge=1, le=10)]
    task_id: Annotated[str, Field(pattern=r"^task_[0-9]{3}$")]
    visible_root: str
    visible_manifest_sha256: Sha256
    scorer_authority_sha256: Sha256
    review_receipt: FileAuthorityV1
    expected_route: Literal["pass", "require_human_review"]
    minimum_severity: Literal["high"] | None
    required_categories: tuple[str, ...]
    forbidden_categories: tuple[str, ...]
    source_blobs: tuple[GLSourceBlobAuthorityV1, ...]
    execution_id: Annotated[str, Field(pattern=r"^gl-[a-f0-9]{32}$")]
    child_run_id: Annotated[str, Field(pattern=r"^pa5d-gl-child-[a-f0-9]{32}$")]
    child_authority_sha256: Sha256
    action_root: str
    prompt_authority_sha256: Sha256
    one_fresh_session: Literal[True]


class GLPilotAuthorityV1(CanonicalModel):
    source_commit: Commit
    catalog_sha256: Sha256
    unit_of_execution: Literal["one_locked_gl_task_per_fresh_pa3_session"]
    task_order_policy: Literal["catalog_declared_order"]
    model_configuration_binding: Literal["same_exact_pa3_configuration_as_benchmark"]
    projected_source_policy: Literal["pa5c1_exact_locked_blobs_plus_reviewed_visible_fixture_only"]
    evidence_policy: Literal["pa5c1_pa2_pa3_verified_evidence_only"]
    production_gl_mode_claim_allowed: Literal[False]
    action_root_derivation: str
    prior_action_or_session_reuse_allowed: Literal[False]
    pilot_root: str
    expected_child_set_sha256: Sha256
    tasks: Annotated[tuple[GLTaskExecutionAuthorityV1, ...], Field(min_length=10, max_length=10)]

    @model_validator(mode="after")
    def validate_tasks(self) -> GLPilotAuthorityV1:
        if tuple(item.ordinal for item in self.tasks) != tuple(range(1, 11)):
            raise ValueError("GL task order is not exact")
        if tuple(item.task_id for item in self.tasks) != tuple(
            f"task_{index:03d}" for index in range(1, 11)
        ):
            raise ValueError("GL task IDs must be exact and ordered")
        namespace = _sha_text(f"{PA5D_ROOT_SEED}|{BASE_COMMIT}|{self.catalog_sha256}|gl")[:24]
        expected_root = f"runs/pa5d1-preregistered-v1/gl-{namespace}"
        if self.pilot_root != expected_root:
            raise ValueError("GL pilot root contradicts deterministic derivation")
        for item in self.tasks:
            digest = _sha_text(f"{PA5D_ROOT_SEED}|{BASE_COMMIT}|gl|{item.task_id}")
            child_payload = {
                "source_commit": self.source_commit,
                "task_id": item.task_id,
                "visible_manifest_sha256": item.visible_manifest_sha256,
            }
            exact = (
                f"gl-{digest[:32]}",
                f"pa5d-gl-child-{digest[:32]}",
                _sha_bytes(canonical_json(child_payload)),
                f"{expected_root}/{item.ordinal:02d}-{item.task_id}-{digest[:16]}",
            )
            observed = (
                item.execution_id,
                item.child_run_id,
                item.child_authority_sha256,
                item.action_root,
            )
            if observed != exact:
                raise ValueError("GL child identity or action-root derivation changed")
            expected_prompt = _sha_text(
                "|".join(
                    (
                        PHYSICS_AUDITOR_PROMPT_TEMPLATE_SHA256,
                        item.visible_manifest_sha256,
                        item.execution_id,
                        "qualified_renderer_exact_inputs_bind_rendered_hash_before_launch",
                    )
                )
            )
            if item.prompt_authority_sha256 != expected_prompt:
                raise ValueError("GL prompt authority derivation changed")
        if (
            len({item.execution_id for item in self.tasks}) != 10
            or len({item.child_run_id for item in self.tasks}) != 10
        ):
            raise ValueError("GL execution and child identities must be unique")
        expected = _sha_bytes(canonical_json([item.model_dump(mode="json") for item in self.tasks]))
        if expected != self.expected_child_set_sha256:
            raise ValueError("expected GL child-set digest is invalid")
        return self


class RecoveryAndHumanActionAuthorityV1(CanonicalModel):
    recovery_sources: tuple[FileAuthorityV1, ...]
    allowed_automatic_recovery_dispositions: tuple[
        Literal["auto_resume", "finish_finalization"], ...
    ]
    infrastructure_recovery_rule: str
    scientific_inputs_frozen_rule: str
    human_action_rule: str
    invalidating_actions: tuple[str, ...]
    post_outcome_change_rule: str
    duplicate_external_action_rule: str


class CalibrationExecutionPolicyV1(CanonicalModel):
    status: Literal["HUMAN_AUTHORITY_REQUIRED", "HUMAN_APPROVED"]
    decision_id: Literal["approve_one_shot_calibration_execution_policy_v1"]
    benchmark_orchestrator: Literal["pa5d_one_shot_pa3_adapter_required_before_launch"]
    gl_orchestrator: Literal["pa5d_one_shot_gl_adapter_required_before_launch"]
    pa5c3_semantics_reused: tuple[
        Literal[
            "frozen_complete_child_set",
            "sequential_deterministic_order",
            "durable_intent_before_external_action",
            "zero_duplicate_external_actions",
            "exact_terminal_proof_rebinding",
        ],
        ...,
    ]
    ordinary_pa4_worker_or_repair_loop_used: Literal[False]
    route_is_observation_not_workflow_command: Literal[True]
    pa3_sessions_per_coordinate: Literal[1]
    benchmark_pa3_session_total: Literal[41]
    gl_pa3_session_total: Literal[10]
    nonpass_route_terminalization: Literal[
        "persist_verified_pa3_proof_and_score_without_worker_repair_or_human_override"
    ]
    infrastructure_recovery: Literal[
        "resume_only_same_proven_action_otherwise_invalidate_calibration"
    ]
    implementation_gate: Literal[
        "deterministic_adapter_tests_and_independent_review_required_before_any_launch"
    ]


class MetricAndScoringPolicyV1(CanonicalModel):
    status: Literal["HUMAN_AUTHORITY_REQUIRED", "HUMAN_APPROVED"]
    decision_id: Literal["approve_metric_and_gl_scoring_policy_v1"]
    benchmark_source: Literal["pa5c2_exact_run_semantics_plus_preregistered_derived_metrics"]
    per_category_assignment: Literal[
        "each_run_contributes_to_each_declared_required_or_acceptable_category_no_inference"
    ]
    per_category_threshold_application: Literal[
        "apply_to_aggregate_and_every_declared_category_with_nonzero_denominator"
    ]
    semantic_failure_denominator_policy: Literal[
        "inherit_pa5c2_metric_specific_tri_state_exactly_no_global_malformed_or_infrastructure_override"
    ]
    cross_domain_aggregation: Literal[
        "benchmark_and_gl_are_separate_cohorts_each_applicable_threshold_must_pass_no_cross_domain_pooling"
    ]
    pair_definition: Literal[
        "same_case_repetition_one_both_variants_validated_no_infrastructure_or_malformed"
    ]
    repeat_definition: Literal[
        "same_case_variant_repetitions_one_and_two_equal_route_and_finding_category_set"
    ]
    duration_summary: Literal["per_run_min_median_nearest_rank_p95_max_sum_no_imputation"]
    token_summary: Literal[
        "provider_authoritative_input_plus_output_combined_reasoning_subset_not_added_twice_no_estimation"
    ]
    gl_scoring: Literal[
        "derive_same_separate_route_category_severity_evidence_malformed_and_infrastructure_criteria_from_each_locked_task_authority"
    ]
    gl_expected_pass_metric: Literal["gl_expected_pass_route_rate"]
    gl_expected_human_metric: Literal["human_escalation_rate"]
    gl_production_claim: Literal[False]
    implementation_gate: Literal[
        "deterministic_gl_scorer_tests_and_independent_review_required_before_any_launch"
    ]


class ContaminationRegisterEntryV1(CanonicalModel):
    identity: str
    commit: Commit | None
    artifact: FileAuthorityV1 | None
    allowed_use: Literal["contamination_history_only"]
    forbidden_uses: tuple[str, ...]


class RequiredHumanDecisionV1(CanonicalModel):
    decision_id: str
    subject: str
    allowed_values: tuple[str, ...]
    consequence: str


class PA5DCalibrationAuthorityV1(CanonicalModel):
    """Complete candidate authority; not an approval receipt."""

    schema_version: Literal[1] = 1
    authority_type: Literal["PA5DCalibrationAuthorityV1"] = "PA5DCalibrationAuthorityV1"
    approval_state: Literal["DRAFT_HUMAN_AUTHORITY_REQUIRED", "HUMAN_APPROVED"]
    base_commit: Commit
    schedule: BenchmarkScheduleAuthorityV1
    benchmark_catalog_variants: tuple[BenchmarkVariantAuthorityV1, ...]
    model_configuration: ModelConfigurationAuthorityV1
    metrics: tuple[MetricDefinitionV1, ...]
    thresholds: tuple[ThresholdAuthorityV1, ...]
    gl_pilot: GLPilotAuthorityV1
    execution_policy: CalibrationExecutionPolicyV1
    metric_and_scoring_policy: MetricAndScoringPolicyV1
    recovery_and_human_action: RecoveryAndHumanActionAuthorityV1
    contamination_register: tuple[ContaminationRegisterEntryV1, ...]
    human_decision_sha256: Sha256 | None
    authority_sha256: Sha256

    @model_validator(mode="after")
    def validate_self_hash(self) -> PA5DCalibrationAuthorityV1:
        metric_ids = tuple(item.metric_id for item in self.metrics)
        threshold_ids = tuple(item.threshold_id for item in self.thresholds)
        if len(metric_ids) != len(set(metric_ids)):
            raise ValueError("metric IDs must be unique")
        if len(threshold_ids) != len(set(threshold_ids)):
            raise ValueError("threshold IDs must be unique")
        if any(item.metric_id not in set(metric_ids) for item in self.thresholds):
            raise ValueError("every threshold must reference one exact metric definition")
        variants = {
            (item.case_id, item.variant_id): item for item in self.benchmark_catalog_variants
        }
        if len(variants) != 42 or len({key[0] for key in variants}) != 21:
            raise ValueError("candidate authority must bind all 42 catalog variants")
        expected_schedule = _schedule(
            self.schedule.schedule_id,
            tuple(sorted(variants.values(), key=lambda item: (item.case_id, item.variant_id))),
            self.schedule.catalog_sha256,
        )
        observed_schedule = self.schedule.model_dump(mode="json", exclude={"status"})
        rebuilt_schedule = expected_schedule.model_dump(mode="json", exclude={"status"})
        if observed_schedule != rebuilt_schedule:
            raise ValueError("schedule differs from its exact neutral catalog-only rule")
        for run in self.schedule.executions:
            variant = variants[(run.case_id, run.variant_id)]
            expected_prompt = _sha_text(
                "|".join(
                    (
                        self.model_configuration.prompt_template_sha256,
                        variant.visible_manifest_sha256,
                        variant.contract_sha256,
                        run.execution_id,
                        "qualified_renderer_exact_inputs_bind_rendered_hash_before_launch",
                    )
                )
            )
            if run.prompt_authority_sha256 != expected_prompt:
                raise ValueError("per-run prompt authority derivation changed")
        config = PhysicsAuditorExecutionConfigV1.model_validate_json(
            canonical_json(self.model_configuration.config)
        )
        if config.canonical_sha256() != self.model_configuration.execution_config_sha256:
            raise ValueError("model configuration canonical identity changed")
        if (
            self.model_configuration.execution_config_sha256
            != "9e930328a244aba56f5f096da4a5972817f6ccd028af797fdc52169666d1470b"
            or self.model_configuration.prompt_template_sha256
            != "dcc15eac412efd0b8a1628adc05f21765d744ba1265bd8296aeac0c0cd477c6c"
            or self.model_configuration.output_schema_sha256
            != "82ffc2fe49e3929678368733c6200933d072c27abcd548d65cb52dbe62121297"
        ):
            raise ValueError("candidate is not the exact qualified PA-3 configuration")
        human_approved = self.approval_state == "HUMAN_APPROVED"
        if human_approved != (self.human_decision_sha256 is not None):
            raise ValueError("approval state and human decision binding disagree")
        performance_thresholds = tuple(
            item for item in self.thresholds if item.gate_kind == "performance_gate"
        )
        if human_approved:
            if (
                self.schedule.status != "human_selected"
                or self.model_configuration.status != "HUMAN_APPROVED"
                or self.execution_policy.status != "HUMAN_APPROVED"
                or self.metric_and_scoring_policy.status != "HUMAN_APPROVED"
                or any(
                    item.authority_status != "HUMAN_APPROVED" or item.decision_id is not None
                    for item in performance_thresholds
                )
            ):
                raise ValueError("approved authority has incomplete subordinate approval state")
        elif (
            self.schedule.status != "candidate_not_selected"
            or self.model_configuration.status != HUMAN_AUTHORITY_REQUIRED
            or self.execution_policy.status != HUMAN_AUTHORITY_REQUIRED
            or self.metric_and_scoring_policy.status != HUMAN_AUTHORITY_REQUIRED
            or any(
                item.authority_status != HUMAN_AUTHORITY_REQUIRED or item.decision_id is None
                for item in performance_thresholds
            )
        ):
            raise ValueError("draft authority contains premature subordinate approval state")
        payload = self.model_dump(mode="json", exclude={"authority_sha256"})
        if _sha_bytes(canonical_json(payload)) != self.authority_sha256:
            raise ValueError("calibration authority self-hash is invalid")
        return self


class PA5DPreregistrationReviewAuthorityV1(CanonicalModel):
    schema_version: Literal[1] = 1
    authority_type: Literal["PA5DPreregistrationReviewAuthorityV1"] = (
        "PA5DPreregistrationReviewAuthorityV1"
    )
    stage: Literal["PA-5D0"] = "PA-5D0"
    status: Literal["HUMAN_AUTHORITY_REQUIRED"]
    model_session_launch_capability: Literal[False] = False
    qualified_sources: QualifiedAuthoritySourcesV1
    schedule_alternatives: tuple[BenchmarkScheduleAuthorityV1, ...]
    candidate_authorities: tuple[PA5DCalibrationAuthorityV1, ...]
    required_human_decisions: tuple[RequiredHumanDecisionV1, ...]
    final_approved_receipt_issued: Literal[False] = False
    benchmark_sessions_launched: Literal[0] = 0
    gl_sessions_launched: Literal[0] = 0
    draft_sha256: Sha256

    @model_validator(mode="after")
    def validate_review(self) -> PA5DPreregistrationReviewAuthorityV1:
        if len(self.schedule_alternatives) < 2:
            raise ValueError("reasonable schedule alternatives require human selection")
        alternatives = {item.schedule_id: item for item in self.schedule_alternatives}
        candidates = {item.schedule.schedule_id: item for item in self.candidate_authorities}
        if len(alternatives) != len(self.schedule_alternatives) or len(candidates) != len(
            self.candidate_authorities
        ):
            raise ValueError("schedule alternatives and candidates must be unique")
        if set(candidates) != set(alternatives):
            raise ValueError("candidate authorities do not cover the schedule alternatives")
        shared_payloads: list[dict[str, Any]] = []
        for schedule_id, candidate in candidates.items():
            if candidate.schedule != alternatives[schedule_id]:
                raise ValueError("candidate schedule differs from its reviewed alternative")
            if (
                candidate.approval_state != "DRAFT_HUMAN_AUTHORITY_REQUIRED"
                or candidate.human_decision_sha256 is not None
                or candidate.base_commit != self.qualified_sources.base_commit
                or candidate.schedule.catalog_sha256
                != self.qualified_sources.catalog_canonical_sha256
            ):
                raise ValueError("review candidate contains unapproved or foreign authority")
            shared_payloads.append(
                candidate.model_dump(mode="json", exclude={"schedule", "authority_sha256"})
            )
        if any(item != shared_payloads[0] for item in shared_payloads[1:]):
            raise ValueError("schedule candidates differ outside the exact schedule")
        payload = self.model_dump(mode="json", exclude={"draft_sha256"})
        if _sha_bytes(canonical_json(payload)) != self.draft_sha256:
            raise ValueError("review authority self-hash is invalid")
        return self


class ThresholdHumanDecisionV1(CanonicalModel):
    threshold_id: str
    decision: Literal["approve_proposed", "replace"]
    replacement_operator: Literal["==", "<=", ">="] | None = None
    replacement_value: int | float | None = None
    rationale: Annotated[str, Field(min_length=1, max_length=2048)]

    @model_validator(mode="after")
    def validate_replacement(self) -> ThresholdHumanDecisionV1:
        replacement = self.decision == "replace"
        if replacement != (
            self.replacement_operator is not None and self.replacement_value is not None
        ):
            raise ValueError("replacement thresholds require exact operator and value")
        return self


class PA5DHumanDecisionsV1(CanonicalModel):
    schema_version: Literal[1] = 1
    review_draft_sha256: Sha256
    reviewer_identity: Annotated[str, Field(min_length=1, max_length=240)]
    decided_at: Annotated[
        str,
        Field(
            pattern=(
                r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
                r"(?:Z|[+-][0-9]{2}:[0-9]{2})$"
            )
        ),
    ]
    selected_schedule_id: ScheduleId
    model_configuration_decision: Literal["approve_exact_qualified_pa3_configuration"]
    execution_policy_decision: Literal["approve_one_shot_calibration_execution_policy_v1"]
    metric_and_gl_scoring_decision: Literal["approve_metric_and_gl_scoring_policy_v1"]
    threshold_decisions: tuple[ThresholdHumanDecisionV1, ...]
    explicit_no_pa5b_derivation_attestation: Literal[True]
    decision_sha256: Sha256

    @model_validator(mode="after")
    def validate_self_hash(self) -> PA5DHumanDecisionsV1:
        try:
            parsed = datetime.fromisoformat(self.decided_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("decided_at must be a real RFC3339 timestamp") from exc
        if parsed.tzinfo is None:
            raise ValueError("decided_at must include an RFC3339 UTC offset")
        payload = self.model_dump(mode="json", exclude={"decision_sha256"})
        if _sha_bytes(canonical_json(payload)) != self.decision_sha256:
            raise ValueError("human decision self-hash is invalid")
        return self


def _rank(catalog_hash: str, rule: str, key: str) -> str:
    return _sha_text(f"{catalog_hash}|{rule}|{key}")


def _execution_coordinate(
    *,
    schedule_id: ScheduleId,
    campaign_root: str,
    ordinal: int,
    variant: BenchmarkVariantAuthorityV1,
    repetition_id: int,
) -> ExecutionCoordinateV1:
    key = f"{variant.case_id}|{variant.variant_id}|{repetition_id}"
    digest = _sha_text(f"{PA5D_ROOT_SEED}|{BASE_COMMIT}|{schedule_id}|{key}")
    execution_id = f"benchmark-{digest[:32]}"
    child_run_id = f"pa5d-child-{digest[:32]}"
    run_token = f"pa5d-{digest[32:64]}"
    child_path = f"{campaign_root}/child-authority/{execution_id}.json"
    workflow_root = f"{campaign_root}/children/{variant.case_id}-{run_token}"
    action_root = f"{workflow_root}/physics-auditor/{execution_id}"
    child_payload = {
        "case_id": variant.case_id,
        "child_run_id": child_run_id,
        "pair_id": variant.pair_id,
        "repetition_id": repetition_id,
        "run_token": run_token,
        "schedule_id": schedule_id,
        "variant_id": variant.variant_id,
        "workflow_run_root": workflow_root,
    }
    prompt_authority = _sha_text(
        "|".join(
            (
                PHYSICS_AUDITOR_PROMPT_TEMPLATE_SHA256,
                variant.visible_manifest_sha256,
                variant.contract_sha256,
                execution_id,
                "qualified_renderer_exact_inputs_bind_rendered_hash_before_launch",
            )
        )
    )
    return ExecutionCoordinateV1(
        ordinal=ordinal,
        execution_id=execution_id,
        case_id=variant.case_id,
        pair_id=variant.pair_id,
        variant_id=variant.variant_id,
        repetition_id=repetition_id,
        child_run_id=child_run_id,
        child_run_token=run_token,
        child_authority_path=child_path,
        child_authority_sha256=_sha_bytes(canonical_json(child_payload)),
        workflow_run_root=workflow_root,
        pa3_action_root=action_root,
        prompt_authority_sha256=prompt_authority,
        session_policy="one_fresh_ephemeral_session_no_resume",
    )


def _schedule(
    schedule_id: ScheduleId,
    variants: tuple[BenchmarkVariantAuthorityV1, ...],
    catalog_hash: str,
) -> BenchmarkScheduleAuthorityV1:
    by_key = {f"{item.case_id}/{item.variant_id}": item for item in variants}
    omitted_keys: tuple[str, ...]
    if schedule_id == "schedule_maximum_variant_coverage_v1":
        omitted = max(by_key, key=lambda key: (_rank(catalog_hash, schedule_id, key), key))
        selected = [(item, 1) for key, item in by_key.items() if key != omitted]
        tradeoff = (
            "Maximizes distinct catalog-variant coverage (41 of 42); no exact variant is "
            "repeated, so repeat consistency is not applicable."
        )
        rule = (
            "Enumerate all 42 catalog variants; omit the unique variant with the greatest "
            "SHA-256 rank of catalog_hash|schedule_id|case/variant; run every remainder once."
        )
        omitted_keys = (omitted,)
    else:
        clean = {key: item for key, item in by_key.items() if item.fixture_label == "clean"}
        defective = {key: item for key, item in by_key.items() if item.fixture_label == "defective"}
        omitted_clean = max(
            clean, key=lambda key: (_rank(catalog_hash, f"{schedule_id}|omit-clean", key), key)
        )
        omitted_defective = max(
            defective,
            key=lambda key: (_rank(catalog_hash, f"{schedule_id}|omit-defective", key), key),
        )
        remaining = {
            key: item
            for key, item in by_key.items()
            if key not in {omitted_clean, omitted_defective}
        }
        repeated = max(
            remaining,
            key=lambda key: (_rank(catalog_hash, f"{schedule_id}|repeat", key), key),
        )
        selected = [(item, 1) for item in remaining.values()] + [(remaining[repeated], 2)]
        tradeoff = (
            "Retains 40 distinct variants with balanced omission of one clean and one defective "
            "variant, and adds one exact repeat to make repeat consistency observable once."
        )
        rule = (
            "SHA-256-rank clean and defective variants separately to omit one of each, then "
            "SHA-256-rank the 40 remaining variants and repeat the greatest-ranked variant."
        )
        omitted_keys = tuple(sorted((omitted_clean, omitted_defective)))
    selected.sort(key=lambda item: (item[0].case_id, item[0].variant_id, item[1]))
    namespace = _sha_text(f"{PA5D_ROOT_SEED}|{BASE_COMMIT}|{catalog_hash}|{schedule_id}")[:24]
    root = f"runs/pa5d1-preregistered-v1/benchmark-{namespace}"
    executions = tuple(
        _execution_coordinate(
            schedule_id=schedule_id,
            campaign_root=root,
            ordinal=index,
            variant=variant,
            repetition_id=repetition,
        )
        for index, (variant, repetition) in enumerate(selected, start=1)
    )
    child_hash = _sha_bytes(canonical_json([item.model_dump(mode="json") for item in executions]))
    return BenchmarkScheduleAuthorityV1(
        schedule_id=schedule_id,
        status="candidate_not_selected",
        unit_of_execution="one_case_variant_repetition_per_fresh_pa3_session",
        neutral_rule=rule,
        scientific_tradeoff=tradeoff,
        catalog_variant_count=42,
        catalog_sha256=catalog_hash,
        distinct_variant_count=len({(item.case_id, item.variant_id) for item in executions}),
        repeated_execution_count=41 - len({(item.case_id, item.variant_id) for item in executions}),
        omitted_variant_keys=omitted_keys,
        ordering_policy="lexicographic_case_variant_repetition",
        action_root_derivation=(
            "namespace=first24(sha256(PA5D_ROOT_SEED|base_commit|catalog_hash|schedule_id)); "
            "per-run digest=sha256(PA5D_ROOT_SEED|base_commit|schedule_id|"
            "case_id|variant_id|repetition_id); exact roots are enumerated below"
        ),
        prior_action_or_session_reuse_allowed=False,
        campaign_root=root,
        expected_child_set_sha256=child_hash,
        executions=executions,
    )


def _metric_definitions() -> tuple[MetricDefinitionV1, ...]:
    raw = (
        (
            "answer_key_exposure_count",
            "count",
            "verified answer-key or scorer-only exposure events",
            "complete calibration",
            "Count any scorer-only byte reachable by an Auditor launch namespace",
            "both",
        ),
        (
            "session_reuse_count",
            "count",
            "provider session identities observed more than once",
            "complete calibration",
            "Count every reuse beyond the first occurrence",
            "both",
        ),
        (
            "duplicate_external_action_count",
            "count",
            "duplicate semantic external action identities",
            "complete calibration",
            "Use durable PA-5C3-style action identity, not process count",
            "both",
        ),
        (
            "unverified_pa2_pa3_evidence_accepted_count",
            "count",
            "accepted evidence objects lacking exact qualified proof closure",
            "complete calibration",
            "Count every accepted unverified PA-2 or PA-3 object",
            "both",
        ),
        (
            "run_identity_bijection",
            "consistency",
            "expected identities matched exactly once",
            "complete expected identity set",
            "Exact equality of expected and observed identity hashes and repetition keys",
            "both",
        ),
        (
            "malformed_report_fail_closed",
            "consistency",
            "malformed reports producing no accepted scientific score",
            "all malformed reports",
            "Every malformed report must remain a separate failed-closed observation",
            "both",
        ),
        (
            "scorer_accessible_to_auditor",
            "count",
            "Auditor launch namespaces containing any scorer authority",
            "all Auditor launches",
            "Inspect exact PA-5C1 launch manifests and mounted bytes",
            "both",
        ),
        (
            "scientific_authority_mutation_count",
            "count",
            "post-freeze scientific authority byte changes",
            "complete calibration",
            "Count any change to a canonical preregistered scientific input",
            "both",
        ),
        (
            "post_outcome_scientific_change_count",
            "count",
            "scientific design changes after any outcome visibility",
            "complete calibration",
            "Any such change invalidates the calibration in addition to being counted",
            "both",
        ),
        (
            "critical_defect_recognition_rate",
            "rate",
            "eligible critical defect runs recognized",
            "eligible runs whose scorer minimum severity is critical",
            "PA-5C2 category recognition must be correct and at least one finding "
            "must meet critical severity",
            "benchmark_and_per_category",
        ),
        (
            "defect_category_recognition_rate",
            "rate",
            "defective runs with correct defect-category recognition",
            "defective routed reports eligible under PA-5C2",
            "Use PA-5C2 defect_category_recognition without substituting route or severity",
            "benchmark_and_per_category",
        ),
        (
            "false_pass_rate",
            "rate",
            "defective runs deterministically routed pass",
            "defective runs with a validated route",
            "A pass on any catalog-labeled defective variant is a false pass",
            "benchmark_and_per_category",
        ),
        (
            "clean_pass_rate",
            "rate",
            "clean runs satisfying PA-5C2 clean_case_pass",
            "clean runs with a validated report",
            "Use PA-5C2 clean_case_pass; findings or a non-pass route make it incorrect",
            "benchmark_and_per_category",
        ),
        (
            "false_critical_finding_rate",
            "rate",
            "clean runs containing any open critical finding",
            "clean runs with a validated report",
            "Count per run, independently of clean-pass status",
            "benchmark_and_per_category",
        ),
        (
            "repair_routing_rate",
            "rate",
            "request_repair-expected runs routed request_repair",
            "catalog variants whose expected route is request_repair",
            "Compare the PA-1 deterministic route with scorer authority",
            "benchmark_and_per_category",
        ),
        (
            "human_escalation_rate",
            "rate",
            "human-review-expected runs routed require_human_review",
            "catalog variants whose expected route is require_human_review",
            "Compare the PA-1 deterministic route with scorer authority",
            "both",
        ),
        (
            "gl_expected_pass_route_rate",
            "rate",
            "expected-pass GL tasks routed pass",
            "locked GL tasks whose expected route is pass",
            "Compare deterministic PA-1 route with each GL task scorer authority",
            "gl",
        ),
        (
            "insufficient_evidence_routing_rate",
            "rate",
            "insufficient-evidence-expected runs routed block_insufficient_evidence",
            "catalog variants whose expected route is block_insufficient_evidence",
            "Compare the PA-1 deterministic route with scorer authority",
            "benchmark_and_per_category",
        ),
        (
            "malformed_report_rate",
            "rate",
            "runs with PA-5C2 malformed_report true",
            "all expected runs",
            "Malformed output remains separate and must fail closed",
            "both",
        ),
        (
            "infrastructure_failure_rate",
            "rate",
            "runs with PA-5C2 infrastructure_failure true",
            "all expected runs",
            "Infrastructure failure is never reclassified as scientific failure",
            "both",
        ),
        (
            "severity_correctness_rate",
            "rate",
            "PA-5C2 severity_correctness correct",
            "PA-5C2 severity-eligible runs",
            "Use PA-5C2 tri-state denominator",
            "both",
        ),
        (
            "required_category_recognition_rate",
            "rate",
            "PA-5C2 required_categories correct",
            "PA-5C2 required-category-eligible runs",
            "All required categories must be satisfied",
            "both",
        ),
        (
            "acceptable_alternative_recognition_rate",
            "rate",
            "PA-5C2 acceptable_alternatives correct",
            "PA-5C2 alternative-eligible runs",
            "Approved alternative categories only; no free semantic equivalence",
            "benchmark_and_per_category",
        ),
        (
            "forbidden_category_violation_rate",
            "rate",
            "runs with PA-5C2 forbidden_categories incorrect",
            "PA-5C2 forbidden-category-eligible runs",
            "Incorrect means at least one forbidden category occurred",
            "both",
        ),
        (
            "forbidden_route_violation_rate",
            "rate",
            "runs with PA-5C2 forbidden_routes incorrect",
            "PA-5C2 forbidden-route-eligible runs",
            "Incorrect means the deterministic route was forbidden",
            "both",
        ),
        (
            "evidence_validity_rate",
            "rate",
            "PA-5C2 evidence_validity correct",
            "PA-5C2 evidence-eligible runs",
            "Every retained evidence reference must verify",
            "both",
        ),
        (
            "route_consistency_rate",
            "consistency",
            "run pairs for one case with routes matching each variant's scorer authority",
            "case pairs for which both scheduled variants produced validated routes",
            "Pair comparison is separate from each run's route correctness",
            "benchmark_and_per_category",
        ),
        (
            "finding_category_consistency_rate",
            "consistency",
            "run pairs whose findings obey both variants' required/forbidden category authorities",
            "case pairs for which both scheduled variants produced validated reports",
            "Pair-level category consistency does not replace PA-5C2 per-run criteria",
            "benchmark_and_per_category",
        ),
        (
            "repeat_consistency_rate",
            "consistency",
            "exact repeated variant pairs with identical route and finding-category set",
            "exact variant pairs having repetition 1 and 2 and no infrastructure/malformed result",
            "A zero denominator is not applicable, never perfect consistency",
            "benchmark",
        ),
        (
            "duration_seconds",
            "duration",
            "sum of authoritative PA-3 wall durations",
            "runs exposing verified duration",
            "Report per-run, median, p95, minimum, maximum, and sum; never impute missing values",
            "both",
        ),
        (
            "authoritative_input_tokens",
            "token_usage",
            "sum of provider-authoritative input tokens",
            "runs exposing that counter",
            "Report exposed counters and missing count; never estimate",
            "both",
        ),
        (
            "authoritative_output_tokens",
            "token_usage",
            "sum of provider-authoritative output tokens",
            "runs exposing that counter",
            "Report output and reasoning-output separately when exposed; never estimate",
            "both",
        ),
        (
            "authoritative_combined_tokens",
            "token_usage",
            "sum of provider-authoritative combined tokens",
            "runs exposing that counter or both exact input/output",
            "Do not synthesize a split when only a combined counter exists",
            "both",
        ),
    )
    return tuple(
        MetricDefinitionV1(
            metric_id=metric_id,
            kind=cast(Any, kind),
            numerator=numerator,
            denominator=denominator,
            exact_rule=rule,
            zero_denominator="not_applicable",
            aggregation=cast(Any, aggregation),
        )
        for metric_id, kind, numerator, denominator, rule, aggregation in raw
    )


def _thresholds() -> tuple[ThresholdAuthorityV1, ...]:
    structural = (
        (
            "zero_answer_key_exposure",
            "answer_key_exposure_count",
            "==",
            0,
            "PA-5C1 blindness authority",
        ),
        (
            "zero_session_reuse",
            "session_reuse_count",
            "==",
            0,
            "PA-3 fresh-ephemeral and PA-5C structural authority",
        ),
        (
            "zero_duplicate_external_actions",
            "duplicate_external_action_count",
            "==",
            0,
            "PA-5A/PA-5C3 exactly-once recovery authority",
        ),
        (
            "zero_unverified_evidence_accepted",
            "unverified_pa2_pa3_evidence_accepted_count",
            "==",
            0,
            "PA-5C1/PA-5C2 proof closure",
        ),
        (
            "exact_run_identity_bijection",
            "run_identity_bijection",
            "==",
            "exact",
            "PA-5C2 expected/observed identity equality",
        ),
        (
            "malformed_reports_fail_closed",
            "malformed_report_fail_closed",
            "==",
            True,
            "PA-3/PA-5C2 strict report boundary",
        ),
        (
            "scorer_inaccessible_to_auditor",
            "scorer_accessible_to_auditor",
            "==",
            False,
            "PA-5C1 disjoint namespace",
        ),
        (
            "scientific_authority_frozen",
            "scientific_authority_mutation_count",
            "==",
            0,
            "PA-5C3 frozen manifest and PA-5C4 Start authority",
        ),
        (
            "zero_post_outcome_changes",
            "post_outcome_scientific_change_count",
            "==",
            0,
            "Prospective calibration immutability",
        ),
    )
    values: list[ThresholdAuthorityV1] = [
        ThresholdAuthorityV1(
            threshold_id=identifier,
            metric_id=metric,
            operator=cast(Any, operator),
            value=value,
            gate_kind="structural_hard_gate",
            authority_status="QUALIFIED_PRE_OUTCOME",
            application_scope="global",
            rationale=rationale,
            decision_id=None,
        )
        for identifier, metric, operator, value, rationale in structural
    ]
    proposed = (
        (
            "critical_defect_recognition_rate",
            "NO_GATE",
            None,
            "The qualified catalog has zero critical-minimum variants; metric is N/A and the "
            "proposal is explicitly no acceptance gate.",
        ),
        ("defect_category_recognition_rate", ">=", 0.90, "Proposed broad category-quality floor."),
        ("false_pass_rate", "==", 0.0, "A known defective fixture should not pass."),
        ("clean_pass_rate", ">=", 0.90, "Proposed clean-fixture specificity floor."),
        (
            "false_critical_finding_rate",
            "==",
            0.0,
            "A clean fixture should not receive a critical finding.",
        ),
        ("repair_routing_rate", ">=", 0.90, "Proposed repair-route quality floor."),
        ("human_escalation_rate", "==", 1.0, "All frozen human-gate cases should escalate."),
        (
            "gl_expected_pass_route_rate",
            "==",
            1.0,
            "All locked expected-pass GL tasks should pass.",
        ),
        ("insufficient_evidence_routing_rate", "==", 1.0, "Missing authority must fail closed."),
        (
            "malformed_report_rate",
            "==",
            0.0,
            "Proposed output-quality gate, separate from fail-closed handling.",
        ),
        (
            "infrastructure_failure_rate",
            "<=",
            0.0,
            "Proposed requirement for a complete interpretable calibration.",
        ),
        ("severity_correctness_rate", ">=", 0.90, "Proposed severity-quality floor."),
        ("required_category_recognition_rate", ">=", 0.90, "Proposed required-category floor."),
        (
            "acceptable_alternative_recognition_rate",
            ">=",
            0.90,
            "Proposed approved-alternative floor.",
        ),
        (
            "forbidden_category_violation_rate",
            "==",
            0.0,
            "Forbidden categories should never appear.",
        ),
        (
            "forbidden_route_violation_rate",
            "==",
            0.0,
            "Forbidden deterministic routes should never occur.",
        ),
        (
            "evidence_validity_rate",
            "==",
            1.0,
            "Every accepted scientific claim must cite verified evidence.",
        ),
        ("route_consistency_rate", ">=", 0.95, "Proposed paired-route consistency floor."),
        (
            "finding_category_consistency_rate",
            ">=",
            0.95,
            "Proposed paired-category consistency floor.",
        ),
        (
            "repeat_consistency_rate",
            "==",
            1.0,
            "If repeats are selected, their semantic outputs should agree.",
        ),
    )
    values.extend(
        ThresholdAuthorityV1(
            threshold_id=f"performance_{metric}",
            metric_id=metric,
            operator=cast(Any, operator),
            value=value,
            gate_kind="performance_gate",
            authority_status="HUMAN_AUTHORITY_REQUIRED",
            application_scope=(
                "aggregate_and_each_nonzero_declared_category"
                if metric
                in {
                    "defect_category_recognition_rate",
                    "false_pass_rate",
                    "clean_pass_rate",
                    "false_critical_finding_rate",
                    "repair_routing_rate",
                    "human_escalation_rate",
                    "insufficient_evidence_routing_rate",
                    "severity_correctness_rate",
                    "required_category_recognition_rate",
                    "acceptable_alternative_recognition_rate",
                    "forbidden_category_violation_rate",
                    "forbidden_route_violation_rate",
                    "evidence_validity_rate",
                }
                else "global"
            ),
            rationale=f"{rationale} This value is proposed, not authorized.",
            decision_id=f"approve_threshold_{metric}",
        )
        for metric, operator, value, rationale in proposed
    )
    for metric in (
        "duration_seconds",
        "authoritative_input_tokens",
        "authoritative_output_tokens",
        "authoritative_combined_tokens",
    ):
        values.append(
            ThresholdAuthorityV1(
                threshold_id=f"descriptive_{metric}",
                metric_id=metric,
                operator="NO_GATE",
                value=None,
                gate_kind="descriptive",
                authority_status="NOT_AN_ACCEPTANCE_GATE",
                application_scope="not_applicable_descriptive",
                rationale=(
                    "Observational resource metric; report exactly where exposed without "
                    "acceptance tuning."
                ),
                decision_id=None,
            )
        )
    return tuple(values)


def _variant_authorities(
    repository: Path, catalog: PhysicsBlindFixtureCatalogV1
) -> tuple[BenchmarkVariantAuthorityV1, ...]:
    result: list[BenchmarkVariantAuthorityV1] = []
    for pair in catalog.pairs:
        paired = build_paired_visible_manifest(pair, repository_root=repository)
        by_variant = {item.variant_id: item for item in paired.variants}
        receipt = repository / pair.receipt_path
        for variant in pair.variants:
            visible = by_variant[variant.variant_id]
            result.append(
                BenchmarkVariantAuthorityV1(
                    case_id=pair.case_id,
                    pair_id=pair.pair_id,
                    variant_id=cast(Any, variant.variant_id),
                    fixture_label=variant.fixture_label,
                    expected_route=variant.expected_route,
                    minimum_severity=cast(Any, variant.minimum_severity),
                    required_categories=variant.required_categories,
                    acceptable_alternative_categories=(variant.acceptable_alternative_categories),
                    forbidden_categories=variant.forbidden_categories,
                    visible_root=variant.visible_root,
                    visible_manifest_sha256=visible.canonical_sha256(),
                    paired_visible_manifest_sha256=paired.canonical_sha256(),
                    scorer_authority_sha256=pair.canonical_sha256(),
                    review_receipt=FileAuthorityV1(
                        path=pair.receipt_path, sha256=_file_sha256(receipt)
                    ),
                    contract_sha256=paired.contract_sha256,
                )
            )
    return tuple(sorted(result, key=lambda item: (item.case_id, item.variant_id)))


def _gl_task(
    *,
    repository: Path,
    source_repository: Path,
    catalog: PhysicsBlindFixtureCatalogV1,
    task: GLFixtureAuthorityV1,
    ordinal: int,
    root: str,
) -> GLTaskExecutionAuthorityV1:
    manifest = build_gl_visible_manifest(
        task,
        repository_root=repository,
        source_repository_root=source_repository,
        source_commit=catalog.gl_source_commit,
    )
    digest = _sha_text(f"{PA5D_ROOT_SEED}|{BASE_COMMIT}|gl|{task.task_id}")
    child_payload = {
        "source_commit": catalog.gl_source_commit,
        "task_id": task.task_id,
        "visible_manifest_sha256": manifest.canonical_sha256(),
    }
    return GLTaskExecutionAuthorityV1(
        ordinal=ordinal,
        task_id=task.task_id,
        visible_root=task.visible_root,
        visible_manifest_sha256=manifest.canonical_sha256(),
        scorer_authority_sha256=task.canonical_sha256(),
        review_receipt=FileAuthorityV1(
            path=task.receipt_path,
            sha256=_file_sha256(repository / task.receipt_path),
        ),
        expected_route=cast(Any, task.expected_route),
        minimum_severity=cast(Any, task.minimum_severity),
        required_categories=task.required_categories,
        forbidden_categories=task.forbidden_categories,
        source_blobs=tuple(
            GLSourceBlobAuthorityV1(
                path=item.path,
                role=item.role,
                byte_length=item.byte_length,
                sha256=item.sha256,
            )
            for item in task.source_blobs
        ),
        execution_id=f"gl-{digest[:32]}",
        child_run_id=f"pa5d-gl-child-{digest[:32]}",
        child_authority_sha256=_sha_bytes(canonical_json(child_payload)),
        action_root=f"{root}/{ordinal:02d}-{task.task_id}-{digest[:16]}",
        prompt_authority_sha256=_sha_text(
            "|".join(
                (
                    PHYSICS_AUDITOR_PROMPT_TEMPLATE_SHA256,
                    manifest.canonical_sha256(),
                    f"gl-{digest[:32]}",
                    "qualified_renderer_exact_inputs_bind_rendered_hash_before_launch",
                )
            )
        ),
        one_fresh_session=True,
    )


def _self_hashed_authority(payload: Mapping[str, object]) -> PA5DCalibrationAuthorityV1:
    digest = _sha_bytes(canonical_json(payload))
    return PA5DCalibrationAuthorityV1.model_validate_json(
        canonical_json({**payload, "authority_sha256": digest})
    )


def build_review_authority(repository_root: Path) -> PA5DPreregistrationReviewAuthorityV1:
    repository = repository_root.resolve(strict=True)
    source_repository = (repository / GL_SOURCE_RELATIVE).resolve(strict=True)
    catalog_path = repository / CATALOG_RELATIVE
    catalog = load_blind_fixture_catalog(catalog_path)
    qualification = qualify_fixture_authority(
        catalog_path,
        repository_root=repository,
        source_repository_root=source_repository,
    )
    if (
        qualification.canonical_sha256()
        != "1fdb54d40ae2828225be35966ab7844ca3114fb6e3f6d730089f0a987576056b"
    ):
        raise ValueError("fresh PA-5C1 fixture qualification changed")
    review_packet = FixtureReviewPacketV1.model_validate_json(
        (
            repository / "examples/physics_auditor/benchmark_v1/scorer_only/review-packet.json"
        ).read_bytes()
    )
    if (
        review_packet.review_packet_sha256
        != "4aafec7dd51ab66e8190699a72a9514e46c3228379d0d835e2b1c1220cd34cfb"
    ):
        raise ValueError("qualified PA-5C1 review packet changed")
    variants = _variant_authorities(repository, catalog)
    if len(variants) != 42 or len({item.case_id for item in variants}) != 21:
        raise ValueError("qualified catalog is not the exact 21-pair/42-variant authority")
    schedules = tuple(
        _schedule(cast(ScheduleId, schedule_id), variants, catalog.canonical_sha256())
        for schedule_id in (
            "schedule_maximum_variant_coverage_v1",
            "schedule_balanced_single_repeat_v1",
        )
    )
    config_path = repository / CONFIG_RELATIVE
    config: PhysicsAuditorExecutionConfigV1 = load_physics_auditor_execution_config(config_path)
    output_schema_sha = _sha_bytes(canonical_json(PHYSICS_AUDIT_REPORT_OUTPUT_SCHEMA))
    model = ModelConfigurationAuthorityV1(
        status="HUMAN_AUTHORITY_REQUIRED",
        decision_id="approve_pa3_qualified_model_configuration_v1",
        qualified_source=FileAuthorityV1(path=CONFIG_RELATIVE, sha256=_file_sha256(config_path)),
        execution_config_sha256=config.canonical_sha256(),
        config=config.model_dump(mode="json"),
        codex_cli_version="codex-cli 0.146.0",
        codex_executable_sha256=(
            "2e863156ed35ecc5253b1e2f907a9143077b9f7cb51942070c61996471ff6e04"
        ),
        bubblewrap_version="bubblewrap 0.11.1",
        bubblewrap_backend_identity_sha256=(
            "e7ffcfcfc7611c7cc6bb5913841e6385684429b8ab9b0c4c7b7c506fd94eaedc"
        ),
        runtime_identity_drift_allowed=False,
        role_policy_sha256=PHYSICS_AUDITOR_CODEX_ROLE_POLICY_V1.canonical_sha256(),
        bubblewrap_policy_sha256=PHYSICS_AUDITOR_BUBBLEWRAP_POLICY_V1.canonical_sha256(),
        prompt_template_version="physics_auditor_prompt_v2",
        prompt_template_sha256=PHYSICS_AUDITOR_PROMPT_TEMPLATE_SHA256,
        prompt_renderer=FileAuthorityV1(
            path="src/research_automation_supervisor/physics_auditor_prompts.py",
            sha256=_file_sha256(
                repository / "src/research_automation_supervisor/physics_auditor_prompts.py"
            ),
        ),
        rendered_prompt_policy="qualified_renderer_over_exact_run_inputs_then_bind_rendered_sha256_before_launch",
        output_schema_id="physics_audit_report_v1",
        output_schema_sha256=output_schema_sha,
        fresh_session_required=True,
        resume_allowed=False,
        yolo_or_full_access_allowed=False,
        blindness_certificate_required_before_launch=True,
        scorer_namespace_present_in_auditor_sandbox=False,
        same_configuration_for_benchmark_and_gl=True,
    )
    gl_namespace = _sha_text(f"{PA5D_ROOT_SEED}|{BASE_COMMIT}|{catalog.canonical_sha256()}|gl")[:24]
    gl_root = f"runs/pa5d1-preregistered-v1/gl-{gl_namespace}"
    gl_tasks = tuple(
        _gl_task(
            repository=repository,
            source_repository=source_repository,
            catalog=catalog,
            task=task,
            ordinal=index,
            root=gl_root,
        )
        for index, task in enumerate(catalog.gl_tasks, start=1)
    )
    gl = GLPilotAuthorityV1(
        source_commit=catalog.gl_source_commit,
        catalog_sha256=catalog.canonical_sha256(),
        unit_of_execution="one_locked_gl_task_per_fresh_pa3_session",
        task_order_policy="catalog_declared_order",
        model_configuration_binding="same_exact_pa3_configuration_as_benchmark",
        projected_source_policy="pa5c1_exact_locked_blobs_plus_reviewed_visible_fixture_only",
        evidence_policy="pa5c1_pa2_pa3_verified_evidence_only",
        production_gl_mode_claim_allowed=False,
        action_root_derivation=(
            "namespace=first24(sha256(PA5D_ROOT_SEED|base_commit|catalog_hash|gl)); "
            "per-task digest=sha256(PA5D_ROOT_SEED|base_commit|gl|task_id); exact roots "
            "are enumerated below"
        ),
        prior_action_or_session_reuse_allowed=False,
        pilot_root=gl_root,
        expected_child_set_sha256=_sha_bytes(
            canonical_json([item.model_dump(mode="json") for item in gl_tasks])
        ),
        tasks=gl_tasks,
    )
    execution_policy = CalibrationExecutionPolicyV1(
        status="HUMAN_AUTHORITY_REQUIRED",
        decision_id="approve_one_shot_calibration_execution_policy_v1",
        benchmark_orchestrator="pa5d_one_shot_pa3_adapter_required_before_launch",
        gl_orchestrator="pa5d_one_shot_gl_adapter_required_before_launch",
        pa5c3_semantics_reused=(
            "frozen_complete_child_set",
            "sequential_deterministic_order",
            "durable_intent_before_external_action",
            "zero_duplicate_external_actions",
            "exact_terminal_proof_rebinding",
        ),
        ordinary_pa4_worker_or_repair_loop_used=False,
        route_is_observation_not_workflow_command=True,
        pa3_sessions_per_coordinate=1,
        benchmark_pa3_session_total=41,
        gl_pa3_session_total=10,
        nonpass_route_terminalization=(
            "persist_verified_pa3_proof_and_score_without_worker_repair_or_human_override"
        ),
        infrastructure_recovery=("resume_only_same_proven_action_otherwise_invalidate_calibration"),
        implementation_gate=(
            "deterministic_adapter_tests_and_independent_review_required_before_any_launch"
        ),
    )
    metric_policy = MetricAndScoringPolicyV1(
        status="HUMAN_AUTHORITY_REQUIRED",
        decision_id="approve_metric_and_gl_scoring_policy_v1",
        benchmark_source=("pa5c2_exact_run_semantics_plus_preregistered_derived_metrics"),
        per_category_assignment=(
            "each_run_contributes_to_each_declared_required_or_acceptable_category_no_inference"
        ),
        per_category_threshold_application=(
            "apply_to_aggregate_and_every_declared_category_with_nonzero_denominator"
        ),
        semantic_failure_denominator_policy=(
            "inherit_pa5c2_metric_specific_tri_state_exactly_no_global_malformed_or_"
            "infrastructure_override"
        ),
        cross_domain_aggregation=(
            "benchmark_and_gl_are_separate_cohorts_each_applicable_threshold_must_pass_"
            "no_cross_domain_pooling"
        ),
        pair_definition=(
            "same_case_repetition_one_both_variants_validated_no_infrastructure_or_malformed"
        ),
        repeat_definition=(
            "same_case_variant_repetitions_one_and_two_equal_route_and_finding_category_set"
        ),
        duration_summary="per_run_min_median_nearest_rank_p95_max_sum_no_imputation",
        token_summary=(
            "provider_authoritative_input_plus_output_combined_reasoning_subset_not_added_"
            "twice_no_estimation"
        ),
        gl_scoring=(
            "derive_same_separate_route_category_severity_evidence_malformed_and_"
            "infrastructure_criteria_from_each_locked_task_authority"
        ),
        gl_expected_pass_metric="gl_expected_pass_route_rate",
        gl_expected_human_metric="human_escalation_rate",
        gl_production_claim=False,
        implementation_gate=(
            "deterministic_gl_scorer_tests_and_independent_review_required_before_any_launch"
        ),
    )
    sources = _qualified_sources(repository, catalog)
    recovery = RecoveryAndHumanActionAuthorityV1(
        recovery_sources=(
            sources.recovery_authority,
            sources.campaign_authority,
            sources.custodian_authority,
        ),
        allowed_automatic_recovery_dispositions=("auto_resume", "finish_finalization"),
        infrastructure_recovery_rule=(
            "Rebuild PA-5A/PA-5C3 authority and continue only an already-identical action; "
            "ambiguous post-launch intent, stale/reused process identity, or missing proof "
            "hard-stops without relaunch."
        ),
        scientific_inputs_frozen_rule=(
            "Schedule, fixtures, scorer, metrics, thresholds, model/configuration, prompt "
            "renderer, action roots, and GL authority remain byte-identical through recovery."
        ),
        human_action_rule=(
            "Only an exact create-once PA-5C4/C4-U request/response bound to the current "
            "durable head may resolve an authorized pause; campaign orchestration itself "
            "accepts no human decision."
        ),
        invalidating_actions=(
            "any post-outcome prompt or prompt-renderer change",
            "any post-outcome threshold or metric change",
            "any fixture, receipt, catalog, scorer, source-blob, or expected-route change",
            "any model, reasoning, timeout, sandbox, isolation, or session-policy change",
            "any schedule, ordering, repetition, child-set, action-root, or GL-task change",
            "resuming or reusing a provider session",
            "relaunching an ambiguous external action",
            "accepting unverified PA-2 or PA-3 evidence",
        ),
        post_outcome_change_rule=(
            "Invalidate the entire calibration; never amend, tune, or continue it. A different "
            "design requires a new versioned preregistration and entirely fresh roots/sessions."
        ),
        duplicate_external_action_rule=(
            "Zero duplicate external actions; ambiguous launch state blocks and is not retried."
        ),
    )
    contamination = (
        ContaminationRegisterEntryV1(
            identity="invalidated_pa5b_branch_tip",
            commit="344d55c53899f8e030826cfefa76d1438e50e4f8",
            artifact=None,
            allowed_use="contamination_history_only",
            forbidden_uses=(
                "schedule",
                "thresholds",
                "prompt wording",
                "expected routes",
                "model configuration",
                "repetition selection",
                "scoring policy",
            ),
        ),
        ContaminationRegisterEntryV1(
            identity="pa5d_prelaunch_hard_stop_history",
            commit=BASE_COMMIT,
            artifact=FileAuthorityV1(
                path="docs/validation/physics_auditor_pa5d_failure.json",
                sha256=_file_sha256(
                    repository / "docs/validation/physics_auditor_pa5d_failure.json"
                ),
            ),
            allowed_use="contamination_history_only",
            forbidden_uses=(
                "scientific outcomes",
                "schedule",
                "thresholds",
                "prompt wording",
                "model configuration",
                "scoring policy",
            ),
        ),
    )
    metrics = _metric_definitions()
    thresholds = _thresholds()
    candidates = tuple(
        _self_hashed_authority(
            {
                "schema_version": 1,
                "authority_type": "PA5DCalibrationAuthorityV1",
                "approval_state": "DRAFT_HUMAN_AUTHORITY_REQUIRED",
                "base_commit": BASE_COMMIT,
                "schedule": schedule.model_dump(mode="json"),
                "benchmark_catalog_variants": [item.model_dump(mode="json") for item in variants],
                "model_configuration": model.model_dump(mode="json"),
                "metrics": [item.model_dump(mode="json") for item in metrics],
                "thresholds": [item.model_dump(mode="json") for item in thresholds],
                "gl_pilot": gl.model_dump(mode="json"),
                "execution_policy": execution_policy.model_dump(mode="json"),
                "metric_and_scoring_policy": metric_policy.model_dump(mode="json"),
                "recovery_and_human_action": recovery.model_dump(mode="json"),
                "contamination_register": [item.model_dump(mode="json") for item in contamination],
                "human_decision_sha256": None,
            }
        )
        for schedule in schedules
    )
    decisions: list[RequiredHumanDecisionV1] = [
        RequiredHumanDecisionV1(
            decision_id="select_benchmark_schedule_v1",
            subject="Exact 41-session schedule",
            allowed_values=tuple(item.schedule_id for item in schedules),
            consequence=(
                "Selects variant omissions, repetition coverage, ordering, and every fresh "
                "action/run root."
            ),
        ),
        RequiredHumanDecisionV1(
            decision_id=model.decision_id,
            subject="Exact PA-3 model and execution configuration",
            allowed_values=("approve_exact_qualified_pa3_configuration",),
            consequence=(
                "Binds gpt-5.6-sol/high and the complete qualified read-only fresh-session "
                "configuration for benchmark and GL."
            ),
        ),
        RequiredHumanDecisionV1(
            decision_id=execution_policy.decision_id,
            subject="One-shot benchmark and GL execution lifecycle",
            allowed_values=("approve_one_shot_calibration_execution_policy_v1",),
            consequence=(
                "Requires dedicated deterministic adapters, exactly one PA-3 session per "
                "coordinate, no ordinary PA-4 Worker/repair loop, and no scientific override."
            ),
        ),
        RequiredHumanDecisionV1(
            decision_id=metric_policy.decision_id,
            subject="Derived metric, per-category, pair/repeat, token, and GL scoring policy",
            allowed_values=("approve_metric_and_gl_scoring_policy_v1",),
            consequence=(
                "Authorizes every new pre-outcome aggregation definition and requires its "
                "deterministic implementation to qualify before launch."
            ),
        ),
    ]
    decisions.extend(
        RequiredHumanDecisionV1(
            decision_id=cast(str, item.decision_id),
            subject=f"Performance threshold {item.metric_id}: {item.operator} {item.value}",
            allowed_values=("approve_proposed",)
            if item.operator == "NO_GATE"
            else (
                "approve_proposed",
                "replace_with_explicit_operator_value_and_rationale",
            ),
            consequence=(
                "Confirms that the catalog-zero denominator has no numeric gate; changing this "
                "requires revising and re-reviewing the authority."
                if item.operator == "NO_GATE"
                else "Creates a normative pre-outcome performance gate; no proposed value is "
                "active until approved."
            ),
        )
        for item in thresholds
        if item.authority_status == HUMAN_AUTHORITY_REQUIRED
    )
    payload: dict[str, object] = {
        "schema_version": 1,
        "authority_type": "PA5DPreregistrationReviewAuthorityV1",
        "stage": "PA-5D0",
        "status": HUMAN_AUTHORITY_REQUIRED,
        "model_session_launch_capability": False,
        "qualified_sources": sources.model_dump(mode="json"),
        "schedule_alternatives": [item.model_dump(mode="json") for item in schedules],
        "candidate_authorities": [item.model_dump(mode="json") for item in candidates],
        "required_human_decisions": [item.model_dump(mode="json") for item in decisions],
        "final_approved_receipt_issued": False,
        "benchmark_sessions_launched": 0,
        "gl_sessions_launched": 0,
    }
    return PA5DPreregistrationReviewAuthorityV1.model_validate_json(
        canonical_json({**payload, "draft_sha256": _sha_bytes(canonical_json(payload))})
    )


def _qualified_sources(
    repository: Path, catalog: PhysicsBlindFixtureCatalogV1
) -> QualifiedAuthoritySourcesV1:
    def authority(path: str) -> FileAuthorityV1:
        return FileAuthorityV1(path=path, sha256=_file_sha256(repository / path))

    return QualifiedAuthoritySourcesV1(
        base_commit=BASE_COMMIT,
        pa5a_commit="5741f41bb91cc203152c758a9665e4dad43a2f85",
        pa5c1_commit="ce547648bdfbcc0c114bd06eccba711a3f4be8b7",
        pa5c2_commit="87be443dcb9e957aa056a065664ecd14699792ca",
        pa5c3_commit="0ee2bc91c067eab6efd9e3e115fad63c0f811a45",
        pa5c4_commit="df3553f584c6e9109c0e4561eab58e480a78b4b5",
        catalog_file=FileAuthorityV1(
            path=CATALOG_RELATIVE, sha256=_file_sha256(repository / CATALOG_RELATIVE)
        ),
        catalog_id=catalog.catalog_id,
        catalog_canonical_sha256=catalog.canonical_sha256(),
        review_packet_sha256="4aafec7dd51ab66e8190699a72a9514e46c3228379d0d835e2b1c1220cd34cfb",
        fixture_qualification_sha256="1fdb54d40ae2828225be35966ab7844ca3114fb6e3f6d730089f0a987576056b",
        scorer_root_manifest_sha256="4a4ea8e3ea95563381571d1745e8825b8f7a51280827d14a221324ea52436f79",
        frozen_pa5c_tree_sha256="8d7d7bb580bc0a14d899a20fc0ccb321c633488691323fd14c252e56c3096ae8",
        blindness_authority=authority("docs/physics_benchmark_blind_authority.md"),
        scoring_authority=authority("docs/physics_benchmark_exact_scoring.md"),
        campaign_authority=authority("docs/physics_benchmark_campaign.md"),
        recovery_authority=authority("docs/workflow_recovery.md"),
        custodian_authority=authority("docs/campaign_custodian.md"),
        pa3_qualification=authority("docs/validation/physics_auditor_pa3.json"),
        roadmap_pre_outcome_authority=authority("docs/roadmap/physics_auditor_v1.md"),
    )


def finalize_calibration_authority(
    review: PA5DPreregistrationReviewAuthorityV1,
    decisions: PA5DHumanDecisionsV1,
) -> PA5DCalibrationAuthorityV1:
    """Convert explicit exact decisions to authority, without issuing an approval receipt."""
    if decisions.review_draft_sha256 != review.draft_sha256:
        raise ValueError("human decisions target another review draft")
    selected = next(
        (
            item
            for item in review.candidate_authorities
            if item.schedule.schedule_id == decisions.selected_schedule_id
        ),
        None,
    )
    if selected is None:
        raise ValueError("selected schedule is unavailable")
    required = {
        item.threshold_id
        for item in selected.thresholds
        if item.authority_status == HUMAN_AUTHORITY_REQUIRED
    }
    observed = {item.threshold_id for item in decisions.threshold_decisions}
    if observed != required or len(observed) != len(decisions.threshold_decisions):
        raise ValueError("human threshold decisions must exactly cover every proposed gate")
    by_id = {item.threshold_id: item for item in decisions.threshold_decisions}
    thresholds: list[ThresholdAuthorityV1] = []
    for threshold in selected.thresholds:
        if threshold.authority_status != HUMAN_AUTHORITY_REQUIRED:
            thresholds.append(threshold)
            continue
        decision = by_id[threshold.threshold_id]
        if (
            threshold.metric_id == "critical_defect_recognition_rate"
            and decision.decision == "replace"
        ):
            raise ValueError(
                "the zero-denominator critical metric cannot acquire a numeric threshold"
            )
        operator = (
            threshold.operator
            if decision.decision == "approve_proposed"
            else cast(Any, decision.replacement_operator)
        )
        value = (
            threshold.value
            if decision.decision == "approve_proposed"
            else decision.replacement_value
        )
        thresholds.append(
            ThresholdAuthorityV1(
                threshold_id=threshold.threshold_id,
                metric_id=threshold.metric_id,
                operator=operator,
                value=value,
                gate_kind=threshold.gate_kind,
                authority_status="HUMAN_APPROVED",
                application_scope=threshold.application_scope,
                rationale=f"Human decision {decisions.decision_sha256}: {decision.rationale}",
                decision_id=None,
            )
        )
    model_payload = selected.model_configuration.model_dump(mode="json")
    model_payload["status"] = "HUMAN_APPROVED"
    execution_payload = selected.execution_policy.model_dump(mode="json")
    execution_payload["status"] = "HUMAN_APPROVED"
    metric_payload = selected.metric_and_scoring_policy.model_dump(mode="json")
    metric_payload["status"] = "HUMAN_APPROVED"
    payload = selected.model_dump(mode="json", exclude={"authority_sha256"})
    payload["approval_state"] = "HUMAN_APPROVED"
    schedule_payload = selected.schedule.model_dump(mode="json")
    schedule_payload["status"] = "human_selected"
    payload["schedule"] = schedule_payload
    payload["model_configuration"] = model_payload
    payload["execution_policy"] = execution_payload
    payload["metric_and_scoring_policy"] = metric_payload
    payload["thresholds"] = [item.model_dump(mode="json") for item in thresholds]
    payload["human_decision_sha256"] = decisions.decision_sha256
    return _self_hashed_authority(payload)
