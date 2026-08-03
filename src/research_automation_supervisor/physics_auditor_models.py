"""Strict Codex-specific models for standalone Physics Auditor actions (PA-3)."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal, TypeAlias

import yaml  # type: ignore[import-untyped]
from pydantic import (
    AfterValidator,
    BeforeValidator,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from research_automation_supervisor.codex_models import (
    MAX_CODEX_TIMEOUT_SECONDS,
    MIN_CODEX_TIMEOUT_SECONDS,
    CodexRunResult,
    ModelName,
    ReasoningEffort,
    RunStatus,
)
from research_automation_supervisor.contract import (
    _format_validation_error,
    _UniqueKeySafeLoader,
)
from research_automation_supervisor.durable_state import ZERO_HASH, canonical_json
from research_automation_supervisor.errors import PhysicsAuditorInputError
from research_automation_supervisor.physics_models import (
    MAX_PHYSICS_ITEMS,
    PhysicsCanonicalModel,
    RelativePhysicsPath,
    SortedIdentifiers,
)
from research_automation_supervisor.physics_oracle_models import (
    OracleFailureReason,
    OracleStatus,
    PhysicsOracleArtifactV1,
    PhysicsOracleWorkspaceIdentityV1,
    Sha256,
)
from research_automation_supervisor.physics_routing import PhysicsRoutingDecisionV1
from research_automation_supervisor.workflow_models import Identifier, _freeze_sequence

MAX_PHYSICS_AUDITOR_CONFIG_BYTES = 2 * 1024 * 1024
MAX_PHYSICS_AUDITOR_OUTPUT_BYTES = 2 * 1024 * 1024
MAX_PHYSICS_AUDITOR_FILES = 1_000
MAX_PHYSICS_AUDITOR_STREAM_BYTES = 100 * 1024 * 1024
PHYSICS_AUDITOR_PROMPT_TEMPLATE_VERSION = "physics_auditor_prompt_v1"
PHYSICS_AUDITOR_OUTPUT_SCHEMA_ID = "physics_audit_report_v1"

PhysicsAuditorPhase: TypeAlias = Literal[
    "action_accepted",
    "evidence_verified",
    "prompt_finalized",
    "model_launch_attempted",
    "model_running",
    "model_exit_observed",
    "output_captured",
    "report_validated",
    "workspace_rechecked",
    "routing_completed",
    "action_proof_finalized",
]
PhysicsAuditorStatus: TypeAlias = Literal[
    "routing_completed",
    "report_invalid",
    "workspace_integrity_failure",
    "evidence_integrity_failure",
    "infrastructure_failure",
    "indeterminate_recovery",
]
PhysicsAuditorFailureReason: TypeAlias = Literal[
    "none",
    "evidence_missing_or_invalid",
    "model_launch_failed",
    "model_timed_out",
    "model_output_limit_exceeded",
    "model_process_failed",
    "oracle_execution_attempted",
    "invalid_structured_output",
    "workspace_changed",
    "recovery_ambiguous",
    "stale_process_identity",
    "reused_process_identity",
]


def _canonical_unique(value: tuple[str, ...]) -> tuple[str, ...]:
    if len(value) != len(set(value)):
        raise ValueError("values must be unique")
    return tuple(sorted(value))


def _canonical_paths(value: tuple[str, ...]) -> tuple[str, ...]:
    normalized = tuple(_relative_path(item) for item in value)
    return _canonical_unique(normalized)


def _relative_path(value: Any) -> str:
    if not isinstance(value, str) or value != value.strip() or "\\" in value or "\x00" in value:
        raise ValueError("paths must use canonical relative POSIX syntax")
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.as_posix() != value
        or ":" in path.parts[0]
    ):
        raise ValueError("paths must identify a location below the assigned root")
    return value


CanonicalPaths = Annotated[
    tuple[RelativePhysicsPath, ...],
    BeforeValidator(_freeze_sequence),
    AfterValidator(_canonical_paths),
    Field(max_length=MAX_PHYSICS_AUDITOR_FILES),
]


class PhysicsAuditorExecutableIdentityV1(PhysicsCanonicalModel):
    """Optional hash-pinned trusted Codex CLI selected by the operator."""

    path: str
    sha256: Sha256

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        path = PurePosixPath(value)
        if not path.is_absolute() or path.as_posix() != value or ".." in path.parts:
            raise ValueError("trusted Codex executable must be an exact absolute path")
        return value


class PhysicsAuditorExecutionConfigV1(PhysicsCanonicalModel):
    """Strict operator-owned Codex CLI policy for one standalone audit."""

    schema_version: Literal[1]
    backend: Literal["codex_cli"]
    model: ModelName
    reasoning_effort: ReasoningEffort
    timeout_seconds: Annotated[
        int, Field(ge=MIN_CODEX_TIMEOUT_SECONDS, le=MAX_CODEX_TIMEOUT_SECONDS)
    ]
    max_stdout_bytes: Annotated[int, Field(ge=1024, le=MAX_PHYSICS_AUDITOR_STREAM_BYTES)]
    max_stderr_bytes: Annotated[int, Field(ge=1024, le=MAX_PHYSICS_AUDITOR_STREAM_BYTES)]
    sandbox_policy: Literal["read_only"]
    approval_policy: Literal["never"]
    network_policy: Literal["disabled_by_codex_policy_not_kernel_enforced"]
    output_schema_id: Literal["physics_audit_report_v1"]
    prompt_template_version: Literal["physics_auditor_prompt_v1"]
    session_policy: Literal["fresh_ephemeral"]
    structured_output_policy: Literal["strict"]
    trusted_executable: PhysicsAuditorExecutableIdentityV1 | None = None
    environment_allowlist_profile: Literal["codex_cli_minimal_v1"]


class PhysicsAuditorCodexRolePolicyV1(PhysicsCanonicalModel):
    """Codex-specific semantic policy layered over the frozen adapter auditor role."""

    schema_version: Literal[1] = 1
    backend: Literal["codex_cli"] = "codex_cli"
    adapter_role: Literal["auditor"] = "auditor"
    sandbox: Literal["read-only"] = "read-only"
    approval: Literal["never"] = "never"
    ephemeral: Literal[True] = True
    resume_allowed: Literal[False] = False
    workspace_write_allowed: Literal[False] = False
    danger_full_access_allowed: Literal[False] = False
    oracle_execution_surface: Literal["none"] = "none"
    arbitrary_command_surface: Literal["none_added"] = "none_added"
    network_enforcement: Literal["codex_policy_disabled_not_kernel_enforced"] = (
        "codex_policy_disabled_not_kernel_enforced"
    )


PHYSICS_AUDITOR_CODEX_ROLE_POLICY_V1 = PhysicsAuditorCodexRolePolicyV1()


class PhysicsAuditorChangedPathManifestV1(PhysicsCanonicalModel):
    """Path-only candidate delta; it contains no patch or volatile workspace locator."""

    schema_version: Literal[1] = 1
    workspace_identity_sha256: Sha256
    paths: CanonicalPaths


class PhysicsAuditorWorkspaceFileV1(PhysicsCanonicalModel):
    """Bounded metadata for one declared or changed workspace path."""

    path: RelativePhysicsPath
    kind: Literal["regular", "symlink", "directory", "missing"]
    byte_length: Annotated[int, Field(ge=0)]
    mode: Annotated[int, Field(ge=0, le=0o7777)] | None
    sha256: Sha256 | None
    line_count: Annotated[int, Field(ge=0, le=10_000_000)] | None
    declared_evidence_ids: SortedIdentifiers = ()
    changed: bool

    @model_validator(mode="after")
    def validate_metadata(self) -> PhysicsAuditorWorkspaceFileV1:
        missing = self.kind == "missing"
        if missing and (self.mode is not None or self.sha256 is not None):
            raise ValueError("missing workspace-file metadata is contradictory")
        if not missing and (self.mode is None or self.sha256 is None):
            raise ValueError("missing workspace-file metadata is contradictory")
        if self.kind != "regular" and self.line_count is not None:
            raise ValueError("only regular files may declare a line count")
        return self


class PhysicsAuditorDeclaredEvidenceV1(PhysicsCanonicalModel):
    """One contract-declared non-oracle evidence authority."""

    id: Identifier
    kind: Literal["test", "artifact", "derivation", "document", "numerical"]
    path: RelativePhysicsPath | None
    required_for: SortedIdentifiers
    availability: Literal["declared", "present", "missing"]


class PhysicsAuditorOracleEvidenceV1(PhysicsCanonicalModel):
    """Safe summary of one verified PA-2 result, or explicit missing evidence."""

    oracle_id: Identifier
    required: bool
    availability: Literal["verified", "missing"]
    completion_proof_id: Identifier | None
    result_sha256: Sha256 | None
    completion_proof_sha256: Sha256 | None
    trusted_intent_sha256: Sha256 | None
    execution_policy_sha256: Sha256 | None
    workspace_identity_sha256: Sha256 | None
    status: OracleStatus | None
    failure_reason: OracleFailureReason | None
    declared_outcome: Literal["passed", "functional_failure"] | None
    structured_result_sha256: Sha256 | None
    artifacts: Annotated[
        tuple[PhysicsOracleArtifactV1, ...],
        BeforeValidator(_freeze_sequence),
        Field(max_length=100),
    ] = ()

    @model_validator(mode="after")
    def validate_availability(self) -> PhysicsAuditorOracleEvidenceV1:
        bound = (
            self.completion_proof_id,
            self.result_sha256,
            self.completion_proof_sha256,
            self.trusted_intent_sha256,
            self.execution_policy_sha256,
            self.workspace_identity_sha256,
            self.status,
            self.failure_reason,
        )
        if self.availability == "verified" and any(item is None for item in bound):
            raise ValueError("verified oracle evidence lacks its PA-2 authority")
        if self.availability == "missing" and (
            any(item is not None for item in bound)
            or self.declared_outcome is not None
            or self.structured_result_sha256 is not None
            or self.artifacts
        ):
            raise ValueError("missing oracle evidence unexpectedly claims PA-2 authority")
        return self


class PhysicsAuditorEvidenceIndexV1(PhysicsCanonicalModel):
    """Bounded authority index; raw oracle streams and source contents are excluded."""

    schema_version: Literal[1]
    contract_sha256: Sha256
    workspace_identity_sha256: Sha256
    changed_path_manifest_sha256: Sha256
    convention_ids: SortedIdentifiers
    assumption_ids: SortedIdentifiers
    required_identity_ids: SortedIdentifiers
    limiting_case_ids: SortedIdentifiers
    forbidden_claim_ids: SortedIdentifiers
    declared_evidence: Annotated[
        tuple[PhysicsAuditorDeclaredEvidenceV1, ...],
        BeforeValidator(_freeze_sequence),
        Field(max_length=MAX_PHYSICS_ITEMS),
    ]
    workspace_files: Annotated[
        tuple[PhysicsAuditorWorkspaceFileV1, ...],
        BeforeValidator(_freeze_sequence),
        Field(max_length=MAX_PHYSICS_AUDITOR_FILES),
    ]
    oracle_evidence: Annotated[
        tuple[PhysicsAuditorOracleEvidenceV1, ...],
        BeforeValidator(_freeze_sequence),
        Field(max_length=MAX_PHYSICS_ITEMS),
    ]
    raw_oracle_streams_embedded: Literal[False] = False
    protected_historical_material: Literal["excluded"] = "excluded"
    machine_temporary_paths_embedded: Literal[False] = False

    @field_validator("declared_evidence", "oracle_evidence")
    @classmethod
    def canonicalize_id_items(cls, value: tuple[Any, ...]) -> tuple[Any, ...]:
        key = "id" if value and hasattr(value[0], "id") else "oracle_id"
        items = tuple(sorted(value, key=lambda item: getattr(item, key)))
        ids = [getattr(item, key) for item in items]
        if len(ids) != len(set(ids)):
            raise ValueError("evidence-index IDs must be unique")
        return items

    @field_validator("workspace_files")
    @classmethod
    def canonicalize_files(
        cls, value: tuple[PhysicsAuditorWorkspaceFileV1, ...]
    ) -> tuple[PhysicsAuditorWorkspaceFileV1, ...]:
        items = tuple(sorted(value, key=lambda item: item.path))
        if len({item.path for item in items}) != len(items):
            raise ValueError("evidence-index workspace paths must be unique")
        return items


class PhysicsAuditorOracleProofBindingV1(PhysicsCanonicalModel):
    """One request-level binding to independently verified PA-2 evidence."""

    completion_proof_id: Identifier
    oracle_id: Identifier
    result_sha256: Sha256
    completion_proof_sha256: Sha256
    trusted_intent_sha256: Sha256
    execution_policy_sha256: Sha256


class PhysicsAuditorActionRequestV1(PhysicsCanonicalModel):
    """Internal semantic authority for one fresh standalone model action."""

    schema_version: Literal[1]
    action_id: Identifier
    task_id: Identifier
    physics_contract_sha256: Sha256
    execution_config_sha256: Sha256
    workspace_identity_sha256: Sha256
    changed_path_manifest_sha256: Sha256
    evidence_index_sha256: Sha256
    oracle_completion_proofs: Annotated[
        tuple[PhysicsAuditorOracleProofBindingV1, ...],
        BeforeValidator(_freeze_sequence),
        Field(max_length=MAX_PHYSICS_ITEMS),
    ]
    declared_derivation_paths: CanonicalPaths = ()
    declared_document_paths: CanonicalPaths = ()
    prompt_template_version: Literal["physics_auditor_prompt_v1"]
    prompt_template_sha256: Sha256
    output_schema_sha256: Sha256
    attempt_number: Annotated[int, Field(ge=1, le=1000)]
    output_directory_identity: Literal["standalone_physics_auditor_action_v1"]

    @field_validator("oracle_completion_proofs")
    @classmethod
    def canonicalize_proofs(
        cls, value: tuple[PhysicsAuditorOracleProofBindingV1, ...]
    ) -> tuple[PhysicsAuditorOracleProofBindingV1, ...]:
        items = tuple(sorted(value, key=lambda item: (item.oracle_id, item.completion_proof_id)))
        if len({item.oracle_id for item in items}) != len(items):
            raise ValueError("oracle proof bindings must have unique oracle IDs")
        if len({item.completion_proof_id for item in items}) != len(items):
            raise ValueError("oracle completion-proof IDs must be unique")
        return items


class PhysicsAuditorProcessIdentityV1(PhysicsCanonicalModel):
    """Operational Linux process identity; it is not included in the semantic proof."""

    pid: Annotated[int, Field(gt=0)]
    process_group_id: Annotated[int, Field(gt=0)]
    start_ticks: Annotated[int, Field(gt=0)]

    @model_validator(mode="after")
    def validate_group(self) -> PhysicsAuditorProcessIdentityV1:
        if self.pid != self.process_group_id:
            raise ValueError("Codex action process must own its process group")
        return self


class PhysicsAuditorProviderObservationV1(PhysicsCanonicalModel):
    """Operational Codex evidence persisted before semantic output capture."""

    schema_version: Literal[1]
    adapter_result: CodexRunResult
    codex_executable_sha256: Sha256
    codex_cli_version: Annotated[str, Field(min_length=1, max_length=256)] | None
    provider_session_id: Annotated[str, Field(min_length=1, max_length=512)] | None
    provider_thread_started_ids: Annotated[
        tuple[Annotated[str, Field(min_length=1, max_length=512)], ...],
        BeforeValidator(_freeze_sequence),
        Field(max_length=20),
    ]
    backend_policy_evidence_sha256: Sha256
    model_output_sha256: Sha256
    model_output_byte_length: Annotated[int, Field(ge=0)]
    model_output_truncated: bool
    oracle_execution_detected: bool


class PhysicsAuditorActionRecordV1(PhysicsCanonicalModel):
    """Immutable PA-3 hash-chain record, deliberately separate from workflow records."""

    schema_version: Literal[1]
    record_sha256: Sha256
    sequence: Annotated[int, Field(ge=1, le=100)]
    phase: PhysicsAuditorPhase
    previous_record_sha256: Sha256
    request: PhysicsAuditorActionRequestV1
    process_identity: PhysicsAuditorProcessIdentityV1 | None = None
    provider_status: RunStatus | None = None
    provider_session_id: Annotated[str, Field(min_length=1, max_length=512)] | None = None
    provider_thread_started_ids: Annotated[
        tuple[Annotated[str, Field(min_length=1, max_length=512)], ...],
        BeforeValidator(_freeze_sequence),
        Field(max_length=20),
    ] = ()
    backend_artifact_manifest_sha256: Sha256 | None = None
    model_output_sha256: Sha256 | None = None
    model_output_byte_length: Annotated[int, Field(ge=0)] | None = None
    parsed_report_sha256: Sha256 | None = None
    post_model_workspace_identity: PhysicsOracleWorkspaceIdentityV1 | None = None
    final_workspace_identity: PhysicsOracleWorkspaceIdentityV1 | None = None
    routing_decision_sha256: Sha256 | None = None
    result_sha256: Sha256 | None = None
    action_proof_sha256: Sha256 | None = None

    @model_validator(mode="after")
    def validate_record_hash(self) -> PhysicsAuditorActionRecordV1:
        payload = self.model_dump(mode="json", exclude={"record_sha256"})
        if self.record_sha256 != hashlib.sha256(canonical_json(payload)).hexdigest():
            raise ValueError("Physics Auditor action-record digest is invalid")
        if (self.model_output_sha256 is None) != (self.model_output_byte_length is None):
            raise ValueError("model-output digest and length must be recorded together")
        return self


class PhysicsAuditorActionProofV1(PhysicsCanonicalModel):
    """Codex-specific independently verifiable semantic completion proof."""

    schema_version: Literal[1]
    action_id: Identifier
    task_id: Identifier
    action_request_sha256: Sha256
    physics_contract_sha256: Sha256
    execution_config_sha256: Sha256
    backend: Literal["codex_cli"]
    backend_executable_sha256: Sha256 | None
    backend_version: Annotated[str, Field(min_length=1, max_length=256)] | None
    model: ModelName
    reasoning_effort: ReasoningEffort
    role_policy_sha256: Sha256
    prompt_template_version: Literal["physics_auditor_prompt_v1"]
    prompt_template_sha256: Sha256
    canonical_prompt_sha256: Sha256
    output_schema_sha256: Sha256
    initial_workspace_identity: PhysicsOracleWorkspaceIdentityV1
    evidence_index_sha256: Sha256
    changed_path_manifest_sha256: Sha256
    oracle_completion_proof_manifest_sha256: Sha256
    model_process_status: RunStatus | Literal["not_launched", "recovery_ambiguous"]
    backend_artifact_manifest_sha256: Sha256
    model_output_sha256: Sha256
    model_output_byte_length: Annotated[int, Field(ge=0)]
    oracle_execution_detected: bool
    parsed_report_sha256: Sha256 | None
    routing_decision_sha256: Sha256 | None
    post_model_workspace_identity: PhysicsOracleWorkspaceIdentityV1
    final_workspace_identity: PhysicsOracleWorkspaceIdentityV1
    integrity_verdict: Literal["unchanged", "changed"]
    action_status: PhysicsAuditorStatus

    @model_validator(mode="after")
    def validate_integrity(self) -> PhysicsAuditorActionProofV1:
        changed = (
            self.initial_workspace_identity != self.post_model_workspace_identity
            or self.initial_workspace_identity != self.final_workspace_identity
        )
        if changed != (self.integrity_verdict == "changed"):
            raise ValueError("Physics Auditor proof workspace verdict is contradictory")
        if changed and self.action_status != "workspace_integrity_failure":
            raise ValueError("workspace drift must override the Physics Auditor outcome")
        if self.action_status == "routing_completed" and (
            self.parsed_report_sha256 is None or self.routing_decision_sha256 is None
        ):
            raise ValueError("completed routing requires both report and decision hashes")
        if self.action_status != "routing_completed" and self.routing_decision_sha256 is not None:
            raise ValueError("non-routed proof unexpectedly binds a routing decision")
        if self.oracle_execution_detected and self.action_status != "infrastructure_failure":
            raise ValueError("oracle execution detection must fail the action")
        return self


class PhysicsAuditorActionResultV1(PhysicsCanonicalModel):
    """Bounded standalone outcome; no repair or workflow mutation is represented."""

    schema_version: Literal[1]
    request: PhysicsAuditorActionRequestV1
    status: PhysicsAuditorStatus
    failure_reason: PhysicsAuditorFailureReason
    model_process_completed: bool
    provider_status: RunStatus | None
    report_validated: bool
    oracle_execution_detected: bool
    routing_decision: PhysicsRoutingDecisionV1 | None
    model_output_sha256: Sha256
    model_output_byte_length: Annotated[int, Field(ge=0)]
    parsed_report_sha256: Sha256 | None
    initial_workspace_identity: PhysicsOracleWorkspaceIdentityV1
    post_model_workspace_identity: PhysicsOracleWorkspaceIdentityV1
    final_workspace_identity: PhysicsOracleWorkspaceIdentityV1
    integrity_verdict: Literal["unchanged", "changed"]
    action_proof_sha256: Sha256

    @model_validator(mode="after")
    def validate_result(self) -> PhysicsAuditorActionResultV1:
        changed = (
            self.initial_workspace_identity != self.post_model_workspace_identity
            or self.initial_workspace_identity != self.final_workspace_identity
        )
        if changed != (self.integrity_verdict == "changed"):
            raise ValueError("Physics Auditor result workspace verdict is contradictory")
        if changed and self.status != "workspace_integrity_failure":
            raise ValueError("workspace drift must override every Physics Auditor outcome")
        if self.status == "routing_completed":
            if (
                not self.report_validated
                or self.routing_decision is None
                or self.parsed_report_sha256 is None
            ):
                raise ValueError("routing completion requires a validated report and decision")
            if self.failure_reason != "none":
                raise ValueError("routing completion cannot claim a failure")
        elif self.routing_decision is not None:
            raise ValueError("non-routed action unexpectedly contains a routing decision")
        if self.oracle_execution_detected and (
            self.status != "infrastructure_failure"
            or self.failure_reason != "oracle_execution_attempted"
        ):
            raise ValueError("oracle execution detection must fail the action")
        if (
            not self.oracle_execution_detected
            and self.failure_reason == "oracle_execution_attempted"
        ):
            raise ValueError("oracle execution failure requires a detected command")
        return self


def load_physics_auditor_execution_config(
    path: Path,
) -> PhysicsAuditorExecutionConfigV1:
    """Load one bounded, unique-key, trusted PA-3 execution configuration."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise PhysicsAuditorInputError("could not read Physics Auditor execution config") from exc
    if len(raw) > MAX_PHYSICS_AUDITOR_CONFIG_BYTES:
        raise PhysicsAuditorInputError("Physics Auditor execution config exceeds its size limit")
    try:
        source = raw.decode("utf-8")
        value: Any = yaml.load(source, Loader=_UniqueKeySafeLoader)
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise PhysicsAuditorInputError("Physics Auditor execution config is malformed") from exc
    if not isinstance(value, dict):
        raise PhysicsAuditorInputError("Physics Auditor execution config root must be a mapping")
    try:
        return PhysicsAuditorExecutionConfigV1.model_validate(value)
    except ValidationError as exc:
        details = "; ".join(_format_validation_error(error) for error in exc.errors())
        raise PhysicsAuditorInputError(
            "Physics Auditor execution config validation failed: " + details
        ) from exc


def validate_trusted_codex_executable(
    identity: PhysicsAuditorExecutableIdentityV1,
) -> Path:
    """Verify an optional operator-pinned Codex executable without executing it."""
    path = Path(identity.path)
    try:
        absolute = Path(os.path.abspath(path))
        resolved = path.resolve(strict=True)
        path.lstat()
        digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
    except (OSError, RuntimeError, ValueError) as exc:
        raise PhysicsAuditorInputError("trusted Codex executable is unavailable") from exc
    if (
        absolute != resolved
        or path.is_symlink()
        or not resolved.is_file()
        or not os.access(resolved, os.X_OK)
    ):
        raise PhysicsAuditorInputError("trusted Codex executable identity is unsafe")
    if digest != identity.sha256:
        raise PhysicsAuditorInputError("trusted Codex executable hash does not match")
    return resolved


def zero_sha256() -> str:
    """Return the canonical empty SHA-256 used by non-launched action results."""
    return hashlib.sha256(b"").hexdigest()


assert zero_sha256() != ZERO_HASH
