"""Authoritative token accounting for Codex JSONL runtime events."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
UsageRole = Literal[
    "worker",
    "coding_auditor",
    "physics_auditor",
    "other_model_session",
]
UsageActionKind = Literal["initial", "repair_retry"]


class CodexTurnUsageV1(BaseModel):
    """One exact ``turn.completed.usage`` object and its event binding."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    event_index: Annotated[int, Field(ge=0)]
    event_sha256: Sha256
    input_tokens: Annotated[int, Field(ge=0)]
    cached_input_tokens: Annotated[int, Field(ge=0)]
    output_tokens: Annotated[int, Field(ge=0)]
    reasoning_output_tokens: Annotated[int, Field(ge=0)]
    combined_tokens: Annotated[int, Field(ge=0)]

    @model_validator(mode="after")
    def validate_totals(self) -> CodexTurnUsageV1:
        if self.cached_input_tokens > self.input_tokens:
            raise ValueError("cached input tokens cannot exceed input tokens")
        if self.combined_tokens != self.input_tokens + self.output_tokens:
            raise ValueError("combined tokens must equal input plus output")
        return self


class CodexUsageReceiptV1(BaseModel):
    """Strict action receipt derived only from persisted Codex runtime events."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    authority: Literal["codex_runtime.turn.completed.usage"] = (
        "codex_runtime.turn.completed.usage"
    )
    complete: bool
    incomplete_reasons: tuple[str, ...]
    campaign_task_id: str
    role: UsageRole
    action_kind: UsageActionKind
    action_id: str
    codex_thread_id: str | None
    resumed_thread_id: str | None
    model: str
    codex_cli_version: str | None
    event_log_path: str
    event_log_sha256: Sha256
    event_log_disposition: Literal["raw", "ras_redacted"]
    valid_event_count: Annotated[int, Field(ge=0)]
    malformed_event_count: Annotated[int, Field(ge=0)]
    completed_turn_count: Annotated[int, Field(ge=0)]
    usage_event_count: Annotated[int, Field(ge=0)]
    turn_usages: tuple[CodexTurnUsageV1, ...]
    input_tokens: Annotated[int, Field(ge=0)]
    cached_input_tokens: Annotated[int, Field(ge=0)]
    output_tokens: Annotated[int, Field(ge=0)]
    reasoning_output_tokens: Annotated[int, Field(ge=0)]
    combined_tokens: Annotated[int, Field(ge=0)]

    @model_validator(mode="after")
    def validate_receipt(self) -> CodexUsageReceiptV1:
        if self.complete == bool(self.incomplete_reasons):
            raise ValueError("receipt completeness contradicts its reasons")
        if self.usage_event_count != len(self.turn_usages):
            raise ValueError("usage event count does not match turn usages")
        expected = (
            sum(item.input_tokens for item in self.turn_usages),
            sum(item.cached_input_tokens for item in self.turn_usages),
            sum(item.output_tokens for item in self.turn_usages),
            sum(item.reasoning_output_tokens for item in self.turn_usages),
            sum(item.combined_tokens for item in self.turn_usages),
        )
        actual = (
            self.input_tokens,
            self.cached_input_tokens,
            self.output_tokens,
            self.reasoning_output_tokens,
            self.combined_tokens,
        )
        if actual != expected:
            raise ValueError("receipt totals do not match its turn usages")
        if self.combined_tokens != self.input_tokens + self.output_tokens:
            raise ValueError("combined tokens must equal input plus output")
        return self


class TokenTotalsV1(BaseModel):
    """Exact aggregate counters; cached and reasoning values are submetrics."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    input_tokens: Annotated[int, Field(ge=0)] = 0
    cached_input_tokens: Annotated[int, Field(ge=0)] = 0
    output_tokens: Annotated[int, Field(ge=0)] = 0
    reasoning_output_tokens: Annotated[int, Field(ge=0)] = 0
    combined_tokens: Annotated[int, Field(ge=0)] = 0
    session_count: Annotated[int, Field(ge=0)] = 0

    @model_validator(mode="after")
    def validate_combined(self) -> TokenTotalsV1:
        if self.cached_input_tokens > self.input_tokens:
            raise ValueError("cached input tokens cannot exceed input tokens")
        if self.combined_tokens != self.input_tokens + self.output_tokens:
            raise ValueError("combined tokens must equal input plus output")
        return self


class TaskTokenLedgerV1(BaseModel):
    """Deduplicated campaign/task aggregate over verified action receipts."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    task_id: str
    receipt_sha256: tuple[Sha256, ...]
    receipts: tuple[CodexUsageReceiptV1, ...]
    worker: TokenTotalsV1
    coding_auditor: TokenTotalsV1
    physics_auditor: TokenTotalsV1
    repairs_retries: TokenTotalsV1
    other_model_sessions: TokenTotalsV1
    total: TokenTotalsV1
    complete: bool
    incomplete_action_ids: tuple[str, ...]

    @model_validator(mode="after")
    def validate_aggregate(self) -> TaskTokenLedgerV1:
        rebuilt = build_task_token_ledger(self.task_id, self.receipts, _validate=False)
        if self.model_dump(mode="json") != rebuilt.model_dump(mode="json"):
            raise ValueError("task token ledger does not match its receipts")
        return self


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


def receipt_sha256(receipt: CodexUsageReceiptV1) -> str:
    """Hash one canonical receipt without relying on its presentation bytes."""
    return hashlib.sha256(_canonical_bytes(receipt.model_dump(mode="json"))).hexdigest()


def parse_codex_usage_jsonl(
    event_log_path: Path,
    *,
    campaign_task_id: str,
    role: UsageRole,
    action_kind: UsageActionKind = "initial",
    action_id: str,
    model: str,
    codex_cli_version: str | None,
    resumed_thread_id: str | None = None,
    disposition: Literal["raw", "ras_redacted"] = "ras_redacted",
    external_malformed_event_count: int = 0,
) -> CodexUsageReceiptV1:
    """Parse exact runtime counters from JSONL; malformed or duplicate data fails closed."""
    content = event_log_path.read_bytes()
    reasons: set[str] = set()
    turns: list[CodexTurnUsageV1] = []
    fingerprints: set[str] = set()
    thread_ids: list[str] = []
    valid = 0
    if external_malformed_event_count < 0:
        raise ValueError("external malformed event count cannot be negative")
    malformed = external_malformed_event_count
    if external_malformed_event_count:
        reasons.add("malformed_jsonl")
    completed = 0

    for index, raw_line in enumerate(content.splitlines()):
        if not raw_line.strip():
            continue
        try:
            value = json.loads(raw_line.decode("utf-8"), parse_constant=_reject_constant)
            if not isinstance(value, dict):
                raise ValueError("event is not an object")
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            malformed += 1
            reasons.add("malformed_jsonl")
            continue
        valid += 1
        event_type = value.get("type")
        if event_type == "thread.started":
            thread_id = value.get("thread_id")
            if isinstance(thread_id, str) and thread_id and thread_id not in thread_ids:
                thread_ids.append(thread_id)
        if event_type != "turn.completed":
            continue
        completed += 1
        event_bytes = _canonical_bytes(value)
        fingerprint = hashlib.sha256(event_bytes).hexdigest()
        if fingerprint in fingerprints:
            reasons.add("duplicate_turn_completed_event")
            continue
        fingerprints.add(fingerprint)
        usage = value.get("usage")
        try:
            parsed = _parse_usage(usage, index, fingerprint)
        except (TypeError, ValueError):
            reasons.add("completed_turn_missing_or_invalid_usage")
            continue
        turns.append(parsed)

    if completed == 0:
        reasons.add("no_completed_turn")
    if completed != len(turns):
        reasons.add("completed_turn_usage_count_mismatch")
    if len(thread_ids) > 1:
        reasons.add("conflicting_thread_ids")
    thread_id = thread_ids[0] if len(thread_ids) == 1 else resumed_thread_id
    if resumed_thread_id is not None and thread_id not in {None, resumed_thread_id}:
        reasons.add("resumed_thread_id_mismatch")

    return CodexUsageReceiptV1(
        complete=not reasons,
        incomplete_reasons=tuple(sorted(reasons)),
        campaign_task_id=campaign_task_id,
        role=role,
        action_kind=action_kind,
        action_id=action_id,
        codex_thread_id=thread_id,
        resumed_thread_id=resumed_thread_id,
        model=model,
        codex_cli_version=codex_cli_version,
        event_log_path=str(event_log_path),
        event_log_sha256=hashlib.sha256(content).hexdigest(),
        event_log_disposition=disposition,
        valid_event_count=valid,
        malformed_event_count=malformed,
        completed_turn_count=completed,
        usage_event_count=len(turns),
        turn_usages=tuple(turns),
        input_tokens=sum(item.input_tokens for item in turns),
        cached_input_tokens=sum(item.cached_input_tokens for item in turns),
        output_tokens=sum(item.output_tokens for item in turns),
        reasoning_output_tokens=sum(item.reasoning_output_tokens for item in turns),
        combined_tokens=sum(item.combined_tokens for item in turns),
    )


def _parse_usage(value: object, index: int, event_sha256: str) -> CodexTurnUsageV1:
    if not isinstance(value, dict) or set(value) != {
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
    }:
        raise ValueError("usage does not match the documented schema")
    counters: dict[str, int] = {}
    for name, counter in value.items():
        if isinstance(counter, bool) or not isinstance(counter, int) or counter < 0:
            raise TypeError("usage counters must be nonnegative integers")
        counters[name] = counter
    return CodexTurnUsageV1(
        event_index=index,
        event_sha256=event_sha256,
        input_tokens=counters["input_tokens"],
        cached_input_tokens=counters["cached_input_tokens"],
        output_tokens=counters["output_tokens"],
        reasoning_output_tokens=counters["reasoning_output_tokens"],
        combined_tokens=counters["input_tokens"] + counters["output_tokens"],
    )


def _reject_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def build_task_token_ledger(
    task_id: str,
    receipts: Iterable[CodexUsageReceiptV1],
    *,
    _validate: bool = True,
) -> TaskTokenLedgerV1:
    """Build one exclusive-bucket aggregate, rejecting duplicate source event logs."""
    ordered = sorted(
        receipts,
        key=lambda item: (item.action_id, item.event_log_sha256, item.role),
    )
    unique: list[CodexUsageReceiptV1] = []
    seen_logs: set[str] = set()
    seen_actions: set[str] = set()
    for receipt in ordered:
        if receipt.campaign_task_id != task_id:
            raise ValueError("receipt task binding does not match ledger")
        if receipt.event_log_sha256 in seen_logs:
            continue
        if receipt.action_id in seen_actions:
            raise ValueError("one action cannot bind multiple event logs")
        seen_logs.add(receipt.event_log_sha256)
        seen_actions.add(receipt.action_id)
        unique.append(receipt)

    def totals(selected: Iterable[CodexUsageReceiptV1]) -> TokenTotalsV1:
        values = tuple(selected)
        return TokenTotalsV1(
            input_tokens=sum(item.input_tokens for item in values),
            cached_input_tokens=sum(item.cached_input_tokens for item in values),
            output_tokens=sum(item.output_tokens for item in values),
            reasoning_output_tokens=sum(item.reasoning_output_tokens for item in values),
            combined_tokens=sum(item.combined_tokens for item in values),
            session_count=len(values),
        )

    role_names: tuple[UsageRole, ...] = (
        "worker",
        "coding_auditor",
        "physics_auditor",
        "other_model_session",
    )
    buckets = {
        name: totals(item for item in unique if item.role == name)
        for name in role_names
    }
    hashes = tuple(receipt_sha256(item) for item in unique)
    values: dict[str, object] = {
        "schema_version": 1,
        "task_id": task_id,
        "receipt_sha256": hashes,
        "receipts": tuple(unique),
        "worker": buckets["worker"],
        "coding_auditor": buckets["coding_auditor"],
        "physics_auditor": buckets["physics_auditor"],
        "repairs_retries": totals(
            item for item in unique if item.action_kind == "repair_retry"
        ),
        "other_model_sessions": buckets["other_model_session"],
        "total": totals(unique),
        "complete": all(item.complete for item in unique),
        "incomplete_action_ids": tuple(
            item.action_id for item in unique if not item.complete
        ),
    }
    if not _validate:
        return TaskTokenLedgerV1.model_construct(**values)  # type: ignore[arg-type]
    return TaskTokenLedgerV1.model_validate(values)


def persist_usage_receipt(path: Path, receipt: CodexUsageReceiptV1) -> str:
    """Atomically persist a receipt and return its canonical SHA-256."""
    _atomic_write(path, _canonical_bytes(receipt.model_dump(mode="json")) + b"\n")
    return receipt_sha256(receipt)


def update_task_token_ledger(path: Path, receipt: CodexUsageReceiptV1) -> TaskTokenLedgerV1:
    """Recover/reuse an existing receipt or append one new action without double counting."""
    existing: tuple[CodexUsageReceiptV1, ...] = ()
    if path.is_file():
        loaded = TaskTokenLedgerV1.model_validate_json(path.read_bytes(), strict=True)
        if loaded.task_id != receipt.campaign_task_id:
            raise ValueError("existing ledger belongs to another task")
        matching = [item for item in loaded.receipts if item.action_id == receipt.action_id]
        if matching:
            if len(matching) != 1 or matching[0] != receipt:
                raise ValueError("recovered action receipt does not match durable authority")
            return loaded
        existing = loaded.receipts
    ledger = build_task_token_ledger(receipt.campaign_task_id, (*existing, receipt))
    _atomic_write(path, _canonical_bytes(ledger.model_dump(mode="json")) + b"\n")
    return ledger


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)
