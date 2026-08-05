"""Bounded configuration and result models for the PA-5B GL-with-AI pilot."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Annotated, Any, Literal, TypeAlias

import yaml  # type: ignore[import-untyped]
from pydantic import BeforeValidator, Field, ValidationError, field_validator, model_validator

from research_automation_supervisor.contract import _UniqueKeySafeLoader
from research_automation_supervisor.durable_state import render_json_bytes
from research_automation_supervisor.errors import (
    PhysicsBenchmarkInputError,
    PhysicsBenchmarkIntegrityError,
    PhysicsBenchmarkStateError,
)
from research_automation_supervisor.physics_benchmark import fixture_sha256
from research_automation_supervisor.physics_benchmark_models import (
    BenchmarkCanonicalModel,
    CanonicalCategories,
    CanonicalStrings,
    PhysicsBenchmarkFindingObservationV1,
    PhysicsBenchmarkUsageV1,
    Sha256,
)
from research_automation_supervisor.physics_models import PhysicsVerdict, load_physics_task_contract
from research_automation_supervisor.physics_routing import PhysicsRoutingOutcome
from research_automation_supervisor.workflow_models import Identifier, _freeze_sequence

GLPilotTopic: TypeAlias = Literal[
    "uniform_ingoing_gp_background_ledger",
    "trace_free_conformal_extrinsic_curvature",
    "locked_lapse_gauge_source_consistency",
    "so3_cartoon_hat_gamma_x_consistency",
    "stage4aob_discrete_background_residual_convergence",
    "seeded_constraint_heavy_candidate",
    "seeded_gauge_mode_candidate",
    "seeded_boundary_localized_candidate",
    "unresolved_physical_constraint_classification",
    "clean_bounded_accepted_implementation",
]
GLPilotOutcome: TypeAlias = Literal["completed_bounded", "not_completed"]

REQUIRED_GL_PILOT_TOPICS = frozenset(
    {
        "uniform_ingoing_gp_background_ledger",
        "trace_free_conformal_extrinsic_curvature",
        "locked_lapse_gauge_source_consistency",
        "so3_cartoon_hat_gamma_x_consistency",
        "stage4aob_discrete_background_residual_convergence",
        "seeded_constraint_heavy_candidate",
        "seeded_gauge_mode_candidate",
        "seeded_boundary_localized_candidate",
        "unresolved_physical_constraint_classification",
        "clean_bounded_accepted_implementation",
    }
)


class PhysicsGLPilotSourceRefV1(BenchmarkCanonicalModel):
    path: str
    sha256: Sha256
    role: Literal["locked_derivation", "implementation", "test"]

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        normalized = Path(value).as_posix()
        if normalized != value or value.startswith("/") or ".." in Path(value).parts:
            raise ValueError("GL pilot source references must be normalized relative paths")
        return value


class PhysicsGLPilotTaskV1(BenchmarkCanonicalModel):
    schema_version: Literal[1] = 1
    task_id: Identifier
    topic: GLPilotTopic
    fixture_root: str
    contract_path: str
    oracle_program_path: str
    source_refs: Annotated[
        tuple[PhysicsGLPilotSourceRefV1, ...],
        BeforeValidator(_freeze_sequence),
        Field(min_length=1, max_length=10),
    ]
    locked_authority_summary: Annotated[str, Field(min_length=1, max_length=2_000)]
    expected_route: PhysicsRoutingOutcome
    required_finding_categories: CanonicalCategories
    deliberately_seeded: bool
    human_review_mandatory: bool

    @field_validator("fixture_root", "contract_path", "oracle_program_path")
    @classmethod
    def validate_paths(cls, value: str) -> str:
        normalized = Path(value).as_posix()
        if normalized != value or value.startswith("/") or ".." in Path(value).parts:
            raise ValueError("GL pilot paths must be normalized and repository-relative")
        return value

    @field_validator("source_refs")
    @classmethod
    def canonicalize_sources(
        cls, value: tuple[PhysicsGLPilotSourceRefV1, ...]
    ) -> tuple[PhysicsGLPilotSourceRefV1, ...]:
        items = tuple(sorted(value, key=lambda item: item.path))
        if len({item.path for item in items}) != len(items):
            raise ValueError("GL pilot source references must be unique")
        return items

    @model_validator(mode="after")
    def validate_route(self) -> PhysicsGLPilotTaskV1:
        if self.human_review_mandatory != (self.expected_route == "require_human_review"):
            raise ValueError("GL pilot human-review authority contradicts its route")
        if self.expected_route == "pass" and self.required_finding_categories:
            raise ValueError("clean GL pilot tasks cannot require findings")
        if self.deliberately_seeded and self.expected_route == "pass":
            raise ValueError("a deliberately seeded pilot task cannot expect pass")
        return self


class PhysicsGLPilotConfigV1(BenchmarkCanonicalModel):
    schema_version: Literal[1] = 1
    pilot_id: Identifier
    methodology_version: Literal["physics_gl_pilot_pa5b_v1"]
    source_repository: Literal["GL-with-AI"]
    source_commit: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    source_state_policy: Literal["clean_commit_snapshot_only"]
    auditor_workspace_policy: Literal["exact_read_only_projection_v1"]
    production_mutation_allowed: Literal[False]
    open_research_questions_allowed: Literal[False]
    tasks: Annotated[
        tuple[PhysicsGLPilotTaskV1, ...],
        BeforeValidator(_freeze_sequence),
        Field(min_length=10, max_length=10),
    ]

    @field_validator("tasks")
    @classmethod
    def canonicalize_tasks(
        cls, value: tuple[PhysicsGLPilotTaskV1, ...]
    ) -> tuple[PhysicsGLPilotTaskV1, ...]:
        items = tuple(sorted(value, key=lambda item: item.task_id))
        if len({item.task_id for item in items}) != len(items):
            raise ValueError("GL pilot task IDs must be unique")
        topics = {item.topic for item in items}
        if topics != REQUIRED_GL_PILOT_TOPICS:
            raise ValueError("GL pilot must contain the complete bounded topic set")
        return items

    def task(self, task_id: str) -> PhysicsGLPilotTaskV1:
        for item in self.tasks:
            if item.task_id == task_id:
                return item
        raise KeyError(task_id)


class PhysicsGLPilotRunV1(BenchmarkCanonicalModel):
    schema_version: Literal[1] = 1
    pilot_id: Identifier
    task_id: Identifier
    topic: GLPilotTopic
    fixture_sha256: Sha256
    contract_sha256: Sha256
    locked_authority_sha256: Sha256
    expected_route: PhysicsRoutingOutcome
    actual_report_verdict: PhysicsVerdict | None
    actual_route: PhysicsRoutingOutcome | None
    required_finding_categories: CanonicalCategories
    findings: Annotated[
        tuple[PhysicsBenchmarkFindingObservationV1, ...],
        BeforeValidator(_freeze_sequence),
        Field(max_length=100),
    ]
    human_review_mandatory: bool
    route_matched: bool
    run_status: Literal["routing_completed", "malformed_report", "infrastructure_failure"]
    fresh_session_identity_sha256: Sha256 | None
    prompt_sha256: Sha256
    projection_sha256: Sha256
    oracle_proof_manifest_sha256: Sha256
    action_proof_sha256: Sha256
    recovery_proof_sha256: Sha256
    workspace_integrity: Literal["unchanged", "changed", "not_available"]
    answer_key_or_oracle_exposure_detected: bool
    session_reused: bool
    yolo_inheritance_detected: bool
    pa2_pa3_proofs_verified: bool
    duration_seconds: Annotated[float, Field(ge=0.0, le=86_400.0)]
    usage: PhysicsBenchmarkUsageV1

    @model_validator(mode="after")
    def validate_run(self) -> PhysicsGLPilotRunV1:
        if self.route_matched != (self.actual_route == self.expected_route):
            raise ValueError("GL pilot route match flag is contradictory")
        if self.run_status == "routing_completed" and (
            self.actual_report_verdict is None
            or self.actual_route is None
            or not self.pa2_pa3_proofs_verified
        ):
            raise ValueError("completed GL pilot routing lacks verified semantic evidence")
        if self.run_status != "routing_completed" and self.actual_route is not None:
            raise ValueError("failed GL pilot action cannot claim a route")
        return self


class PhysicsGLPilotReportV1(BenchmarkCanonicalModel):
    schema_version: Literal[1] = 1
    pilot_id: Identifier
    config_sha256: Sha256
    source_commit: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    outcome: GLPilotOutcome
    run_count: Annotated[int, Field(ge=0, le=10)]
    matched_route_count: Annotated[int, Field(ge=0, le=10)]
    pass_route_count: Annotated[int, Field(ge=0, le=10)]
    human_review_route_count: Annotated[int, Field(ge=0, le=10)]
    malformed_report_count: Annotated[int, Field(ge=0, le=10)]
    infrastructure_failure_count: Annotated[int, Field(ge=0, le=10)]
    all_mandatory_human_routes_satisfied: bool
    zero_workspace_mutations: bool
    zero_authority_exposure: bool
    zero_session_reuse_or_yolo: bool
    records: Annotated[
        tuple[PhysicsGLPilotRunV1, ...],
        BeforeValidator(_freeze_sequence),
        Field(min_length=1, max_length=10),
    ]
    limitations: CanonicalStrings

    @model_validator(mode="after")
    def validate_report(self) -> PhysicsGLPilotReportV1:
        completed = (
            self.run_count == 10
            and self.matched_route_count == 10
            and self.malformed_report_count == 0
            and self.infrastructure_failure_count == 0
            and self.all_mandatory_human_routes_satisfied
            and self.zero_workspace_mutations
            and self.zero_authority_exposure
            and self.zero_session_reuse_or_yolo
        )
        if (self.outcome == "completed_bounded") != completed:
            raise ValueError("GL pilot outcome contradicts its bounded gates")
        return self


def load_physics_gl_pilot_config(path: Path) -> PhysicsGLPilotConfigV1:
    try:
        raw = path.read_bytes()
        value: Any = yaml.load(raw.decode("utf-8"), Loader=_UniqueKeySafeLoader)
        return PhysicsGLPilotConfigV1.model_validate(value)
    except (OSError, UnicodeDecodeError, yaml.YAMLError, ValidationError, ValueError) as exc:
        raise PhysicsBenchmarkInputError("GL pilot configuration is invalid") from exc


def locked_authority_sha256(task: PhysicsGLPilotTaskV1) -> str:
    value = {
        "expected_route": task.expected_route,
        "human_review_mandatory": task.human_review_mandatory,
        "locked_authority_summary": task.locked_authority_summary,
        "required_finding_categories": list(task.required_finding_categories),
        "source_refs": [item.model_dump(mode="json") for item in task.source_refs],
        "task_id": task.task_id,
        "topic": task.topic,
    }
    from research_automation_supervisor.durable_state import canonical_json

    return hashlib.sha256(canonical_json(value)).hexdigest()


def validate_physics_gl_pilot(
    config: PhysicsGLPilotConfigV1,
    *,
    repository_root: Path,
    config_path: Path,
) -> dict[str, str]:
    try:
        root = repository_root.resolve(strict=True)
        authority = config_path.resolve(strict=True)
        authority_relative = authority.relative_to(root).as_posix()
    except (OSError, RuntimeError, ValueError) as exc:
        raise PhysicsBenchmarkInputError("GL pilot configuration escapes its repository") from exc
    hashes: dict[str, str] = {}
    for task in config.tasks:
        fixture = (root / task.fixture_root).resolve(strict=True)
        contract_path = (root / task.contract_path).resolve(strict=True)
        oracle = (root / task.oracle_program_path).resolve(strict=True)
        for path in (fixture, contract_path, oracle):
            try:
                path.relative_to(root)
            except ValueError as exc:
                raise PhysicsBenchmarkInputError("GL pilot path escapes its repository") from exc
        if authority == fixture or fixture in authority.parents:
            raise PhysicsBenchmarkIntegrityError("GL pilot answer authority overlaps a fixture")
        if fixture not in contract_path.parents:
            raise PhysicsBenchmarkIntegrityError("GL pilot contract escapes its fixture")
        contract = load_physics_task_contract(contract_path)
        declared = {item.path for item in contract.evidence if item.path is not None}
        if task.oracle_program_path in declared or authority_relative in declared:
            raise PhysicsBenchmarkIntegrityError("GL pilot projects answer or oracle authority")
        for relative in declared:
            evidence = (root / relative).resolve(strict=True)
            if fixture not in evidence.parents:
                raise PhysicsBenchmarkIntegrityError("GL pilot evidence escapes its fixture")
        hashes[task.task_id] = fixture_sha256(fixture)
    return hashes


def aggregate_physics_gl_pilot(
    config: PhysicsGLPilotConfigV1,
    records: tuple[PhysicsGLPilotRunV1, ...],
) -> PhysicsGLPilotReportV1:
    if len({item.task_id for item in records}) != len(records):
        raise PhysicsBenchmarkInputError("GL pilot task records must be unique")
    for record in records:
        try:
            task = config.task(record.task_id)
        except KeyError as exc:
            raise PhysicsBenchmarkInputError("GL pilot record names an unknown task") from exc
        if (
            record.pilot_id != config.pilot_id
            or record.topic != task.topic
            or record.expected_route != task.expected_route
            or record.required_finding_categories != task.required_finding_categories
            or record.locked_authority_sha256 != locked_authority_sha256(task)
        ):
            raise PhysicsBenchmarkIntegrityError("GL pilot record contradicts its authority")
    human = [item for item in records if item.human_review_mandatory]
    matched = sum(item.route_matched for item in records)
    malformed = sum(item.run_status == "malformed_report" for item in records)
    infrastructure = sum(item.run_status == "infrastructure_failure" for item in records)
    human_satisfied = all(item.actual_route == "require_human_review" for item in human)
    workspace = not any(item.workspace_integrity == "changed" for item in records)
    exposure = not any(item.answer_key_or_oracle_exposure_detected for item in records)
    sessions = not any(item.session_reused or item.yolo_inheritance_detected for item in records)
    completed = (
        len(records) == 10
        and matched == 10
        and malformed == 0
        and infrastructure == 0
        and human_satisfied
        and workspace
        and exposure
        and sessions
    )
    return PhysicsGLPilotReportV1(
        pilot_id=config.pilot_id,
        config_sha256=config.canonical_sha256(),
        source_commit=config.source_commit,
        outcome="completed_bounded" if completed else "not_completed",
        run_count=len(records),
        matched_route_count=matched,
        pass_route_count=sum(item.actual_route == "pass" for item in records),
        human_review_route_count=sum(
            item.actual_route == "require_human_review" for item in records
        ),
        malformed_report_count=malformed,
        infrastructure_failure_count=infrastructure,
        all_mandatory_human_routes_satisfied=human_satisfied,
        zero_workspace_mutations=workspace,
        zero_authority_exposure=exposure,
        zero_session_reuse_or_yolo=sessions,
        records=tuple(sorted(records, key=lambda item: item.task_id)),
        limitations=(
            "The pilot uses bounded public snapshots of already-locked questions.",
            "It does not inspect hidden evaluation material or project logs as answer keys.",
            "It does not claim a GL mode, settle unresolved classification, or approve "
            "publication.",
        ),
    )


def finalize_physics_gl_pilot_report(
    output_directory: Path,
    report: PhysicsGLPilotReportV1,
) -> Path:
    output_directory.mkdir(parents=True, exist_ok=True)
    path = output_directory / "gl-pilot-report.json"
    content = render_json_bytes(report.model_dump(mode="json"))
    if path.exists():
        if path.read_bytes() != content:
            raise PhysicsBenchmarkStateError("existing GL pilot report contradicts recovery")
    else:
        path.write_bytes(content)
    return path


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is forbidden: {value}")
