"""Strict trusted models for model-free Physics Oracle execution v1."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path, PurePosixPath
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
from research_automation_supervisor.errors import PhysicsOracleInputError
from research_automation_supervisor.workflow_models import Identifier, _freeze_sequence

MAX_CATALOG_BYTES = 2 * 1024 * 1024
MAX_ORACLE_OUTPUT_BYTES = 16 * 1024 * 1024
MAX_ORACLE_TIMEOUT_SECONDS = 3600
MAX_ORACLE_ARTIFACT_BYTES = 64 * 1024 * 1024
MAX_ORACLE_ARTIFACTS = 100
MAX_ARGV_ITEMS = 128
MAX_ARG_BYTES = 4096

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
OracleStatus: TypeAlias = Literal[
    "passed",
    "functional_failure",
    "timed_out",
    "infrastructure_failure",
    "workspace_integrity_failure",
    "output_contract_failure",
    "cancelled",
    "indeterminate_recovery",
]
OracleFailureReason: TypeAlias = Literal[
    "none",
    "process_exit_not_accepted",
    "declared_functional_failure",
    "timeout",
    "network_isolation_unavailable",
    "executable_unavailable",
    "launch_failed",
    "output_limit_exceeded",
    "termination_unproven",
    "structured_output_missing",
    "structured_output_malformed",
    "structured_output_mismatch",
    "artifact_contract_failed",
    "workspace_changed",
    "recovery_ambiguous",
    "cancelled",
]
OraclePhase: TypeAlias = Literal[
    "intent_accepted",
    "execution_prepared",
    "process_launch_attempted",
    "process_running",
    "process_exit_observed",
    "output_captured",
    "workspace_rechecked",
    "completion_proof_finalized",
]


def _canonical_unique_strings(value: tuple[str, ...]) -> tuple[str, ...]:
    if len(value) != len(set(value)):
        raise ValueError("values must be unique")
    return tuple(sorted(value))


def _relative_path(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("paths must be strings")
    if value != value.strip() or "\\" in value or "\x00" in value:
        raise ValueError("paths must use canonical relative POSIX syntax")
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.as_posix() != value
        or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", value)
    ):
        raise ValueError("paths must identify a location below the assigned root")
    return value


RelativePath = Annotated[str, BeforeValidator(_relative_path)]


class OracleCanonicalModel(BaseModel):
    """Shared frozen strict model and qualified canonical representation."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        str_strip_whitespace=False,
    )

    def to_canonical_dict(self) -> dict[str, object]:
        return self.model_dump(mode="json")

    def to_canonical_json(self) -> bytes:
        return canonical_json(self.to_canonical_dict())

    def canonical_sha256(self) -> str:
        return hashlib.sha256(self.to_canonical_json()).hexdigest()


class PhysicsOracleEnvironmentProfileV1(OracleCanonicalModel):
    """One closed built-in environment; catalogs cannot inject variables."""

    schema_version: Literal[1]
    id: Identifier
    profile: Literal["minimal_python_v1"]


class PhysicsOracleExecutableV1(OracleCanonicalModel):
    """Exact trusted system-Python identity selected by the operator catalog."""

    schema_version: Literal[1]
    policy: Literal["isolated_system_python_v1"]
    path: str
    sha256: Sha256

    @field_validator("path")
    @classmethod
    def validate_executable_path(cls, value: str) -> str:
        path = PurePosixPath(value)
        if (
            not path.is_absolute()
            or path.as_posix() != value
            or ".." in path.parts
            or path.parent.as_posix() not in {"/usr/bin", "/bin"}
            or not path.name.startswith("python3")
        ):
            raise ValueError("v1 executable must be an exact system Python 3 path")
        return value


class PhysicsOracleTrustedProgramV1(OracleCanonicalModel):
    """Hash-pinned operator-owned Python entry point inside the workspace."""

    path: RelativePath
    sha256: Sha256


class PhysicsOracleArtifactDeclarationV1(OracleCanonicalModel):
    """One exact regular-file output permitted below the assigned scratch root."""

    id: Identifier
    path: RelativePath
    required: bool
    max_bytes: Annotated[int, Field(ge=1, le=MAX_ORACLE_ARTIFACT_BYTES)]

    @field_validator("id")
    @classmethod
    def reserve_undeclared_prefix(cls, value: str) -> str:
        if value.startswith("undeclared-"):
            raise ValueError("artifact IDs must not use the engine-reserved prefix")
        return value


class PhysicsOracleExecutionPolicyV1(OracleCanonicalModel):
    """Closed v1 isolation and resource policy; every field is non-negotiable."""

    schema_version: Literal[1]
    policy_id: Identifier
    isolation_backend: Literal["bubblewrap_unshare_all_v1"]
    working_directory: Literal["workspace_root"]
    workspace_access: Literal["read_only"]
    scratch_output: Literal["scratch_only"]
    network: Literal["disabled"]
    environment_profile_id: Identifier
    timeout_seconds: Annotated[int, Field(ge=1, le=MAX_ORACLE_TIMEOUT_SECONDS)]
    max_stdout_bytes: Annotated[int, Field(ge=1, le=MAX_ORACLE_OUTPUT_BYTES)]
    max_stderr_bytes: Annotated[int, Field(ge=1, le=MAX_ORACLE_OUTPUT_BYTES)]
    accepted_exit_codes: Annotated[
        tuple[Annotated[int, Field(ge=0, le=255)], ...],
        BeforeValidator(_freeze_sequence),
        AfterValidator(lambda value: tuple(sorted(set(value)))),
        Field(min_length=1, max_length=32),
    ]
    structured_output_schema: Literal["physics_oracle_result_v1", "none"]
    required_artifacts: Annotated[
        tuple[PhysicsOracleArtifactDeclarationV1, ...],
        BeforeValidator(_freeze_sequence),
        Field(max_length=MAX_ORACLE_ARTIFACTS),
    ] = ()

    @field_validator("required_artifacts")
    @classmethod
    def canonicalize_artifacts(
        cls, value: tuple[PhysicsOracleArtifactDeclarationV1, ...]
    ) -> tuple[PhysicsOracleArtifactDeclarationV1, ...]:
        ids = [item.id for item in value]
        paths = [item.path for item in value]
        if len(ids) != len(set(ids)) or len(paths) != len(set(paths)):
            raise ValueError("artifact IDs and normalized paths must be unique")
        if sum(item.max_bytes for item in value) > MAX_ORACLE_ARTIFACT_BYTES:
            raise ValueError("aggregate declared artifact bytes exceed the v1 bound")
        return tuple(sorted(value, key=lambda item: item.id))


class PhysicsOracleIntentV1(OracleCanonicalModel):
    """One fixed shell-free intent owned only by the trusted catalog."""

    schema_version: Literal[1]
    id: Identifier
    executable: PhysicsOracleExecutableV1
    program: PhysicsOracleTrustedProgramV1
    argv: Annotated[
        tuple[Annotated[str, Field(min_length=1, max_length=MAX_ARG_BYTES)], ...],
        BeforeValidator(_freeze_sequence),
        Field(min_length=5, max_length=MAX_ARGV_ITEMS),
    ]
    execution_policy: PhysicsOracleExecutionPolicyV1

    @model_validator(mode="after")
    def validate_fixed_python_intent(self) -> PhysicsOracleIntentV1:
        if self.argv[0] != self.executable.path:
            raise ValueError("argv[0] must exactly match the trusted executable")
        if self.argv[1:4] != ("-I", "-S", "-B"):
            raise ValueError("v1 Python intents require the fixed -I -S -B policy")
        if self.argv[4] != self.program.path:
            raise ValueError("argv program must exactly match the trusted program")
        if any(
            "\x00" in item or any(ord(character) < 32 for character in item) for item in self.argv
        ):
            raise ValueError("argv elements must not contain control characters")
        return self

    def execution_policy_sha256(self) -> str:
        return self.execution_policy.canonical_sha256()


class PhysicsOracleCatalogV1(OracleCanonicalModel):
    """Versioned operator-owned catalog of fixed executable intents."""

    schema_version: Literal[1]
    catalog_id: Identifier
    environment_profiles: Annotated[
        tuple[PhysicsOracleEnvironmentProfileV1, ...],
        BeforeValidator(_freeze_sequence),
        Field(min_length=1, max_length=20),
    ]
    intents: Annotated[
        tuple[PhysicsOracleIntentV1, ...],
        BeforeValidator(_freeze_sequence),
        Field(min_length=1, max_length=100),
    ]

    @model_validator(mode="after")
    def validate_catalog(self) -> PhysicsOracleCatalogV1:
        profile_ids = [item.id for item in self.environment_profiles]
        intent_ids = [item.id for item in self.intents]
        if len(profile_ids) != len(set(profile_ids)):
            raise ValueError("environment-profile IDs must be unique")
        if len(intent_ids) != len(set(intent_ids)):
            raise ValueError("oracle intent IDs must be unique")
        known_profiles = set(profile_ids)
        for intent in self.intents:
            if intent.execution_policy.environment_profile_id not in known_profiles:
                raise ValueError("intent references an unknown environment profile")
        object.__setattr__(
            self,
            "environment_profiles",
            tuple(sorted(self.environment_profiles, key=lambda item: item.id)),
        )
        object.__setattr__(
            self,
            "intents",
            tuple(sorted(self.intents, key=lambda item: item.id)),
        )
        return self

    def intent(self, oracle_id: str) -> PhysicsOracleIntentV1:
        matches = tuple(item for item in self.intents if item.id == oracle_id)
        if len(matches) != 1:
            raise PhysicsOracleInputError("oracle ID is absent from the trusted catalog")
        return matches[0]

    def environment_profile(self, profile_id: str) -> PhysicsOracleEnvironmentProfileV1:
        matches = tuple(item for item in self.environment_profiles if item.id == profile_id)
        if len(matches) != 1:
            raise PhysicsOracleInputError("oracle environment profile is unavailable")
        return matches[0]


class PhysicsOracleWorkspaceIdentityV1(OracleCanonicalModel):
    """Path-independent Git worktree, index, mode, symlink, and submodule identity."""

    schema_version: Literal[1]
    head_commit: Annotated[str, Field(pattern=r"^[0-9a-f]{40,64}$")]
    branch: str | None
    detached: bool
    object_format: Literal["sha1", "sha256"]
    index_manifest_sha256: Sha256
    index_file_sha256: Sha256
    tracked_diff_sha256: Sha256
    tracked_worktree_manifest_sha256: Sha256
    tracked_path_count: Annotated[int, Field(ge=0)]
    untracked_manifest_sha256: Sha256
    untracked_path_count: Annotated[int, Field(ge=0)]
    status_sha256: Sha256
    submodule_status_sha256: Sha256


class PhysicsOracleExecutionRequestV1(OracleCanonicalModel):
    """Internal hash-bound request derived from trusted inputs only."""

    schema_version: Literal[1]
    task_id: Identifier
    contract_sha256: Sha256
    oracle_id: Identifier
    trusted_intent_sha256: Sha256
    execution_policy_sha256: Sha256
    workspace_reference: Literal["workspace"]
    initial_workspace_identity: PhysicsOracleWorkspaceIdentityV1
    scratch_reference: Literal["scratch"]
    attempt_number: Annotated[int, Field(ge=1, le=1000)]
    action_id: Identifier


class PhysicsOracleNetworkEnforcementV1(OracleCanonicalModel):
    """Durable capability and policy identity for actual disabled networking."""

    schema_version: Literal[1]
    requested_policy: Literal["disabled"]
    backend: Literal["bubblewrap"]
    backend_policy: Literal["unshare_all_network_namespace_v1"]
    capability: Literal["enforced", "unavailable"]
    bubblewrap_version: str | None
    bubblewrap_sha256: Sha256 | None

    @model_validator(mode="after")
    def validate_capability(self) -> PhysicsOracleNetworkEnforcementV1:
        if self.capability == "enforced":
            if self.bubblewrap_version is None or self.bubblewrap_sha256 is None:
                raise ValueError("enforced network policy requires backend identity")
        elif self.bubblewrap_version is not None or self.bubblewrap_sha256 is not None:
            raise ValueError("unavailable capability must not claim backend identity")
        return self


class PhysicsOracleStreamDigestV1(OracleCanonicalModel):
    """Bounded raw-stream observation and separately stored diagnostic prefix."""

    observed_byte_length: Annotated[int, Field(ge=0)]
    observed_sha256: Sha256
    captured_prefix_byte_length: Annotated[int, Field(ge=0)]
    captured_prefix_sha256: Sha256
    truncated: bool

    @model_validator(mode="after")
    def validate_lengths(self) -> PhysicsOracleStreamDigestV1:
        if self.captured_prefix_byte_length > self.observed_byte_length:
            raise ValueError("captured prefix exceeds observed output")
        if self.truncated != (self.captured_prefix_byte_length < self.observed_byte_length):
            raise ValueError("stream truncation flag contradicts byte lengths")
        return self


class PhysicsOracleDeclaredCheckV1(OracleCanonicalModel):
    """One bounded process-declared boolean with no prose authority."""

    id: Identifier
    passed: bool


class PhysicsOracleDeclaredResultV1(OracleCanonicalModel):
    """The only structured stdout contract understood by PA-2."""

    schema_version: Literal[1]
    oracle_id: Identifier
    outcome: Literal["passed", "functional_failure"]
    checks: Annotated[
        tuple[PhysicsOracleDeclaredCheckV1, ...],
        BeforeValidator(_freeze_sequence),
        Field(max_length=200),
    ] = ()

    @field_validator("checks")
    @classmethod
    def canonicalize_checks(
        cls, value: tuple[PhysicsOracleDeclaredCheckV1, ...]
    ) -> tuple[PhysicsOracleDeclaredCheckV1, ...]:
        ids = [item.id for item in value]
        if len(ids) != len(set(ids)):
            raise ValueError("declared check IDs must be unique")
        return tuple(sorted(value, key=lambda item: item.id))

    @model_validator(mode="after")
    def validate_outcome(self) -> PhysicsOracleDeclaredResultV1:
        if self.outcome == "passed" and any(not item.passed for item in self.checks):
            raise ValueError("passing outcome contradicts a failed declared check")
        if (
            self.outcome == "functional_failure"
            and self.checks
            and all(item.passed for item in self.checks)
        ):
            raise ValueError("functional failure contradicts all declared checks")
        return self


class PhysicsOracleArtifactV1(OracleCanonicalModel):
    """Hash-bound scratch artifact metadata; the path is scratch-relative."""

    id: Identifier
    path: RelativePath
    declared: bool
    kind: Literal["regular", "symlink", "directory"]
    byte_length: Annotated[int, Field(ge=0, le=1024 * 1024 * 1024 * 1024)]
    mode: Annotated[int, Field(ge=0, le=0o7777)]
    sha256: Sha256


class PhysicsOracleExecutionResultV1(OracleCanonicalModel):
    """Bounded canonical semantic result; raw process output is never embedded."""

    schema_version: Literal[1]
    request: PhysicsOracleExecutionRequestV1
    status: OracleStatus
    failure_reason: OracleFailureReason
    process_exit_code: int | None
    timed_out: bool
    stdout: PhysicsOracleStreamDigestV1
    stderr: PhysicsOracleStreamDigestV1
    structured_output_status: Literal["not_required", "parsed", "malformed", "missing"]
    structured_result_sha256: Sha256 | None
    declared_outcome: Literal["passed", "functional_failure"] | None
    artifacts: Annotated[
        tuple[PhysicsOracleArtifactV1, ...],
        BeforeValidator(_freeze_sequence),
        Field(max_length=MAX_ORACLE_ARTIFACTS),
    ]
    artifact_manifest_sha256: Sha256
    initial_workspace_identity: PhysicsOracleWorkspaceIdentityV1
    final_workspace_identity: PhysicsOracleWorkspaceIdentityV1
    integrity_verdict: Literal["unchanged", "changed"]
    network_enforcement: PhysicsOracleNetworkEnforcementV1
    environment_profile_sha256: Sha256
    completion_proof_sha256: Sha256

    @model_validator(mode="after")
    def validate_result(self) -> PhysicsOracleExecutionResultV1:
        if self.initial_workspace_identity != self.request.initial_workspace_identity:
            raise ValueError("result initial identity contradicts its request")
        changed = self.initial_workspace_identity != self.final_workspace_identity
        if changed != (self.integrity_verdict == "changed"):
            raise ValueError("workspace identities contradict the integrity verdict")
        if changed and self.status != "workspace_integrity_failure":
            raise ValueError("workspace drift must override every process outcome")
        allowed_reasons: dict[OracleStatus, frozenset[OracleFailureReason]] = {
            "passed": frozenset({"none"}),
            "functional_failure": frozenset(
                {"process_exit_not_accepted", "declared_functional_failure"}
            ),
            "timed_out": frozenset({"timeout"}),
            "infrastructure_failure": frozenset(
                {
                    "network_isolation_unavailable",
                    "executable_unavailable",
                    "launch_failed",
                    "termination_unproven",
                }
            ),
            "workspace_integrity_failure": frozenset({"workspace_changed"}),
            "output_contract_failure": frozenset(
                {
                    "output_limit_exceeded",
                    "structured_output_missing",
                    "structured_output_malformed",
                    "structured_output_mismatch",
                    "artifact_contract_failed",
                }
            ),
            "cancelled": frozenset({"cancelled"}),
            "indeterminate_recovery": frozenset({"recovery_ambiguous"}),
        }
        if self.failure_reason not in allowed_reasons[self.status]:
            raise ValueError("oracle status contradicts its failure reason")
        if self.status == "passed" and (
            self.process_exit_code is None or self.process_exit_code < 0
        ):
            raise ValueError("passing process outcome lacks a successful exit code")
        if self.status == "passed" and (
            self.timed_out
            or self.integrity_verdict != "unchanged"
            or self.network_enforcement.capability != "enforced"
            or self.structured_output_status in {"malformed", "missing"}
            or self.declared_outcome == "functional_failure"
        ):
            raise ValueError("passing oracle result is contradictory")
        if self.status == "timed_out" and (not self.timed_out or self.failure_reason != "timeout"):
            raise ValueError("timed-out oracle result is contradictory")
        if self.timed_out and self.status not in {
            "timed_out",
            "infrastructure_failure",
            "workspace_integrity_failure",
        }:
            raise ValueError("timeout flag lacks a timeout-compatible status")
        if self.structured_output_status == "parsed":
            if self.structured_result_sha256 is None or self.declared_outcome is None:
                raise ValueError("parsed structured output lacks its digest or outcome")
        elif self.structured_result_sha256 is not None or self.declared_outcome is not None:
            raise ValueError("unparsed structured output claims semantic content")
        ids = [item.id for item in self.artifacts]
        if len(ids) != len(set(ids)) or tuple(sorted(ids)) != tuple(ids):
            raise ValueError("artifact records must be unique and ID-sorted")
        if (
            self.artifact_manifest_sha256
            != hashlib.sha256(
                canonical_json([item.model_dump(mode="json") for item in self.artifacts])
            ).hexdigest()
        ):
            raise ValueError("artifact manifest digest is invalid")
        return self


class PhysicsOracleCompletionProofV1(OracleCanonicalModel):
    """Canonical independently verifiable completion binding for one oracle attempt."""

    schema_version: Literal[1]
    task_id: Identifier
    physics_contract_sha256: Sha256
    trusted_oracle_id: Identifier
    trusted_intent_sha256: Sha256
    execution_policy_sha256: Sha256
    network_enforcement: PhysicsOracleNetworkEnforcementV1
    environment_profile_sha256: Sha256
    initial_workspace_identity: PhysicsOracleWorkspaceIdentityV1
    process_status: OracleStatus
    failure_reason: OracleFailureReason
    process_exit_code: int | None
    timed_out: bool
    stdout: PhysicsOracleStreamDigestV1
    stderr: PhysicsOracleStreamDigestV1
    structured_output_status: Literal["not_required", "parsed", "malformed", "missing"]
    structured_result_sha256: Sha256 | None
    declared_outcome: Literal["passed", "functional_failure"] | None
    artifact_manifest_sha256: Sha256
    final_workspace_identity: PhysicsOracleWorkspaceIdentityV1
    integrity_verdict: Literal["unchanged", "changed"]


class PhysicsOracleProcessIdentityV1(OracleCanonicalModel):
    """Operational Linux identity used only to reject ambiguous recovery."""

    pid: Annotated[int, Field(gt=0)]
    process_group_id: Annotated[int, Field(gt=0)]
    start_ticks: Annotated[int, Field(gt=0)]

    @model_validator(mode="after")
    def validate_group(self) -> PhysicsOracleProcessIdentityV1:
        if self.pid != self.process_group_id:
            raise ValueError("oracle process must own its process group")
        return self


class PhysicsOracleActionRecordV1(OracleCanonicalModel):
    """One immutable hash-chained PA-2 boundary record, separate from workflows."""

    schema_version: Literal[1]
    record_sha256: Sha256
    sequence: Annotated[int, Field(ge=1, le=100)]
    phase: OraclePhase
    previous_record_sha256: Sha256
    request: PhysicsOracleExecutionRequestV1
    network_enforcement: PhysicsOracleNetworkEnforcementV1 | None = None
    environment_profile_sha256: Sha256 | None = None
    process_identity: PhysicsOracleProcessIdentityV1 | None = None
    process_exit_code: int | None = None
    timed_out: bool | None = None
    stdout: PhysicsOracleStreamDigestV1 | None = None
    stderr: PhysicsOracleStreamDigestV1 | None = None
    execution_status: OracleStatus | None = None
    failure_reason: OracleFailureReason | None = None
    structured_output_status: Literal["not_required", "parsed", "malformed", "missing"] | None = (
        None
    )
    structured_result_sha256: Sha256 | None = None
    declared_outcome: Literal["passed", "functional_failure"] | None = None
    artifacts: Annotated[
        tuple[PhysicsOracleArtifactV1, ...],
        BeforeValidator(_freeze_sequence),
        Field(max_length=MAX_ORACLE_ARTIFACTS),
    ] = ()
    artifact_manifest_sha256: Sha256 | None = None
    final_workspace_identity: PhysicsOracleWorkspaceIdentityV1 | None = None
    result_sha256: Sha256 | None = None
    completion_proof_sha256: Sha256 | None = None

    @model_validator(mode="after")
    def validate_record_hash(self) -> PhysicsOracleActionRecordV1:
        payload = self.model_dump(mode="json", exclude={"record_sha256"})
        if self.record_sha256 != hashlib.sha256(canonical_json(payload)).hexdigest():
            raise ValueError("oracle action record digest is invalid")
        return self


def load_physics_oracle_catalog(path: Path) -> PhysicsOracleCatalogV1:
    """Read one bounded unique-key YAML/JSON trusted catalog."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise PhysicsOracleInputError("trusted oracle catalog could not be read") from exc
    if not raw or len(raw) > MAX_CATALOG_BYTES:
        raise PhysicsOracleInputError("trusted oracle catalog is empty or too large")
    try:
        text = raw.decode("utf-8")
        value = yaml.load(text, Loader=_UniqueKeySafeLoader)
        return PhysicsOracleCatalogV1.model_validate(value)
    except (UnicodeDecodeError, yaml.YAMLError, ValidationError, ValueError) as exc:
        detail = (
            "; ".join(_format_validation_error(error) for error in exc.errors())
            if isinstance(exc, ValidationError)
            else str(exc)
        )
        raise PhysicsOracleInputError(
            f"trusted oracle catalog is malformed or invalid: {detail}"
        ) from exc


def parse_physics_oracle_declared_result(
    raw: bytes, oracle_id: str
) -> PhysicsOracleDeclaredResultV1:
    """Parse exact bounded JSON stdout and close it over the selected oracle ID."""
    try:
        value = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
        result = PhysicsOracleDeclaredResultV1.model_validate(value)
    except (UnicodeDecodeError, json.JSONDecodeError, ValidationError, ValueError) as exc:
        raise PhysicsOracleInputError("structured oracle output is malformed") from exc
    if result.oracle_id != oracle_id:
        raise PhysicsOracleInputError("structured oracle output names a different oracle")
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")
