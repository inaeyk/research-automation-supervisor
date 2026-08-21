"""Deterministic observation and checkpointing for per-turn execution budgets."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationError, model_validator

from research_automation_supervisor.durable_state import ZERO_HASH, atomic_write_json
from research_automation_supervisor.token_accounting import (
    CodexTurnUsageV1,
    cumulative_usage_delta,
)

HardLimitReason = Literal[
    "max_inference_samples",
    "max_tool_calls",
    "max_patch_calls",
    "max_compactions",
    "max_input_token_delta",
]
CompletionKind = Literal["turn_completed", "task_complete"]
AuthoritativeUsageEventKind = Literal["token_count", "turn_completed"]
CompletionReconciliationState = Literal["not_open", "open", "closed"]

_LIMIT_ORDER: tuple[HardLimitReason, ...] = (
    "max_inference_samples",
    "max_tool_calls",
    "max_patch_calls",
    "max_compactions",
    "max_input_token_delta",
)


def _tuple_from_json(value: object) -> object:
    return tuple(value) if isinstance(value, list) else value


HardLimitReasonTuple = Annotated[
    tuple[HardLimitReason, ...],
    BeforeValidator(_tuple_from_json),
]
Sha256Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class ExecutionBudgetPolicyV1(BaseModel):
    """Hard ceilings for one logical Codex turn."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    max_inference_samples: Annotated[int, Field(ge=1)] = 64
    max_tool_calls: Annotated[int, Field(ge=1)] = 64
    max_patch_calls: Annotated[int, Field(ge=1)] = 8
    max_compactions: Annotated[int, Field(ge=1)] = 3
    max_input_token_delta: Annotated[int, Field(ge=1)] = 3_000_000


DEFAULT_EXECUTION_BUDGET_POLICY_V1 = ExecutionBudgetPolicyV1()


class ExecutionBudgetStateV1(BaseModel):
    """Exact immutable state derived from native events observed so far."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    policy: ExecutionBudgetPolicyV1 = DEFAULT_EXECUTION_BUDGET_POLICY_V1
    last_supervisor_event_sequence: Annotated[int, Field(ge=-1)]
    baseline_cumulative_usage: CodexTurnUsageV1 | None = None
    latest_authoritative_cumulative_usage: CodexTurnUsageV1 | None = None
    latest_authoritative_usage_event_kind: AuthoritativeUsageEventKind | None = None
    current_turn_usage_delta: CodexTurnUsageV1 | None = None
    inference_sample_count: Annotated[int, Field(ge=0)] = 0
    tool_call_count: Annotated[int, Field(ge=0)] = 0
    patch_call_count: Annotated[int, Field(ge=0)] = 0
    compaction_count: Annotated[int, Field(ge=0)] = 0
    reached_hard_limits: HardLimitReasonTuple = ()
    completion_kind: CompletionKind | None = None

    @property
    def current_turn_combined_tokens(self) -> int | None:
        """Canonical combined delta; cache and reasoning counters are submetrics."""
        if self.current_turn_usage_delta is None:
            return None
        return (
            self.current_turn_usage_delta.input_tokens
            + self.current_turn_usage_delta.output_tokens
        )

    @model_validator(mode="after")
    def validate_derived_state(self) -> ExecutionBudgetStateV1:
        latest = self.latest_authoritative_cumulative_usage
        latest_kind = self.latest_authoritative_usage_event_kind
        delta = self.current_turn_usage_delta
        if (latest is None) != (delta is None):
            raise ValueError("latest cumulative usage and current-turn delta must appear together")
        if (latest is None) != (latest_kind is None):
            raise ValueError("authoritative usage and its event kind must appear together")
        if latest is not None:
            expected_delta = cumulative_usage_delta(
                latest,
                baseline=self.baseline_cumulative_usage,
            )
            if delta != expected_delta:
                raise ValueError("current-turn usage delta does not match cumulative usage")
        if self.completion_kind == "turn_completed" and latest_kind != "turn_completed":
            raise ValueError("turn_completed requires valid completion usage")
        if self.completion_kind == "task_complete" and latest_kind != "token_count":
            raise ValueError("task_complete requires valid token_count usage")
        expected_limits = _reached_hard_limits(
            policy=self.policy,
            inference_sample_count=self.inference_sample_count,
            tool_call_count=self.tool_call_count,
            patch_call_count=self.patch_call_count,
            compaction_count=self.compaction_count,
            usage_delta=delta,
        )
        if self.reached_hard_limits != expected_limits:
            raise ValueError("reached hard limits do not match the observed state")
        return self


class PartialExecutionBudgetCheckpointV1(BaseModel):
    """Durable partial state, deliberately distinct from a completed usage receipt."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, str_strip_whitespace=True)

    schema_version: Literal[1] = 1
    checkpoint_kind: Literal["partial_execution_budget"] = "partial_execution_budget"
    task_id: Annotated[str, Field(min_length=1)]
    codex_thread_id: Annotated[str, Field(min_length=1)] | None = None
    normalized_event_journal_base_sequence: Annotated[int, Field(ge=-1)] = -1
    normalized_event_journal_base_sha256: Sha256Digest = ZERO_HASH
    normalized_event_journal_prefix_sha256: Sha256Digest = ZERO_HASH
    completion_reconciliation_state: CompletionReconciliationState = "not_open"
    state: ExecutionBudgetStateV1

    @model_validator(mode="after")
    def validate_journal_anchor(self) -> PartialExecutionBudgetCheckpointV1:
        cursor = self.state.last_supervisor_event_sequence
        base = self.normalized_event_journal_base_sequence
        if base > cursor:
            raise ValueError("normalized event journal base cannot exceed the state cursor")
        if base == cursor and (
            self.normalized_event_journal_base_sha256
            != self.normalized_event_journal_prefix_sha256
        ):
            raise ValueError("an empty normalized event journal suffix cannot change its anchor")
        if self.codex_thread_id is None and cursor != -1:
            raise ValueError("an observed normalized event requires a bound Codex thread")
        if self.completion_reconciliation_state == "open" and (
            not self.state.reached_hard_limits or self.state.completion_kind is not None
        ):
            raise ValueError(
                "completion reconciliation may be open only for an incomplete budget stop"
            )
        return self


class SupervisorNormalizedExecutionEventV1(BaseModel):
    """One complete native event bound to its Supervisor-contiguous position."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    sequence_position: Annotated[int, Field(ge=0)]
    native_event: dict[str, object]


class ExecutionBudgetCheckpointError(ValueError):
    """A partial execution-budget checkpoint is unavailable or invalid."""


class ExecutionBudgetEventCursorError(ValueError):
    """A normalized execution-event sequence is malformed or leaves a gap."""


class ExecutionBudgetAccountingIntegrityError(ValueError):
    """An authoritative runtime-usage event is malformed or regresses."""


class ExecutionBudgetLifecycleError(ValueError):
    """An unseen event was presented to a completed per-turn budget state."""


def initial_execution_budget_state(
    *,
    policy: ExecutionBudgetPolicyV1 = DEFAULT_EXECUTION_BUDGET_POLICY_V1,
    baseline_cumulative_usage: CodexTurnUsageV1 | None = None,
    starting_after_supervisor_event_sequence: int = -1,
) -> ExecutionBudgetStateV1:
    """Create zero-count turn state after a prior normalized sequence position."""
    if (
        isinstance(starting_after_supervisor_event_sequence, bool)
        or not isinstance(starting_after_supervisor_event_sequence, int)
        or starting_after_supervisor_event_sequence < -1
    ):
        raise ExecutionBudgetEventCursorError(
            "starting sequence position must be an integer greater than or equal to -1"
        )
    return ExecutionBudgetStateV1(
        policy=policy,
        last_supervisor_event_sequence=starting_after_supervisor_event_sequence,
        baseline_cumulative_usage=baseline_cumulative_usage,
    )


def observe_execution_event(
    state: ExecutionBudgetStateV1,
    normalized_event: SupervisorNormalizedExecutionEventV1,
) -> ExecutionBudgetStateV1:
    """Apply one Supervisor-normalized event, ignoring checkpointed replay."""
    sequence_position = normalized_event.sequence_position
    if sequence_position <= state.last_supervisor_event_sequence:
        return state
    if state.completion_kind is not None:
        raise ExecutionBudgetLifecycleError("completed execution-budget state is terminal")
    expected_position = state.last_supervisor_event_sequence + 1
    if sequence_position != expected_position:
        raise ExecutionBudgetEventCursorError(
            "normalized execution-event sequence gap: "
            f"expected {expected_position}, got {sequence_position}"
        )

    event: Mapping[str, object] = normalized_event.native_event
    event_type = event.get("type")
    payload = event.get("payload")
    payload_type = payload.get("type") if isinstance(payload, Mapping) else None
    inference_increment = 0
    tool_increment = 0
    patch_increment = 0
    compaction_increment = int(event_type == "compacted")
    latest = state.latest_authoritative_cumulative_usage
    latest_kind = state.latest_authoritative_usage_event_kind
    delta = state.current_turn_usage_delta
    completion_kind: CompletionKind | None = state.completion_kind

    usage: CodexTurnUsageV1 | None = None
    if event_type == "event_msg":
        if isinstance(payload, Mapping) and payload_type == "token_count":
            usage = _token_count_usage(payload)
            if usage is None:
                raise ExecutionBudgetAccountingIntegrityError(
                    "token_count event has malformed authoritative cumulative usage"
                )
            if not _usage_advances_validly(state, usage):
                raise ExecutionBudgetAccountingIntegrityError(
                    "token_count authoritative cumulative usage regressed"
                )
            inference_increment = 1
            latest_kind = "token_count"
    elif event_type == "turn.completed":
        usage = _validated_usage(event.get("usage"))
        if usage is None:
            raise ExecutionBudgetAccountingIntegrityError(
                "turn.completed event has malformed authoritative cumulative usage"
            )
        if not _usage_advances_validly(state, usage):
            raise ExecutionBudgetAccountingIntegrityError(
                "turn.completed authoritative cumulative usage regressed"
            )
        latest_kind = "turn_completed"
        completion_kind = "turn_completed"

    interactive_completion = event_type == "task_complete" or (
        event_type == "event_msg" and payload_type == "task_complete"
    )
    if interactive_completion:
        if (
            state.latest_authoritative_cumulative_usage is None
            or state.latest_authoritative_usage_event_kind != "token_count"
        ):
            raise ExecutionBudgetAccountingIntegrityError(
                "task_complete requires prior valid authoritative token_count usage"
            )
        completion_kind = "task_complete"

    tool = _tool_call(event)
    if tool is not None:
        tool_increment = 1
        name, call_input = tool
        patch_increment = int(_is_native_patch_call(name, call_input))

    if usage is not None:
        latest = usage
        delta = cumulative_usage_delta(usage, baseline=state.baseline_cumulative_usage)

    inference_sample_count = state.inference_sample_count + inference_increment
    tool_call_count = state.tool_call_count + tool_increment
    patch_call_count = state.patch_call_count + patch_increment
    compaction_count = state.compaction_count + compaction_increment
    reached = _reached_hard_limits(
        policy=state.policy,
        inference_sample_count=inference_sample_count,
        tool_call_count=tool_call_count,
        patch_call_count=patch_call_count,
        compaction_count=compaction_count,
        usage_delta=delta,
    )
    return ExecutionBudgetStateV1(
        policy=state.policy,
        last_supervisor_event_sequence=sequence_position,
        baseline_cumulative_usage=state.baseline_cumulative_usage,
        latest_authoritative_cumulative_usage=latest,
        latest_authoritative_usage_event_kind=latest_kind,
        current_turn_usage_delta=delta,
        inference_sample_count=inference_sample_count,
        tool_call_count=tool_call_count,
        patch_call_count=patch_call_count,
        compaction_count=compaction_count,
        reached_hard_limits=reached,
        completion_kind=completion_kind,
    )


def partial_execution_budget_checkpoint(
    *,
    task_id: str,
    codex_thread_id: str | None,
    state: ExecutionBudgetStateV1,
    normalized_event_journal_base_sequence: int = -1,
    normalized_event_journal_base_sha256: str = ZERO_HASH,
    normalized_event_journal_prefix_sha256: str = ZERO_HASH,
    completion_reconciliation_state: CompletionReconciliationState = "not_open",
) -> PartialExecutionBudgetCheckpointV1:
    """Bind exact partial budget state to its task and Codex thread."""
    return PartialExecutionBudgetCheckpointV1(
        task_id=task_id,
        codex_thread_id=codex_thread_id,
        normalized_event_journal_base_sequence=normalized_event_journal_base_sequence,
        normalized_event_journal_base_sha256=normalized_event_journal_base_sha256,
        normalized_event_journal_prefix_sha256=normalized_event_journal_prefix_sha256,
        completion_reconciliation_state=completion_reconciliation_state,
        state=state,
    )


def write_partial_execution_budget_checkpoint(
    path: Path,
    checkpoint: PartialExecutionBudgetCheckpointV1,
) -> None:
    """Atomically persist one canonical partial checkpoint."""
    atomic_write_json(
        path,
        checkpoint.model_dump(mode="json", exclude_none=True),
        error_factory=ExecutionBudgetCheckpointError,
        error_message="partial execution-budget checkpoint could not be written",
    )


def load_partial_execution_budget_checkpoint(path: Path) -> PartialExecutionBudgetCheckpointV1:
    """Load one strict partial checkpoint without manufacturing a usage receipt."""
    try:
        return PartialExecutionBudgetCheckpointV1.model_validate_json(path.read_bytes())
    except (OSError, ValidationError, ValueError) as exc:
        raise ExecutionBudgetCheckpointError(
            "partial execution-budget checkpoint is invalid"
        ) from exc


def _token_count_usage(payload: Mapping[str, object]) -> CodexTurnUsageV1 | None:
    info = payload.get("info")
    candidates: tuple[object, ...] = (
        info.get("total_token_usage") if isinstance(info, Mapping) else None,
        payload.get("total_token_usage"),
        payload.get("usage"),
    )
    for candidate in candidates:
        usage = _validated_usage(candidate)
        if usage is not None:
            return usage
    return None


def _validated_usage(value: object) -> CodexTurnUsageV1 | None:
    try:
        return CodexTurnUsageV1.model_validate(value)
    except ValidationError:
        return None


def _usage_advances_validly(state: ExecutionBudgetStateV1, usage: CodexTurnUsageV1) -> bool:
    prior = state.latest_authoritative_cumulative_usage or state.baseline_cumulative_usage
    try:
        cumulative_usage_delta(usage, baseline=prior)
        cumulative_usage_delta(usage, baseline=state.baseline_cumulative_usage)
    except ValueError:
        return False
    return True


def _tool_call(event: Mapping[str, object]) -> tuple[str, object] | None:
    candidate = event
    if event.get("type") == "response_item":
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            return None
        candidate = payload
    event_type = candidate.get("type")
    if event_type == "custom_tool_call":
        call_input = candidate.get("input")
    elif event_type == "function_call":
        call_input = candidate.get("arguments")
    else:
        return None
    name = candidate.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    return name, call_input


def _is_native_patch_call(name: str, call_input: object) -> bool:
    if name != "exec" or not isinstance(call_input, str):
        return False
    if "tools.exec_command(" in call_input:
        return False
    return "tools.apply_patch(" in call_input and (
        "const patch =" in call_input or "*** Begin Patch" in call_input
    )


def _reached_hard_limits(
    *,
    policy: ExecutionBudgetPolicyV1,
    inference_sample_count: int,
    tool_call_count: int,
    patch_call_count: int,
    compaction_count: int,
    usage_delta: CodexTurnUsageV1 | None,
) -> tuple[HardLimitReason, ...]:
    reached: set[HardLimitReason] = set()
    if inference_sample_count >= policy.max_inference_samples:
        reached.add("max_inference_samples")
    if tool_call_count >= policy.max_tool_calls:
        reached.add("max_tool_calls")
    if patch_call_count >= policy.max_patch_calls:
        reached.add("max_patch_calls")
    if compaction_count >= policy.max_compactions:
        reached.add("max_compactions")
    if usage_delta is not None and usage_delta.input_tokens >= policy.max_input_token_delta:
        reached.add("max_input_token_delta")
    return tuple(reason for reason in _LIMIT_ORDER if reason in reached)
