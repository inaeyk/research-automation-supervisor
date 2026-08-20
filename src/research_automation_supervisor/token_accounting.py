"""Authoritative Codex JSON-event token receipts and aggregate ledgers."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
UsageRole = Literal[
    "worker",
    "coding_auditor",
    "physics_auditor",
    "supervisor",
    "other",
]


def _tuple_from_json(value: object) -> object:
    return tuple(value) if isinstance(value, list) else value


StringTuple = Annotated[tuple[str, ...], BeforeValidator(_tuple_from_json)]
Sha256Tuple = Annotated[tuple[Sha256, ...], BeforeValidator(_tuple_from_json)]


class CodexTurnUsageV1(BaseModel):
    """Required runtime counters plus validated additive counter extensions."""

    model_config = ConfigDict(extra="allow", frozen=True, strict=True)

    input_tokens: Annotated[int, Field(ge=0)]
    cached_input_tokens: Annotated[int, Field(ge=0)]
    # This remains a submetric: it is retained exactly but is never added to
    # input_tokens or combined_tokens.
    cache_write_input_tokens: Annotated[int, Field(ge=0)] | None = None
    output_tokens: Annotated[int, Field(ge=0)]
    reasoning_output_tokens: Annotated[int, Field(ge=0)]

    @model_validator(mode="before")
    @classmethod
    def validate_additive_counters(cls, value: object) -> object:
        if not isinstance(value, Mapping):
            return value
        for name, counter in value.items():
            if name in cls.model_fields:
                continue
            if isinstance(counter, bool) or not isinstance(counter, int) or counter < 0:
                raise ValueError(f"additive usage counter {name!r} must be a nonnegative integer")
        return value

    @model_validator(mode="after")
    def validate_cached_subset(self) -> CodexTurnUsageV1:
        if self.cached_input_tokens > self.input_tokens:
            raise ValueError("cached input tokens cannot exceed input tokens")
        return self


class TokenTotalsV1(BaseModel):
    """Additive totals; cached input is retained but never added again."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    input_tokens: Annotated[int, Field(ge=0)] = 0
    cached_input_tokens: Annotated[int, Field(ge=0)] = 0
    cache_write_input_tokens: Annotated[int, Field(ge=0)] | None = None
    output_tokens: Annotated[int, Field(ge=0)] = 0
    reasoning_output_tokens: Annotated[int, Field(ge=0)] = 0
    combined_tokens: Annotated[int, Field(ge=0)] = 0
    turn_count: Annotated[int, Field(ge=0)] = 0
    session_count: Annotated[int, Field(ge=0)] = 0

    @model_validator(mode="after")
    def validate_combined(self) -> TokenTotalsV1:
        if self.combined_tokens != self.input_tokens + self.output_tokens:
            raise ValueError("combined tokens must equal input plus output")
        if self.cached_input_tokens > self.input_tokens:
            raise ValueError("cached input tokens cannot exceed input tokens")
        return self


class CodexUsageBindingV1(BaseModel):
    """Supervisor-owned identity and attribution for one model action."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    campaign_id: Annotated[str, Field(min_length=1)]
    task_id: Annotated[str, Field(min_length=1)]
    action_id: Annotated[str, Field(min_length=1)]
    role: UsageRole
    repair_or_retry: bool = False


class CodexUsageReceiptV1(BaseModel):
    """Immutable per-action receipt derived only from retained JSONL events."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    receipt_id: Sha256
    complete: bool
    incomplete_reasons: StringTuple
    campaign_id: str
    task_id: str
    action_id: str
    role: UsageRole
    repair_or_retry: bool
    codex_thread_id: str | None
    model: str
    codex_cli_version: str | None
    event_log_sha256: Sha256
    event_count: Annotated[int, Field(ge=0)]
    completed_turn_count: Annotated[int, Field(ge=0)]
    input_tokens: Annotated[int, Field(ge=0)]
    cached_input_tokens: Annotated[int, Field(ge=0)]
    cache_write_input_tokens: Annotated[int, Field(ge=0)] | None = None
    output_tokens: Annotated[int, Field(ge=0)]
    reasoning_output_tokens: Annotated[int, Field(ge=0)]
    combined_tokens: Annotated[int, Field(ge=0)]

    @model_validator(mode="after")
    def validate_receipt(self) -> CodexUsageReceiptV1:
        if self.complete == bool(self.incomplete_reasons):
            raise ValueError("receipt completeness contradicts its reasons")
        if self.combined_tokens != self.input_tokens + self.output_tokens:
            raise ValueError("combined tokens must equal input plus output")
        if self.cached_input_tokens > self.input_tokens:
            raise ValueError("cached input tokens cannot exceed input tokens")
        if not self.complete and any(
            (
                self.completed_turn_count,
                self.input_tokens,
                self.cached_input_tokens,
                self.cache_write_input_tokens or 0,
                self.output_tokens,
                self.reasoning_output_tokens,
                self.combined_tokens,
            )
        ):
            raise ValueError("an incomplete receipt must fail closed with zero totals")
        canonical = self.model_dump(mode="json", exclude={"receipt_id"})
        if self.cache_write_input_tokens is None:
            # Keep pre-extension receipt IDs stable: only the newly optional
            # submetric is absent from their canonical representation.
            del canonical["cache_write_input_tokens"]
        expected = _receipt_id(canonical)
        if self.receipt_id != expected:
            raise ValueError("receipt ID does not match the canonical receipt")
        return self


class TaskTokenLedgerV1(BaseModel):
    """Deduplicated campaign/task aggregation across model action receipts."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    campaign_id: str
    task_id: str
    complete: bool
    receipt_ids: Sha256Tuple
    incomplete_receipt_ids: Sha256Tuple
    worker: TokenTotalsV1
    coding_auditor: TokenTotalsV1
    physics_auditor: TokenTotalsV1
    repairs_retries: TokenTotalsV1
    other_model_sessions: TokenTotalsV1
    total_input_tokens: Annotated[int, Field(ge=0)]
    total_cached_input_tokens: Annotated[int, Field(ge=0)]
    total_cache_write_input_tokens: Annotated[int, Field(ge=0)] | None = None
    total_output_tokens: Annotated[int, Field(ge=0)]
    total_reasoning_output_tokens: Annotated[int, Field(ge=0)]
    total_combined_tokens: Annotated[int, Field(ge=0)]
    total_turn_count: Annotated[int, Field(ge=0)]
    total_session_count: Annotated[int, Field(ge=0)]

    @model_validator(mode="after")
    def validate_ledger(self) -> TaskTokenLedgerV1:
        if self.complete == bool(self.incomplete_receipt_ids):
            raise ValueError("ledger completeness contradicts incomplete receipts")
        if self.total_combined_tokens != self.total_input_tokens + self.total_output_tokens:
            raise ValueError("ledger combined total must equal input plus output")
        if self.total_cached_input_tokens > self.total_input_tokens:
            raise ValueError("ledger cached input cannot exceed input")
        if tuple(sorted(set(self.receipt_ids))) != self.receipt_ids:
            raise ValueError("ledger receipt IDs must be unique and sorted")
        if not set(self.incomplete_receipt_ids).issubset(self.receipt_ids):
            raise ValueError("incomplete receipt IDs must belong to the ledger")
        return self


def receipt_from_jsonl(
    event_log: Path,
    *,
    binding: CodexUsageBindingV1,
    model: str,
    codex_cli_version: str | None,
    known_malformed_event_count: int = 0,
    prior_cumulative_usage: CodexTurnUsageV1 | None = None,
    require_prior_cumulative_usage: bool = False,
) -> CodexUsageReceiptV1:
    """Derive exact deltas from cumulative snapshots; ambiguity fails closed."""
    source, events, thread_ids, usages, reasons = _parse_event_usage(event_log)
    if known_malformed_event_count:
        reasons.add("malformed_jsonl")
    if require_prior_cumulative_usage and prior_cumulative_usage is None:
        reasons.add("missing_prior_cumulative_usage")

    complete = not reasons
    if complete:
        try:
            deltas = _cumulative_deltas(usages, prior=prior_cumulative_usage)
        except ValueError:
            reasons.add("non_monotonic_or_changed_usage_counters")
            complete = False
            deltas = ()
    else:
        deltas = ()
    if complete:
        input_tokens = sum(item.input_tokens for item in deltas)
        cached_input_tokens = sum(item.cached_input_tokens for item in deltas)
        cache_writes = [item.cache_write_input_tokens for item in deltas]
        cache_write_input_tokens = (
            sum(item for item in cache_writes if item is not None)
            if all(item is not None for item in cache_writes)
            else None
        )
        output_tokens = sum(item.output_tokens for item in deltas)
        reasoning_output_tokens = sum(item.reasoning_output_tokens for item in deltas)
        completed_turn_count = len(deltas)
    else:
        input_tokens = cached_input_tokens = output_tokens = reasoning_output_tokens = 0
        cache_write_input_tokens = None
        completed_turn_count = 0
    core: dict[str, object] = {
        "schema_version": 1,
        "complete": complete,
        "incomplete_reasons": tuple(sorted(reasons)),
        "campaign_id": binding.campaign_id,
        "task_id": binding.task_id,
        "action_id": binding.action_id,
        "role": binding.role,
        "repair_or_retry": binding.repair_or_retry,
        "codex_thread_id": next(iter(thread_ids)) if len(thread_ids) == 1 else None,
        "model": model,
        "codex_cli_version": codex_cli_version,
        "event_log_sha256": hashlib.sha256(source).hexdigest(),
        "event_count": len(events),
        "completed_turn_count": completed_turn_count,
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_input_tokens,
        "cache_write_input_tokens": cache_write_input_tokens,
        "output_tokens": output_tokens,
        "reasoning_output_tokens": reasoning_output_tokens,
        "combined_tokens": input_tokens + output_tokens,
    }
    if cache_write_input_tokens is None:
        del core["cache_write_input_tokens"]
    return CodexUsageReceiptV1(
        receipt_id=_receipt_id(core),
        complete=complete,
        incomplete_reasons=tuple(sorted(reasons)),
        campaign_id=binding.campaign_id,
        task_id=binding.task_id,
        action_id=binding.action_id,
        role=binding.role,
        repair_or_retry=binding.repair_or_retry,
        codex_thread_id=next(iter(thread_ids)) if len(thread_ids) == 1 else None,
        model=model,
        codex_cli_version=codex_cli_version,
        event_log_sha256=hashlib.sha256(source).hexdigest(),
        event_count=len(events),
        completed_turn_count=completed_turn_count,
        input_tokens=input_tokens,
        cached_input_tokens=cached_input_tokens,
        cache_write_input_tokens=cache_write_input_tokens,
        output_tokens=output_tokens,
        reasoning_output_tokens=reasoning_output_tokens,
        combined_tokens=input_tokens + output_tokens,
    )


def aggregate_task_receipts(
    receipts: Iterable[CodexUsageReceiptV1],
    *,
    campaign_id: str,
    task_id: str,
) -> TaskTokenLedgerV1:
    """Aggregate verified receipts once each; ``*`` selects the whole campaign."""
    unique: dict[str, CodexUsageReceiptV1] = {}
    action_receipts: dict[tuple[str, str, str], str] = {}
    for receipt in receipts:
        if receipt.campaign_id != campaign_id or (task_id != "*" and receipt.task_id != task_id):
            continue
        prior = unique.get(receipt.receipt_id)
        if prior is not None:
            if prior != receipt:
                raise ValueError("one receipt ID resolved to different receipt content")
            continue
        action_key = (receipt.campaign_id, receipt.task_id, receipt.action_id)
        prior_id = action_receipts.get(action_key)
        if prior_id is not None and prior_id != receipt.receipt_id:
            raise ValueError("one model action has conflicting usage receipts")
        action_receipts[action_key] = receipt.receipt_id
        unique[receipt.receipt_id] = receipt

    complete_receipts = [item for item in unique.values() if item.complete]
    worker = _sum_receipts(item for item in complete_receipts if item.role == "worker")
    coding = _sum_receipts(item for item in complete_receipts if item.role == "coding_auditor")
    physics = _sum_receipts(item for item in complete_receipts if item.role == "physics_auditor")
    repairs = _sum_receipts(item for item in complete_receipts if item.repair_or_retry)
    other = _sum_receipts(
        item for item in complete_receipts if item.role in {"supervisor", "other"}
    )
    total = _sum_receipts(iter(complete_receipts))
    receipt_ids = tuple(sorted(unique))
    incomplete_ids = tuple(sorted(item.receipt_id for item in unique.values() if not item.complete))
    return TaskTokenLedgerV1(
        campaign_id=campaign_id,
        task_id=task_id,
        complete=not incomplete_ids,
        receipt_ids=receipt_ids,
        incomplete_receipt_ids=incomplete_ids,
        worker=worker,
        coding_auditor=coding,
        physics_auditor=physics,
        repairs_retries=repairs,
        other_model_sessions=other,
        total_input_tokens=total.input_tokens,
        total_cached_input_tokens=total.cached_input_tokens,
        total_cache_write_input_tokens=total.cache_write_input_tokens,
        total_output_tokens=total.output_tokens,
        total_reasoning_output_tokens=total.reasoning_output_tokens,
        total_combined_tokens=total.combined_tokens,
        total_turn_count=total.turn_count,
        total_session_count=total.session_count,
    )


def load_verified_receipt(path: Path, *, event_log: Path | None = None) -> CodexUsageReceiptV1:
    """Load a strict receipt and optionally rebind it to its retained event log."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"), parse_constant=_reject_constant)
        receipt = CodexUsageReceiptV1.model_validate(value)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError, ValidationError) as exc:
        raise ValueError("Codex usage receipt is invalid") from exc
    if event_log is not None:
        actual = hashlib.sha256(event_log.read_bytes()).hexdigest()
        if actual != receipt.event_log_sha256:
            raise ValueError("Codex usage receipt event-log hash does not match")
    return receipt


def write_receipt(path: Path, receipt: CodexUsageReceiptV1) -> None:
    """Atomically persist one canonical receipt."""
    value = receipt.model_dump(mode="json")
    if receipt.cache_write_input_tokens is None:
        del value["cache_write_input_tokens"]
    _atomic_write_json(path, value)


def write_ledger(path: Path, ledger: TaskTokenLedgerV1) -> None:
    """Atomically persist one canonical aggregate ledger."""
    _atomic_write_json(path, ledger.model_dump(mode="json", exclude_none=True))


def _sum_receipts(receipts: Iterable[CodexUsageReceiptV1]) -> TokenTotalsV1:
    items = tuple(receipts)
    input_tokens = sum(item.input_tokens for item in items)
    output_tokens = sum(item.output_tokens for item in items)
    cache_writes = [item.cache_write_input_tokens for item in items]
    return TokenTotalsV1(
        input_tokens=input_tokens,
        cached_input_tokens=sum(item.cached_input_tokens for item in items),
        cache_write_input_tokens=(
            sum(item for item in cache_writes if item is not None)
            if all(item is not None for item in cache_writes)
            else None
        ),
        output_tokens=output_tokens,
        reasoning_output_tokens=sum(item.reasoning_output_tokens for item in items),
        combined_tokens=input_tokens + output_tokens,
        turn_count=sum(item.completed_turn_count for item in items),
        session_count=len({item.codex_thread_id for item in items}),
    )


def cumulative_usage_from_jsonl(event_log: Path) -> tuple[str, CodexTurnUsageV1]:
    """Return the one thread identity and final valid cumulative runtime snapshot."""
    _, _, thread_ids, usages, reasons = _parse_event_usage(event_log)
    if reasons or len(thread_ids) != 1 or not usages:
        raise ValueError("Codex event log lacks one complete cumulative usage snapshot")
    _cumulative_deltas(usages, prior=None)
    return next(iter(thread_ids)), usages[-1]


def cumulative_usage_delta(
    current: CodexTurnUsageV1,
    *,
    baseline: CodexTurnUsageV1 | None,
) -> CodexTurnUsageV1:
    """Return one exact delta from authoritative cumulative runtime counters."""
    return _cumulative_deltas((current,), prior=baseline)[0]


def _parse_event_usage(
    event_log: Path,
) -> tuple[
    bytes,
    list[Mapping[str, Any]],
    set[str],
    list[CodexTurnUsageV1],
    set[str],
]:
    source = event_log.read_bytes()
    reasons: set[str] = set()
    events: list[Mapping[str, Any]] = []
    completed_hashes: set[str] = set()
    usages: list[CodexTurnUsageV1] = []
    thread_ids: set[str] = set()
    for raw_line in source.splitlines():
        if not raw_line.strip():
            continue
        try:
            value = json.loads(raw_line.decode("utf-8"), parse_constant=_reject_constant)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            reasons.add("malformed_jsonl")
            continue
        if not isinstance(value, dict):
            reasons.add("malformed_jsonl")
            continue
        events.append(value)
        if value.get("type") == "thread.started":
            thread_id = value.get("thread_id")
            if isinstance(thread_id, str) and thread_id.strip():
                thread_ids.add(thread_id.strip())
        if value.get("type") != "turn.completed":
            continue
        event_hash = hashlib.sha256(_canonical_json(value)).hexdigest()
        if event_hash in completed_hashes:
            reasons.add("duplicate_turn_completed_event")
            continue
        completed_hashes.add(event_hash)
        try:
            usages.append(CodexTurnUsageV1.model_validate(value.get("usage")))
        except ValidationError:
            reasons.add("missing_or_invalid_turn_usage")
    if not completed_hashes:
        reasons.add("missing_turn_completed_event")
    if len(usages) != len(completed_hashes):
        reasons.add("missing_or_invalid_turn_usage")
    if len(thread_ids) != 1:
        reasons.add("missing_or_ambiguous_thread_id")
    return source, events, thread_ids, usages, reasons


def _cumulative_deltas(
    usages: Iterable[CodexTurnUsageV1],
    *,
    prior: CodexTurnUsageV1 | None,
) -> tuple[CodexTurnUsageV1, ...]:
    deltas: list[CodexTurnUsageV1] = []
    previous = prior
    for current in usages:
        current_values = current.model_dump(exclude_none=True)
        if previous is None:
            delta_values = current_values
        else:
            previous_values = previous.model_dump(exclude_none=True)
            if current_values.keys() != previous_values.keys():
                raise ValueError("cumulative usage counter set changed")
            delta_values = {}
            for name, value in current_values.items():
                prior_value = previous_values[name]
                if value < prior_value:
                    raise ValueError(f"cumulative usage counter decreased: {name}")
                delta_values[name] = value - prior_value
        deltas.append(CodexTurnUsageV1.model_validate(delta_values))
        previous = current
    return tuple(deltas)


def _receipt_id(value: Mapping[str, object]) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


def _reject_constant(value: str) -> Any:
    raise ValueError(f"non-finite JSON constant is forbidden: {value}")


def _atomic_write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, allow_nan=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()
