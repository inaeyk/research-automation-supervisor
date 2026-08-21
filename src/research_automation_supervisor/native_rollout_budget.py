"""Durable native Codex rollout observation for live execution budgets."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from research_automation_supervisor.durable_state import atomic_write_json
from research_automation_supervisor.execution_budget import Sha256Digest
from research_automation_supervisor.execution_budget_enforcement import (
    ExecutionBudgetEnforcementOutcomeV1,
    LiveExecutionBudgetControllerV1,
)

_SOURCE_RECORD_METADATA_KEY = "supervisor_native_rollout_record_v1"
_MAX_NATIVE_RECORD_BYTES = 100 * 1024 * 1024
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


class NativeRolloutSourceCursorV1(BaseModel):
    """Exact durable position and prefix binding for one Codex rollout JSONL."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    cursor_kind: Literal["native_rollout_budget_source"] = (
        "native_rollout_budget_source"
    )
    codex_thread_id: Annotated[str, Field(min_length=1)]
    rollout_relative_path: Annotated[str, Field(min_length=1)]
    rollout_source_identity_sha256: Sha256Digest
    consumed_byte_offset: Annotated[int, Field(ge=0)] = 0
    consumed_record_count: Annotated[int, Field(ge=0)] = 0
    consumed_prefix_sha256: Sha256Digest = _EMPTY_SHA256
    last_record_sha256: Sha256Digest | None = None


class NativeRolloutBudgetObserverConfigurationError(ValueError):
    """Native rollout discovery or restored source state is inconsistent."""


class NativeRolloutBudgetObserverCursorError(ValueError):
    """Native rollout cursor storage is unavailable or invalid."""


@dataclass
class NativeRolloutBudgetObserverV1:
    """Tail one append-only Codex rollout and drive one budget controller."""

    controller: LiveExecutionBudgetControllerV1
    sessions_root: Path
    source_cursor_directory: Path
    _expected_thread_id: str | None
    _require_existing_source_cursor: bool
    _source_cursor_path: Path | None = None
    _cursor: NativeRolloutSourceCursorV1 | None = None
    _rollout_path: Path | None = None
    _prefix_hasher: Any = None

    @classmethod
    def create(
        cls,
        *,
        controller: LiveExecutionBudgetControllerV1,
        sessions_root: Path,
        source_cursor_directory: Path,
        require_existing_source_cursor: bool = False,
    ) -> NativeRolloutBudgetObserverV1:
        """Create a fresh observer or restore its exact durable source position."""
        root = sessions_root.resolve()
        observer = cls(
            controller=controller,
            sessions_root=root,
            source_cursor_directory=source_cursor_directory.resolve(),
            _expected_thread_id=controller.checkpoint.codex_thread_id,
            _require_existing_source_cursor=require_existing_source_cursor,
        )
        if observer._expected_thread_id is not None:
            observer._bind_cursor_storage(observer._expected_thread_id)
        elif require_existing_source_cursor:
            observer._fail("identity", "resumed Codex rollout has no bound thread")
        return observer

    @property
    def cursor(self) -> NativeRolloutSourceCursorV1 | None:
        return self._cursor

    @property
    def source_cursor_path(self) -> Path | None:
        """Thread-scoped durable cursor path, available once identity is bound."""
        return self._source_cursor_path

    @property
    def outcome(self) -> ExecutionBudgetEnforcementOutcomeV1:
        return self.controller.outcome

    def bind_thread(self, codex_thread_id: str) -> ExecutionBudgetEnforcementOutcomeV1:
        """Bind the stdout-authoritative thread ID once, then discover its rollout."""
        normalized = codex_thread_id.strip()
        if not normalized:
            return self._fail("identity", "Codex thread identity is empty")
        if self._expected_thread_id is not None and self._expected_thread_id != normalized:
            return self._fail("identity", "native rollout Codex thread identity mismatch")
        outcome = self.controller.bind_codex_thread_id(normalized)
        if outcome.decision == "accounting_integrity_failure":
            return outcome
        self._expected_thread_id = normalized
        if not self._bind_cursor_storage(normalized):
            return self.outcome
        if self._cursor is not None and self._cursor.codex_thread_id != normalized:
            return self._fail("identity", "native source cursor Codex thread mismatch")
        return self.poll()

    def _bind_cursor_storage(self, codex_thread_id: str) -> bool:
        candidate = _thread_source_cursor_path(
            self.source_cursor_directory,
            codex_thread_id,
        )
        if self._source_cursor_path is not None and self._source_cursor_path != candidate:
            self._fail("identity", "native source cursor storage identity mismatch")
            return False
        self._source_cursor_path = candidate
        if candidate.exists() or candidate.is_symlink():
            self._restore_cursor()
            return self.outcome.decision != "accounting_integrity_failure"
        if self._require_existing_source_cursor:
            self._fail(
                "event_sequence",
                "resumed Codex rollout requires an existing native source cursor",
            )
            return False
        return True

    def poll(self) -> ExecutionBudgetEnforcementOutcomeV1:
        """Consume every currently complete newline-terminated rollout record."""
        if self.outcome.decision == "accounting_integrity_failure":
            return self.outcome
        if self._expected_thread_id is None:
            return self.outcome
        if self._rollout_path is None and not self._discover_rollout():
            return self.outcome
        assert self._rollout_path is not None
        try:
            if self._rollout_path.is_symlink() or not self._rollout_path.is_file():
                raise ValueError("native rollout source is not a regular file")
            offset = self._cursor.consumed_byte_offset if self._cursor is not None else 0
            size = self._rollout_path.stat().st_size
            if size < offset:
                raise ValueError("native rollout source was truncated before its cursor")
            with self._rollout_path.open("rb") as source:
                source.seek(offset)
                available = source.read()
        except (OSError, ValueError) as exc:
            return self._fail("event_sequence", f"native rollout read failed: {exc}")

        consumed_from_buffer = 0
        while True:
            newline = available.find(b"\n", consumed_from_buffer)
            if newline < 0:
                break
            record_start = offset + consumed_from_buffer
            raw_record = available[consumed_from_buffer : newline + 1]
            record_end = offset + newline + 1
            consumed_from_buffer = newline + 1
            if len(raw_record) > _MAX_NATIVE_RECORD_BYTES:
                return self._fail("event_sequence", "native rollout record is too large")
            outcome = self._consume_complete_record(
                raw_record,
                record_start=record_start,
                record_end=record_end,
            )
            if outcome.decision == "accounting_integrity_failure":
                return outcome
            if outcome.decision == "completed":
                break
            if (
                outcome.decision == "bounded_continuation_required"
                and outcome.checkpoint.completion_reconciliation_state != "open"
            ):
                break
        return self.outcome

    def _discover_rollout(self) -> bool:
        assert self._expected_thread_id is not None
        if not self.sessions_root.exists():
            return False
        pattern = f"rollout-*-{self._expected_thread_id}.jsonl"
        candidates = sorted(self.sessions_root.rglob(pattern), key=lambda item: item.as_posix())
        if not candidates:
            return False
        if len(candidates) != 1:
            self._fail("identity", "Codex thread maps to multiple native rollout sources")
            return False
        candidate = candidates[0]
        try:
            relative = _validated_relative_source(candidate, self.sessions_root)
            first_record = _read_first_complete_record(candidate)
            if first_record is None:
                return False
            session_id = _session_id_from_meta(first_record)
            if session_id != self._expected_thread_id:
                raise ValueError("native rollout session_meta identity mismatch")
            source_identity = _source_identity_sha256(
                relative,
                session_id,
                first_record,
            )
        except (OSError, ValueError) as exc:
            self._fail("identity", f"native rollout discovery failed: {exc}")
            return False
        self._rollout_path = candidate
        if self._cursor is None:
            self._cursor = NativeRolloutSourceCursorV1(
                codex_thread_id=session_id,
                rollout_relative_path=relative,
                rollout_source_identity_sha256=source_identity,
            )
            self._prefix_hasher = hashlib.sha256()
        elif (
            self._cursor.rollout_relative_path != relative
            or self._cursor.rollout_source_identity_sha256 != source_identity
        ):
            self._fail("identity", "restored native rollout source identity mismatch")
            return False
        return True

    def _restore_cursor(self) -> None:
        assert self._source_cursor_path is not None
        try:
            if (
                self._source_cursor_path.is_symlink()
                or not self._source_cursor_path.is_file()
            ):
                raise ValueError("native source cursor is not a regular file")
            cursor = NativeRolloutSourceCursorV1.model_validate_json(
                self._source_cursor_path.read_bytes()
            )
            expected = self.controller.checkpoint.codex_thread_id
            if expected is None or cursor.codex_thread_id != expected:
                raise ValueError("native source cursor Codex thread identity mismatch")
            source = self.sessions_root / cursor.rollout_relative_path
            relative = _validated_relative_source(source, self.sessions_root)
            if relative != cursor.rollout_relative_path:
                raise ValueError("native source cursor path is not canonical")
            first_record = _read_first_complete_record(source)
            if first_record is None:
                raise ValueError("native rollout no longer has a complete session_meta")
            session_id = _session_id_from_meta(first_record)
            identity = _source_identity_sha256(relative, session_id, first_record)
            if session_id != cursor.codex_thread_id:
                raise ValueError("native rollout session_meta identity mismatch")
            if identity != cursor.rollout_source_identity_sha256:
                raise ValueError("native rollout source identity changed")
            with source.open("rb") as handle:
                prefix = handle.read(cursor.consumed_byte_offset)
            if len(prefix) != cursor.consumed_byte_offset:
                raise ValueError("native rollout was truncated before its durable cursor")
            if hashlib.sha256(prefix).hexdigest() != cursor.consumed_prefix_sha256:
                raise ValueError("native rollout consumed prefix integrity mismatch")
            if cursor.consumed_byte_offset and not prefix.endswith(b"\n"):
                raise ValueError("native rollout cursor does not end at a complete record")
        except (OSError, ValidationError, ValueError) as exc:
            self._fail("event_sequence", f"native source cursor restore failed: {exc}")
            return
        self._expected_thread_id = cursor.codex_thread_id
        self._cursor = cursor
        self._rollout_path = source
        self._prefix_hasher = hashlib.sha256(prefix)

    def _consume_complete_record(
        self,
        raw_record: bytes,
        *,
        record_start: int,
        record_end: int,
    ) -> ExecutionBudgetEnforcementOutcomeV1:
        assert self._cursor is not None
        try:
            line = raw_record[:-1].removesuffix(b"\r")
            value = json.loads(line.decode("utf-8"), parse_constant=_reject_json_constant)
            if not isinstance(value, dict):
                raise ValueError("native rollout JSONL record is not an object")
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            return self._fail("event_sequence", f"malformed native rollout record: {exc}")

        record_sha256 = hashlib.sha256(raw_record).hexdigest()
        relevant = _is_budget_relevant_native_event(value)
        reconciled = None
        normalized_event: dict[str, object] | None = None
        if relevant:
            bound_event = dict(value)
            bound_event[_SOURCE_RECORD_METADATA_KEY] = {
                "rollout_source_identity_sha256": (
                    self._cursor.rollout_source_identity_sha256
                ),
                "record_start_byte_offset": record_start,
                "record_end_byte_offset": record_end,
                "record_sha256": record_sha256,
            }
            normalized_event = {str(key): item for key, item in bound_event.items()}
            reconciled = self.controller.reconcile_last_durable_native_event(
                normalized_event
            )

        if reconciled is None:
            current = self.controller.outcome
            if current.decision == "completed":
                return current.model_copy(update={"event_admitted": False})
            if current.decision == "bounded_continuation_required":
                if current.checkpoint.completion_reconciliation_state != "open":
                    return current.model_copy(update={"event_admitted": False})
                if normalized_event is None or not _is_task_complete_native_event(value):
                    return self.controller.close_completion_reconciliation()

            if normalized_event is not None:
                outcome = self.controller.observe_native_event(normalized_event)
                if outcome.decision == "accounting_integrity_failure":
                    return outcome
            else:
                outcome = self.controller.close_completion_reconciliation()
                if outcome.decision == "accounting_integrity_failure":
                    return outcome

        assert self._prefix_hasher is not None
        candidate_hasher = self._prefix_hasher.copy()
        candidate_hasher.update(raw_record)
        candidate = self._cursor.model_copy(
            update={
                "consumed_byte_offset": record_end,
                "consumed_record_count": self._cursor.consumed_record_count + 1,
                "consumed_prefix_sha256": candidate_hasher.hexdigest(),
                "last_record_sha256": record_sha256,
            }
        )
        assert self._source_cursor_path is not None
        try:
            atomic_write_json(
                self._source_cursor_path,
                candidate.model_dump(mode="json"),
                error_factory=NativeRolloutBudgetObserverCursorError,
                error_message="native rollout source cursor could not be written",
            )
        except (NativeRolloutBudgetObserverCursorError, OSError) as exc:
            return self._fail("durability", f"native source cursor persistence failed: {exc}")
        self._cursor = candidate
        self._prefix_hasher = candidate_hasher
        return self.controller.outcome

    def _fail(
        self,
        kind: Literal["accounting", "event_sequence", "identity", "lifecycle", "durability"],
        message: str,
    ) -> ExecutionBudgetEnforcementOutcomeV1:
        return self.controller.record_integrity_failure(kind, message)


def _validated_relative_source(path: Path, sessions_root: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ValueError("native rollout source is not a regular file")
    resolved_root = sessions_root.resolve()
    resolved_path = path.resolve()
    try:
        relative = resolved_path.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError("native rollout source escapes the Codex sessions root") from exc
    return relative.as_posix()


def _thread_source_cursor_path(directory: Path, codex_thread_id: str) -> Path:
    """Return the stable cursor location for one persistent Codex thread."""
    thread_digest = hashlib.sha256(codex_thread_id.encode("utf-8")).hexdigest()
    return directory / f"codex-thread-{thread_digest}.json"


def _read_first_complete_record(path: Path) -> bytes | None:
    with path.open("rb") as source:
        first = source.readline(_MAX_NATIVE_RECORD_BYTES + 1)
    if len(first) > _MAX_NATIVE_RECORD_BYTES:
        raise ValueError("native rollout session_meta record is too large")
    if not first.endswith(b"\n"):
        return None
    return first


def _session_id_from_meta(raw_record: bytes) -> str:
    try:
        value = json.loads(raw_record[:-1].decode("utf-8"), parse_constant=_reject_json_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("native rollout session_meta is malformed") from exc
    if not isinstance(value, dict) or value.get("type") != "session_meta":
        raise ValueError("native rollout first record is not session_meta")
    payload = value.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("native rollout session_meta payload is malformed")
    session_id = payload.get("id", payload.get("session_id"))
    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError("native rollout session_meta has no session identity")
    return session_id.strip()


def _source_identity_sha256(
    relative_path: str,
    session_id: str,
    first_record: bytes,
) -> str:
    identity = b"\0".join(
        (
            relative_path.encode("utf-8"),
            session_id.encode("utf-8"),
            hashlib.sha256(first_record).hexdigest().encode("ascii"),
        )
    )
    return hashlib.sha256(identity).hexdigest()


def _is_budget_relevant_native_event(event: dict[str, Any]) -> bool:
    event_type = event.get("type")
    payload = event.get("payload")
    if event_type == "compacted":
        return True
    if not isinstance(payload, dict):
        return False
    payload_type = payload.get("type")
    if event_type == "event_msg":
        return payload_type in {"token_count", "task_complete"}
    if event_type == "response_item":
        return payload_type in {"custom_tool_call", "function_call"}
    return False


def _is_task_complete_native_event(event: dict[str, Any]) -> bool:
    payload = event.get("payload")
    return (
        event.get("type") == "event_msg"
        and isinstance(payload, dict)
        and payload.get("type") == "task_complete"
    )


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant is not allowed: {value}")
