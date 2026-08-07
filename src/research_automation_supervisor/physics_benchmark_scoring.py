"""Exact PA-5C2 benchmark run binding and semantic scoring.

The scorer has no execution or recovery surface.  It accepts an immutable expected
identity manifest, independently re-verifies every observed PA-2/PA-3 artifact, and
only then computes separate semantic scores.
"""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal, TypeAlias, cast

from pydantic import BeforeValidator, Field, field_validator, model_validator

from research_automation_supervisor.durable_state import canonical_json
from research_automation_supervisor.errors import (
    PhysicsBenchmarkScoringError,
    PhysicsBenchmarkScoringInputError,
    PhysicsBenchmarkScoringIntegrityError,
)
from research_automation_supervisor.physics_auditor_execution import (
    BLINDNESS_CERTIFICATE_FILE,
    CONTROL_DIRECTORY,
    PROOF_FILE,
    REPORT_FILE,
    verify_physics_auditor_action,
)
from research_automation_supervisor.physics_auditor_models import (
    PhysicsAuditorActionProofV1,
    PhysicsAuditorActionResultV1,
    PhysicsAuditorProjectionManifestV1,
)
from research_automation_supervisor.physics_auditor_projection import (
    AUTHORITY_DIRECTORY,
    PROJECTION_MANIFEST_FILE,
)
from research_automation_supervisor.physics_benchmark_blindness import (
    AuditorVisibleManifestV1,
    BlindnessCertificateV1,
    BlindVariantAuthorityV1,
    PhysicsBlindFixtureCatalogV1,
    build_paired_visible_manifest,
    load_blind_fixture_catalog,
    load_human_review_receipt,
)
from research_automation_supervisor.physics_models import (
    PhysicsAuditReportV1,
    PhysicsCanonicalModel,
    PhysicsEvidenceReferenceV1,
    PhysicsFindingCategory,
    PhysicsFindingSeverity,
)
from research_automation_supervisor.physics_oracle_models import Sha256
from research_automation_supervisor.physics_routing import PhysicsRoutingOutcome
from research_automation_supervisor.workflow_models import Identifier, _freeze_sequence

MAX_EXACT_BENCHMARK_RUNS = 10_000
SCORER_CATALOG_FILE = "catalog.json"
_ROUTES: frozenset[PhysicsRoutingOutcome] = frozenset(
    {
        "pass",
        "request_repair",
        "require_human_review",
        "block_insufficient_evidence",
        "infrastructure_failure",
    }
)
_SEVERITY_RANK: dict[str, int] = {
    "informational": 0,
    "low": 1,
    "medium": 2,
    "high": 3,
    "critical": 4,
}

CriterionStatus: TypeAlias = Literal["correct", "incorrect", "not_applicable"]
OwnerKind: TypeAlias = Literal["check", "finding", "unresolved_question"]


def _sorted_unique_strings(value: tuple[str, ...]) -> tuple[str, ...]:
    if len(value) != len(set(value)):
        raise ValueError("values must be unique")
    return tuple(sorted(value))


def _run_key(identity: ExactBenchmarkRunIdentityV1) -> tuple[str, str, int]:
    return identity.case_id, identity.variant_id, identity.repetition_id


class PA2ProofIdentityV1(PhysicsCanonicalModel):
    """Exact PA-2 proof authority embedded in one verified PA-3 request."""

    completion_proof_id: Identifier
    oracle_id: Identifier
    result_sha256: Sha256
    completion_proof_sha256: Sha256
    trusted_intent_sha256: Sha256
    execution_policy_sha256: Sha256


class FindingSeverityIdentityV1(PhysicsCanonicalModel):
    """Finding identity retained so category sets cannot erase severity changes."""

    finding_id: Identifier
    category: PhysicsFindingCategory
    severity: PhysicsFindingSeverity
    status: Literal["open", "resolved"]


class SemanticEvidenceIdentityV1(PhysicsCanonicalModel):
    """One cited evidence identity, including the semantic object that cites it."""

    owner_kind: OwnerKind
    owner_id: Identifier
    evidence: PhysicsEvidenceReferenceV1


class ExactBenchmarkRunIdentityV1(PhysicsCanonicalModel):
    """Canonical cryptographic and semantic identity for one benchmark repetition."""

    schema_version: Literal[1] = 1
    case_id: Annotated[str, Field(pattern=r"^case_[0-9]{3}$")]
    pair_id: Annotated[str, Field(pattern=r"^pair_[0-9]{3}$")]
    variant_id: Literal["variant_001", "variant_002"]
    repetition_id: Annotated[int, Field(ge=1, le=1000)]
    catalog_id: Literal["physics_benchmark_blind_authority_v1"]
    catalog_sha256: Sha256
    visible_manifest_sha256: Sha256
    scorer_authority_sha256: Sha256
    scorer_root_manifest_sha256: Sha256
    review_receipt_sha256: Sha256
    contract_sha256: Sha256
    source_workspace_identity_sha256: Sha256
    projection_manifest_sha256: Sha256
    pa2_proof_identities: Annotated[
        tuple[PA2ProofIdentityV1, ...],
        BeforeValidator(_freeze_sequence),
        Field(min_length=1, max_length=200),
    ]
    pa3_action_id: Identifier
    pa3_action_proof_sha256: Sha256
    pa3_launch_manifest_sha256: Sha256
    pa5c1_blindness_certificate_sha256: Sha256
    auditor_report_sha256: Sha256 | None
    deterministic_route: PhysicsRoutingOutcome | None
    finding_category_set: Annotated[
        tuple[PhysicsFindingCategory, ...],
        BeforeValidator(_freeze_sequence),
    ]
    finding_severities: Annotated[
        tuple[FindingSeverityIdentityV1, ...],
        BeforeValidator(_freeze_sequence),
        Field(max_length=200),
    ]
    evidence_references: Annotated[
        tuple[SemanticEvidenceIdentityV1, ...],
        BeforeValidator(_freeze_sequence),
        Field(max_length=600),
    ]
    semantic_observations_sha256: Sha256
    action_status: str
    failure_reason: str

    @field_validator("pa2_proof_identities")
    @classmethod
    def canonicalize_pa2(
        cls, value: tuple[PA2ProofIdentityV1, ...]
    ) -> tuple[PA2ProofIdentityV1, ...]:
        items = tuple(sorted(value, key=lambda item: (item.oracle_id, item.completion_proof_id)))
        if len({item.oracle_id for item in items}) != len(items):
            raise ValueError("run identity contains duplicate PA-2 oracle IDs")
        if len({item.completion_proof_id for item in items}) != len(items):
            raise ValueError("run identity contains duplicate PA-2 proof IDs")
        return items

    @field_validator("finding_category_set")
    @classmethod
    def canonicalize_categories(
        cls, value: tuple[PhysicsFindingCategory, ...]
    ) -> tuple[PhysicsFindingCategory, ...]:
        return cast(tuple[PhysicsFindingCategory, ...], _sorted_unique_strings(value))

    @field_validator("finding_severities")
    @classmethod
    def canonicalize_severities(
        cls, value: tuple[FindingSeverityIdentityV1, ...]
    ) -> tuple[FindingSeverityIdentityV1, ...]:
        items = tuple(sorted(value, key=lambda item: item.finding_id))
        if len({item.finding_id for item in items}) != len(items):
            raise ValueError("run identity contains duplicate finding IDs")
        return items

    @field_validator("evidence_references")
    @classmethod
    def canonicalize_evidence(
        cls, value: tuple[SemanticEvidenceIdentityV1, ...]
    ) -> tuple[SemanticEvidenceIdentityV1, ...]:
        def key(item: SemanticEvidenceIdentityV1) -> tuple[object, ...]:
            evidence = item.evidence
            return (
                item.owner_kind,
                item.owner_id,
                evidence.kind,
                evidence.reference or "",
                evidence.path or "",
                evidence.line_start or 0,
                evidence.line_end or 0,
            )

        items = tuple(sorted(value, key=key))
        if len({key(item) for item in items}) != len(items):
            raise ValueError("run identity contains duplicate semantic evidence bindings")
        return items

    @model_validator(mode="after")
    def validate_semantic_shape(self) -> ExactBenchmarkRunIdentityV1:
        categories = tuple(sorted({item.category for item in self.finding_severities}))
        if categories != self.finding_category_set:
            raise ValueError("finding-category set contradicts severity identities")
        routed = self.action_status == "routing_completed"
        if routed != (
            self.auditor_report_sha256 is not None and self.deterministic_route is not None
        ):
            raise ValueError("run report/route identity contradicts the PA-3 action status")
        if not routed and (
            self.finding_category_set or self.finding_severities or self.evidence_references
        ):
            raise ValueError("an unscored report cannot claim semantic observations")
        return self


class ExactBenchmarkExpectedRunsV1(PhysicsCanonicalModel):
    """Self-hashed scorer input whose run identities are the complete expected key set."""

    schema_version: Literal[1] = 1
    manifest_sha256: Sha256
    catalog_id: Literal["physics_benchmark_blind_authority_v1"]
    catalog_sha256: Sha256
    run_identities: Annotated[
        tuple[ExactBenchmarkRunIdentityV1, ...],
        BeforeValidator(_freeze_sequence),
        Field(min_length=1, max_length=MAX_EXACT_BENCHMARK_RUNS),
    ]

    @model_validator(mode="after")
    def validate_manifest(self) -> ExactBenchmarkExpectedRunsV1:
        items = tuple(sorted(self.run_identities, key=_run_key))
        object.__setattr__(self, "run_identities", items)
        keys = tuple(_run_key(item) for item in items)
        if len(keys) != len(set(keys)):
            raise ValueError("expected run identities contain duplicate repetition keys")
        hashes = tuple(item.canonical_sha256() for item in items)
        if len(hashes) != len(set(hashes)):
            raise ValueError("expected run identities contain duplicates")
        if any(
            item.catalog_id != self.catalog_id or item.catalog_sha256 != self.catalog_sha256
            for item in items
        ):
            raise ValueError("expected run identity contradicts manifest catalog authority")
        pa3_ids = tuple(item.pa3_action_id for item in items)
        pa3_hashes = tuple(item.pa3_action_proof_sha256 for item in items)
        pa2_ids = tuple(
            proof.completion_proof_id for item in items for proof in item.pa2_proof_identities
        )
        if (
            len(pa3_ids) != len(set(pa3_ids))
            or len(pa3_hashes) != len(set(pa3_hashes))
            or len(pa2_ids) != len(set(pa2_ids))
        ):
            raise ValueError("expected repetitions reuse a PA-2 or PA-3 proof identity")
        payload = self.model_dump(mode="json", exclude={"manifest_sha256"})
        if self.manifest_sha256 != hashlib.sha256(canonical_json(payload)).hexdigest():
            raise ValueError("expected-run manifest digest is invalid")
        return self


@dataclass(frozen=True)
class ExactBenchmarkRunArtifacts:
    """Read-only locations required to re-verify one observed benchmark action."""

    case_id: str
    variant_id: str
    repetition_id: int
    contract_path: Path
    execution_config_path: Path
    workspace: Path
    oracle_evidence_root: Path
    output_directory: Path
    attempt_number: int = 1


@dataclass(frozen=True)
class ExactBenchmarkObservedRun:
    """Claimed run identity plus the artifacts from which it must be re-derived."""

    identity: ExactBenchmarkRunIdentityV1
    artifacts: ExactBenchmarkRunArtifacts


class ExactRunSemanticScoreV1(PhysicsCanonicalModel):
    """Independent scoring dimensions for one fully verified run."""

    schema_version: Literal[1] = 1
    run_identity_sha256: Sha256
    case_id: str
    variant_id: str
    repetition_id: int
    defect_category_recognition: CriterionStatus
    severity_correctness: CriterionStatus
    route_correctness: CriterionStatus
    required_categories: CriterionStatus
    acceptable_alternatives: CriterionStatus
    forbidden_categories: CriterionStatus
    forbidden_routes: CriterionStatus
    evidence_validity: CriterionStatus
    clean_case_pass: CriterionStatus
    malformed_report: bool
    infrastructure_failure: bool


class ExactCriterionAggregateV1(PhysicsCanonicalModel):
    """Numerator, denominator, and rate for one non-collapsed criterion."""

    eligible_runs: Annotated[int, Field(ge=0)]
    correct_runs: Annotated[int, Field(ge=0)]
    rate: Annotated[float, Field(ge=0.0, le=1.0)] | None

    @model_validator(mode="after")
    def validate_rate(self) -> ExactCriterionAggregateV1:
        expected = self.correct_runs / self.eligible_runs if self.eligible_runs else None
        if self.correct_runs > self.eligible_runs or self.rate != expected:
            raise ValueError("criterion aggregate is contradictory")
        return self


class ExactBenchmarkAggregateV1(PhysicsCanonicalModel):
    """Aggregate metrics computed only after every run identity is closed."""

    run_count: Annotated[int, Field(ge=1)]
    defect_category_recognition: ExactCriterionAggregateV1
    severity_correctness: ExactCriterionAggregateV1
    route_correctness: ExactCriterionAggregateV1
    required_categories: ExactCriterionAggregateV1
    acceptable_alternatives: ExactCriterionAggregateV1
    forbidden_categories: ExactCriterionAggregateV1
    forbidden_routes: ExactCriterionAggregateV1
    evidence_validity: ExactCriterionAggregateV1
    clean_case_pass: ExactCriterionAggregateV1
    malformed_report_count: Annotated[int, Field(ge=0)]
    infrastructure_failure_count: Annotated[int, Field(ge=0)]


class ExactBenchmarkScoreReportV1(PhysicsCanonicalModel):
    """Complete PA-5C2 score with exact-run bijection already proven."""

    schema_version: Literal[1] = 1
    expected_run_manifest_sha256: Sha256
    catalog_sha256: Sha256
    exact_run_identity_bijection: Literal[True] = True
    all_runs_proof_verified: Literal[True] = True
    run_scores: Annotated[
        tuple[ExactRunSemanticScoreV1, ...],
        BeforeValidator(_freeze_sequence),
        Field(min_length=1, max_length=MAX_EXACT_BENCHMARK_RUNS),
    ]
    aggregate: ExactBenchmarkAggregateV1


def issue_expected_run_manifest(
    catalog: PhysicsBlindFixtureCatalogV1,
    identities: tuple[ExactBenchmarkRunIdentityV1, ...],
) -> ExactBenchmarkExpectedRunsV1:
    """Issue the immutable expected identity set after trusted artifact capture."""
    ordered = tuple(sorted(identities, key=_run_key))
    payload: dict[str, object] = {
        "schema_version": 1,
        "catalog_id": catalog.catalog_id,
        "catalog_sha256": catalog.canonical_sha256(),
        "run_identities": [item.model_dump(mode="json") for item in ordered],
    }
    return ExactBenchmarkExpectedRunsV1.model_validate(
        {
            **payload,
            "manifest_sha256": hashlib.sha256(canonical_json(payload)).hexdigest(),
        }
    )


def bind_exact_benchmark_run(
    catalog: PhysicsBlindFixtureCatalogV1,
    artifacts: ExactBenchmarkRunArtifacts,
    *,
    repository_root: Path,
) -> ExactBenchmarkRunIdentityV1:
    """Independently verify and bind one exact PA-2/PA-3 benchmark repetition."""
    try:
        return _bind_exact_benchmark_run(catalog, artifacts, repository_root=repository_root)
    except PhysicsBenchmarkScoringError:
        raise
    except Exception as exc:
        raise PhysicsBenchmarkScoringIntegrityError(
            "benchmark run proof, report, source, projection, or authority did not close"
        ) from exc


def score_exact_physics_benchmark(
    expected: ExactBenchmarkExpectedRunsV1,
    observed: tuple[ExactBenchmarkObservedRun, ...],
    *,
    catalog_path: Path,
    repository_root: Path,
) -> ExactBenchmarkScoreReportV1:
    """Require exact identity equality, re-verify all runs, then aggregate scores."""
    root = _canonical_directory(repository_root, "repository root")
    catalog, _ = _load_certified_catalog(catalog_path, root)
    if (
        expected.catalog_id != catalog.catalog_id
        or expected.catalog_sha256 != catalog.canonical_sha256()
    ):
        raise PhysicsBenchmarkScoringIntegrityError("expected scorer catalog authority is stale")
    if not observed:
        raise PhysicsBenchmarkScoringInputError("observed run set is empty")

    expected_hashes = {item.canonical_sha256() for item in expected.run_identities}
    observed_hash_list = [item.identity.canonical_sha256() for item in observed]
    if len(observed_hash_list) != len(set(observed_hash_list)):
        raise PhysicsBenchmarkScoringIntegrityError("observed run identities contain duplicates")
    observed_hashes = set(observed_hash_list)
    if expected_hashes != observed_hashes:
        raise PhysicsBenchmarkScoringIntegrityError(
            "expected run identities do not exactly equal observed run identities"
        )
    expected_keys = {_run_key(item) for item in expected.run_identities}
    observed_keys = {_run_key(item.identity) for item in observed}
    if len(observed_keys) != len(observed) or expected_keys != observed_keys:
        raise PhysicsBenchmarkScoringIntegrityError(
            "expected repetition keys do not exactly equal observed repetition keys"
        )

    verified: list[tuple[ExactBenchmarkRunIdentityV1, BlindVariantAuthorityV1]] = []
    for item in sorted(observed, key=lambda run: _run_key(run.identity)):
        coordinates = (
            item.artifacts.case_id,
            item.artifacts.variant_id,
            item.artifacts.repetition_id,
        )
        if coordinates != _run_key(item.identity):
            raise PhysicsBenchmarkScoringIntegrityError(
                "observed artifact coordinates contradict the declared run identity"
            )
        derived = bind_exact_benchmark_run(
            catalog,
            item.artifacts,
            repository_root=root,
        )
        if derived != item.identity:
            raise PhysicsBenchmarkScoringIntegrityError(
                "observed run identity was not produced by its exact PA-2/PA-3 artifacts"
            )
        pair = catalog.pair(derived.case_id)
        variant = next(value for value in pair.variants if value.variant_id == derived.variant_id)
        verified.append((derived, variant))

    scores = tuple(_semantic_score(identity, authority) for identity, authority in verified)
    return ExactBenchmarkScoreReportV1(
        expected_run_manifest_sha256=expected.manifest_sha256,
        catalog_sha256=catalog.canonical_sha256(),
        run_scores=scores,
        aggregate=_aggregate_scores(scores),
    )


def _bind_exact_benchmark_run(
    catalog: PhysicsBlindFixtureCatalogV1,
    artifacts: ExactBenchmarkRunArtifacts,
    *,
    repository_root: Path,
) -> ExactBenchmarkRunIdentityV1:
    root = _canonical_directory(repository_root, "repository root")
    scorer_root = _certified_scorer_root(catalog, root)
    pair = catalog.pair(artifacts.case_id)
    variants = tuple(item for item in pair.variants if item.variant_id == artifacts.variant_id)
    if len(variants) != 1:
        raise PhysicsBenchmarkScoringInputError("benchmark variant is unavailable")
    paired_manifest = build_paired_visible_manifest(pair, repository_root=root)
    visible_manifest = next(
        item for item in paired_manifest.variants if item.variant_id == artifacts.variant_id
    )
    try:
        receipt_path = _safe_file_below(
            scorer_root,
            root / pair.receipt_path,
            "review receipt",
        )
    except PhysicsBenchmarkScoringInputError as exc:
        raise PhysicsBenchmarkScoringIntegrityError(
            "review receipt is not inside the exact certified scorer root"
        ) from exc
    receipt = load_human_review_receipt(receipt_path)
    _verify_receipt(catalog, pair.case_id, pair.canonical_sha256(), paired_manifest, receipt)

    try:
        result = verify_physics_auditor_action(
            contract_path=artifacts.contract_path,
            execution_config_path=artifacts.execution_config_path,
            task_id=artifacts.case_id,
            workspace=artifacts.workspace,
            oracle_evidence_root=artifacts.oracle_evidence_root,
            output_directory=artifacts.output_directory,
            attempt_number=artifacts.attempt_number,
        )
    except Exception as exc:
        raise PhysicsBenchmarkScoringIntegrityError(
            "PA-2/PA-3 proof verification failed before scoring"
        ) from exc
    if not result.request.oracle_completion_proofs:
        raise PhysicsBenchmarkScoringIntegrityError(
            "benchmark repetition lacks its required PA-2 proof identity"
        )
    if result.request.task_id != artifacts.case_id:
        raise PhysicsBenchmarkScoringIntegrityError("PA-3 proof belongs to another benchmark case")

    output = _canonical_directory(artifacts.output_directory, "PA-3 output directory")
    proof = _load_exact_model(output / PROOF_FILE, PhysicsAuditorActionProofV1, "PA-3 proof")
    projection = _load_exact_model(
        output / CONTROL_DIRECTORY / PROJECTION_MANIFEST_FILE,
        PhysicsAuditorProjectionManifestV1,
        "PA-3 projection manifest",
    )
    certificate = _load_exact_model(
        output / CONTROL_DIRECTORY / BLINDNESS_CERTIFICATE_FILE,
        BlindnessCertificateV1,
        "PA-5C1 blindness certificate",
    )
    _verify_blindness_binding(
        catalog=catalog,
        pair_sha256=pair.canonical_sha256(),
        pair_id=pair.pair_id,
        case_id=pair.case_id,
        variant_id=artifacts.variant_id,
        paired_manifest=paired_manifest,
        visible_manifest=visible_manifest,
        receipt_sha256=receipt.receipt_sha256,
        certificate=certificate,
        projection=projection,
        proof=proof,
        result=result,
        scorer_root=scorer_root,
    )

    report: PhysicsAuditReportV1 | None = None
    if result.report_validated:
        report = _load_exact_model(output / REPORT_FILE, PhysicsAuditReportV1, "auditor report")
        if report.canonical_sha256() != result.parsed_report_sha256:
            raise PhysicsBenchmarkScoringIntegrityError("PA-3 proof binds another report")
    semantic_hash = _semantic_observations_sha256(result, report)
    findings = (
        tuple(
            FindingSeverityIdentityV1(
                finding_id=item.id,
                category=item.category,
                severity=item.severity,
                status=item.status,
            )
            for item in report.findings
        )
        if report is not None
        else ()
    )
    evidence = _semantic_evidence(report) if report is not None else ()
    route = result.routing_decision.outcome if result.routing_decision is not None else None
    return ExactBenchmarkRunIdentityV1(
        case_id=pair.case_id,
        pair_id=pair.pair_id,
        variant_id=cast(Any, artifacts.variant_id),
        repetition_id=artifacts.repetition_id,
        catalog_id=catalog.catalog_id,
        catalog_sha256=catalog.canonical_sha256(),
        visible_manifest_sha256=visible_manifest.canonical_sha256(),
        scorer_authority_sha256=pair.canonical_sha256(),
        scorer_root_manifest_sha256=certificate.scorer_root_manifest_sha256,
        review_receipt_sha256=receipt.receipt_sha256,
        contract_sha256=result.request.physics_contract_sha256,
        source_workspace_identity_sha256=result.request.workspace_identity_sha256,
        projection_manifest_sha256=result.request.projection_manifest_sha256,
        pa2_proof_identities=tuple(
            PA2ProofIdentityV1(**item.model_dump(mode="python"))
            for item in result.request.oracle_completion_proofs
        ),
        pa3_action_id=proof.action_id,
        pa3_action_proof_sha256=proof.canonical_sha256(),
        pa3_launch_manifest_sha256=certificate.pa3_launch_manifest_sha256,
        pa5c1_blindness_certificate_sha256=certificate.canonical_sha256(),
        auditor_report_sha256=result.parsed_report_sha256,
        deterministic_route=route,
        finding_category_set=tuple(sorted({item.category for item in findings})),
        finding_severities=findings,
        evidence_references=evidence,
        semantic_observations_sha256=semantic_hash,
        action_status=result.status,
        failure_reason=result.failure_reason,
    )


def _verify_blindness_binding(
    *,
    catalog: PhysicsBlindFixtureCatalogV1,
    pair_sha256: str,
    pair_id: str,
    case_id: str,
    variant_id: str,
    paired_manifest: Any,
    visible_manifest: AuditorVisibleManifestV1,
    receipt_sha256: str,
    certificate: BlindnessCertificateV1,
    projection: PhysicsAuditorProjectionManifestV1,
    proof: PhysicsAuditorActionProofV1,
    result: PhysicsAuditorActionResultV1,
    scorer_root: Path,
) -> None:
    request = result.request
    pa2_payload = [item.model_dump(mode="json") for item in request.oracle_completion_proofs]
    expected = (
        certificate.case_id == case_id,
        certificate.pair_id == pair_id,
        certificate.variant_id == variant_id,
        certificate.auditor_visible_manifest_sha256 == visible_manifest.canonical_sha256(),
        certificate.paired_visible_manifest_sha256 == paired_manifest.canonical_sha256(),
        certificate.pa3_projection_manifest_sha256 == projection.canonical_sha256(),
        certificate.scorer_root_manifest_sha256 == _directory_manifest_sha256(scorer_root),
        certificate.paired_contract_sha256 == paired_manifest.contract_sha256,
        certificate.review_receipt_sha256 == receipt_sha256,
        certificate.reviewed_visible_manifest_sha256 == paired_manifest.canonical_sha256(),
        certificate.reviewed_scorer_authority_sha256 == pair_sha256,
        certificate.launch_manifest.action_request_sha256 == request.canonical_sha256(),
        certificate.launch_manifest.execution_config_sha256 == request.execution_config_sha256,
        certificate.launch_manifest.evidence_index_sha256 == request.evidence_index_sha256,
        certificate.launch_manifest.oracle_completion_proof_set_sha256
        == hashlib.sha256(canonical_json(pa2_payload)).hexdigest(),
        certificate.launch_manifest.workspace_identity_sha256 == request.workspace_identity_sha256,
        certificate.launch_manifest.prompt_sha256 == proof.canonical_prompt_sha256,
        certificate.launch_manifest.output_schema_sha256 == proof.output_schema_sha256,
        certificate.launch_manifest.projection_manifest_sha256
        == request.projection_manifest_sha256,
        certificate.launch_manifest.bubblewrap_policy_sha256 == request.bubblewrap_policy_sha256,
        certificate.launch_manifest.bubblewrap_backend_identity_sha256
        == proof.bubblewrap_backend_identity_sha256,
        certificate.pa3_launch_manifest_sha256 == certificate.launch_manifest.canonical_sha256(),
        certificate.launch_manifest.scorer_root_mount == "absent",
        certificate.launch_manifest.source_workspace_mount == "absent",
        certificate.launch_manifest.oracle_evidence_mount == "absent",
        proof.action_request_sha256 == request.canonical_sha256(),
        proof.physics_contract_sha256 == request.physics_contract_sha256,
        proof.projection_manifest_sha256 == request.projection_manifest_sha256,
        proof.evidence_index_sha256 == request.evidence_index_sha256,
        proof.action_id == request.action_id,
        proof.task_id == case_id,
    )
    if not all(expected):
        raise PhysicsBenchmarkScoringIntegrityError(
            "PA-5C1 certificate does not bind the exact scorer/source/projection/proof authority"
        )
    _verify_projection_matches_visible(projection, visible_manifest)


def _verify_projection_matches_visible(
    projection: PhysicsAuditorProjectionManifestV1,
    visible: AuditorVisibleManifestV1,
) -> None:
    allowed = {
        item.path: item
        for item in visible.objects
        if item.role not in {"contract", "oracle_program"}
    }
    observed: set[str] = set()
    for item in projection.objects:
        if item.kind != "regular" or item.path.startswith(f"{AUTHORITY_DIRECTORY}/"):
            continue
        expected = allowed.get(item.path)
        if (
            expected is None
            or expected.sha256 != item.sha256
            or expected.byte_length != item.byte_length
        ):
            raise PhysicsBenchmarkScoringIntegrityError(
                "PA-3 projection is not the exact auditor-visible variant"
            )
        observed.add(item.path)
    if observed != set(allowed):
        raise PhysicsBenchmarkScoringIntegrityError(
            "PA-3 projection omits or substitutes auditor-visible source"
        )


def _verify_receipt(
    catalog: PhysicsBlindFixtureCatalogV1,
    case_id: str,
    pair_sha256: str,
    paired_manifest: Any,
    receipt: Any,
) -> None:
    if (
        receipt.subject_id != case_id
        or receipt.fixture_author_ids != catalog.fixture_author_ids
        or receipt.reviewer_id in set(receipt.fixture_author_ids)
        or receipt.reviewed_visible_manifest_sha256 != paired_manifest.canonical_sha256()
        or receipt.reviewed_scorer_authority_sha256 != pair_sha256
        or receipt.decision != "approved"
    ):
        raise PhysicsBenchmarkScoringIntegrityError(
            "stale scorer authority or visible-manifest review receipt"
        )


def _semantic_observations_sha256(
    result: PhysicsAuditorActionResultV1,
    report: PhysicsAuditReportV1 | None,
) -> str:
    payload = {
        "action_status": result.status,
        "failure_reason": result.failure_reason,
        "report": report.model_dump(mode="json") if report is not None else None,
        "routing_decision": (
            result.routing_decision.model_dump(mode="json")
            if result.routing_decision is not None
            else None
        ),
    }
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def _semantic_evidence(
    report: PhysicsAuditReportV1,
) -> tuple[SemanticEvidenceIdentityV1, ...]:
    values: list[SemanticEvidenceIdentityV1] = []
    for owner_kind, owners in (
        ("check", report.checks),
        ("finding", report.findings),
        ("unresolved_question", report.unresolved_questions),
    ):
        for owner in owners:
            values.extend(
                SemanticEvidenceIdentityV1(
                    owner_kind=cast(Any, owner_kind),
                    owner_id=owner.id,
                    evidence=evidence,
                )
                for evidence in owner.evidence
            )
    return tuple(values)


def _semantic_score(
    identity: ExactBenchmarkRunIdentityV1,
    authority: BlindVariantAuthorityV1,
) -> ExactRunSemanticScoreV1:
    categories = {item.category for item in identity.finding_severities if item.status == "open"}
    required = set(authority.required_categories)
    alternatives = set(authority.acceptable_alternative_categories)
    required_satisfied = required.issubset(categories)
    alternative_satisfied = bool(alternatives & categories)
    recognized = required_satisfied or alternative_satisfied
    accepted_categories = required | alternatives
    severity_correct = False
    if authority.minimum_severity is not None:
        severity_correct = any(
            item.status == "open"
            and item.category in accepted_categories
            and _SEVERITY_RANK[item.severity] >= _SEVERITY_RANK[authority.minimum_severity]
            for item in identity.finding_severities
        )
    route = identity.deterministic_route
    forbidden_routes = _ROUTES - {authority.expected_route}
    clean = authority.fixture_label == "clean"
    malformed = identity.action_status == "report_invalid"
    infrastructure = identity.action_status not in {"routing_completed", "report_invalid"}
    return ExactRunSemanticScoreV1(
        run_identity_sha256=identity.canonical_sha256(),
        case_id=identity.case_id,
        variant_id=identity.variant_id,
        repetition_id=identity.repetition_id,
        defect_category_recognition=(
            "not_applicable" if clean else "correct" if recognized else "incorrect"
        ),
        severity_correctness=(
            "not_applicable"
            if authority.minimum_severity is None
            else "correct"
            if severity_correct
            else "incorrect"
        ),
        route_correctness=("correct" if route == authority.expected_route else "incorrect"),
        required_categories="correct" if required_satisfied else "incorrect",
        acceptable_alternatives=(
            "not_applicable"
            if not alternatives
            else "correct"
            if required_satisfied or alternative_satisfied
            else "incorrect"
        ),
        forbidden_categories=(
            "correct" if not (categories & set(authority.forbidden_categories)) else "incorrect"
        ),
        forbidden_routes=("incorrect" if route in forbidden_routes else "correct"),
        evidence_validity=(
            "correct" if identity.action_status == "routing_completed" else "not_applicable"
        ),
        clean_case_pass=(
            "not_applicable"
            if not clean
            else "correct"
            if route == "pass" and not categories
            else "incorrect"
        ),
        malformed_report=malformed,
        infrastructure_failure=infrastructure,
    )


def _aggregate_scores(
    scores: tuple[ExactRunSemanticScoreV1, ...],
) -> ExactBenchmarkAggregateV1:
    fields = (
        "defect_category_recognition",
        "severity_correctness",
        "route_correctness",
        "required_categories",
        "acceptable_alternatives",
        "forbidden_categories",
        "forbidden_routes",
        "evidence_validity",
        "clean_case_pass",
    )
    aggregates: dict[str, ExactCriterionAggregateV1] = {}
    for field in fields:
        values = tuple(getattr(score, field) for score in scores)
        eligible = sum(value != "not_applicable" for value in values)
        correct = sum(value == "correct" for value in values)
        aggregates[field] = ExactCriterionAggregateV1(
            eligible_runs=eligible,
            correct_runs=correct,
            rate=correct / eligible if eligible else None,
        )
    return ExactBenchmarkAggregateV1(
        run_count=len(scores),
        malformed_report_count=sum(score.malformed_report for score in scores),
        infrastructure_failure_count=sum(score.infrastructure_failure for score in scores),
        **aggregates,
    )


def _load_exact_model(path: Path, model: type[Any], label: str) -> Any:
    path = _safe_file(path, label)
    try:
        raw = path.read_bytes()
        value = model.model_validate_json(raw)
    except Exception as exc:
        raise PhysicsBenchmarkScoringIntegrityError(f"{label} is malformed or unavailable") from exc
    if raw != value.to_canonical_json():
        raise PhysicsBenchmarkScoringIntegrityError(f"{label} is not canonical")
    return value


def _load_certified_catalog(
    catalog_path: Path,
    repository_root: Path,
) -> tuple[PhysicsBlindFixtureCatalogV1, Path]:
    """Load only the catalog located at the authority's exact scorer-root path."""
    path = _safe_file_below(repository_root, catalog_path, "scorer catalog")
    catalog = load_blind_fixture_catalog(path)
    scorer_root = _certified_scorer_root(catalog, repository_root)
    expected_path = _safe_file_below(
        scorer_root,
        scorer_root / SCORER_CATALOG_FILE,
        "certified scorer catalog",
    )
    if path != expected_path:
        raise PhysicsBenchmarkScoringIntegrityError(
            "catalog is not the exact catalog inside the certified scorer root"
        )
    return catalog, scorer_root


def _certified_scorer_root(
    catalog: PhysicsBlindFixtureCatalogV1,
    repository_root: Path,
) -> Path:
    """Require a supplied catalog object to equal the catalog in its scorer root."""
    scorer_root = _safe_directory_below(
        repository_root,
        repository_root / catalog.scorer_only_root,
        "scorer authority root",
    )
    catalog_path = _safe_file_below(
        scorer_root,
        scorer_root / SCORER_CATALOG_FILE,
        "certified scorer catalog",
    )
    certified = load_blind_fixture_catalog(catalog_path)
    if certified != catalog:
        raise PhysicsBenchmarkScoringIntegrityError(
            "supplied catalog differs from the catalog in the certified scorer root"
        )
    return scorer_root


def _directory_manifest_sha256(root: Path) -> str:
    objects: list[dict[str, object]] = []
    for path in sorted(root.rglob("*")):
        status = path.lstat()
        relative = path.relative_to(root).as_posix()
        if stat.S_ISDIR(status.st_mode):
            objects.append({"kind": "directory", "path": relative})
            continue
        if not stat.S_ISREG(status.st_mode) or path.is_symlink() or status.st_nlink != 1:
            raise PhysicsBenchmarkScoringIntegrityError(
                "scorer authority root contains an unsafe object"
            )
        content = path.read_bytes()
        objects.append(
            {
                "byte_length": len(content),
                "kind": "regular",
                "path": relative,
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    return hashlib.sha256(canonical_json(objects)).hexdigest()


def _canonical_directory(path: Path, label: str) -> Path:
    try:
        absolute = Path(os.path.abspath(path))
        resolved = path.resolve(strict=True)
        status = path.lstat()
    except (OSError, RuntimeError, ValueError) as exc:
        raise PhysicsBenchmarkScoringInputError(f"{label} is unavailable") from exc
    if (
        ".." in path.parts
        or absolute != resolved
        or path.is_symlink()
        or not stat.S_ISDIR(status.st_mode)
        or resolved == Path("/proc")
        or resolved.is_relative_to(Path("/proc"))
    ):
        raise PhysicsBenchmarkScoringInputError(f"{label} is not a canonical directory")
    return resolved


def _safe_directory_below(root: Path, path: Path, label: str) -> Path:
    resolved = _canonical_directory(path, label)
    if resolved == root or not resolved.is_relative_to(root):
        raise PhysicsBenchmarkScoringInputError(f"{label} escapes the repository root")
    return resolved


def _safe_file(path: Path, label: str) -> Path:
    try:
        absolute = Path(os.path.abspath(path))
        resolved = path.resolve(strict=True)
        status = path.lstat()
    except (OSError, RuntimeError, ValueError) as exc:
        raise PhysicsBenchmarkScoringInputError(f"{label} is unavailable") from exc
    if (
        ".." in path.parts
        or absolute != resolved
        or path.is_symlink()
        or not stat.S_ISREG(status.st_mode)
        or status.st_nlink != 1
        or resolved.is_relative_to(Path("/proc"))
    ):
        raise PhysicsBenchmarkScoringInputError(f"{label} is not a canonical regular file")
    return resolved


def _safe_file_below(root: Path, path: Path, label: str) -> Path:
    resolved = _safe_file(path, label)
    if not resolved.is_relative_to(root):
        raise PhysicsBenchmarkScoringInputError(f"{label} escapes the repository root")
    return resolved
