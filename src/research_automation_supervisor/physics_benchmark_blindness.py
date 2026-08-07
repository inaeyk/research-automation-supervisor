"""PA-5C1 blind Physics Auditor fixture and scientific-authority qualification.

This module prepares and certifies fixture inputs.  It deliberately contains no
benchmark scorer, route scorer, model orchestration, or real-model execution.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import stat
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal, TypeAlias, cast

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

from research_automation_supervisor.durable_state import canonical_json
from research_automation_supervisor.errors import (
    PhysicsBenchmarkBlindnessInputError,
    PhysicsBenchmarkBlindnessIntegrityError,
)
from research_automation_supervisor.physics_auditor_models import (
    PHYSICS_AUDITOR_BUBBLEWRAP_POLICY_V1,
    PhysicsAuditorProjectionManifestV1,
)
from research_automation_supervisor.physics_auditor_projection import (
    AUTHORITY_DIRECTORY,
    verify_physics_auditor_projection,
)
from research_automation_supervisor.physics_models import load_physics_task_contract
from research_automation_supervisor.workflow_models import _freeze_sequence

MAX_BLIND_CATALOG_BYTES = 4 * 1024 * 1024
MAX_BLIND_FILES = 2_000
MAX_BLIND_FILE_BYTES = 16 * 1024 * 1024
MAX_RAW_MEASUREMENTS = 1_000

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
NeutralCaseId = Annotated[str, Field(pattern=r"^case_[0-9]{3}$")]
NeutralTaskId = Annotated[str, Field(pattern=r"^task_[0-9]{3}$")]
NeutralPairId = Annotated[str, Field(pattern=r"^pair_[0-9]{3}$")]
NeutralVariantId = Annotated[str, Field(pattern=r"^variant_[0-9]{3}$")]
NeutralSubjectId: TypeAlias = NeutralCaseId | NeutralTaskId

_SCORER_ONLY_KEYS = frozenset(
    {
        "acceptable_alternative_categories",
        "acceptable_alternative_routes",
        "approval",
        "approval_record",
        "clean_case",
        "clean_label",
        "defect_label",
        "diagnosis",
        "expected_diagnosis",
        "expected_interpretation",
        "expected_route",
        "fixture_authority",
        "fixture_label",
        "forbidden_categories",
        "forbidden_finding_categories",
        "forbidden_routes",
        "minimum_severity",
        "required_categories",
        "required_finding_categories",
        "review_receipt",
        "seed_kind",
        "seeded_defect",
        "seeded_diagnosis",
        "severity_floor",
    }
)
_CLASSIFICATION_OUTPUT_KEYS = frozenset(
    {
        "approved",
        "category",
        "classification",
        "clean",
        "decision",
        "defect",
        "diagnosis",
        "expected",
        "failed",
        "outcome",
        "passed",
        "route",
        "severity",
        "status",
        "verdict",
    }
)
_DIAGNOSTIC_TITLE_WORDS = re.compile(
    r"\b(?:absent|ambiguity|clean|conflict|defect|diagnosis|error|expected|failure|"
    r"forbidden|missing|route|seeded|unsupported|wrong)\b",
    re.IGNORECASE,
)
_DIAGNOSTIC_CONTRACT_WORDS = re.compile(
    r"\b(?:absent|clean|conflict|defect|diagnosis|expected|failure|forbidden|missing|"
    r"route|seeded|unsupported|wrong)\b",
    re.IGNORECASE,
)
_IDENTITY_LITERAL = re.compile(r"^(?:case|task|pair|variant)_[0-9]{3}$")
_SYSTEM_MOUNT_ROOTS = tuple(Path(item) for item in ("/usr", "/bin", "/sbin", "/lib", "/lib64"))


def _relative_path(value: Any) -> str:
    if not isinstance(value, str) or value != value.strip() or "\\" in value or "\x00" in value:
        raise ValueError("paths must use canonical relative POSIX syntax")
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or path.as_posix() != value
        or any(part in {"", ".", ".."} for part in path.parts)
        or ":" in path.parts[0]
    ):
        raise ValueError("paths must remain below their assigned root")
    return value


RelativePath = Annotated[str, BeforeValidator(_relative_path)]


def _sorted_unique_strings(value: tuple[str, ...]) -> tuple[str, ...]:
    if len(value) != len(set(value)):
        raise ValueError("values must be unique")
    return tuple(sorted(value))


SortedStrings = Annotated[
    tuple[str, ...],
    BeforeValidator(_freeze_sequence),
    AfterValidator(_sorted_unique_strings),
    Field(max_length=MAX_BLIND_FILES),
]


class BlindCanonicalModel(BaseModel):
    """Strict frozen model with the repository's canonical serializer."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    def to_canonical_json(self) -> bytes:
        return canonical_json(self.model_dump(mode="json"))

    def canonical_sha256(self) -> str:
        return hashlib.sha256(self.to_canonical_json()).hexdigest()


class AuditorVisibleObjectV1(BlindCanonicalModel):
    """One exact byte object eligible for auditor-visible preparation."""

    path: RelativePath
    role: Literal[
        "contract",
        "title",
        "evidence",
        "source",
        "raw_observation",
        "oracle_program",
    ]
    byte_length: Annotated[int, Field(ge=0, le=MAX_BLIND_FILE_BYTES)]
    sha256: Sha256


class AuditorVisibleManifestV1(BlindCanonicalModel):
    """Exact byte manifest for one neutral Auditor input variant."""

    schema_version: Literal[1] = 1
    subject_id: str
    pair_id: NeutralPairId | None
    variant_id: NeutralVariantId | None
    objects: Annotated[
        tuple[AuditorVisibleObjectV1, ...],
        BeforeValidator(_freeze_sequence),
        Field(min_length=1, max_length=MAX_BLIND_FILES),
    ]

    @field_validator("subject_id")
    @classmethod
    def validate_subject_id(cls, value: str) -> str:
        if not re.fullmatch(r"(?:case|task)_[0-9]{3}", value):
            raise ValueError("fixture subjects require neutral identifiers")
        return value

    @field_validator("objects")
    @classmethod
    def canonicalize_objects(
        cls, value: tuple[AuditorVisibleObjectV1, ...]
    ) -> tuple[AuditorVisibleObjectV1, ...]:
        items = tuple(sorted(value, key=lambda item: item.path))
        if len({item.path for item in items}) != len(items):
            raise ValueError("visible manifest paths must be unique")
        return items


class PairedVisibleManifestV1(BlindCanonicalModel):
    """The two exact manifests reviewed together as one blind pair."""

    schema_version: Literal[1] = 1
    case_id: NeutralCaseId
    pair_id: NeutralPairId
    variants: Annotated[
        tuple[AuditorVisibleManifestV1, AuditorVisibleManifestV1],
        BeforeValidator(_freeze_sequence),
    ]
    contract_sha256: Sha256
    oracle_definition_sha256: Sha256
    evidence_schema_sha256: Sha256

    @model_validator(mode="after")
    def validate_pair(self) -> PairedVisibleManifestV1:
        if tuple(item.variant_id for item in self.variants) != (
            "variant_001",
            "variant_002",
        ):
            raise ValueError("blind pairs require the two fixed neutral variant IDs")
        if any(
            item.subject_id != self.case_id or item.pair_id != self.pair_id
            for item in self.variants
        ):
            raise ValueError("paired visible manifests contradict their neutral pair")
        return self


class BlindVariantAuthorityV1(BlindCanonicalModel):
    """Scorer-only semantic authority for one neutral variant."""

    variant_id: NeutralVariantId
    visible_root: RelativePath
    fixture_label: Literal["clean", "defective"]
    expected_route: Literal[
        "pass",
        "request_repair",
        "block_insufficient_evidence",
        "require_human_review",
    ]
    diagnosis: Annotated[str, Field(min_length=1, max_length=2_000)]
    minimum_severity: Literal["informational", "low", "medium", "high", "critical"] | None
    required_categories: SortedStrings = ()
    acceptable_alternative_categories: SortedStrings = Field(
        default=(), exclude_if=lambda value: not value
    )
    forbidden_categories: SortedStrings = ()


class BlindPairAuthorityV1(BlindCanonicalModel):
    """Scorer-only pair definition; this model must never enter PA-3."""

    schema_version: Literal[1] = 1
    case_id: NeutralCaseId
    pair_id: NeutralPairId
    title_file: RelativePath
    contract_file: RelativePath
    oracle_files: Annotated[
        tuple[RelativePath, ...],
        BeforeValidator(_freeze_sequence),
        Field(min_length=1, max_length=20),
    ]
    raw_observation_files: Annotated[
        tuple[RelativePath, ...],
        BeforeValidator(_freeze_sequence),
        Field(min_length=1, max_length=20),
    ]
    variable_files: Annotated[
        tuple[RelativePath, ...],
        BeforeValidator(_freeze_sequence),
        Field(min_length=1, max_length=20),
    ]
    variants: Annotated[
        tuple[BlindVariantAuthorityV1, BlindVariantAuthorityV1],
        BeforeValidator(_freeze_sequence),
    ]
    receipt_path: RelativePath

    @model_validator(mode="after")
    def validate_authority(self) -> BlindPairAuthorityV1:
        if tuple(item.variant_id for item in self.variants) != (
            "variant_001",
            "variant_002",
        ):
            raise ValueError("pair variants must use the two neutral IDs in order")
        if {item.fixture_label for item in self.variants} != {"clean", "defective"}:
            raise ValueError(
                "each scorer-only pair must contain one clean and one defective variant"
            )
        required_variable = set(self.raw_observation_files)
        if not required_variable.issubset(self.variable_files):
            raise ValueError("raw observations must be explicitly pair-variable")
        if self.contract_file in self.variable_files or set(self.oracle_files) & set(
            self.variable_files
        ):
            raise ValueError("paired contract and oracle definitions cannot vary")
        return self


class GLSourceBlobV1(BlindCanonicalModel):
    """Exact source blob eligible for model-free GL preparation."""

    path: RelativePath
    role: Literal["implementation", "test", "locked_derivation"]
    byte_length: Annotated[int, Field(ge=1, le=MAX_BLIND_FILE_BYTES)]
    sha256: Sha256


class GLFixtureAuthorityV1(BlindCanonicalModel):
    """Scorer-only authority for one bounded, never-launched GL fixture."""

    schema_version: Literal[1] = 1
    task_id: NeutralTaskId
    visible_root: RelativePath
    title_file: RelativePath
    contract_file: RelativePath
    oracle_files: Annotated[
        tuple[RelativePath, ...],
        BeforeValidator(_freeze_sequence),
        Field(min_length=1, max_length=20),
    ]
    source_blobs: Annotated[
        tuple[GLSourceBlobV1, ...],
        BeforeValidator(_freeze_sequence),
        Field(min_length=1, max_length=100),
    ]
    expected_route: Literal[
        "pass",
        "request_repair",
        "block_insufficient_evidence",
        "require_human_review",
    ]
    expected_interpretation: Annotated[str, Field(min_length=1, max_length=2_000)]
    minimum_severity: Literal["informational", "low", "medium", "high", "critical"] | None
    required_categories: SortedStrings = ()
    forbidden_categories: SortedStrings = ()
    receipt_path: RelativePath

    @field_validator("source_blobs")
    @classmethod
    def canonicalize_blobs(cls, value: tuple[GLSourceBlobV1, ...]) -> tuple[GLSourceBlobV1, ...]:
        items = tuple(sorted(value, key=lambda item: item.path))
        if len({item.path for item in items}) != len(items):
            raise ValueError("GL source paths must be unique")
        return items


class PhysicsBlindFixtureCatalogV1(BlindCanonicalModel):
    """Detached scorer-only catalog for PA-5C1 fixture qualification."""

    schema_version: Literal[1] = 1
    catalog_id: Literal["physics_benchmark_blind_authority_v1"]
    auditor_visible_root: RelativePath
    scorer_only_root: RelativePath
    fixture_author_ids: SortedStrings
    gl_source_commit: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    pairs: Annotated[
        tuple[BlindPairAuthorityV1, ...],
        BeforeValidator(_freeze_sequence),
        Field(min_length=1, max_length=100),
    ]
    gl_tasks: Annotated[
        tuple[GLFixtureAuthorityV1, ...],
        BeforeValidator(_freeze_sequence),
        Field(min_length=1, max_length=100),
    ]

    @model_validator(mode="after")
    def validate_catalog(self) -> PhysicsBlindFixtureCatalogV1:
        if self.auditor_visible_root == self.scorer_only_root:
            raise ValueError("auditor-visible and scorer-only roots must be distinct")
        pair_ids = [item.pair_id for item in self.pairs]
        case_ids = [item.case_id for item in self.pairs]
        task_ids = [item.task_id for item in self.gl_tasks]
        if len(pair_ids) != len(set(pair_ids)) or len(case_ids) != len(set(case_ids)):
            raise ValueError("blind pair and case IDs must be unique")
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("GL task IDs must be unique")
        object.__setattr__(self, "pairs", tuple(sorted(self.pairs, key=lambda item: item.case_id)))
        object.__setattr__(
            self,
            "gl_tasks",
            tuple(sorted(self.gl_tasks, key=lambda item: item.task_id)),
        )
        return self

    def pair(self, case_id: str) -> BlindPairAuthorityV1:
        matches = tuple(item for item in self.pairs if item.case_id == case_id)
        if len(matches) != 1:
            raise PhysicsBenchmarkBlindnessInputError("neutral benchmark case is unavailable")
        return matches[0]

    def gl_task(self, task_id: str) -> GLFixtureAuthorityV1:
        matches = tuple(item for item in self.gl_tasks if item.task_id == task_id)
        if len(matches) != 1:
            raise PhysicsBenchmarkBlindnessInputError("neutral GL fixture is unavailable")
        return matches[0]


class HumanReviewReceiptV1(BlindCanonicalModel):
    """Detached immutable human review; no generator in this package issues one."""

    schema_version: Literal[1] = 1
    receipt_sha256: Sha256
    receipt_id: Annotated[str, Field(pattern=r"^review_[a-z0-9_]{1,72}$")]
    subject_id: str
    reviewer_id: Annotated[str, Field(min_length=3, max_length=160)]
    reviewer_kind: Literal["human"]
    fixture_author_ids: SortedStrings
    reviewed_visible_manifest_sha256: Sha256
    reviewed_scorer_authority_sha256: Sha256
    decision: Literal["approved", "revise", "remove"]
    scientific_review: Annotated[str, Field(min_length=20, max_length=4_000)]
    issued_at: Annotated[
        str, Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
    ]

    @field_validator("subject_id")
    @classmethod
    def validate_subject_id(cls, value: str) -> str:
        if not re.fullmatch(r"(?:case|task)_[0-9]{3}", value):
            raise ValueError("review receipt subject must be neutral")
        return value

    @model_validator(mode="after")
    def validate_receipt_hash(self) -> HumanReviewReceiptV1:
        payload = self.model_dump(mode="json", exclude={"receipt_sha256"})
        if self.receipt_sha256 != hashlib.sha256(canonical_json(payload)).hexdigest():
            raise ValueError("human-review receipt digest is invalid")
        return self


class RawMeasurementV1(BlindCanonicalModel):
    """One uninterpreted scalar observation emitted by a generic oracle."""

    name: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,79}$")]
    value: float | int | None
    unit: Annotated[str, Field(min_length=1, max_length=80)]
    uncertainty: float | int | None = None

    @field_validator("name")
    @classmethod
    def reject_classification_name(cls, value: str) -> str:
        if value.casefold() in _CLASSIFICATION_OUTPUT_KEYS:
            raise ValueError("raw measurement names cannot carry classifications")
        return value

    @field_validator("value", "uncertainty")
    @classmethod
    def reject_boolean_measurements(cls, value: float | int | None) -> float | int | None:
        if isinstance(value, bool):
            raise ValueError("raw measurements cannot encode boolean classifications")
        return value


class RawOracleOutputV1(BlindCanonicalModel):
    """Raw-only generic oracle output, with no outcome or classification field."""

    schema_version: Literal[1] = 1
    measurements: Annotated[
        tuple[RawMeasurementV1, ...],
        BeforeValidator(_freeze_sequence),
        Field(min_length=1, max_length=MAX_RAW_MEASUREMENTS),
    ]

    @field_validator("measurements")
    @classmethod
    def canonicalize_measurements(
        cls, value: tuple[RawMeasurementV1, ...]
    ) -> tuple[RawMeasurementV1, ...]:
        items = tuple(sorted(value, key=lambda item: item.name))
        if len({item.name for item in items}) != len(items):
            raise ValueError("raw measurement names must be unique")
        return items


class BlindnessCertificateV1(BlindCanonicalModel):
    """Prelaunch binding for one exact neutral PA-3 fixture projection."""

    schema_version: Literal[1] = 1
    certificate_id: Annotated[str, Field(pattern=r"^blindness_[a-z0-9_]{1,68}$")]
    case_id: NeutralCaseId
    pair_id: NeutralPairId
    variant_id: NeutralVariantId
    auditor_visible_manifest_sha256: Sha256
    paired_visible_manifest_sha256: Sha256
    pa3_projection_manifest_sha256: Sha256
    scorer_root_manifest_sha256: Sha256
    scorer_root_exclusion_sha256: Sha256
    paired_contract_sha256: Sha256
    paired_oracle_sha256: Sha256
    paired_evidence_schema_sha256: Sha256
    neutral_identifiers_sha256: Sha256
    review_receipt_sha256: Sha256
    reviewed_visible_manifest_sha256: Sha256
    reviewed_scorer_authority_sha256: Sha256
    bubblewrap_policy_sha256: Sha256
    runtime_home_empty_sha256: Sha256
    validation_phase: Literal["before_model_launch"] = "before_model_launch"
    model_launched_during_validation: Literal[False] = False


class FixtureQualificationV1(BlindCanonicalModel):
    """Read-only PA-5C1 fixture-authority qualification result."""

    schema_version: Literal[1] = 1
    catalog_sha256: Sha256
    scorer_root_manifest_sha256: Sha256
    pair_manifests: Annotated[
        tuple[PairedVisibleManifestV1, ...],
        BeforeValidator(_freeze_sequence),
        Field(min_length=1, max_length=100),
    ]
    gl_manifests: Annotated[
        tuple[AuditorVisibleManifestV1, ...],
        BeforeValidator(_freeze_sequence),
        Field(min_length=1, max_length=100),
    ]
    approved_subject_ids: SortedStrings
    model_launched: Literal[False] = False
    gl_pilot_launched: Literal[False] = False


class FixtureReviewSubjectV1(BlindCanonicalModel):
    """One exact subject offered to, but not approved by, a human reviewer."""

    subject_id: str
    visible_manifest_sha256: Sha256
    scorer_authority_sha256: Sha256
    review_status: Literal["unreviewed"] = "unreviewed"

    @field_validator("subject_id")
    @classmethod
    def validate_subject_id(cls, value: str) -> str:
        if not re.fullmatch(r"(?:case|task)_[0-9]{3}", value):
            raise ValueError("review packet subject must be neutral")
        return value


class FixtureReviewPacketV1(BlindCanonicalModel):
    """Generated exact-hash packet that carries no approval assertion."""

    schema_version: Literal[1] = 1
    review_packet_sha256: Sha256
    catalog_sha256: Sha256
    gl_source_commit: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    subjects: Annotated[
        tuple[FixtureReviewSubjectV1, ...],
        BeforeValidator(_freeze_sequence),
        Field(min_length=1, max_length=200),
    ]
    approval_present: Literal[False] = False
    model_launched: Literal[False] = False
    gl_pilot_launched: Literal[False] = False

    @field_validator("subjects")
    @classmethod
    def canonicalize_subjects(
        cls, value: tuple[FixtureReviewSubjectV1, ...]
    ) -> tuple[FixtureReviewSubjectV1, ...]:
        items = tuple(sorted(value, key=lambda item: item.subject_id))
        if len({item.subject_id for item in items}) != len(items):
            raise ValueError("review packet subjects must be unique")
        return items

    @model_validator(mode="after")
    def validate_packet_hash(self) -> FixtureReviewPacketV1:
        payload = self.model_dump(mode="json", exclude={"review_packet_sha256"})
        if self.review_packet_sha256 != hashlib.sha256(canonical_json(payload)).hexdigest():
            raise ValueError("fixture review-packet digest is invalid")
        return self


def load_blind_fixture_catalog(path: Path) -> PhysicsBlindFixtureCatalogV1:
    """Load one strict scorer-only fixture catalog."""
    return cast(
        PhysicsBlindFixtureCatalogV1,
        _load_json_model(path, PhysicsBlindFixtureCatalogV1, "blind fixture catalog"),
    )


def load_human_review_receipt(path: Path) -> HumanReviewReceiptV1:
    """Load one immutable receipt; this package intentionally has no receipt generator."""
    return cast(
        HumanReviewReceiptV1,
        _load_json_model(path, HumanReviewReceiptV1, "human-review receipt"),
    )


def build_paired_visible_manifest(
    pair: BlindPairAuthorityV1,
    *,
    repository_root: Path,
) -> PairedVisibleManifestV1:
    """Validate pair blindness and return its exact reviewed-byte manifest."""
    root = _canonical_directory(repository_root, "repository root")
    manifests = tuple(
        _manifest_for_directory(
            subject_id=pair.case_id,
            pair_id=pair.pair_id,
            variant_id=variant.variant_id,
            directory=_resolve_below(root, variant.visible_root, kind="directory"),
        )
        for variant in pair.variants
    )
    left, right = cast(tuple[AuditorVisibleManifestV1, AuditorVisibleManifestV1], manifests)
    left_objects = {item.path: item for item in left.objects}
    right_objects = {item.path: item for item in right.objects}
    if set(left_objects) != set(right_objects):
        raise PhysicsBenchmarkBlindnessIntegrityError(
            "paired variants do not expose identical filenames"
        )
    variable = set(pair.variable_files)
    for path in sorted(left_objects):
        if path not in variable and left_objects[path] != right_objects[path]:
            raise PhysicsBenchmarkBlindnessIntegrityError(
                "paired variants differ outside source bytes or raw observations"
            )
    if not any(left_objects[path].sha256 != right_objects[path].sha256 for path in variable):
        raise PhysicsBenchmarkBlindnessIntegrityError("paired variants contain no blind byte delta")
    title_left = _object_bytes(root, pair.variants[0].visible_root, pair.title_file)
    title_right = _object_bytes(root, pair.variants[1].visible_root, pair.title_file)
    if title_left != title_right:
        raise PhysicsBenchmarkBlindnessIntegrityError(
            "paired fixture titles are not byte-identical"
        )
    _validate_neutral_title(title_left)
    contract_left = _object_bytes(root, pair.variants[0].visible_root, pair.contract_file)
    contract_right = _object_bytes(root, pair.variants[1].visible_root, pair.contract_file)
    if contract_left != contract_right:
        raise PhysicsBenchmarkBlindnessIntegrityError(
            "paired visible contracts are not byte-identical"
        )
    contract_path = _resolve_below(
        _resolve_below(root, pair.variants[0].visible_root, kind="directory"),
        pair.contract_file,
        kind="file",
    )
    contract = load_physics_task_contract(contract_path)
    _validate_contract_diagnosis_neutral(contract)
    oracle_parts: list[bytes] = []
    for relative in pair.oracle_files:
        oracle_left = _object_bytes(root, pair.variants[0].visible_root, relative)
        oracle_right = _object_bytes(root, pair.variants[1].visible_root, relative)
        if oracle_left != oracle_right:
            raise PhysicsBenchmarkBlindnessIntegrityError(
                "paired oracle programs or definitions are not byte-identical"
            )
        validate_generic_raw_oracle_program(oracle_left)
        oracle_parts.append(relative.encode("ascii") + b"\0" + oracle_left)
    evidence_schema = _paired_observation_schema(
        root,
        pair.variants[0].visible_root,
        pair.variants[1].visible_root,
        pair.raw_observation_files,
    )
    for manifest, variant in zip(manifests, pair.variants, strict=True):
        visible_files = {
            item.path: _object_bytes(root, variant.visible_root, item.path)
            for item in manifest.objects
        }
        _scan_visible_namespace(visible_files)
        visible_blob = b"\n".join(visible_files.values()).lower()
        if variant.diagnosis.encode("utf-8").lower() in visible_blob:
            raise PhysicsBenchmarkBlindnessIntegrityError(
                "seeded diagnosis entered auditor-visible bytes"
            )
    return PairedVisibleManifestV1(
        case_id=pair.case_id,
        pair_id=pair.pair_id,
        variants=cast(Any, manifests),
        contract_sha256=hashlib.sha256(contract_left).hexdigest(),
        oracle_definition_sha256=hashlib.sha256(b"".join(oracle_parts)).hexdigest(),
        evidence_schema_sha256=hashlib.sha256(canonical_json(evidence_schema)).hexdigest(),
    )


def build_gl_visible_manifest(
    task: GLFixtureAuthorityV1,
    *,
    repository_root: Path,
    source_repository_root: Path,
    source_commit: str,
) -> AuditorVisibleManifestV1:
    """Bind visible GL templates plus exact source blobs without running a pilot."""
    root = _canonical_directory(repository_root, "repository root")
    source = _canonical_directory(source_repository_root, "GL source repository")
    template_root = _resolve_below(root, task.visible_root, kind="directory")
    template = _manifest_for_directory(
        subject_id=task.task_id,
        pair_id=None,
        variant_id=None,
        directory=template_root,
    )
    objects = list(template.objects)
    for blob in task.source_blobs:
        content = read_exact_git_blob(source, source_commit, blob.path)
        if len(content) != blob.byte_length or hashlib.sha256(content).hexdigest() != blob.sha256:
            raise PhysicsBenchmarkBlindnessIntegrityError("GL source blob authority changed")
        objects.append(
            AuditorVisibleObjectV1(
                path=f"source/{blob.path}",
                role="source",
                byte_length=len(content),
                sha256=blob.sha256,
            )
        )
    _validate_neutral_title((template_root / task.title_file).read_bytes())
    contract = load_physics_task_contract(template_root / task.contract_file)
    _validate_contract_diagnosis_neutral(contract)
    for relative in task.oracle_files:
        validate_generic_raw_oracle_program((template_root / relative).read_bytes())
    template_files = {
        item.path: (template_root / item.path).read_bytes() for item in template.objects
    }
    _scan_visible_namespace(template_files)
    if (
        task.expected_interpretation.encode("utf-8").lower()
        in b"\n".join(template_files.values()).lower()
    ):
        raise PhysicsBenchmarkBlindnessIntegrityError(
            "expected GL interpretation entered auditor-visible bytes"
        )
    return AuditorVisibleManifestV1(
        subject_id=task.task_id,
        pair_id=None,
        variant_id=None,
        objects=tuple(objects),
    )


def qualify_fixture_authority(
    catalog_path: Path,
    *,
    repository_root: Path,
    source_repository_root: Path,
) -> FixtureQualificationV1:
    """Review every synthetic and GL fixture and require detached exact-byte approvals."""
    catalog = load_blind_fixture_catalog(catalog_path)
    root = _canonical_directory(repository_root, "repository root")
    scorer_root = _resolve_below(root, catalog.scorer_only_root, kind="directory")
    visible_root = _resolve_below(root, catalog.auditor_visible_root, kind="directory")
    _validate_root_separation(visible_root, scorer_root)
    expected_catalog = _resolve_below(scorer_root, "catalog.json", kind="file")
    if expected_catalog != catalog_path.resolve(strict=True):
        raise PhysicsBenchmarkBlindnessInputError("catalog is not in the exact scorer-only root")
    pair_manifests = tuple(
        build_paired_visible_manifest(item, repository_root=root) for item in catalog.pairs
    )
    gl_manifests = tuple(
        build_gl_visible_manifest(
            item,
            repository_root=root,
            source_repository_root=source_repository_root,
            source_commit=catalog.gl_source_commit,
        )
        for item in catalog.gl_tasks
    )
    manifest_by_subject: dict[str, BlindCanonicalModel] = {
        **{item.case_id: item for item in pair_manifests},
        **{item.subject_id: item for item in gl_manifests},
    }
    approved: list[str] = []
    subjects: Sequence[BlindPairAuthorityV1 | GLFixtureAuthorityV1] = (
        *catalog.pairs,
        *catalog.gl_tasks,
    )
    for subject in subjects:
        subject_id = (
            subject.case_id if isinstance(subject, BlindPairAuthorityV1) else subject.task_id
        )
        try:
            receipt_path = _resolve_below(root, subject.receipt_path, kind="file")
        except PhysicsBenchmarkBlindnessInputError as exc:
            raise PhysicsBenchmarkBlindnessInputError(
                "human-review receipt is unavailable"
            ) from exc
        if not receipt_path.is_relative_to(scorer_root):
            raise PhysicsBenchmarkBlindnessInputError("review receipt is not scorer-only")
        receipt = load_human_review_receipt(receipt_path)
        expected_manifest = manifest_by_subject[subject_id]
        _validate_receipt(
            receipt,
            catalog,
            subject_id,
            expected_manifest.canonical_sha256(),
            subject.canonical_sha256(),
        )
        approved.append(subject_id)
    return FixtureQualificationV1(
        catalog_sha256=catalog.canonical_sha256(),
        scorer_root_manifest_sha256=_directory_manifest_sha256(scorer_root),
        pair_manifests=pair_manifests,
        gl_manifests=gl_manifests,
        approved_subject_ids=tuple(approved),
    )


def build_fixture_review_packet(
    catalog: PhysicsBlindFixtureCatalogV1,
    *,
    repository_root: Path,
    source_repository_root: Path,
) -> FixtureReviewPacketV1:
    """Build the exact unapproved packet a human reviewer must decide."""
    pair_manifests = {
        item.case_id: build_paired_visible_manifest(item, repository_root=repository_root)
        for item in catalog.pairs
    }
    gl_manifests = {
        item.task_id: build_gl_visible_manifest(
            item,
            repository_root=repository_root,
            source_repository_root=source_repository_root,
            source_commit=catalog.gl_source_commit,
        )
        for item in catalog.gl_tasks
    }
    subjects = tuple(
        [
            FixtureReviewSubjectV1(
                subject_id=item.case_id,
                visible_manifest_sha256=pair_manifests[item.case_id].canonical_sha256(),
                scorer_authority_sha256=item.canonical_sha256(),
            )
            for item in catalog.pairs
        ]
        + [
            FixtureReviewSubjectV1(
                subject_id=item.task_id,
                visible_manifest_sha256=gl_manifests[item.task_id].canonical_sha256(),
                scorer_authority_sha256=item.canonical_sha256(),
            )
            for item in catalog.gl_tasks
        ]
    )
    body = {
        "approval_present": False,
        "catalog_sha256": catalog.canonical_sha256(),
        "gl_pilot_launched": False,
        "gl_source_commit": catalog.gl_source_commit,
        "model_launched": False,
        "schema_version": 1,
        "subjects": [item.model_dump(mode="json") for item in subjects],
    }
    return FixtureReviewPacketV1(
        catalog_sha256=catalog.canonical_sha256(),
        gl_source_commit=catalog.gl_source_commit,
        subjects=subjects,
        review_packet_sha256=hashlib.sha256(canonical_json(body)).hexdigest(),
    )


def issue_blindness_certificate(
    *,
    catalog: PhysicsBlindFixtureCatalogV1,
    pair: BlindPairAuthorityV1,
    variant_id: str,
    repository_root: Path,
    projection_manifest: PhysicsAuditorProjectionManifestV1,
    projection_root: Path,
    prompt: bytes,
    runtime_home: Path,
) -> BlindnessCertificateV1:
    """Complete every blindness check before any model-launch attempt."""
    root = _canonical_directory(repository_root, "repository root")
    scorer_root = _resolve_below(root, catalog.scorer_only_root, kind="directory")
    visible_root = _resolve_below(root, catalog.auditor_visible_root, kind="directory")
    _validate_root_separation(visible_root, scorer_root)
    variants = tuple(item for item in pair.variants if item.variant_id == variant_id)
    if len(variants) != 1:
        raise PhysicsBenchmarkBlindnessInputError("neutral pair variant is unavailable")
    variant = variants[0]
    paired_manifest = build_paired_visible_manifest(pair, repository_root=root)
    visible_manifest = next(
        item for item in paired_manifest.variants if item.variant_id == variant.variant_id
    )
    receipt = load_human_review_receipt(_resolve_below(root, pair.receipt_path, kind="file"))
    _validate_receipt(
        receipt,
        catalog,
        pair.case_id,
        paired_manifest.canonical_sha256(),
        pair.canonical_sha256(),
    )
    verify_physics_auditor_projection(projection_manifest, projection_root)
    projected_bytes = _regular_tree_bytes(projection_root)
    _scan_visible_namespace(projected_bytes)
    _scan_visible_namespace({"prompt.txt": prompt})
    _verify_projection_is_visible_only(
        projection_manifest,
        projected_bytes,
        visible_manifest,
        excluded_oracles=set(pair.oracle_files),
    )
    runtime = _canonical_directory(runtime_home, "PA-3 runtime home")
    try:
        if any(os.scandir(runtime)):
            raise PhysicsBenchmarkBlindnessIntegrityError(
                "PA-3 runtime home is not empty at blindness certification"
            )
    except OSError as exc:
        raise PhysicsBenchmarkBlindnessIntegrityError("PA-3 runtime home is unavailable") from exc
    scorer_manifest_sha256 = _directory_manifest_sha256(scorer_root)
    exclusion = canonical_json(
        {
            "bubblewrap_policy_sha256": PHYSICS_AUDITOR_BUBBLEWRAP_POLICY_V1.canonical_sha256(),
            "projection_manifest_sha256": projection_manifest.canonical_sha256(),
            "scorer_root": catalog.scorer_only_root,
            "scorer_root_manifest_sha256": scorer_manifest_sha256,
            "source_workspace_mount": "absent",
        }
    )
    neutral = canonical_json(
        {
            "case_id": pair.case_id,
            "pair_id": pair.pair_id,
            "variant_id": variant.variant_id,
        }
    )
    return BlindnessCertificateV1(
        certificate_id=f"blindness_{pair.case_id}_{variant.variant_id}",
        case_id=pair.case_id,
        pair_id=pair.pair_id,
        variant_id=cast(Any, variant.variant_id),
        auditor_visible_manifest_sha256=visible_manifest.canonical_sha256(),
        paired_visible_manifest_sha256=paired_manifest.canonical_sha256(),
        pa3_projection_manifest_sha256=projection_manifest.canonical_sha256(),
        scorer_root_manifest_sha256=scorer_manifest_sha256,
        scorer_root_exclusion_sha256=hashlib.sha256(exclusion).hexdigest(),
        paired_contract_sha256=paired_manifest.contract_sha256,
        paired_oracle_sha256=paired_manifest.oracle_definition_sha256,
        paired_evidence_schema_sha256=paired_manifest.evidence_schema_sha256,
        neutral_identifiers_sha256=hashlib.sha256(neutral).hexdigest(),
        review_receipt_sha256=receipt.receipt_sha256,
        reviewed_visible_manifest_sha256=receipt.reviewed_visible_manifest_sha256,
        reviewed_scorer_authority_sha256=receipt.reviewed_scorer_authority_sha256,
        bubblewrap_policy_sha256=PHYSICS_AUDITOR_BUBBLEWRAP_POLICY_V1.canonical_sha256(),
        runtime_home_empty_sha256=hashlib.sha256(b"").hexdigest(),
    )


def persist_blindness_certificate(certificate: BlindnessCertificateV1, path: Path) -> None:
    """Persist a certificate once; replacement is forbidden."""
    if ".." in path.parts or not path.name:
        raise PhysicsBenchmarkBlindnessInputError("certificate path is unsafe")
    try:
        parent = path.parent.resolve(strict=True)
        destination = parent / path.name
        descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
            0o400,
        )
        try:
            content = certificate.to_canonical_json()
            offset = 0
            while offset < len(content):
                offset += os.write(descriptor, content[offset:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        directory_descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except FileExistsError as exc:
        raise PhysicsBenchmarkBlindnessIntegrityError(
            "blindness certificate is immutable and already exists"
        ) from exc
    except OSError as exc:
        raise PhysicsBenchmarkBlindnessIntegrityError(
            "blindness certificate could not be persisted"
        ) from exc


def verify_scorer_root_excluded_from_bubblewrap_command(
    command: Sequence[str],
    *,
    scorer_root: Path,
) -> None:
    """Verify the real PA-3 Bubblewrap argv cannot expose scorer authority or host proc."""
    scorer = _canonical_directory(scorer_root, "scorer-only root")
    if "--unshare-pid" not in command or "--proc" not in command:
        raise PhysicsBenchmarkBlindnessIntegrityError(
            "PA-3 namespace lacks a private PID/proc boundary"
        )
    proc_indexes = [index for index, item in enumerate(command) if item == "--proc"]
    if (
        len(proc_indexes) != 1
        or proc_indexes[0] + 1 >= len(command)
        or command[proc_indexes[0] + 1] != "/proc"
    ):
        raise PhysicsBenchmarkBlindnessIntegrityError("PA-3 proc namespace is ambiguous")
    mount_options = {"--bind", "--ro-bind", "--dev-bind"}
    for index, item in enumerate(command):
        if item not in mount_options:
            continue
        if index + 2 >= len(command):
            raise PhysicsBenchmarkBlindnessIntegrityError("Bubblewrap mount argv is truncated")
        source_text, destination = command[index + 1 : index + 3]
        if destination == "/proc":
            raise PhysicsBenchmarkBlindnessIntegrityError("host proc was mounted into PA-3")
        source = Path(source_text)
        try:
            resolved = source.resolve(strict=False)
        except (OSError, RuntimeError, ValueError) as exc:
            raise PhysicsBenchmarkBlindnessIntegrityError(
                "Bubblewrap mount source is unsafe"
            ) from exc
        if _paths_overlap(resolved, scorer):
            raise PhysicsBenchmarkBlindnessIntegrityError(
                "real PA-3 namespace mounts scorer-only authority"
            )
    if any(str(scorer) == item or item.startswith(f"{scorer}/") for item in command):
        raise PhysicsBenchmarkBlindnessIntegrityError(
            "real PA-3 namespace argv contains the scorer-only root"
        )


def validate_generic_raw_oracle_program(source: bytes) -> None:
    """Reject identity-conditioned or classifying oracle source."""
    try:
        text = source.decode("utf-8")
        tree = ast.parse(text)
    except (UnicodeDecodeError, SyntaxError) as exc:
        raise PhysicsBenchmarkBlindnessInputError("generic oracle source is malformed") from exc
    lowered = text.casefold()
    if re.search(r"(?:case|task)[_-]?(?:id|[0-9]{3})", lowered):
        raise PhysicsBenchmarkBlindnessIntegrityError(
            "generic oracle depends on a case or task identity"
        )
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id.casefold() in {
            "case_id",
            "task_id",
            "expected_route",
            "seeded_diagnosis",
        }:
            raise PhysicsBenchmarkBlindnessIntegrityError(
                "generic oracle depends on scorer identity or authority"
            )
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            value = node.value.casefold()
            if _IDENTITY_LITERAL.fullmatch(value) or value in _CLASSIFICATION_OUTPUT_KEYS:
                raise PhysicsBenchmarkBlindnessIntegrityError(
                    "generic oracle embeds identity-dependent or classifying behavior"
                )
        if isinstance(node, ast.Compare):
            names = {item.id.casefold() for item in ast.walk(node) if isinstance(item, ast.Name)}
            if names & {"case", "case_id", "task", "task_id", "oracle_id"}:
                raise PhysicsBenchmarkBlindnessIntegrityError(
                    "generic oracle classifies by invocation identity"
                )
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Attribute)
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id == "sys"
            and node.value.attr == "argv"
            and not (isinstance(node.slice, ast.Constant) and node.slice.value == 1)
        ):
            raise PhysicsBenchmarkBlindnessIntegrityError(
                "generic oracle accepts invocation identity arguments"
            )
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "os"
            and node.attr == "environ"
        ):
            raise PhysicsBenchmarkBlindnessIntegrityError(
                "generic oracle depends on environment identity"
            )


def parse_raw_oracle_output(raw: bytes) -> RawOracleOutputV1:
    """Accept only raw scalar measurements and reject semantic classifications."""
    try:
        value: Any = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PhysicsBenchmarkBlindnessInputError("raw oracle output is malformed") from exc
    _reject_classification_keys(value)
    try:
        return RawOracleOutputV1.model_validate(value)
    except ValidationError as exc:
        raise PhysicsBenchmarkBlindnessInputError("raw oracle output schema is invalid") from exc


def read_exact_git_blob(repository: Path, commit: str, relative: str) -> bytes:
    """Read one exact committed blob without checkout, hooks, or model execution."""
    source = _canonical_directory(repository, "GL source repository")
    relative = _relative_path(relative)
    try:
        completed = subprocess.run(
            ("/usr/bin/git", "-C", source, "show", f"{commit}:{relative}"),
            capture_output=True,
            check=False,
            timeout=60,
            env={
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_OPTIONAL_LOCKS": "0",
                "GIT_TERMINAL_PROMPT": "0",
                "HOME": "/nonexistent",
                "LC_ALL": "C",
                "PATH": "/usr/bin:/bin",
            },
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PhysicsBenchmarkBlindnessInputError("exact GL source blob is unavailable") from exc
    if completed.returncode != 0 or len(completed.stdout) > MAX_BLIND_FILE_BYTES:
        raise PhysicsBenchmarkBlindnessInputError("exact GL source blob is unavailable")
    return completed.stdout


def prepare_exact_gl_fixture(
    task: GLFixtureAuthorityV1,
    *,
    repository_root: Path,
    source_repository_root: Path,
    source_commit: str,
    destination: Path,
) -> AuditorVisibleManifestV1:
    """Extract and verify exact GL bytes only; never run PA-2, PA-3, or a GL pilot."""
    root = _canonical_directory(repository_root, "repository root")
    source = _canonical_directory(source_repository_root, "GL source repository")
    template_root = _resolve_below(root, task.visible_root, kind="directory")
    if ".." in destination.parts or not destination.name:
        raise PhysicsBenchmarkBlindnessInputError("GL preparation destination is unsafe")
    try:
        parent = destination.parent.resolve(strict=True)
        prepared = parent / destination.name
        if prepared.exists():
            raise PhysicsBenchmarkBlindnessInputError("GL preparation destination must be new")
        prepared.mkdir(mode=0o700)
        for entry in sorted(os.scandir(template_root), key=lambda item: item.name):
            status = entry.stat(follow_symlinks=False)
            if not stat.S_ISREG(status.st_mode):
                raise PhysicsBenchmarkBlindnessInputError(
                    "GL visible template is not flat and regular"
                )
            content = Path(entry.path).read_bytes()
            (prepared / entry.name).write_bytes(content)
        for blob in task.source_blobs:
            content = read_exact_git_blob(source, source_commit, blob.path)
            if (
                len(content) != blob.byte_length
                or hashlib.sha256(content).hexdigest() != blob.sha256
            ):
                raise PhysicsBenchmarkBlindnessIntegrityError("GL source blob authority changed")
            target = prepared / "source" / blob.path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
    except (PhysicsBenchmarkBlindnessInputError, PhysicsBenchmarkBlindnessIntegrityError):
        raise
    except OSError as exc:
        raise PhysicsBenchmarkBlindnessIntegrityError("GL fixture could not be prepared") from exc
    observed = _manifest_for_directory(
        subject_id=task.task_id,
        pair_id=None,
        variant_id=None,
        directory=prepared,
    )
    expected = build_gl_visible_manifest(
        task,
        repository_root=root,
        source_repository_root=source,
        source_commit=source_commit,
    )
    if observed != expected:
        raise PhysicsBenchmarkBlindnessIntegrityError("prepared GL visible manifest changed")
    return observed


def _load_json_model(path: Path, model: type[BaseModel], label: str) -> Any:
    try:
        status = path.lstat()
        raw = path.read_bytes()
    except OSError as exc:
        raise PhysicsBenchmarkBlindnessInputError(f"{label} is unavailable") from exc
    if not stat.S_ISREG(status.st_mode) or path.is_symlink() or len(raw) > MAX_BLIND_CATALOG_BYTES:
        raise PhysicsBenchmarkBlindnessInputError(f"{label} is unsafe or oversized")
    try:
        value: Any = json.loads(raw)
        return model.model_validate(value)
    except (UnicodeDecodeError, json.JSONDecodeError, ValidationError) as exc:
        raise PhysicsBenchmarkBlindnessInputError(f"{label} is malformed") from exc


def _manifest_for_directory(
    *,
    subject_id: str,
    pair_id: str | None,
    variant_id: str | None,
    directory: Path,
) -> AuditorVisibleManifestV1:
    objects: list[AuditorVisibleObjectV1] = []
    seen_inodes: set[tuple[int, int]] = set()
    try:
        entries = sorted(directory.rglob("*"))
    except OSError as exc:
        raise PhysicsBenchmarkBlindnessInputError(
            "visible fixture could not be enumerated"
        ) from exc
    for path in entries:
        relative = path.relative_to(directory).as_posix()
        try:
            status = path.lstat()
        except OSError as exc:
            raise PhysicsBenchmarkBlindnessInputError(
                "visible fixture object is unavailable"
            ) from exc
        if stat.S_ISDIR(status.st_mode):
            continue
        if (
            not stat.S_ISREG(status.st_mode)
            or path.is_symlink()
            or status.st_nlink != 1
            or status.st_size > MAX_BLIND_FILE_BYTES
        ):
            raise PhysicsBenchmarkBlindnessInputError(
                "visible fixture requires single-link regular files only"
            )
        inode = (status.st_dev, status.st_ino)
        if inode in seen_inodes:
            raise PhysicsBenchmarkBlindnessInputError("visible fixture contains a hard-link alias")
        seen_inodes.add(inode)
        content = path.read_bytes()
        if len(content) != status.st_size:
            raise PhysicsBenchmarkBlindnessIntegrityError("visible fixture changed while read")
        objects.append(
            AuditorVisibleObjectV1(
                path=relative,
                role=_visible_role(relative),
                byte_length=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
            )
        )
    return AuditorVisibleManifestV1(
        subject_id=subject_id,
        pair_id=cast(Any, pair_id),
        variant_id=cast(Any, variant_id),
        objects=tuple(objects),
    )


def _visible_role(
    relative: str,
) -> Literal[
    "contract",
    "title",
    "evidence",
    "source",
    "raw_observation",
    "oracle_program",
]:
    name = PurePosixPath(relative).name
    if name == "contract.yaml":
        return "contract"
    if name == "title.txt":
        return "title"
    if name == "evidence.md":
        return "evidence"
    if name == "observations.json":
        return "raw_observation"
    if name.endswith("oracle.py"):
        return "oracle_program"
    return "source"


def _object_bytes(root: Path, visible_root: str, relative: str) -> bytes:
    directory = _resolve_below(root, visible_root, kind="directory")
    return _resolve_below(directory, relative, kind="file").read_bytes()


def _paired_observation_schema(
    root: Path,
    left_root: str,
    right_root: str,
    paths: Sequence[str],
) -> object:
    schemas: list[object] = []
    for relative in paths:
        try:
            left: Any = json.loads(_object_bytes(root, left_root, relative))
            right: Any = json.loads(_object_bytes(root, right_root, relative))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PhysicsBenchmarkBlindnessInputError("raw observation JSON is malformed") from exc
        left_output = parse_raw_oracle_output(canonical_json(left))
        right_output = parse_raw_oracle_output(canonical_json(right))
        left_schema = _raw_measurement_schema(left_output)
        right_schema = _raw_measurement_schema(right_output)
        if left_schema != right_schema:
            raise PhysicsBenchmarkBlindnessIntegrityError(
                "paired raw-observation evidence schemas differ"
            )
        schemas.append({"path": relative, "schema": left_schema})
    return schemas


def _raw_measurement_schema(output: RawOracleOutputV1) -> object:
    return [
        {
            "name": item.name,
            "uncertainty_type": "number_or_null",
            "unit": item.unit,
            "value_type": "number_or_null",
        }
        for item in output.measurements
    ]


def _validate_contract_diagnosis_neutral(contract: Any) -> None:
    texts = [
        *(item.value for item in contract.conventions),
        *(item.statement for item in contract.assumptions),
        *(item.statement for item in contract.required_identities),
        *(item.statement for item in contract.limiting_cases),
        *(item.description for item in contract.evidence),
        *(item.statement for item in contract.oracles),
        *(item.statement for item in contract.forbidden_claims),
    ]
    if any(_DIAGNOSTIC_CONTRACT_WORDS.search(item) for item in texts):
        raise PhysicsBenchmarkBlindnessIntegrityError(
            "visible contract embeds a semantic diagnosis or scorer label"
        )


def _scan_visible_namespace(files: Mapping[str, bytes]) -> None:
    for path, content in files.items():
        if any(
            part.casefold() in {"scorer_only", "authority", "review_receipts"}
            for part in PurePosixPath(path).parts
        ):
            raise PhysicsBenchmarkBlindnessIntegrityError(
                "scorer-only path entered the Auditor namespace"
            )
        lowered = content.lower()
        for key in _SCORER_ONLY_KEYS:
            if key.encode("ascii") in lowered:
                raise PhysicsBenchmarkBlindnessIntegrityError(
                    "scorer-only semantic authority entered auditor-visible bytes"
                )


def _validate_neutral_title(raw: bytes) -> None:
    try:
        title = raw.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise PhysicsBenchmarkBlindnessInputError("fixture title is not UTF-8") from exc
    if not re.fullmatch(r"(?:Benchmark|GL) fixture [0-9]{3}", title):
        raise PhysicsBenchmarkBlindnessIntegrityError("fixture title is not neutral")
    if _DIAGNOSTIC_TITLE_WORDS.search(title):
        raise PhysicsBenchmarkBlindnessIntegrityError("fixture title contains a semantic diagnosis")


def _reject_classification_keys(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or key.casefold() in _CLASSIFICATION_OUTPUT_KEYS:
                raise PhysicsBenchmarkBlindnessIntegrityError(
                    "oracle output contains a classification field"
                )
            _reject_classification_keys(item)
    elif isinstance(value, list):
        for item in value:
            _reject_classification_keys(item)
    elif isinstance(value, bool):
        raise PhysicsBenchmarkBlindnessIntegrityError(
            "oracle output contains a boolean classification"
        )


def _validate_receipt(
    receipt: HumanReviewReceiptV1,
    catalog: PhysicsBlindFixtureCatalogV1,
    subject_id: str,
    manifest_sha256: str,
    scorer_authority_sha256: str,
) -> None:
    if (
        receipt.subject_id != subject_id
        or receipt.fixture_author_ids != catalog.fixture_author_ids
        or receipt.reviewer_id in set(receipt.fixture_author_ids)
        or receipt.reviewed_visible_manifest_sha256 != manifest_sha256
        or receipt.reviewed_scorer_authority_sha256 != scorer_authority_sha256
        or receipt.decision != "approved"
    ):
        raise PhysicsBenchmarkBlindnessIntegrityError(
            "fixture lacks independent approval of the exact visible-byte manifest"
        )


def _verify_projection_is_visible_only(
    projection: PhysicsAuditorProjectionManifestV1,
    projected_bytes: Mapping[str, bytes],
    visible: AuditorVisibleManifestV1,
    *,
    excluded_oracles: set[str],
) -> None:
    allowed = {item.path: item for item in visible.objects if item.path not in excluded_oracles}
    observed_user: set[str] = set()
    for item in projection.objects:
        if item.kind != "regular" or item.path.startswith(f"{AUTHORITY_DIRECTORY}/"):
            continue
        expected = allowed.get(item.path)
        if (
            expected is None
            or expected.sha256 != item.sha256
            or expected.byte_length != item.byte_length
        ):
            raise PhysicsBenchmarkBlindnessIntegrityError(
                "PA-3 projection contains a file outside the auditor-visible manifest"
            )
        observed_user.add(item.path)
    required = {
        item.path for item in visible.objects if item.role not in {"contract", "oracle_program"}
    }
    if observed_user != required:
        raise PhysicsBenchmarkBlindnessIntegrityError(
            "PA-3 projection omits or substitutes auditor-visible files"
        )
    manifest_files = {item.path for item in projection.objects if item.kind == "regular"}
    if manifest_files != set(projected_bytes):
        raise PhysicsBenchmarkBlindnessIntegrityError("PA-3 projected byte set is contradictory")


def _regular_tree_bytes(root: Path) -> dict[str, bytes]:
    result: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        status = path.lstat()
        if stat.S_ISDIR(status.st_mode):
            continue
        if not stat.S_ISREG(status.st_mode) or path.is_symlink():
            raise PhysicsBenchmarkBlindnessIntegrityError(
                "PA-3 projection contains an unsafe object"
            )
        result[path.relative_to(root).as_posix()] = path.read_bytes()
    return result


def _directory_manifest_sha256(root: Path) -> str:
    objects: list[dict[str, object]] = []
    for path in sorted(root.rglob("*")):
        status = path.lstat()
        relative = path.relative_to(root).as_posix()
        if stat.S_ISDIR(status.st_mode):
            objects.append({"kind": "directory", "path": relative})
            continue
        if not stat.S_ISREG(status.st_mode) or path.is_symlink() or status.st_nlink != 1:
            raise PhysicsBenchmarkBlindnessInputError("authority root contains an unsafe object")
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
    if ".." in path.parts:
        raise PhysicsBenchmarkBlindnessInputError(f"{label} contains parent traversal")
    try:
        absolute = Path(os.path.abspath(path))
        resolved = path.resolve(strict=True)
        status = path.lstat()
    except (OSError, RuntimeError, ValueError) as exc:
        raise PhysicsBenchmarkBlindnessInputError(f"{label} is unavailable") from exc
    if absolute != resolved or path.is_symlink() or not stat.S_ISDIR(status.st_mode):
        raise PhysicsBenchmarkBlindnessInputError(f"{label} is not a canonical directory")
    if resolved == Path("/proc") or resolved.is_relative_to(Path("/proc")):
        raise PhysicsBenchmarkBlindnessInputError(f"{label} cannot use procfs")
    return resolved


def _resolve_below(root: Path, relative: str, *, kind: Literal["file", "directory"]) -> Path:
    relative = _relative_path(relative)
    candidate = root / relative
    try:
        resolved = candidate.resolve(strict=True)
        status = candidate.lstat()
    except (OSError, RuntimeError, ValueError) as exc:
        raise PhysicsBenchmarkBlindnessInputError("declared fixture path is unavailable") from exc
    if not resolved.is_relative_to(root) or resolved != candidate or candidate.is_symlink():
        raise PhysicsBenchmarkBlindnessInputError(
            "declared fixture path escapes its authority root"
        )
    if kind == "file" and not stat.S_ISREG(status.st_mode):
        raise PhysicsBenchmarkBlindnessInputError("declared fixture file is not regular")
    if kind == "directory" and not stat.S_ISDIR(status.st_mode):
        raise PhysicsBenchmarkBlindnessInputError("declared fixture directory is unavailable")
    return resolved


def _validate_root_separation(visible: Path, scorer: Path) -> None:
    if _paths_overlap(visible, scorer):
        raise PhysicsBenchmarkBlindnessInputError("auditor-visible and scorer-only roots overlap")
    for system_root in _SYSTEM_MOUNT_ROOTS:
        try:
            system = system_root.resolve(strict=False)
        except (OSError, RuntimeError):
            continue
        if _paths_overlap(scorer, system):
            raise PhysicsBenchmarkBlindnessInputError(
                "scorer-only root overlaps a PA-3 system-runtime mount"
            )


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left.is_relative_to(right) or right.is_relative_to(left)
