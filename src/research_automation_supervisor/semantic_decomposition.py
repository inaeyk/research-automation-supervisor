"""Strict semantic decomposition, compact handoffs, and exact telemetry models."""

from __future__ import annotations

import hashlib
import json
import os
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from contextlib import suppress
from pathlib import Path
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from research_automation_supervisor.context_economy import ContextEconomyReceiptV1
from research_automation_supervisor.token_accounting import CodexUsageReceiptV1

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
SubtaskRole = Literal[
    "worker",
    "coding_auditor",
    "physics_auditor",
    "repair",
    "supervisor",
    "custodian",
    "other",
]
SemanticBoundary = Literal[
    "independent_component",
    "investigation_to_implementation",
    "implementation_to_audit",
    "implementation_to_qualification",
    "independent_repair",
]
ContinuationReason = Literal[
    "qualified_recovery",
    "unrepresentable_working_context",
]
ArtifactKind = Literal[
    "authority",
    "handoff",
    "repository_state",
    "diff",
    "test_receipt",
    "evidence",
    "deliverable",
]
ValidationReuseReason = Literal[
    "unchanged_pass_receipt",
    "prior_result_not_pass",
    "test_code_changed",
    "relevant_source_changed",
    "config_changed",
    "environment_changed",
]

DEFAULT_HANDOFF_TARGET_TOKENS = 1_000
DEFAULT_HANDOFF_SOFT_MAX_TOKENS = 2_000
ABSOLUTE_HANDOFF_TOKEN_UPPER_BOUND = 8_000
DEFAULT_HANDOFF_TARGET_BYTES = 3_072
DEFAULT_HANDOFF_SOFT_MAX_BYTES = 4_096
ABSOLUTE_HANDOFF_BYTE_UPPER_BOUND = 8_192


def _freeze_sequence(value: object) -> object:
    return tuple(value) if isinstance(value, list) else value


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    ).encode("utf-8")


class AuthorityReferenceV1(BaseModel):
    """A stable authority path and the exact bytes it identifies."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, str_strip_whitespace=True)

    path: Annotated[str, Field(min_length=1, max_length=1_024)]
    sha256: Sha256


class ArtifactReferenceV1(BaseModel):
    """A durable predecessor, evidence, or deliverable reference."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, str_strip_whitespace=True)

    path: Annotated[str, Field(min_length=1, max_length=1_024)]
    sha256: Sha256
    kind: ArtifactKind


class PredecessorArtifactRequirementV1(BaseModel):
    """A known artifact or a future handoff named before its producer is launched."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, str_strip_whitespace=True)

    path: Annotated[str, Field(min_length=1, max_length=1_024)]
    kind: ArtifactKind
    sha256: Sha256 | None = None
    produced_by_subtask_id: (
        Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,127}$")] | None
    ) = None

    @model_validator(mode="after")
    def require_exact_or_planned_identity(self) -> PredecessorArtifactRequirementV1:
        if (self.sha256 is None) == (self.produced_by_subtask_id is None):
            raise ValueError(
                "predecessor requirement needs exactly one existing hash or producer subtask"
            )
        return self


class SemanticSubtaskV1(BaseModel):
    """One material unit whose completion is independently checkable."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, str_strip_whitespace=True)

    schema_version: Literal[1] = 1
    subtask_id: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,127}$")]
    sequence: Annotated[int, Field(ge=1)]
    role: SubtaskRole
    boundary: SemanticBoundary
    coherence_key: Annotated[str, Field(min_length=3, max_length=256)]
    objective: Annotated[str, Field(min_length=12, max_length=2_000)]
    scope: Annotated[
        tuple[str, ...], BeforeValidator(_freeze_sequence), Field(min_length=1, max_length=20)
    ]
    authority_references: Annotated[
        tuple[AuthorityReferenceV1, ...],
        BeforeValidator(_freeze_sequence),
        Field(min_length=1, max_length=20),
    ]
    required_predecessor_artifacts: Annotated[
        tuple[PredecessorArtifactRequirementV1, ...],
        BeforeValidator(_freeze_sequence),
        Field(max_length=40),
    ] = ()
    deliverables: Annotated[
        tuple[str, ...], BeforeValidator(_freeze_sequence), Field(min_length=1, max_length=20)
    ]
    completion_conditions: Annotated[
        tuple[str, ...], BeforeValidator(_freeze_sequence), Field(min_length=1, max_length=20)
    ]
    validation_requirements: Annotated[
        tuple[str, ...], BeforeValidator(_freeze_sequence), Field(min_length=1, max_length=20)
    ]
    stop_conditions: Annotated[
        tuple[str, ...], BeforeValidator(_freeze_sequence), Field(min_length=1, max_length=20)
    ]

    @field_validator(
        "scope",
        "deliverables",
        "completion_conditions",
        "validation_requirements",
        "stop_conditions",
    )
    @classmethod
    def reject_duplicates(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(item.casefold() for item in value)
        if len(set(normalized)) != len(normalized):
            raise ValueError("semantic subtask collections must not contain duplicates")
        return value

    @model_validator(mode="after")
    def reject_tiny_mechanical_objectives(self) -> SemanticSubtaskV1:
        objective = self.objective.casefold().strip(" .")
        mechanical_prefixes = (
            "grep for ",
            "search for one ",
            "read one file",
            "open one file",
            "edit one line",
            "change one line",
            "run one test",
            "run a single test",
        )
        if objective.startswith(mechanical_prefixes):
            raise ValueError("mechanical operation is not a semantic subtask boundary")
        return self


class SemanticTaskPlanV1(BaseModel):
    """A minimal material decomposition recorded before any subtask is launched."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, str_strip_whitespace=True)

    schema_version: Literal[1] = 1
    plan_id: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,127}$")]
    stage_id: Annotated[str, Field(min_length=1, max_length=256)]
    substantial_stage: bool
    decomposition_policy: Literal["smallest_material_context_reduction"] = (
        "smallest_material_context_reduction"
    )
    authority_references: Annotated[
        tuple[AuthorityReferenceV1, ...],
        BeforeValidator(_freeze_sequence),
        Field(min_length=1, max_length=20),
    ]
    subtasks: Annotated[
        tuple[SemanticSubtaskV1, ...],
        BeforeValidator(_freeze_sequence),
        Field(min_length=1, max_length=6),
    ]

    @model_validator(mode="after")
    def validate_decomposition(self) -> SemanticTaskPlanV1:
        if self.substantial_stage and not 2 <= len(self.subtasks) <= 6:
            raise ValueError("a substantial stage requires 2-6 semantic subtasks")
        if not self.substantial_stage and len(self.subtasks) != 1:
            raise ValueError("a non-substantial stage must remain one subtask")
        sequences = tuple(item.sequence for item in self.subtasks)
        if sequences != tuple(range(1, len(self.subtasks) + 1)):
            raise ValueError("subtask sequence must be contiguous and ordered from one")
        identifiers = tuple(item.subtask_id for item in self.subtasks)
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("subtask IDs must be unique")
        for subtask in self.subtasks:
            if any(
                reference not in self.authority_references
                for reference in subtask.authority_references
            ):
                raise ValueError("subtask authority must be included in the stage authority set")
        seen: set[str] = set()
        roles: dict[str, SubtaskRole] = {}
        for subtask in self.subtasks:
            for requirement in subtask.required_predecessor_artifacts:
                producer = requirement.produced_by_subtask_id
                if producer is not None and producer not in seen:
                    raise ValueError(
                        "planned predecessor must name an earlier subtask in the same plan"
                    )
            if subtask.role in {"coding_auditor", "physics_auditor"} and not any(
                requirement.kind == "handoff"
                and requirement.produced_by_subtask_id is not None
                and roles.get(requirement.produced_by_subtask_id) in {"worker", "repair"}
                for requirement in subtask.required_predecessor_artifacts
            ):
                raise ValueError("auditor subtask requires an earlier Worker or repair handoff")
            seen.add(subtask.subtask_id)
            roles[subtask.subtask_id] = subtask.role
        for previous, current in zip(self.subtasks, self.subtasks[1:], strict=False):
            if (
                previous.coherence_key == current.coherence_key
                and previous.role == current.role
                and previous.boundary == current.boundary
            ):
                raise ValueError(
                    "adjacent work with the same role, boundary, and coherence key is a "
                    "pathological micro-split"
                )
        return self


def write_semantic_task_plan(plan: SemanticTaskPlanV1, destination: Path) -> None:
    """Durably record a validated exact plan once, before any subtask launch."""
    raw = _canonical_json(plan.model_dump(mode="json"))
    destination.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(destination, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        with suppress(OSError):
            os.close(descriptor)
        raise


class RepositoryIdentityV1(BaseModel):
    """Exact candidate tree identity at a handoff boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, str_strip_whitespace=True)

    repository: Annotated[str, Field(min_length=1, max_length=1_024)]
    base_commit: Annotated[str, Field(pattern=r"^[0-9a-f]{40,64}$")]
    head_commit: Annotated[str, Field(pattern=r"^[0-9a-f]{40,64}$")]
    diff_sha256: Sha256
    dirty: bool


class AgentHandoffV1(BaseModel):
    """Compact durable memory transferred across a semantic boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, str_strip_whitespace=True)

    schema_version: Literal[1] = 1
    handoff_id: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,127}$")]
    completed_objective: Annotated[str, Field(min_length=1, max_length=2_000)]
    changed_paths: Annotated[
        tuple[str, ...], BeforeValidator(_freeze_sequence), Field(max_length=100)
    ]
    changed_interfaces: Annotated[
        tuple[str, ...], BeforeValidator(_freeze_sequence), Field(max_length=40)
    ]
    repository_identity: RepositoryIdentityV1
    authority_references: Annotated[
        tuple[AuthorityReferenceV1, ...],
        BeforeValidator(_freeze_sequence),
        Field(min_length=1, max_length=20),
    ]
    valid_evidence_receipts: Annotated[
        tuple[ArtifactReferenceV1, ...],
        BeforeValidator(_freeze_sequence),
        Field(max_length=40),
    ]
    decisions_and_invariants: Annotated[
        tuple[str, ...], BeforeValidator(_freeze_sequence), Field(max_length=40)
    ]
    unresolved_findings: Annotated[
        tuple[str, ...], BeforeValidator(_freeze_sequence), Field(max_length=40)
    ]
    remaining_work: Annotated[
        tuple[str, ...], BeforeValidator(_freeze_sequence), Field(max_length=40)
    ]
    next_subtask_requirements: Annotated[
        tuple[str, ...], BeforeValidator(_freeze_sequence), Field(min_length=1, max_length=40)
    ]
    do_not_rediscover_or_retest: Annotated[
        tuple[str, ...], BeforeValidator(_freeze_sequence), Field(max_length=40)
    ]
    transcript_included: Literal[False] = False
    oversize_justification: Annotated[str, Field(min_length=12, max_length=1_000)] | None = None


class HandoffSizeV1(BaseModel):
    """Measured handoff size without fabricating token counts."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    byte_count: Annotated[int, Field(ge=0)]
    exact_token_count: Annotated[int, Field(ge=0)] | None

    # Token telemetry is meaningful only when an exact counter was supplied.
    token_upper_bound: Annotated[int, Field(ge=0)] | None
    target_tokens: Literal[1000] = 1_000
    soft_max_tokens: Literal[2000] = 2_000
    target_met: bool | None
    soft_max_met: bool | None

    # Deterministic enforcement when no exact tokenizer is available.
    target_bytes: Literal[3072] = 3_072
    soft_max_bytes: Literal[4096] = 4_096
    absolute_max_bytes: Literal[8192] = 8_192
    byte_target_met: bool
    byte_soft_max_met: bool

    counting_method: Literal["exact_counter", "utf8_bytes_only"]


def measure_agent_handoff(
    handoff: AgentHandoffV1,
    *,
    token_counter: Callable[[str], int] | None = None,
) -> HandoffSizeV1:
    """Enforce compact handoffs without pretending UTF-8 bytes are token counts."""

    raw = _canonical_json(handoff.model_dump(mode="json"))
    text = raw.decode("utf-8")
    byte_count = len(raw)

    exact = token_counter(text) if token_counter is not None else None
    if exact is not None and exact < 0:
        raise ValueError("token counter returned a negative size")

    # Bytes are always known exactly. They provide the deterministic fallback
    # policy when no authoritative/model-compatible tokenizer is available.
    if byte_count > ABSOLUTE_HANDOFF_BYTE_UPPER_BOUND:
        raise ValueError("handoff exceeds the absolute 8192-byte upper bound")
    if (
        byte_count > DEFAULT_HANDOFF_SOFT_MAX_BYTES
        and handoff.oversize_justification is None
    ):
        raise ValueError(
            "handoff above the 4096-byte soft maximum requires justification"
        )

    # If an exact compatible tokenizer is available, enforce the token policy
    # independently as well.
    if exact is not None:
        if exact > ABSOLUTE_HANDOFF_TOKEN_UPPER_BOUND:
            raise ValueError("handoff exceeds the absolute 8000-token upper bound")
        if (
            exact > DEFAULT_HANDOFF_SOFT_MAX_TOKENS
            and handoff.oversize_justification is None
        ):
            raise ValueError(
                "handoff above the 2000-token soft maximum requires justification"
            )

    return HandoffSizeV1(
        byte_count=byte_count,
        exact_token_count=exact,
        token_upper_bound=exact,
        target_met=(
            exact <= DEFAULT_HANDOFF_TARGET_TOKENS
            if exact is not None
            else None
        ),
        soft_max_met=(
            exact <= DEFAULT_HANDOFF_SOFT_MAX_TOKENS
            if exact is not None
            else None
        ),
        byte_target_met=byte_count <= DEFAULT_HANDOFF_TARGET_BYTES,
        byte_soft_max_met=byte_count <= DEFAULT_HANDOFF_SOFT_MAX_BYTES,
        counting_method="exact_counter" if exact is not None else "utf8_bytes_only",
    )


def write_agent_handoff(
    handoff: AgentHandoffV1,
    destination: Path,
    *,
    token_counter: Callable[[str], int] | None = None,
) -> HandoffSizeV1:
    """Exclusively create one validated handoff after enforcing its compactness budget."""
    size = measure_agent_handoff(handoff, token_counter=token_counter)
    raw = _canonical_json(handoff.model_dump(mode="json"))
    destination.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(destination, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        with suppress(OSError):
            os.close(descriptor)
        raise
    return size


class SessionLaunchV1(BaseModel):
    """Durable fresh/continue decision made before a subtask launch."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, str_strip_whitespace=True)

    schema_version: Literal[1] = 1
    subtask_id: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,127}$")]
    role: SubtaskRole
    fresh_session: bool = True
    prior_thread_id: Annotated[str, Field(min_length=1, max_length=256)] | None = None
    continuation_reason: ContinuationReason | None = None
    durable_reason: Annotated[str, Field(min_length=16, max_length=2_000)] | None = None
    authority_references: Annotated[
        tuple[AuthorityReferenceV1, ...],
        BeforeValidator(_freeze_sequence),
        Field(max_length=20),
    ] = ()

    @model_validator(mode="after")
    def validate_launch(self) -> SessionLaunchV1:
        if self.role in {"coding_auditor", "physics_auditor"} and not self.fresh_session:
            raise ValueError("auditors must start in a fresh independent session")
        if self.fresh_session:
            if any(
                value is not None
                for value in (
                    self.prior_thread_id,
                    self.continuation_reason,
                    self.durable_reason,
                )
            ):
                raise ValueError("fresh launch must not retain prior session state")
            return self
        if (
            self.prior_thread_id is None
            or self.continuation_reason is None
            or self.durable_reason is None
            or not self.authority_references
        ):
            raise ValueError(
                "continuation requires exact thread, reason, durable explanation, and authority"
            )
        return self


def fresh_session_launch(subtask: SemanticSubtaskV1) -> SessionLaunchV1:
    """Return the default launch policy for every independent semantic subtask."""
    return SessionLaunchV1(subtask_id=subtask.subtask_id, role=subtask.role)


def resolve_predecessor_artifacts(
    subtask: SemanticSubtaskV1,
    *,
    workspace: Path,
) -> tuple[ArtifactReferenceV1, ...]:
    """Resolve every planned predecessor to exact bytes immediately before launch."""
    resolved: list[ArtifactReferenceV1] = []
    resolved_workspace = workspace.resolve(strict=True)
    for requirement in subtask.required_predecessor_artifacts:
        path = Path(requirement.path)
        if path.is_absolute():
            raise ValueError("predecessor artifact path must be relative to workspace")
        candidate = (resolved_workspace / path).resolve(strict=False)
        if not candidate.is_relative_to(resolved_workspace):
            raise ValueError(f"predecessor escapes workspace: {requirement.path}")
        if not candidate.is_file():
            raise ValueError(f"required predecessor artifact is unavailable: {requirement.path}")
        digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
        if requirement.sha256 is not None and digest != requirement.sha256:
            raise ValueError(f"required predecessor artifact hash changed: {requirement.path}")
        resolved.append(
            ArtifactReferenceV1(path=requirement.path, sha256=digest, kind=requirement.kind)
        )
    return tuple(resolved)


class ValidationReceiptV1(BaseModel):
    """Minimal deterministic PASS evidence eligible for exact fingerprint reuse."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, str_strip_whitespace=True)

    schema_version: Literal[1] = 1
    receipt_id: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,127}$")]
    status: Literal["PASS", "FAIL"]
    command: Annotated[
        tuple[str, ...], BeforeValidator(_freeze_sequence), Field(min_length=1, max_length=100)
    ]
    test_code: Annotated[
        tuple[ArtifactReferenceV1, ...],
        BeforeValidator(_freeze_sequence),
        Field(min_length=1, max_length=100),
    ]
    relevant_source: Annotated[
        tuple[ArtifactReferenceV1, ...],
        BeforeValidator(_freeze_sequence),
        Field(min_length=1, max_length=200),
    ]
    config: Annotated[
        tuple[ArtifactReferenceV1, ...],
        BeforeValidator(_freeze_sequence),
        Field(max_length=100),
    ]
    environment_assumptions: Annotated[
        tuple[str, ...], BeforeValidator(_freeze_sequence), Field(min_length=1, max_length=100)
    ]
    exit_code: int
    evidence: ArtifactReferenceV1


class ValidationReuseDecisionV1(BaseModel):
    """Fail-closed decision to cite a PASS receipt or rerun validation."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    reusable: bool
    rerun_required: bool
    reason: ValidationReuseReason


def validation_reuse_decision(
    receipt: ValidationReceiptV1,
    *,
    test_code: Sequence[ArtifactReferenceV1],
    relevant_source: Sequence[ArtifactReferenceV1],
    config: Sequence[ArtifactReferenceV1],
    environment_assumptions: Sequence[str],
) -> ValidationReuseDecisionV1:
    """Reuse only a PASS whose complete validity inputs remain byte-identical."""
    reason: ValidationReuseReason
    if receipt.status != "PASS" or receipt.exit_code != 0:
        reason = "prior_result_not_pass"
    elif tuple(test_code) != receipt.test_code:
        reason = "test_code_changed"
    elif tuple(relevant_source) != receipt.relevant_source:
        reason = "relevant_source_changed"
    elif tuple(config) != receipt.config:
        reason = "config_changed"
    elif tuple(environment_assumptions) != receipt.environment_assumptions:
        reason = "environment_changed"
    else:
        reason = "unchanged_pass_receipt"
    reusable = reason == "unchanged_pass_receipt"
    return ValidationReuseDecisionV1(
        reusable=reusable,
        rerun_required=not reusable,
        reason=reason,
    )


class SemanticSubtaskTelemetryV1(BaseModel):
    """Authoritative counters for one Supervisor-launched model session."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    subtask_id: str
    role: SubtaskRole
    repair_or_retry: bool
    usage_receipt_id: str
    accounting_complete: bool
    input_tokens: Annotated[int, Field(ge=0)] | None
    cached_input_tokens: Annotated[int, Field(ge=0)] | None
    uncached_input_tokens: Annotated[int, Field(ge=0)] | None
    output_tokens: Annotated[int, Field(ge=0)] | None
    reasoning_output_tokens: Annotated[int, Field(ge=0)] | None
    combined_tokens: Annotated[int, Field(ge=0)] | None
    inference_sample_count: Annotated[int, Field(ge=0)] | None
    median_inference_context_tokens: Annotated[int, Field(ge=0)] | None
    max_inference_context_tokens: Annotated[int, Field(ge=0)] | None
    compactions: Annotated[int, Field(ge=0)]
    command_tool_count: Annotated[int, Field(ge=0)]
    model_visible_tool_output_chars: Annotated[int, Field(ge=0)]
    handoff_size_bytes: Annotated[int, Field(ge=0)]
    handoff_size_tokens: Annotated[int, Field(ge=0)] | None
    session_fresh: bool
    continuation_reason: ContinuationReason | None

    @model_validator(mode="after")
    def fail_closed_accounting(self) -> SemanticSubtaskTelemetryV1:
        counters = (
            self.input_tokens,
            self.cached_input_tokens,
            self.uncached_input_tokens,
            self.output_tokens,
            self.reasoning_output_tokens,
            self.combined_tokens,
        )
        if self.accounting_complete and any(item is None for item in counters):
            raise ValueError("complete accounting requires every authoritative token counter")
        if not self.accounting_complete and any(item is not None for item in counters):
            raise ValueError("incomplete accounting must expose token counters as unavailable")
        if self.accounting_complete:
            assert self.input_tokens is not None
            assert self.cached_input_tokens is not None
            assert self.uncached_input_tokens is not None
            assert self.output_tokens is not None
            assert self.combined_tokens is not None
            if self.cached_input_tokens > self.input_tokens:
                raise ValueError("cached input cannot exceed total input")
            if self.uncached_input_tokens != self.input_tokens - self.cached_input_tokens:
                raise ValueError("uncached input must equal input minus cached input")
            if self.combined_tokens != self.input_tokens + self.output_tokens:
                raise ValueError("combined tokens must equal input plus output")
        if self.session_fresh != (self.continuation_reason is None):
            raise ValueError("continuation reason must agree with session freshness")
        return self


def semantic_subtask_telemetry(
    *,
    subtask: SemanticSubtaskV1,
    usage: CodexUsageReceiptV1,
    context: ContextEconomyReceiptV1,
    launch: SessionLaunchV1,
    handoff_size: HandoffSizeV1,
) -> SemanticSubtaskTelemetryV1:
    """Join exact usage/context receipts with launch and handoff measurements."""
    if launch.subtask_id != subtask.subtask_id or launch.role != subtask.role:
        raise ValueError("launch decision does not identify the semantic subtask")
    complete = usage.complete
    return SemanticSubtaskTelemetryV1(
        subtask_id=subtask.subtask_id,
        role=subtask.role,
        repair_or_retry=usage.repair_or_retry,
        usage_receipt_id=usage.receipt_id,
        accounting_complete=complete,
        input_tokens=usage.input_tokens if complete else None,
        cached_input_tokens=usage.cached_input_tokens if complete else None,
        uncached_input_tokens=(
            usage.input_tokens - usage.cached_input_tokens if complete else None
        ),
        output_tokens=usage.output_tokens if complete else None,
        reasoning_output_tokens=usage.reasoning_output_tokens if complete else None,
        combined_tokens=usage.combined_tokens if complete else None,
        inference_sample_count=context.inference_token_sample_count,
        median_inference_context_tokens=context.median_inference_input_tokens,
        max_inference_context_tokens=context.max_inference_input_tokens,
        compactions=context.compaction_count,
        command_tool_count=context.tool_call_count,
        model_visible_tool_output_chars=context.model_visible_tool_output_chars,
        handoff_size_bytes=handoff_size.byte_count,
        handoff_size_tokens=handoff_size.exact_token_count,
        session_fresh=launch.fresh_session,
        continuation_reason=launch.continuation_reason,
    )


class TelemetryTotalsV1(BaseModel):
    """Additive exact totals; unavailable token accounting stays unavailable."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    session_count: Annotated[int, Field(ge=0)]
    incomplete_session_count: Annotated[int, Field(ge=0)]
    input_tokens: Annotated[int, Field(ge=0)] | None
    cached_input_tokens: Annotated[int, Field(ge=0)] | None
    uncached_input_tokens: Annotated[int, Field(ge=0)] | None
    output_tokens: Annotated[int, Field(ge=0)] | None
    reasoning_output_tokens: Annotated[int, Field(ge=0)] | None
    combined_tokens: Annotated[int, Field(ge=0)] | None
    inference_sample_count: Annotated[int, Field(ge=0)] | None
    median_inference_context_tokens: Annotated[int, Field(ge=0)] | None
    max_inference_context_tokens: Annotated[int, Field(ge=0)] | None
    compactions: Annotated[int, Field(ge=0)]
    command_tool_count: Annotated[int, Field(ge=0)]
    model_visible_tool_output_chars: Annotated[int, Field(ge=0)]
    handoff_size_bytes: Annotated[int, Field(ge=0)]
    exact_handoff_size_tokens: Annotated[int, Field(ge=0)] | None


class SemanticStageTelemetryV1(BaseModel):
    """Stage telemetry grouped by role and repair/retry without inferred values."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    by_role: dict[str, TelemetryTotalsV1]
    repair_or_retry: TelemetryTotalsV1
    total_stage: TelemetryTotalsV1


def _telemetry_totals(items: Sequence[SemanticSubtaskTelemetryV1]) -> TelemetryTotalsV1:
    incomplete = sum(not item.accounting_complete for item in items)

    def exact_sum(field: str) -> int | None:
        values = [getattr(item, field) for item in items]
        return sum(values) if all(value is not None for value in values) else None

    maxima = [
        item.max_inference_context_tokens
        for item in items
        if item.max_inference_context_tokens is not None
    ]
    # An aggregate median cannot be recovered exactly from session medians.  Preserve it only
    # for a single session; otherwise report it as unavailable rather than estimating.
    median = items[0].median_inference_context_tokens if len(items) == 1 else None
    return TelemetryTotalsV1(
        session_count=len(items),
        incomplete_session_count=incomplete,
        input_tokens=exact_sum("input_tokens"),
        cached_input_tokens=exact_sum("cached_input_tokens"),
        uncached_input_tokens=exact_sum("uncached_input_tokens"),
        output_tokens=exact_sum("output_tokens"),
        reasoning_output_tokens=exact_sum("reasoning_output_tokens"),
        combined_tokens=exact_sum("combined_tokens"),
        inference_sample_count=exact_sum("inference_sample_count"),
        median_inference_context_tokens=median,
        max_inference_context_tokens=max(maxima, default=None),
        compactions=sum(item.compactions for item in items),
        command_tool_count=sum(item.command_tool_count for item in items),
        model_visible_tool_output_chars=sum(item.model_visible_tool_output_chars for item in items),
        handoff_size_bytes=sum(item.handoff_size_bytes for item in items),
        exact_handoff_size_tokens=exact_sum("handoff_size_tokens"),
    )


def aggregate_semantic_telemetry(
    records: Iterable[SemanticSubtaskTelemetryV1],
) -> SemanticStageTelemetryV1:
    """Aggregate every role and retries exactly once in deterministic key order."""
    items = tuple(records)
    identifiers = tuple(item.usage_receipt_id for item in items)
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("duplicate usage receipt in semantic telemetry")
    grouped: defaultdict[str, list[SemanticSubtaskTelemetryV1]] = defaultdict(list)
    for item in items:
        grouped[item.role].append(item)
    return SemanticStageTelemetryV1(
        by_role={key: _telemetry_totals(grouped[key]) for key in sorted(grouped)},
        repair_or_retry=_telemetry_totals(tuple(item for item in items if item.repair_or_retry)),
        total_stage=_telemetry_totals(items),
    )


def artifact_reference(path: Path, *, kind: ArtifactKind) -> ArtifactReferenceV1:
    """Hash one existing durable artifact without copying its content into a handoff."""
    return ArtifactReferenceV1(
        path=str(path),
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        kind=kind,
    )
