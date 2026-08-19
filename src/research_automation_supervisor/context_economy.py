"""Durable, profile-driven context economy policy for Codex sessions."""

from __future__ import annotations

import hashlib
import json
import os
import statistics
from collections.abc import Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

from research_automation_supervisor.token_accounting import CodexUsageReceiptV1

BrevityProfile = Literal["B0", "B1", "B2", "B3", "B4", "B5"]
BudgetState = Literal["ok", "warning", "strong_warning", "handoff_required"]


def _freeze_json_sequence(value: object) -> object:
    return tuple(value) if isinstance(value, list) else value


class ContextEconomyOverrideV1(BaseModel):
    """A durable, justified exception to one profile's normal limits."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, str_strip_whitespace=True)

    justification: Annotated[str, Field(min_length=8, max_length=1000)]
    model_auto_compact_token_limit: Annotated[int, Field(ge=16_000)] | None = None
    tool_output_token_limit: Annotated[int, Field(ge=256)] | None = None
    tool_call_exhaustion: Annotated[int, Field(ge=1)] | None = None


class ContextEconomyProfileV1(BaseModel):
    """One explicit prompt, compaction, output, and tool-cycle profile."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    name: BrevityProfile
    supervisor_prompt_target_tokens: Annotated[int, Field(ge=1)] | None
    model_auto_compact_token_limit: Annotated[int, Field(ge=1)] | None
    tool_output_token_limit: Annotated[int, Field(ge=1)] | None
    tool_output_visible_char_limit: Annotated[int, Field(ge=1)] | None
    tool_call_warning: Annotated[int, Field(ge=1)] | None
    tool_call_strong_warning: Annotated[int, Field(ge=1)] | None
    tool_call_exhaustion: Annotated[int, Field(ge=1)] | None

    @model_validator(mode="after")
    def validate_order(self) -> ContextEconomyProfileV1:
        values = (
            self.tool_call_warning,
            self.tool_call_strong_warning,
            self.tool_call_exhaustion,
        )
        if all(value is not None for value in values):
            warning, strong, exhaustion = values
            assert warning is not None and strong is not None and exhaustion is not None
            if not warning < strong < exhaustion:
                raise ValueError("tool-call thresholds must increase strictly")
        elif any(value is not None for value in values):
            raise ValueError("tool-call thresholds must all be set or all be absent")
        return self


CONTEXT_ECONOMY_PROFILES: Mapping[BrevityProfile, ContextEconomyProfileV1] = {
    "B5": ContextEconomyProfileV1(
        name="B5", supervisor_prompt_target_tokens=300,
        model_auto_compact_token_limit=48_000, tool_output_token_limit=1_024,
        tool_output_visible_char_limit=4_096, tool_call_warning=12,
        tool_call_strong_warning=18, tool_call_exhaustion=24,
    ),
    "B4": ContextEconomyProfileV1(
        name="B4", supervisor_prompt_target_tokens=700,
        model_auto_compact_token_limit=64_000, tool_output_token_limit=2_048,
        tool_output_visible_char_limit=8_192, tool_call_warning=20,
        tool_call_strong_warning=30, tool_call_exhaustion=40,
    ),
    "B3": ContextEconomyProfileV1(
        name="B3", supervisor_prompt_target_tokens=1_200,
        model_auto_compact_token_limit=80_000, tool_output_token_limit=4_096,
        tool_output_visible_char_limit=16_384, tool_call_warning=30,
        tool_call_strong_warning=45, tool_call_exhaustion=60,
    ),
    "B2": ContextEconomyProfileV1(
        name="B2", supervisor_prompt_target_tokens=2_000,
        model_auto_compact_token_limit=120_000, tool_output_token_limit=8_192,
        tool_output_visible_char_limit=32_768, tool_call_warning=48,
        tool_call_strong_warning=72, tool_call_exhaustion=96,
    ),
    "B1": ContextEconomyProfileV1(
        name="B1", supervisor_prompt_target_tokens=3_500,
        model_auto_compact_token_limit=160_000, tool_output_token_limit=16_384,
        tool_output_visible_char_limit=65_536, tool_call_warning=64,
        tool_call_strong_warning=96, tool_call_exhaustion=128,
    ),
    "B0": ContextEconomyProfileV1(
        name="B0", supervisor_prompt_target_tokens=None,
        model_auto_compact_token_limit=None, tool_output_token_limit=None,
        tool_output_visible_char_limit=None, tool_call_warning=None,
        tool_call_strong_warning=None, tool_call_exhaustion=None,
    ),
}


class ToolCycleDispositionV1(BaseModel):
    """A non-retrying decision at a Supervisor-managed tool boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    state: BudgetState
    tool_calls: Annotated[int, Field(ge=0)]
    effective_exhaustion: Annotated[int, Field(ge=1)] | None
    permit_new_tool_call: bool
    retry_rejected_call: Literal[False] = False
    next_action: Literal["continue", "batch_or_finalize", "compact_handoff_or_stop"]


def tool_cycle_disposition(
    profile_name: BrevityProfile,
    tool_calls: int,
    override: ContextEconomyOverrideV1 | None = None,
) -> ToolCycleDispositionV1:
    """Return one terminal-aware decision; exhaustion never creates a retry loop."""
    if tool_calls < 0:
        raise ValueError("tool_calls must be nonnegative")
    profile = CONTEXT_ECONOMY_PROFILES[profile_name]
    exhaustion = (
        override.tool_call_exhaustion
        if override is not None and override.tool_call_exhaustion is not None
        else profile.tool_call_exhaustion
    )
    if exhaustion is None:
        return ToolCycleDispositionV1(
            state="ok", tool_calls=tool_calls, effective_exhaustion=None,
            permit_new_tool_call=True, next_action="continue",
        )
    warning: int | None
    strong: int | None
    if profile.tool_call_exhaustion is not None and exhaustion != profile.tool_call_exhaustion:
        warning = max(1, exhaustion // 2)
        strong = max(warning + 1, (exhaustion * 3) // 4)
    else:
        warning = profile.tool_call_warning
        strong = profile.tool_call_strong_warning
    assert warning is not None and strong is not None
    if tool_calls >= exhaustion:
        return ToolCycleDispositionV1(
            state="handoff_required", tool_calls=tool_calls,
            effective_exhaustion=exhaustion, permit_new_tool_call=False,
            next_action="compact_handoff_or_stop",
        )
    if tool_calls >= strong:
        state: BudgetState = "strong_warning"
        next_action: Literal[
            "continue", "batch_or_finalize", "compact_handoff_or_stop"
        ] = "batch_or_finalize"
    elif tool_calls >= warning:
        state = "warning"
        next_action = "batch_or_finalize"
    else:
        state = "ok"
        next_action = "continue"
    return ToolCycleDispositionV1(
        state=state, tool_calls=tool_calls, effective_exhaustion=exhaustion,
        permit_new_tool_call=True, next_action=next_action,
    )


class PreservedToolOutputV1(BaseModel):
    """Durable full output and its deterministic bounded model-safe summary."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    path: str
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    byte_count: Annotated[int, Field(ge=0)]
    visible_char_count: Annotated[int, Field(ge=0)]
    truncated: bool
    exit_code: int | None
    failed: bool
    summary: str


def preserve_large_tool_output(
    output: str,
    *,
    destination: Path,
    visible_char_limit: int,
    exit_code: int | None,
) -> PreservedToolOutputV1:
    """Preserve full UTF-8 output and return a failure-faithful bounded summary."""
    if visible_char_limit < 256:
        raise ValueError("visible_char_limit must be at least 256")
    raw = output.encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
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
    failed = exit_code not in {None, 0}
    summary = _bounded_output_summary(
        output, limit=visible_char_limit, exit_code=exit_code, failed=failed,
        path=str(destination), digest=digest,
    )
    return PreservedToolOutputV1(
        path=str(destination), sha256=digest, byte_count=len(raw),
        visible_char_count=len(summary), truncated=len(output) > len(summary),
        exit_code=exit_code, failed=failed, summary=summary,
    )


def retrieve_preserved_tool_output(
    record: PreservedToolOutputV1,
    *,
    start: int,
    length: int,
) -> bytes:
    """Deliberately retrieve one bounded byte range after hash verification."""
    if start < 0 or not 1 <= length <= 65_536:
        raise ValueError("targeted retrieval range is invalid")
    path = Path(record.path)
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != record.sha256:
        raise ValueError("preserved tool output hash mismatch")
    return raw[start : start + length]


def _bounded_output_summary(
    output: str,
    *,
    limit: int,
    exit_code: int | None,
    failed: bool,
    path: str,
    digest: str,
) -> str:
    status = "FAILED" if failed else "SUCCEEDED_OR_UNKNOWN"
    header = f"status={status} exit_code={exit_code} path={path} sha256={digest}\n"
    failure_lines = [
        line
        for line in output.splitlines()
        if any(
            word in line.casefold()
            for word in ("error", "failed", "failure", "traceback", "panic")
        )
    ][:20]
    failure_block = "\n".join(failure_lines)
    if failure_block:
        failure_block = "Failure-significant lines:\n" + failure_block
    reserved = min(len(failure_block), max(0, limit - len(header) - 80))
    failure_block = failure_block[:reserved]
    remaining = max(0, limit - len(header) - len(failure_block) - 64)
    head = output[: remaining // 2]
    tail = output[-(remaining - len(head)) :] if remaining else ""
    marker = "\n... model-visible output truncated; retrieve by path/hash ...\n"
    body = head + marker + tail
    if failure_block:
        body += "\n" + failure_block
    return (header + body)[:limit]


class HandoffV1(BaseModel):
    """Durable task-boundary choice without breaking identity-bound recovery."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    reason: Literal["task_boundary", "budget_exhaustion", "qualified_recovery"]
    start_fresh_session: bool
    preserve_original_session_identity: bool
    compact_handoff_required: bool


def handoff_disposition(
    reason: Literal["task_boundary", "budget_exhaustion", "qualified_recovery"],
) -> HandoffV1:
    """Prefer a fresh session except where recovery is tied to the original identity."""
    preserve = reason == "qualified_recovery"
    return HandoffV1(
        reason=reason,
        start_fresh_session=not preserve,
        preserve_original_session_identity=preserve,
        compact_handoff_required=not preserve,
    )


class ContextEconomyReceiptV1(BaseModel):
    """Per-Codex-task context metrics, with unavailable counters represented as null."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    profile: BrevityProfile
    prompt_bytes: Annotated[int, Field(ge=0)]
    prompt_tokens: Annotated[int, Field(ge=0)] | None
    input_tokens: Annotated[int, Field(ge=0)] | None
    cached_input_tokens: Annotated[int, Field(ge=0)] | None
    uncached_input_tokens: Annotated[int, Field(ge=0)] | None
    output_tokens: Annotated[int, Field(ge=0)] | None
    reasoning_output_tokens: Annotated[int, Field(ge=0)] | None
    combined_tokens: Annotated[int, Field(ge=0)] | None
    inference_token_sample_count: Annotated[int, Field(ge=0)] | None
    tool_call_count: Annotated[int, Field(ge=0)]
    model_visible_tool_output_chars: Annotated[int, Field(ge=0)]
    compaction_count: Annotated[int, Field(ge=0)]
    max_inference_input_tokens: Annotated[int, Field(ge=0)] | None
    median_inference_input_tokens: Annotated[int, Field(ge=0)] | None
    overrides: Annotated[
        tuple[ContextEconomyOverrideV1, ...],
        BeforeValidator(_freeze_json_sequence),
    ]


def context_economy_receipt_from_events(
    event_log: Path,
    *,
    prompt_bytes: int,
    profile: BrevityProfile,
    usage_receipt: CodexUsageReceiptV1,
    overrides: Sequence[ContextEconomyOverrideV1] = (),
) -> ContextEconomyReceiptV1:
    """Derive deterministic context metrics from retained fake or real JSON events."""
    tool_calls = 0
    visible_chars = 0
    compactions = 0
    inference_inputs: list[int] = []
    for raw_line in event_log.read_bytes().splitlines():
        try:
            event = json.loads(raw_line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(event, dict):
            continue
        event_type = event.get("type")
        item = event.get("item")
        if event_type == "item.completed" and isinstance(item, dict) and item.get("type") in {
            "command",
            "command.execution",
            "command_execution",
            "exec",
            "exec_command",
        }:
            tool_calls += 1
            visible_chars += sum(
                len(value)
                for key, value in item.items()
                if key in {"aggregated_output", "output", "stderr", "stdout"}
                and isinstance(value, str)
            )
        if event_type in {"thread.compacted", "context.compacted", "context_compacted"}:
            compactions += 1
        if event_type == "turn.completed":
            usage = event.get("usage")
            if isinstance(usage, dict) and isinstance(usage.get("input_tokens"), int):
                inference_inputs.append(usage["input_tokens"])
    complete = usage_receipt.complete
    return ContextEconomyReceiptV1(
        profile=profile, prompt_bytes=prompt_bytes, prompt_tokens=None,
        input_tokens=usage_receipt.input_tokens if complete else None,
        cached_input_tokens=usage_receipt.cached_input_tokens if complete else None,
        uncached_input_tokens=(
            usage_receipt.input_tokens - usage_receipt.cached_input_tokens if complete else None
        ),
        output_tokens=usage_receipt.output_tokens if complete else None,
        reasoning_output_tokens=(usage_receipt.reasoning_output_tokens if complete else None),
        combined_tokens=usage_receipt.combined_tokens if complete else None,
        inference_token_sample_count=len(inference_inputs) if complete else None,
        tool_call_count=tool_calls, model_visible_tool_output_chars=visible_chars,
        compaction_count=compactions,
        max_inference_input_tokens=max(inference_inputs) if inference_inputs else None,
        median_inference_input_tokens=(
            statistics.median_low(inference_inputs) if inference_inputs else None
        ),
        overrides=tuple(overrides),
    )


def supervisor_developer_instructions(
    profile_name: BrevityProfile,
    override: ContextEconomyOverrideV1 | None,
) -> str:
    """Concise supported Codex instructions for efficient Supervisor sessions."""
    profile = CONTEXT_ECONOMY_PROFILES[profile_name]
    disposition = tool_cycle_disposition(profile_name, 0, override)
    exhaustion = disposition.effective_exhaustion
    warning: int | None
    strong: int | None
    if exhaustion is not None and exhaustion != profile.tool_call_exhaustion:
        warning = max(1, exhaustion // 2)
        strong = max(warning + 1, (exhaustion * 3) // 4)
    else:
        warning = profile.tool_call_warning
        strong = profile.tool_call_strong_warning
    prompt_target = profile.supervisor_prompt_target_tokens
    budget = (
        "unbounded only under the recorded B0 justification"
        if exhaustion is None
        else (
            f"warn at {warning}, strongly batch/finalize at {strong}, and at {exhaustion} "
            "produce a compact durable handoff or stop; never retry a rejected budget call"
        )
    )
    target = "exceptional/unbounded" if prompt_target is None else str(prompt_target)
    return (
        f"Context Economy {profile_name}: Supervisor prompt target {target} tokens; {budget}. "
        "Inspect and plan once; batch related "
        "searches, reads, coherent edits, and tests. Do not rerun a valid PASS unless invalidated. "
        "Redirect verbose output to durable files and return bounded summaries with path/hash. "
        "Avoid repeated git/status/diff/test commands without a new reason. At task boundaries "
        "prefer a fresh session with a compact durable handoff, except recovery that requires "
        "the original identity."
    )


def durable_context_config_item(item: str) -> str:
    """Record exact context config by hash without leaking/redacting its value."""
    prefixes = (
        "model_auto_compact_token_limit=",
        "tool_output_token_limit=",
        "developer_instructions=",
    )
    if not item.startswith(prefixes):
        return item
    digest = hashlib.sha256(item.encode("utf-8")).hexdigest()
    return f"<CONTEXT_CONFIG_SHA256:{digest}>"
