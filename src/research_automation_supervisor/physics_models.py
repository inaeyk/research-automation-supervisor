"""Strict model-free contracts for Physics Auditor version-1 foundations."""

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
from research_automation_supervisor.errors import PhysicsAuditError, PhysicsContractError
from research_automation_supervisor.structured_outputs import normalize_production_schema
from research_automation_supervisor.workflow_models import (
    BoundedString,
    Identifier,
    _freeze_sequence,
    normalize_relative_path,
)

MAX_PHYSICS_ITEMS = 200
MAX_PHYSICS_INPUT_BYTES = 2 * 1024 * 1024
MAX_SOURCE_LINE = 10_000_000

PhysicsProfile: TypeAlias = Literal["physics_implementation"]
PhysicsConventionAuthority: TypeAlias = Literal[
    "project_locked",
    "task_locked",
    "not_applicable",
]
PhysicsEvidenceKind: TypeAlias = Literal[
    "analytic",
    "test",
    "artifact",
    "oracle",
    "derivation",
    "document",
    "numerical",
]
PhysicsOracleKind: TypeAlias = Literal[
    "analytic",
    "test",
    "artifact",
    "derivation",
    "document",
    "numerical",
]
PhysicsHumanGateTrigger: TypeAlias = Literal[
    "convention_change",
    "unresolved_gauge_constraint_ambiguity",
    "new_physical_interpretation",
]
PhysicsVerdict: TypeAlias = Literal[
    "pass",
    "fail_repairable",
    "human_review",
    "blocked_insufficient_evidence",
    "infrastructure_failure",
]
PhysicsEvidenceSufficiency: TypeAlias = Literal[
    "sufficient",
    "partial",
    "insufficient",
    "conflicting",
]
PhysicsCheckTargetKind: TypeAlias = Literal[
    "required_identity",
    "limiting_case",
    "oracle",
]
PhysicsCheckStatus: TypeAlias = Literal["passed", "failed", "unresolved"]
PhysicsFindingCategory: TypeAlias = Literal[
    "convention_mismatch",
    "convention_change_requested",
    "sign_or_normalization_error",
    "dimensional_inconsistency",
    "tensor_or_index_error",
    "violated_identity",
    "failed_limiting_case",
    "continuum_discrete_mismatch",
    "insufficient_numerical_evidence",
    "gauge_constraint_ambiguity",
    "new_physical_interpretation",
    "unsupported_physical_claim",
    "oracle_failure",
    "missing_required_evidence",
    "report_integrity_error",
]
PhysicsFindingSeverity: TypeAlias = Literal[
    "critical",
    "high",
    "medium",
    "low",
    "informational",
]
PhysicsFindingDisposition: TypeAlias = Literal[
    "repairable",
    "human_review",
    "evidence_blocking",
    "infrastructure_failure",
]
PhysicsFindingStatus: TypeAlias = Literal["open", "resolved"]
PhysicsEvidenceReferenceKind: TypeAlias = Literal[
    "task_contract",
    "source",
    "test",
    "artifact",
    "oracle",
    "derivation",
    "document",
    "numerical",
]
PhysicsQuestionCategory: TypeAlias = Literal[
    "convention_change",
    "gauge_constraint_ambiguity",
    "new_physical_interpretation",
    "evidence_conflict",
    "other_physics_question",
]
PhysicsEvidenceRoute: TypeAlias = Literal["block", "human_review"]
PhysicsAdvisoryRoute: TypeAlias = Literal[
    "request_repair",
    "require_human_review",
    "allow_pass",
]

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")
_MANDATORY_HUMAN_GATES = frozenset(
    {
        "convention_change",
        "unresolved_gauge_constraint_ambiguity",
        "new_physical_interpretation",
    }
)
_HUMAN_ONLY_FINDINGS = frozenset(
    {
        "convention_change_requested",
        "gauge_constraint_ambiguity",
        "new_physical_interpretation",
    }
)
_EVIDENCE_ONLY_FINDINGS = frozenset(
    {"insufficient_numerical_evidence", "missing_required_evidence"}
)


def _canonical_unique_strings(value: tuple[str, ...]) -> tuple[str, ...]:
    if len(value) != len(set(value)):
        raise ValueError("values must be unique")
    return tuple(sorted(value))


def _physics_relative_path(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("relative paths must be strings")
    normalized_input = value.strip().replace("\\", "/")
    if (
        re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", normalized_input)
        or normalized_input.startswith("//")
        or "://" in normalized_input
    ):
        raise ValueError("paths must use relative POSIX workspace syntax")
    return normalize_relative_path(normalized_input)


def _is_identifier(value: str) -> bool:
    return _IDENTIFIER_RE.fullmatch(value) is not None


SortedIdentifiers = Annotated[
    tuple[Identifier, ...],
    BeforeValidator(_freeze_sequence),
    AfterValidator(_canonical_unique_strings),
    Field(max_length=MAX_PHYSICS_ITEMS),
]
RequiredSortedIdentifiers = Annotated[
    tuple[Identifier, ...],
    BeforeValidator(_freeze_sequence),
    AfterValidator(_canonical_unique_strings),
    Field(min_length=1, max_length=MAX_PHYSICS_ITEMS),
]
RelativePhysicsPath = Annotated[str, BeforeValidator(_physics_relative_path)]
OptionalPositiveLine = Annotated[int, Field(ge=1, le=MAX_SOURCE_LINE)] | None


class PhysicsCanonicalModel(BaseModel):
    """Shared strict configuration and canonical representation."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        str_strip_whitespace=True,
    )

    def to_canonical_dict(self) -> dict[str, object]:
        """Return the exact JSON-compatible canonical model value."""
        return self.model_dump(mode="json")

    def to_canonical_json(self) -> bytes:
        """Return stable ASCII JSON bytes using the qualified serializer."""
        return canonical_json(self.to_canonical_dict())

    def canonical_sha256(self) -> str:
        """Hash the exact canonical representation."""
        return hashlib.sha256(self.to_canonical_json()).hexdigest()


class PhysicsConventionV1(PhysicsCanonicalModel):
    """One human-declared convention; its text is authority, not executable truth."""

    id: Identifier
    value: BoundedString
    authority: PhysicsConventionAuthority

    @model_validator(mode="after")
    def validate_not_applicable(self) -> PhysicsConventionV1:
        is_not_applicable = self.value.casefold() == "not_applicable"
        if (self.authority == "not_applicable") != is_not_applicable:
            raise ValueError(
                "not_applicable authority and convention value must agree"
            )
        return self


class PhysicsAssumptionV1(PhysicsCanonicalModel):
    """One frozen, human-authored assumption statement."""

    id: Identifier
    statement: BoundedString


class PhysicsRequiredIdentityV1(PhysicsCanonicalModel):
    """One identity that every report must assess."""

    id: Identifier
    statement: BoundedString
    required_evidence_kinds: Annotated[
        tuple[PhysicsEvidenceKind, ...],
        BeforeValidator(_freeze_sequence),
        AfterValidator(_canonical_unique_strings),
        Field(min_length=1, max_length=20),
    ]
    oracle_ids: SortedIdentifiers = ()


class PhysicsLimitingCaseV1(PhysicsCanonicalModel):
    """One frozen limiting case that every report must assess."""

    id: Identifier
    statement: BoundedString
    required_evidence_kinds: Annotated[
        tuple[PhysicsEvidenceKind, ...],
        BeforeValidator(_freeze_sequence),
        AfterValidator(_canonical_unique_strings),
        Field(min_length=1, max_length=20),
    ] = ("analytic",)
    oracle_ids: SortedIdentifiers = ()


class PhysicsEvidenceDeclarationV1(PhysicsCanonicalModel):
    """A declared test ID or path-backed evidence target."""

    id: Identifier
    kind: Literal["test", "artifact", "derivation", "document", "numerical"]
    description: BoundedString
    path: RelativePhysicsPath | None
    required_for: SortedIdentifiers

    @model_validator(mode="after")
    def validate_kind_fields(self) -> PhysicsEvidenceDeclarationV1:
        if self.kind == "test" and self.path is not None:
            raise ValueError("test evidence declarations identify IDs, not paths")
        if self.kind != "test" and self.path is None:
            raise ValueError("path-backed evidence declarations require a path")
        return self


class PhysicsOracleV1(PhysicsCanonicalModel):
    """One declared oracle reference; PA-1 does not execute it."""

    id: Identifier
    kind: PhysicsOracleKind
    reference: BoundedString
    statement: BoundedString
    check_ids: SortedIdentifiers = ()
    required: bool = True

    @model_validator(mode="after")
    def validate_reference_shape(self) -> PhysicsOracleV1:
        reference = self.reference
        if self.kind in {"artifact", "derivation", "document"}:
            reference = _physics_relative_path(reference)
        elif not _is_identifier(reference):
            raise ValueError("non-path oracle references must be valid identifiers")
        object.__setattr__(self, "reference", reference)
        return self


class PhysicsForbiddenClaimV1(PhysicsCanonicalModel):
    """One claim that the task authority explicitly forbids."""

    id: Identifier
    statement: BoundedString


class PhysicsHumanGateV1(PhysicsCanonicalModel):
    """Mandatory human-review triggers for the implementation profile."""

    required_for: Annotated[
        tuple[PhysicsHumanGateTrigger, ...],
        BeforeValidator(_freeze_sequence),
        AfterValidator(_canonical_unique_strings),
        Field(min_length=1, max_length=20),
    ]

    @model_validator(mode="after")
    def validate_mandatory_triggers(self) -> PhysicsHumanGateV1:
        if set(self.required_for) != _MANDATORY_HUMAN_GATES:
            raise ValueError(
                "physics_implementation requires all three fixed human-gate triggers"
            )
        return self


class PhysicsAuditPolicyV1(PhysicsCanonicalModel):
    """Explicit deterministic routing policy for non-human scientific conditions."""

    schema_version: Literal[1] = 1
    insufficient_required_evidence: PhysicsEvidenceRoute
    conflicting_required_evidence: PhysicsEvidenceRoute
    medium_severity: PhysicsAdvisoryRoute
    low_severity: PhysicsAdvisoryRoute
    informational_severity: PhysicsAdvisoryRoute


DEFAULT_PHYSICS_AUDIT_POLICY_V1 = PhysicsAuditPolicyV1(
    insufficient_required_evidence="block",
    conflicting_required_evidence="human_review",
    medium_severity="request_repair",
    low_severity="allow_pass",
    informational_severity="allow_pass",
)


class PhysicsTaskContractV1(PhysicsCanonicalModel):
    """Strict standalone PA-1 task authority for physics implementation review."""

    schema_version: Literal[1]
    profile: PhysicsProfile
    conventions: Annotated[
        tuple[PhysicsConventionV1, ...],
        BeforeValidator(_freeze_sequence),
        Field(min_length=1, max_length=MAX_PHYSICS_ITEMS),
    ]
    assumptions: Annotated[
        tuple[PhysicsAssumptionV1, ...],
        BeforeValidator(_freeze_sequence),
        Field(min_length=1, max_length=MAX_PHYSICS_ITEMS),
    ]
    required_identities: Annotated[
        tuple[PhysicsRequiredIdentityV1, ...],
        BeforeValidator(_freeze_sequence),
        Field(min_length=1, max_length=MAX_PHYSICS_ITEMS),
    ]
    limiting_cases: Annotated[
        tuple[PhysicsLimitingCaseV1, ...],
        BeforeValidator(_freeze_sequence),
        Field(min_length=1, max_length=MAX_PHYSICS_ITEMS),
    ]
    evidence: Annotated[
        tuple[PhysicsEvidenceDeclarationV1, ...],
        BeforeValidator(_freeze_sequence),
        Field(max_length=MAX_PHYSICS_ITEMS),
    ] = ()
    oracles: Annotated[
        tuple[PhysicsOracleV1, ...],
        BeforeValidator(_freeze_sequence),
        Field(min_length=1, max_length=MAX_PHYSICS_ITEMS),
    ]
    forbidden_claims: Annotated[
        tuple[PhysicsForbiddenClaimV1, ...],
        BeforeValidator(_freeze_sequence),
        Field(min_length=1, max_length=MAX_PHYSICS_ITEMS),
    ]
    human_gate: PhysicsHumanGateV1
    audit_policy: PhysicsAuditPolicyV1 | None = None
    auditor_role_ref: Identifier | None = None

    @field_validator(
        "conventions",
        "assumptions",
        "required_identities",
        "limiting_cases",
        "evidence",
        "oracles",
        "forbidden_claims",
    )
    @classmethod
    def canonicalize_id_collections(cls, value: tuple[Any, ...]) -> tuple[Any, ...]:
        return tuple(sorted(value, key=lambda item: item.id))

    @model_validator(mode="after")
    def validate_contract_graph(self) -> PhysicsTaskContractV1:
        named_collections: tuple[tuple[str, tuple[Any, ...]], ...] = (
            ("convention", self.conventions),
            ("assumption", self.assumptions),
            ("required identity", self.required_identities),
            ("limiting case", self.limiting_cases),
            ("evidence", self.evidence),
            ("oracle", self.oracles),
            ("forbidden claim", self.forbidden_claims),
        )
        all_ids: dict[str, str] = {}
        for label, items in named_collections:
            identifiers = [item.id for item in items]
            if len(identifiers) != len(set(identifiers)):
                raise ValueError(f"{label} IDs must be unique")
            for identifier in identifiers:
                prior = all_ids.get(identifier)
                if prior is not None:
                    raise ValueError(
                        f"ID {identifier!r} is declared as both {prior} and {label}"
                    )
                all_ids[identifier] = label

        check_ids = {
            *(item.id for item in self.required_identities),
            *(item.id for item in self.limiting_cases),
        }
        oracle_ids = {item.id for item in self.oracles}
        claim_ids = {item.id for item in self.forbidden_claims}
        reference_targets = check_ids | oracle_ids | claim_ids
        check_oracle_references = tuple(
            (item.id, item.oracle_ids) for item in self.required_identities
        ) + tuple((item.id, item.oracle_ids) for item in self.limiting_cases)
        for check_id, check_oracle_ids in check_oracle_references:
            missing = sorted(set(check_oracle_ids) - oracle_ids)
            if missing:
                raise ValueError(
                    f"{check_id} references undeclared oracles: {', '.join(missing)}"
                )
        for oracle in self.oracles:
            missing = sorted(set(oracle.check_ids) - check_ids)
            if missing:
                raise ValueError(
                    f"{oracle.id} references undeclared checks: {', '.join(missing)}"
                )
        for item in self.evidence:
            missing = sorted(set(item.required_for) - reference_targets)
            if missing:
                raise ValueError(
                    f"{item.id} references undeclared requirements: {', '.join(missing)}"
                )

        oracle_targets = [(item.kind, item.reference) for item in self.oracles]
        if len(oracle_targets) != len(set(oracle_targets)):
            raise ValueError("oracle kind/reference declarations must be unique")
        path_targets = [
            (item.kind, item.path) for item in self.evidence if item.path is not None
        ]
        if len(path_targets) != len(set(path_targets)):
            raise ValueError("evidence kind/path declarations must be unique")
        return self


class PhysicsEvidenceReferenceV1(PhysicsCanonicalModel):
    """One closed, contract-resolved evidence reference from an untrusted report."""

    kind: PhysicsEvidenceReferenceKind
    reference: BoundedString | None
    path: RelativePhysicsPath | None
    line_start: OptionalPositiveLine
    line_end: OptionalPositiveLine

    @model_validator(mode="after")
    def validate_reference_shape(self) -> PhysicsEvidenceReferenceV1:
        identifier_kind = self.kind in {"test", "artifact", "oracle", "numerical"}
        path_kind = self.kind in {"source", "derivation", "document"}
        if self.kind == "task_contract":
            if self.reference is None or self.path is not None:
                raise ValueError("task-contract references require only reference")
        elif identifier_kind:
            if (
                self.reference is None
                or not _is_identifier(self.reference)
                or self.path is not None
            ):
                raise ValueError(f"{self.kind} evidence requires one declared ID")
        elif path_kind and (self.reference is not None or self.path is None):
            raise ValueError(f"{self.kind} evidence requires one declared path")
        if path_kind:
            has_start = self.line_start is not None
            has_end = self.line_end is not None
            if self.kind == "source" and not (has_start and has_end):
                raise ValueError("source evidence requires a bounded line range")
            if has_start != has_end:
                raise ValueError("line ranges require both start and end")
            if (
                has_start
                and self.line_start is not None
                and self.line_end is not None
                and self.line_start > self.line_end
            ):
                raise ValueError("line ranges must not be reversed")
        elif self.line_start is not None or self.line_end is not None:
            raise ValueError("non-path evidence must not contain line numbers")
        return self


def _evidence_reference_key(item: PhysicsEvidenceReferenceV1) -> tuple[object, ...]:
    return (
        item.kind,
        item.reference or "",
        item.path or "",
        item.line_start or 0,
        item.line_end or 0,
    )


def _canonical_evidence_references(
    value: tuple[PhysicsEvidenceReferenceV1, ...],
) -> tuple[PhysicsEvidenceReferenceV1, ...]:
    keys = [_evidence_reference_key(item) for item in value]
    if len(keys) != len(set(keys)):
        raise ValueError("evidence references must be unique")
    return tuple(sorted(value, key=_evidence_reference_key))


EvidenceReferences = Annotated[
    tuple[PhysicsEvidenceReferenceV1, ...],
    BeforeValidator(_freeze_sequence),
    AfterValidator(_canonical_evidence_references),
    Field(max_length=MAX_PHYSICS_ITEMS),
]
RequiredEvidenceReferences = Annotated[
    tuple[PhysicsEvidenceReferenceV1, ...],
    BeforeValidator(_freeze_sequence),
    AfterValidator(_canonical_evidence_references),
    Field(min_length=1, max_length=MAX_PHYSICS_ITEMS),
]


class PhysicsAuditCheckResultV1(PhysicsCanonicalModel):
    """One assessment of a required contract identity, limit, or oracle."""

    id: Identifier
    target_kind: PhysicsCheckTargetKind
    target_id: Identifier
    status: PhysicsCheckStatus
    evidence_sufficiency: PhysicsEvidenceSufficiency
    evidence: EvidenceReferences
    rationale: BoundedString

    @model_validator(mode="after")
    def validate_status_and_evidence(self) -> PhysicsAuditCheckResultV1:
        if self.status in {"passed", "failed"}:
            if self.evidence_sufficiency != "sufficient" or not self.evidence:
                raise ValueError(
                    "passed and failed checks require sufficient referenced evidence"
                )
        elif self.evidence_sufficiency == "sufficient":
            raise ValueError("unresolved checks cannot declare sufficient evidence")
        return self


class PhysicsFindingV1(PhysicsCanonicalModel):
    """One bounded finding whose disposition is validated independently of prose."""

    id: Identifier
    severity: PhysicsFindingSeverity
    category: PhysicsFindingCategory
    status: PhysicsFindingStatus
    disposition: PhysicsFindingDisposition
    check_ids: RequiredSortedIdentifiers
    forbidden_claim_ids: SortedIdentifiers
    evidence: RequiredEvidenceReferences
    statement: BoundedString
    required_action: BoundedString

    @model_validator(mode="after")
    def validate_category_disposition(self) -> PhysicsFindingV1:
        if self.category in _HUMAN_ONLY_FINDINGS and self.disposition != "human_review":
            raise ValueError(f"{self.category} findings require human review")
        if self.disposition == "human_review" and self.status != "open":
            raise ValueError("human-review findings cannot be resolved by a report")
        if (
            self.category in _EVIDENCE_ONLY_FINDINGS
            and self.disposition != "evidence_blocking"
        ):
            raise ValueError(f"{self.category} findings must block on evidence")
        if self.category == "report_integrity_error" and (
            self.disposition != "infrastructure_failure"
        ):
            raise ValueError("report_integrity_error requires infrastructure failure")
        if self.disposition == "infrastructure_failure" and (
            self.category != "report_integrity_error" or self.status != "open"
        ):
            raise ValueError(
                "infrastructure failure requires an open report_integrity_error"
            )
        if self.category == "unsupported_physical_claim" and (
            not self.forbidden_claim_ids
        ):
            raise ValueError(
                "unsupported physical claims must reference a forbidden claim ID"
            )
        return self


class PhysicsUnresolvedQuestionV1(PhysicsCanonicalModel):
    """One bounded unresolved scientific question that cannot route to pass."""

    id: Identifier
    category: PhysicsQuestionCategory
    question: BoundedString
    evidence: RequiredEvidenceReferences


_SUFFICIENCY_RANK: Mapping[str, int] = {
    "sufficient": 0,
    "partial": 1,
    "insufficient": 2,
    "conflicting": 3,
}


class PhysicsAuditReportV1(PhysicsCanonicalModel):
    """Strict untrusted Physics Auditor output; its verdict is not routing authority."""

    schema_version: Literal[1]
    profile: PhysicsProfile
    verdict: PhysicsVerdict
    evidence_sufficiency: PhysicsEvidenceSufficiency
    summary: BoundedString
    human_gate_triggers: Annotated[
        tuple[PhysicsHumanGateTrigger, ...],
        BeforeValidator(_freeze_sequence),
        AfterValidator(_canonical_unique_strings),
        Field(max_length=20),
    ]
    checks: Annotated[
        tuple[PhysicsAuditCheckResultV1, ...],
        BeforeValidator(_freeze_sequence),
        Field(min_length=1, max_length=MAX_PHYSICS_ITEMS),
    ]
    findings: Annotated[
        tuple[PhysicsFindingV1, ...],
        BeforeValidator(_freeze_sequence),
        Field(max_length=MAX_PHYSICS_ITEMS),
    ]
    unresolved_questions: Annotated[
        tuple[PhysicsUnresolvedQuestionV1, ...],
        BeforeValidator(_freeze_sequence),
        Field(max_length=MAX_PHYSICS_ITEMS),
    ]

    @field_validator("checks", "findings", "unresolved_questions")
    @classmethod
    def canonicalize_report_collections(cls, value: tuple[Any, ...]) -> tuple[Any, ...]:
        return tuple(sorted(value, key=lambda item: item.id))

    @model_validator(mode="after")
    def validate_report_consistency(self) -> PhysicsAuditReportV1:
        for label, items in (
            ("check", self.checks),
            ("finding", self.findings),
            ("unresolved-question", self.unresolved_questions),
        ):
            identifiers = [item.id for item in items]
            if len(identifiers) != len(set(identifiers)):
                raise ValueError(f"{label} IDs must be unique")

        check_ids = {item.id for item in self.checks}
        for finding in self.findings:
            missing = sorted(set(finding.check_ids) - check_ids)
            if missing:
                raise ValueError(
                    f"{finding.id} references undeclared checks: {', '.join(missing)}"
                )

        open_findings = tuple(item for item in self.findings if item.status == "open")
        for check in self.checks:
            if check.status != "passed" and not any(
                check.id in finding.check_ids for finding in open_findings
            ):
                raise ValueError(
                    f"non-passing check {check.id} requires an open finding"
                )
        nonpassing_ids = {item.id for item in self.checks if item.status != "passed"}
        for finding in open_findings:
            if finding.severity in {"critical", "high"} and not (
                set(finding.check_ids) & nonpassing_ids
            ):
                raise ValueError(
                    "open critical/high findings require a non-passing check"
                )

        derived_sufficiency = max(
            self.checks,
            key=lambda item: _SUFFICIENCY_RANK[item.evidence_sufficiency],
        ).evidence_sufficiency
        if self.evidence_sufficiency != derived_sufficiency:
            raise ValueError(
                "top-level evidence sufficiency must equal the worst required check"
            )

        if self.verdict == "pass":
            impossible = (
                self.evidence_sufficiency != "sufficient"
                or any(item.status != "passed" for item in self.checks)
                or any(
                    item.status == "open" and item.severity in {"critical", "high"}
                    for item in self.findings
                )
                or any(
                    item.category == "report_integrity_error"
                    for item in self.findings
                )
                or bool(self.human_gate_triggers)
                or bool(self.unresolved_questions)
            )
            if impossible:
                raise ValueError(
                    "pass contradicts checks, evidence, findings, or human gates"
                )
        if self.verdict == "fail_repairable" and not any(
            item.status == "open" and item.disposition == "repairable"
            for item in self.findings
        ):
            raise ValueError("fail_repairable requires an open repairable finding")
        return self


def load_physics_task_contract(path: Path) -> PhysicsTaskContractV1:
    """Read and validate one standalone YAML/JSON physics contract without writes."""
    value = _load_mapping(path, "physics contract", PhysicsContractError)
    try:
        return PhysicsTaskContractV1.model_validate(value)
    except ValidationError as exc:
        raise PhysicsContractError(
            "physics contract validation failed: " + _validation_details(exc)
        ) from exc


def load_physics_audit_report(
    path: Path,
    contract: PhysicsTaskContractV1,
) -> PhysicsAuditReportV1:
    """Read and contract-validate a YAML/JSON audit report without writes."""
    value = _load_mapping(path, "physics audit report", PhysicsAuditError)
    try:
        report = PhysicsAuditReportV1.model_validate(value)
    except ValidationError as exc:
        raise PhysicsAuditError(
            "physics audit report validation failed: " + _validation_details(exc)
        ) from exc
    return validate_physics_audit_report(contract, report)


def parse_physics_audit_report_json(
    value: str | bytes,
    contract: PhysicsTaskContractV1,
) -> PhysicsAuditReportV1:
    """Parse strict JSON model output and close every reference over the contract."""
    try:
        text = value.decode("utf-8") if isinstance(value, bytes) else value
        parsed = json.loads(text, parse_constant=_reject_json_constant)
        if not isinstance(parsed, dict):
            raise ValueError("report root must be an object")
        report = PhysicsAuditReportV1.model_validate(parsed)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, ValidationError) as exc:
        raise PhysicsAuditError("physics audit report JSON is missing or invalid") from exc
    return validate_physics_audit_report(contract, report)


def validate_physics_audit_report(
    contract: PhysicsTaskContractV1,
    report: PhysicsAuditReportV1,
) -> PhysicsAuditReportV1:
    """Validate report targets and evidence references against frozen task authority."""
    if report.profile != contract.profile:
        raise PhysicsAuditError("physics audit report profile does not match the contract")

    expected_targets = {
        *(('required_identity', item.id) for item in contract.required_identities),
        *(('limiting_case', item.id) for item in contract.limiting_cases),
        *(('oracle', item.id) for item in contract.oracles if item.required),
    }
    actual_targets = {(item.target_kind, item.target_id) for item in report.checks}
    if len(actual_targets) != len(report.checks):
        raise PhysicsAuditError("physics audit report repeats a required target")
    if actual_targets != expected_targets:
        raise PhysicsAuditError(
            "physics audit report checks do not exactly cover required contract targets"
        )

    check_by_id = {item.id: item for item in report.checks}
    claim_ids = {item.id for item in contract.forbidden_claims}
    oracle_by_id = {item.id: item for item in contract.oracles}
    required_by_target: dict[tuple[str, str], tuple[str, ...]] = {
        **{
            ("required_identity", item.id): item.required_evidence_kinds
            for item in contract.required_identities
        },
        **{
            ("limiting_case", item.id): item.required_evidence_kinds
            for item in contract.limiting_cases
        },
    }

    for check in report.checks:
        for reference in check.evidence:
            _validate_evidence_reference(contract, reference)
        required_kinds = set(
            required_by_target.get((check.target_kind, check.target_id), ())
        )
        present_kinds = _present_evidence_kinds(check.evidence, oracle_by_id)
        missing = sorted(required_kinds - present_kinds)
        if missing and check.status != "unresolved":
            raise PhysicsAuditError(
                f"check {check.id} lacks required evidence kinds: {', '.join(missing)}"
            )
        if check.target_kind == "oracle" and not any(
            reference.kind == "oracle" and reference.reference == check.target_id
            for reference in check.evidence
        ):
            raise PhysicsAuditError(
                f"oracle check {check.id} must reference its declared oracle"
            )

    for finding in report.findings:
        for reference in finding.evidence:
            _validate_evidence_reference(contract, reference)
        missing_claims = sorted(set(finding.forbidden_claim_ids) - claim_ids)
        if missing_claims:
            raise PhysicsAuditError(
                f"finding {finding.id} references undeclared forbidden claims"
            )
        if (
            finding.disposition == "repairable"
            and finding.severity in {"critical", "high"}
            and not any(
                check_by_id[check_id].status == "failed"
                for check_id in finding.check_ids
            )
        ):
            raise PhysicsAuditError(
                f"repairable finding {finding.id} requires a failed check"
            )
        if finding.disposition == "evidence_blocking" and not any(
            check_by_id[check_id].status == "unresolved"
            for check_id in finding.check_ids
        ):
            raise PhysicsAuditError(
                f"evidence-blocking finding {finding.id} requires an unresolved check"
            )
    for question in report.unresolved_questions:
        for reference in question.evidence:
            _validate_evidence_reference(contract, reference)
    return report


def _present_evidence_kinds(
    references: tuple[PhysicsEvidenceReferenceV1, ...],
    oracle_by_id: Mapping[str, PhysicsOracleV1],
) -> set[str]:
    kinds: set[str] = set()
    for reference in references:
        if reference.kind == "task_contract":
            continue
        if reference.kind == "source":
            kinds.add("artifact")
        elif reference.kind == "oracle":
            kinds.add("oracle")
            assert reference.reference is not None
            kinds.add(oracle_by_id[reference.reference].kind)
        else:
            kinds.add(reference.kind)
    return kinds


def _validate_evidence_reference(
    contract: PhysicsTaskContractV1,
    reference: PhysicsEvidenceReferenceV1,
) -> None:
    declarations = {item.id: item for item in contract.evidence}
    oracle_by_id = {item.id: item for item in contract.oracles}
    if reference.kind == "task_contract":
        assert reference.reference is not None
        if not _contract_field_exists(contract, reference.reference):
            raise PhysicsAuditError(
                "physics audit report references an undeclared contract field"
            )
        return
    if reference.kind == "oracle":
        if reference.reference not in oracle_by_id:
            raise PhysicsAuditError(
                "physics audit report references an undeclared oracle ID"
            )
        return
    if reference.kind in {"test", "artifact", "numerical"}:
        assert reference.reference is not None
        declared = declarations.get(reference.reference)
        oracle_references = {
            item.reference for item in contract.oracles if item.kind == reference.kind
        }
        if not (
            (declared is not None and declared.kind == reference.kind)
            or reference.reference in oracle_references
        ):
            raise PhysicsAuditError(
                f"physics audit report references an undeclared {reference.kind} ID"
            )
        return

    assert reference.path is not None
    declared_paths = {
        item.path
        for item in contract.evidence
        if item.path is not None
        and (
            item.kind == reference.kind
            or (reference.kind == "source" and item.kind in {"artifact", "derivation"})
        )
    }
    oracle_paths = {
        item.reference
        for item in contract.oracles
        if item.kind in {"artifact", "derivation", "document"}
        and (
            item.kind == reference.kind
            or (reference.kind == "source" and item.kind in {"artifact", "derivation"})
        )
    }
    if reference.path not in declared_paths | oracle_paths:
        raise PhysicsAuditError(
            f"physics audit report references an undeclared {reference.kind} path"
        )


def _contract_field_exists(contract: PhysicsTaskContractV1, locator: str) -> bool:
    if locator in {"schema_version", "profile", "human_gate", "audit_policy"}:
        return True
    if "." not in locator:
        return False
    collection, identifier = locator.split(".", 1)
    collections: Mapping[str, tuple[Any, ...]] = {
        "conventions": contract.conventions,
        "assumptions": contract.assumptions,
        "required_identities": contract.required_identities,
        "limiting_cases": contract.limiting_cases,
        "evidence": contract.evidence,
        "oracles": contract.oracles,
        "forbidden_claims": contract.forbidden_claims,
    }
    return collection in collections and any(
        item.id == identifier for item in collections[collection]
    )


def _load_mapping(
    path: Path,
    label: str,
    error_type: type[PhysicsContractError] | type[PhysicsAuditError],
) -> dict[object, object]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise error_type(f"could not read {label}: {exc.strerror or exc}") from exc
    if len(raw) > MAX_PHYSICS_INPUT_BYTES:
        raise error_type(f"{label} exceeds the {MAX_PHYSICS_INPUT_BYTES}-byte limit")
    try:
        source = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise error_type(f"{label} is not valid UTF-8 at byte offset {exc.start}") from exc
    try:
        value: Any = yaml.load(source, Loader=_UniqueKeySafeLoader)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        location = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        problem = getattr(exc, "problem", None) or "invalid YAML or JSON"
        raise error_type(f"malformed {label}{location}: {problem}") from exc
    if not isinstance(value, dict):
        raise error_type(f"{label} root must be a mapping")
    return value


def _validation_details(exc: ValidationError) -> str:
    return "; ".join(_format_validation_error(error) for error in exc.errors())


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


PHYSICS_AUDIT_REPORT_OUTPUT_SCHEMA: dict[str, object] = normalize_production_schema(
    PhysicsAuditReportV1.model_json_schema()
)
