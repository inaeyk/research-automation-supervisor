"""Strict public records for the PA-5B Physics Auditor quality benchmark."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any, Literal, TypeAlias

import yaml  # type: ignore[import-untyped]
from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from research_automation_supervisor.contract import (
    _format_validation_error,
    _UniqueKeySafeLoader,
)
from research_automation_supervisor.durable_state import canonical_json
from research_automation_supervisor.errors import PhysicsBenchmarkInputError
from research_automation_supervisor.physics_models import (
    PhysicsFindingCategory,
    PhysicsVerdict,
)
from research_automation_supervisor.physics_routing import PhysicsRoutingOutcome
from research_automation_supervisor.workflow_models import Identifier, _freeze_sequence

MAX_BENCHMARK_CASES = 100
MAX_BENCHMARK_RUNS = 1_000
MAX_BENCHMARK_FILE_BYTES = 8 * 1024 * 1024

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
BoundedText = Annotated[str, Field(min_length=1, max_length=4_000)]
NonnegativeRate = Annotated[float, Field(ge=0.0, le=1.0)]

PhysicsBenchmarkSeedKind: TypeAlias = Literal[
    "clean_reference",
    "wrong_sign",
    "missing_normalization",
    "missing_metric_factor",
    "raised_lowered_index",
    "dimensional_inconsistency",
    "nonzero_trace",
    "failed_analytic_identity",
    "curved_background_error",
    "continuum_discrete_translation",
    "finite_difference_stencil",
    "false_convergence_claim",
    "constraint_mode_claim",
    "gauge_mode_claim",
    "boundary_localization_claim",
    "norm_sensitivity_claim",
    "correct_alternative",
    "insufficient_evidence",
    "convention_change_request",
    "conflicting_evidence",
    "unsupported_interpretation",
]
PhysicsBenchmarkRisk: TypeAlias = Literal["highest", "mechanism"]
PhysicsBenchmarkRunStatus: TypeAlias = Literal[
    "routing_completed",
    "malformed_report",
    "infrastructure_failure",
]
UsageAvailability: TypeAlias = Literal["provider_reported", "unavailable"]
QualificationVerdict: TypeAlias = Literal["qualified", "not_qualified"]

REQUIRED_SEED_KINDS = frozenset(
    {
        "wrong_sign",
        "missing_normalization",
        "missing_metric_factor",
        "raised_lowered_index",
        "dimensional_inconsistency",
        "nonzero_trace",
        "failed_analytic_identity",
        "curved_background_error",
        "continuum_discrete_translation",
        "finite_difference_stencil",
        "false_convergence_claim",
        "constraint_mode_claim",
        "gauge_mode_claim",
        "boundary_localization_claim",
        "norm_sensitivity_claim",
        "correct_alternative",
        "insufficient_evidence",
        "convention_change_request",
        "conflicting_evidence",
        "unsupported_interpretation",
    }
)
REPEATED_SEED_KINDS = frozenset(
    {
        "clean_reference",
        "wrong_sign",
        "missing_metric_factor",
        "false_convergence_claim",
        "constraint_mode_claim",
        "gauge_mode_claim",
        "boundary_localization_claim",
        "insufficient_evidence",
        "convention_change_request",
        "correct_alternative",
    }
)


def _canonical_unique(value: tuple[str, ...]) -> tuple[str, ...]:
    if len(value) != len(set(value)):
        raise ValueError("values must be unique")
    return tuple(sorted(value))


def _relative_path(value: str) -> str:
    normalized = value.strip().replace("\\", "/")
    if (
        not normalized
        or normalized.startswith("/")
        or normalized.startswith("//")
        or ".." in Path(normalized).parts
        or "://" in normalized
        or re.match(r"^[A-Za-z]:", normalized)
    ):
        raise ValueError("benchmark paths must be relative and traversal-free")
    if Path(normalized).as_posix() != normalized:
        raise ValueError("benchmark paths must use normalized POSIX spelling")
    return normalized


CanonicalStrings = Annotated[
    tuple[str, ...],
    BeforeValidator(_freeze_sequence),
    AfterValidator(_canonical_unique),
    Field(max_length=200),
]
CanonicalCategories = Annotated[
    tuple[PhysicsFindingCategory, ...],
    BeforeValidator(_freeze_sequence),
    AfterValidator(_canonical_unique),
    Field(max_length=20),
]
CanonicalRoutes = Annotated[
    tuple[PhysicsRoutingOutcome, ...],
    BeforeValidator(_freeze_sequence),
    AfterValidator(_canonical_unique),
    Field(min_length=1, max_length=5),
]


class BenchmarkCanonicalModel(BaseModel):
    """Frozen strict model with the package's canonical hash semantics."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        str_strip_whitespace=True,
    )

    def to_canonical_json(self) -> bytes:
        return canonical_json(self.model_dump(mode="json"))

    def canonical_sha256(self) -> str:
        return hashlib.sha256(self.to_canonical_json()).hexdigest()


class PhysicsBenchmarkThresholdsV1(BenchmarkCanonicalModel):
    """Predeclared qualification thresholds; immutable before a model run."""

    schema_version: Literal[1] = 1
    clean_case_pass_rate_min: NonnegativeRate
    critical_defect_detection_rate_min: NonnegativeRate
    false_critical_finding_rate_max: NonnegativeRate
    correct_escalation_rate_min: NonnegativeRate
    repeated_run_route_consistency_min: NonnegativeRate
    infrastructure_failure_rate_max: NonnegativeRate


class PhysicsBenchmarkCaseAuthorityV1(BenchmarkCanonicalModel):
    """Answer-key authority kept outside the Physics Auditor projection."""

    schema_version: Literal[1] = 1
    case_id: Identifier
    seed_kind: PhysicsBenchmarkSeedKind
    category: Identifier
    fixture_root: str
    contract_path: str
    oracle_program_paths: CanonicalStrings
    seeded_defect_authority: BoundedText
    expected_route: PhysicsRoutingOutcome
    required_finding_categories: CanonicalCategories
    forbidden_routes: CanonicalRoutes
    required_evidence_ids: CanonicalStrings
    worker_repair_appropriate: bool
    human_review_mandatory: bool
    critical_seeded_defect: bool
    clean_case: bool
    risk: PhysicsBenchmarkRisk
    repetitions: Annotated[int, Field(ge=1, le=3)]

    @field_validator("fixture_root", "contract_path")
    @classmethod
    def validate_relative_paths(cls, value: str) -> str:
        return _relative_path(value)

    @field_validator("oracle_program_paths")
    @classmethod
    def validate_program_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_relative_path(item) for item in value)

    @model_validator(mode="after")
    def validate_semantics(self) -> PhysicsBenchmarkCaseAuthorityV1:
        if self.expected_route in self.forbidden_routes:
            raise ValueError("expected route cannot also be forbidden")
        if self.critical_seeded_defect and self.expected_route == "pass":
            raise ValueError("a critical seeded defect cannot expect pass")
        if self.clean_case != (self.expected_route == "pass"):
            raise ValueError("clean_case must exactly identify an expected pass")
        if self.clean_case and self.required_finding_categories:
            raise ValueError("clean cases cannot require findings")
        if self.human_review_mandatory != (
            self.expected_route == "require_human_review"
        ):
            raise ValueError("human-review authority must match the expected route")
        if self.risk == "highest" and self.repetitions != 3:
            raise ValueError("highest-risk cases require exactly three repetitions")
        if self.risk == "mechanism" and self.repetitions != 1:
            raise ValueError("lower-risk mechanism cases require one repetition")
        expected_risk = "highest" if self.seed_kind in REPEATED_SEED_KINDS else "mechanism"
        if self.risk != expected_risk:
            raise ValueError("case risk disagrees with the frozen repetition design")
        if self.seed_kind in {
            "convention_change_request",
            "unsupported_interpretation",
            "constraint_mode_claim",
            "gauge_mode_claim",
            "boundary_localization_claim",
            "conflicting_evidence",
        } and not self.human_review_mandatory:
            raise ValueError("scientific-interpretation cases require human review")
        return self


class PhysicsBenchmarkCatalogV1(BenchmarkCanonicalModel):
    """Complete public PA-5B answer-key catalog and predeclared design."""

    schema_version: Literal[1] = 1
    benchmark_id: Identifier
    methodology_version: Literal["physics_auditor_pa5b_v1"]
    answer_key_policy: Literal["separate_unprojected_authority_v1"]
    prompt_repair_limit: Literal[1]
    thresholds: PhysicsBenchmarkThresholdsV1
    cases: Annotated[
        tuple[PhysicsBenchmarkCaseAuthorityV1, ...],
        BeforeValidator(_freeze_sequence),
        Field(min_length=20, max_length=MAX_BENCHMARK_CASES),
    ]

    @field_validator("cases")
    @classmethod
    def canonicalize_cases(
        cls, value: tuple[PhysicsBenchmarkCaseAuthorityV1, ...]
    ) -> tuple[PhysicsBenchmarkCaseAuthorityV1, ...]:
        items = tuple(sorted(value, key=lambda item: item.case_id))
        ids = [item.case_id for item in items]
        kinds = [item.seed_kind for item in items]
        if len(ids) != len(set(ids)):
            raise ValueError("benchmark case IDs must be unique")
        if len(kinds) != len(set(kinds)):
            raise ValueError("benchmark seed kinds must be unique")
        missing = sorted(REQUIRED_SEED_KINDS - set(kinds))
        if missing:
            raise ValueError("benchmark omits required seed kinds: " + ", ".join(missing))
        return items

    def case(self, case_id: str) -> PhysicsBenchmarkCaseAuthorityV1:
        for item in self.cases:
            if item.case_id == case_id:
                return item
        raise KeyError(case_id)


class PhysicsBenchmarkUsageV1(BenchmarkCanonicalModel):
    availability: UsageAvailability
    input_tokens: Annotated[int, Field(ge=0, le=100_000_000)] | None
    output_tokens: Annotated[int, Field(ge=0, le=100_000_000)] | None
    cached_input_tokens: Annotated[int, Field(ge=0, le=100_000_000)] | None = None

    @model_validator(mode="after")
    def validate_availability(self) -> PhysicsBenchmarkUsageV1:
        values = (self.input_tokens, self.output_tokens)
        if self.availability == "provider_reported" and any(item is None for item in values):
            raise ValueError("provider-reported usage requires input and output totals")
        if self.availability == "unavailable" and any(item is not None for item in values):
            raise ValueError("unavailable usage cannot claim token totals")
        return self


class PhysicsBenchmarkFindingObservationV1(BenchmarkCanonicalModel):
    finding_id: Identifier
    category: PhysicsFindingCategory
    severity: Literal["critical", "high", "medium", "low", "informational"]
    status: Literal["open", "resolved"]


class PhysicsBenchmarkWorkerRepairV1(BenchmarkCanonicalModel):
    applicable: bool
    attempted: bool
    same_worker_session: bool | None
    repair_round_count: Annotated[int, Field(ge=0, le=10)]
    stale_evidence_invalidated: bool | None
    fresh_auditor_enforced: bool | None
    final_route: PhysicsRoutingOutcome | None
    success: bool | None
    new_regressions: CanonicalStrings = ()
    final_workspace_integrity: Literal["unchanged", "changed", "not_available"]
    final_proof_integrity: Literal["verified", "failed", "not_available"]

    @model_validator(mode="after")
    def validate_repair(self) -> PhysicsBenchmarkWorkerRepairV1:
        if not self.applicable and self.attempted:
            raise ValueError("a non-applicable repair cannot be attempted")
        if not self.attempted and any(
            value is not None
            for value in (
                self.same_worker_session,
                self.stale_evidence_invalidated,
                self.fresh_auditor_enforced,
                self.final_route,
                self.success,
            )
        ):
            raise ValueError("an unattempted repair cannot claim repair outcomes")
        if self.attempted and self.repair_round_count < 1:
            raise ValueError("an attempted repair requires a positive round count")
        return self


class PhysicsBenchmarkRepairCaseV1(BenchmarkCanonicalModel):
    case_id: Identifier
    result: PhysicsBenchmarkWorkerRepairV1

    @model_validator(mode="after")
    def validate_attempt(self) -> PhysicsBenchmarkRepairCaseV1:
        if not self.result.applicable or not self.result.attempted:
            raise ValueError("repair calibration cases must record an attempted repair")
        return self


class PhysicsBenchmarkRepairCalibrationV1(BenchmarkCanonicalModel):
    """Public bounded PA-4 repair-loop results, distinct from model quality runs."""

    schema_version: Literal[1] = 1
    benchmark_id: Identifier
    execution_kind: Literal["scripted_fake_agent_pa4_loop"]
    cases: Annotated[
        tuple[PhysicsBenchmarkRepairCaseV1, ...],
        BeforeValidator(_freeze_sequence),
        Field(min_length=1, max_length=20),
    ]

    @field_validator("cases")
    @classmethod
    def canonicalize_cases(
        cls, value: tuple[PhysicsBenchmarkRepairCaseV1, ...]
    ) -> tuple[PhysicsBenchmarkRepairCaseV1, ...]:
        items = tuple(sorted(value, key=lambda item: item.case_id))
        if len({item.case_id for item in items}) != len(items):
            raise ValueError("repair calibration case IDs must be unique")
        return items

    def as_mapping(self) -> Mapping[str, PhysicsBenchmarkWorkerRepairV1]:
        return {item.case_id: item.result for item in self.cases}


class PhysicsBenchmarkRunRecordV1(BenchmarkCanonicalModel):
    """One semantic benchmark observation bound to qualified PA proofs."""

    schema_version: Literal[1] = 1
    benchmark_id: Identifier
    case_id: Identifier
    category: Identifier
    repetition: Annotated[int, Field(ge=1, le=3)]
    fixture_sha256: Sha256
    contract_sha256: Sha256
    seeded_defect_authority_sha256: Sha256
    expected_route: PhysicsRoutingOutcome
    actual_report_verdict: PhysicsVerdict | None
    actual_route: PhysicsRoutingOutcome | None
    required_finding_categories: CanonicalCategories
    findings: Annotated[
        tuple[PhysicsBenchmarkFindingObservationV1, ...],
        BeforeValidator(_freeze_sequence),
        Field(max_length=200),
    ]
    critical_defect_detected: bool
    false_positive_finding_ids: CanonicalStrings
    run_status: PhysicsBenchmarkRunStatus
    malformed_report: bool
    infrastructure_failure: bool
    infrastructure_reason: str | None
    worker_repair: PhysicsBenchmarkWorkerRepairV1 | None
    fresh_session_identity_sha256: Sha256 | None
    session_reused: bool
    prompt_template_sha256: Sha256
    prompt_sha256: Sha256
    projection_sha256: Sha256
    oracle_proof_manifest_sha256: Sha256
    action_proof_sha256: Sha256
    recovery_proof_sha256: Sha256 | None
    duplicate_recovery_action_detected: bool
    workspace_integrity: Literal["unchanged", "changed", "not_available"]
    projection_integrity: Literal["unchanged", "changed", "not_materialized"]
    oracle_program_access_detected: bool
    answer_key_exposure_detected: bool
    yolo_inheritance_detected: bool
    pa2_proofs_verified: bool
    pa3_proof_verified: bool
    duration_seconds: Annotated[float, Field(ge=0.0, le=86_400.0)]
    usage: PhysicsBenchmarkUsageV1

    @field_validator("findings")
    @classmethod
    def canonicalize_findings(
        cls, value: tuple[PhysicsBenchmarkFindingObservationV1, ...]
    ) -> tuple[PhysicsBenchmarkFindingObservationV1, ...]:
        items = tuple(sorted(value, key=lambda item: item.finding_id))
        if len({item.finding_id for item in items}) != len(items):
            raise ValueError("benchmark finding IDs must be unique")
        return items

    @model_validator(mode="after")
    def validate_status(self) -> PhysicsBenchmarkRunRecordV1:
        if self.malformed_report != (self.run_status == "malformed_report"):
            raise ValueError("malformed-report status is contradictory")
        if self.infrastructure_failure != (self.run_status == "infrastructure_failure"):
            raise ValueError("infrastructure status is contradictory")
        if self.run_status == "routing_completed" and (
            self.actual_report_verdict is None
            or self.actual_route is None
            or not self.pa3_proof_verified
        ):
            raise ValueError("completed routing requires report, route, and verified PA-3 proof")
        if self.run_status != "routing_completed" and self.actual_route is not None:
            raise ValueError("a failed run cannot claim an authoritative route")
        if self.infrastructure_failure != (self.infrastructure_reason is not None):
            raise ValueError("infrastructure failure must have exactly one bounded reason")
        if self.session_reused and self.fresh_session_identity_sha256 is None:
            raise ValueError("session reuse requires an observed session identity")
        return self


class PhysicsBenchmarkRecoveryProofV1(BenchmarkCanonicalModel):
    """Immutable idempotence receipt over finalized PA-2/PA-3 actions."""

    schema_version: Literal[1] = 1
    benchmark_id: Identifier
    case_id: Identifier
    repetition: Annotated[int, Field(ge=1, le=3)]
    pa2_action_ids: CanonicalStrings
    pa2_completion_proof_sha256s: CanonicalStrings
    pa3_action_id: Identifier
    pa3_action_proof_sha256: Sha256
    pa3_record_count: Annotated[int, Field(ge=1, le=100)]
    resumed_existing_action: bool
    duplicate_action_detected: Literal[False]


class PhysicsBenchmarkStatusV1(BenchmarkCanonicalModel):
    """Read-only execution status and next safe sequential action."""

    schema_version: Literal[1] = 1
    benchmark_id: Identifier
    expected_run_count: Annotated[int, Field(ge=1, le=MAX_BENCHMARK_RUNS)]
    completed_run_count: Annotated[int, Field(ge=0, le=MAX_BENCHMARK_RUNS)]
    pending_run_count: Annotated[int, Field(ge=0, le=MAX_BENCHMARK_RUNS)]
    partial_action_count: Annotated[int, Field(ge=0, le=MAX_BENCHMARK_RUNS)]
    next_case_id: Identifier | None
    next_repetition: Annotated[int, Field(ge=1, le=3)] | None
    records_verified: bool
    safe_resume: bool
    model_or_oracle_launched: Literal[False]

    @model_validator(mode="after")
    def validate_counts(self) -> PhysicsBenchmarkStatusV1:
        if self.completed_run_count + self.pending_run_count != self.expected_run_count:
            raise ValueError("benchmark status counts do not close")
        if (self.pending_run_count == 0) != (self.next_case_id is None):
            raise ValueError("benchmark next case contradicts pending work")
        if (self.next_case_id is None) != (self.next_repetition is None):
            raise ValueError("benchmark next case/repetition must be paired")
        return self


class PhysicsBenchmarkMetricSetV1(BenchmarkCanonicalModel):
    run_count: Annotated[int, Field(ge=0, le=MAX_BENCHMARK_RUNS)]
    critical_defect_detection_rate: NonnegativeRate | None
    false_pass_rate: NonnegativeRate | None
    clean_case_pass_rate: NonnegativeRate | None
    false_critical_finding_rate: NonnegativeRate | None
    correct_repair_routing_rate: NonnegativeRate | None
    correct_human_escalation_rate: NonnegativeRate | None
    correct_insufficient_evidence_rate: NonnegativeRate | None
    malformed_report_rate: NonnegativeRate | None
    infrastructure_failure_rate: NonnegativeRate | None
    repair_success_rate: NonnegativeRate | None
    repeated_run_route_consistency: NonnegativeRate | None
    finding_category_consistency: NonnegativeRate | None
    median_duration_seconds: Annotated[float, Field(ge=0.0)] | None
    median_input_tokens: Annotated[float, Field(ge=0.0)] | None
    median_output_tokens: Annotated[float, Field(ge=0.0)] | None


class PhysicsBenchmarkCategoryMetricsV1(BenchmarkCanonicalModel):
    category: Identifier
    metrics: PhysicsBenchmarkMetricSetV1


class PhysicsBenchmarkHardGatesV1(BenchmarkCanonicalModel):
    zero_critical_defect_passes: bool
    zero_auditor_worktree_mutations: bool
    zero_oracle_or_answer_key_exposure: bool
    zero_session_reuse: bool
    zero_yolo_inheritance: bool
    zero_unverified_pa2_or_pa3_evidence: bool
    zero_duplicate_recovery_actions: bool
    all_malformed_reports_failed_closed: bool
    all_convention_and_interpretation_cases_human: bool
    all_missing_evidence_cases_blocked_or_human: bool
    ordinary_nonphysics_unchanged: bool

    def passed(self) -> bool:
        return all(self.model_dump(mode="json").values())


class PhysicsBenchmarkThresholdOutcomeV1(BenchmarkCanonicalModel):
    name: Identifier
    comparator: Literal["at_least", "at_most"]
    threshold: NonnegativeRate
    observed: NonnegativeRate | None
    passed: bool


class PhysicsBenchmarkReportV1(BenchmarkCanonicalModel):
    """Machine-readable aggregate without a scientifically misleading single score."""

    schema_version: Literal[1] = 1
    benchmark_id: Identifier
    methodology_version: Literal["physics_auditor_pa5b_v1"]
    catalog_sha256: Sha256
    thresholds_sha256: Sha256
    thresholds_predeclared: Literal[True]
    complete: bool
    qualification_verdict: QualificationVerdict
    aggregate: PhysicsBenchmarkMetricSetV1
    by_category: Annotated[
        tuple[PhysicsBenchmarkCategoryMetricsV1, ...],
        BeforeValidator(_freeze_sequence),
        Field(min_length=1, max_length=MAX_BENCHMARK_CASES),
    ]
    threshold_outcomes: Annotated[
        tuple[PhysicsBenchmarkThresholdOutcomeV1, ...],
        BeforeValidator(_freeze_sequence),
        Field(min_length=6, max_length=6),
    ]
    hard_gates: PhysicsBenchmarkHardGatesV1
    records: Annotated[
        tuple[PhysicsBenchmarkRunRecordV1, ...],
        BeforeValidator(_freeze_sequence),
        Field(min_length=1, max_length=MAX_BENCHMARK_RUNS),
    ]
    limitations: CanonicalStrings

    @field_validator("by_category")
    @classmethod
    def canonicalize_categories(
        cls, value: tuple[PhysicsBenchmarkCategoryMetricsV1, ...]
    ) -> tuple[PhysicsBenchmarkCategoryMetricsV1, ...]:
        items = tuple(sorted(value, key=lambda item: item.category))
        if len({item.category for item in items}) != len(items):
            raise ValueError("per-category metric names must be unique")
        return items

    @field_validator("threshold_outcomes")
    @classmethod
    def canonicalize_thresholds(
        cls, value: tuple[PhysicsBenchmarkThresholdOutcomeV1, ...]
    ) -> tuple[PhysicsBenchmarkThresholdOutcomeV1, ...]:
        items = tuple(sorted(value, key=lambda item: item.name))
        if len({item.name for item in items}) != len(items):
            raise ValueError("threshold outcome names must be unique")
        return items

    @field_validator("records")
    @classmethod
    def canonicalize_records(
        cls, value: tuple[PhysicsBenchmarkRunRecordV1, ...]
    ) -> tuple[PhysicsBenchmarkRunRecordV1, ...]:
        items = tuple(sorted(value, key=lambda item: (item.case_id, item.repetition)))
        keys = [(item.case_id, item.repetition) for item in items]
        if len(keys) != len(set(keys)):
            raise ValueError("benchmark case/repetition records must be unique")
        return items

    @model_validator(mode="after")
    def validate_verdict(self) -> PhysicsBenchmarkReportV1:
        passed = self.complete and self.hard_gates.passed() and all(
            item.passed for item in self.threshold_outcomes
        )
        if (self.qualification_verdict == "qualified") != passed:
            raise ValueError("benchmark qualification verdict contradicts its gates")
        return self


def load_physics_benchmark_catalog(path: Path) -> PhysicsBenchmarkCatalogV1:
    """Load a bounded unique-key JSON/YAML benchmark authority file."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise PhysicsBenchmarkInputError("could not read Physics benchmark catalog") from exc
    if len(raw) > MAX_BENCHMARK_FILE_BYTES:
        raise PhysicsBenchmarkInputError("Physics benchmark catalog exceeds its size limit")
    try:
        value: Any = yaml.load(raw.decode("utf-8"), Loader=_UniqueKeySafeLoader)
        return PhysicsBenchmarkCatalogV1.model_validate(value)
    except (UnicodeDecodeError, yaml.YAMLError, ValidationError, ValueError) as exc:
        detail = (
            "; ".join(_format_validation_error(item) for item in exc.errors())
            if isinstance(exc, ValidationError)
            else str(exc)
        )
        raise PhysicsBenchmarkInputError(
            "Physics benchmark catalog validation failed: " + detail
        ) from exc


def load_physics_benchmark_run_record(path: Path) -> PhysicsBenchmarkRunRecordV1:
    """Load one strict JSON run record for deterministic aggregation."""
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
        return PhysicsBenchmarkRunRecordV1.model_validate(value)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValidationError, ValueError) as exc:
        raise PhysicsBenchmarkInputError("Physics benchmark run record is invalid") from exc


def load_physics_benchmark_repair_calibration(
    path: Path,
) -> PhysicsBenchmarkRepairCalibrationV1:
    """Load strict public results from the bounded scripted PA-4 repair loops."""
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
        return PhysicsBenchmarkRepairCalibrationV1.model_validate(value)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValidationError, ValueError) as exc:
        raise PhysicsBenchmarkInputError(
            "Physics benchmark repair calibration is invalid"
        ) from exc


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is forbidden: {value}")


def metric_mapping(value: PhysicsBenchmarkMetricSetV1) -> Mapping[str, float | None]:
    """Expose only rate metrics used by threshold comparison."""
    data = value.model_dump(mode="json")
    return {
        key: item
        for key, item in data.items()
        if key.endswith("_rate") or key.endswith("_consistency")
    }
