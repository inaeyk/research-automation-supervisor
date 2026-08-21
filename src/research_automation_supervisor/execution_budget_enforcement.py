"""Live deterministic stream integration for per-turn execution budgets."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from research_automation_supervisor.durable_state import (
    ZERO_HASH,
    atomic_write_json,
    canonical_json,
)
from research_automation_supervisor.execution_budget import (
    DEFAULT_EXECUTION_BUDGET_POLICY_V1,
    ExecutionBudgetAccountingIntegrityError,
    ExecutionBudgetCheckpointError,
    ExecutionBudgetEventCursorError,
    ExecutionBudgetLifecycleError,
    ExecutionBudgetPolicyV1,
    PartialExecutionBudgetCheckpointV1,
    SupervisorNormalizedExecutionEventV1,
    initial_execution_budget_state,
    load_partial_execution_budget_checkpoint,
    observe_execution_event,
    partial_execution_budget_checkpoint,
    write_partial_execution_budget_checkpoint,
)
from research_automation_supervisor.token_accounting import CodexTurnUsageV1

ExecutionBudgetEnforcementDecision = Literal[
    "continue",
    "completed",
    "bounded_continuation_required",
    "accounting_integrity_failure",
]
ExecutionBudgetIntegrityFailureKind = Literal[
    "accounting",
    "event_sequence",
    "identity",
    "lifecycle",
    "durability",
]

_EVENT_FILE_PREFIX = "normalized-event-"
_EVENT_FILE_SUFFIX = ".json"
_EVENT_POSITION_WIDTH = 20


class ExecutionBudgetEnforcementOutcomeV1(BaseModel):
    """Typed live decision; a budget stop is deliberately not a task failure."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    decision: ExecutionBudgetEnforcementDecision
    checkpoint: PartialExecutionBudgetCheckpointV1
    event_admitted: bool
    integrity_failure_kind: ExecutionBudgetIntegrityFailureKind | None = None
    integrity_failure_message: str | None = None
    task_failure: Literal[False] = False
    automatic_retry_or_repair: Literal[False] = False

    @model_validator(mode="after")
    def validate_decision(self) -> ExecutionBudgetEnforcementOutcomeV1:
        state = self.checkpoint.state
        if self.decision == "bounded_continuation_required":
            if not state.reached_hard_limits:
                raise ValueError("bounded continuation requires a reached hard limit")
        elif self.decision == "completed":
            if state.completion_kind is None:
                raise ValueError("completed outcome requires an observed completion")
        elif self.decision == "continue" and (
            state.reached_hard_limits or state.completion_kind is not None
        ):
            raise ValueError("continue outcome requires an active under-budget turn")
        if self.decision == "accounting_integrity_failure":
            if self.integrity_failure_kind is None or self.integrity_failure_message is None:
                raise ValueError("integrity failure requires deterministic failure details")
            if self.event_admitted:
                raise ValueError("an integrity-failing event cannot be admitted")
        elif self.integrity_failure_kind is not None or self.integrity_failure_message is not None:
            raise ValueError("failure details are only valid for integrity failures")
        return self


class ExecutionBudgetEnforcementConfigurationError(ValueError):
    """Live controller storage or identity is inconsistent before observation."""


class ExecutionBudgetEnforcementDurabilityError(ValueError):
    """A normalized event record could not be persisted atomically."""


@dataclass
class LiveExecutionBudgetControllerV1:
    """Own one contiguous normalized sequence and its per-turn budget state."""

    checkpoint_path: Path
    normalized_event_directory: Path
    _checkpoint: PartialExecutionBudgetCheckpointV1
    _outcome: ExecutionBudgetEnforcementOutcomeV1

    @classmethod
    def start_new_turn(
        cls,
        *,
        checkpoint_path: Path,
        normalized_event_directory: Path,
        task_id: str,
        codex_thread_id: str | None = None,
        policy: ExecutionBudgetPolicyV1 = DEFAULT_EXECUTION_BUDGET_POLICY_V1,
    ) -> LiveExecutionBudgetControllerV1:
        """Start the first zero-count turn in a fresh normalized stream."""
        return cls._start_turn(
            checkpoint_path=checkpoint_path,
            normalized_event_directory=normalized_event_directory,
            task_id=task_id,
            codex_thread_id=codex_thread_id,
            policy=policy,
            baseline_cumulative_usage=None,
            starting_after_supervisor_event_sequence=-1,
            normalized_event_journal_base_sequence=-1,
            normalized_event_journal_base_sha256=ZERO_HASH,
            normalized_event_journal_prefix_sha256=ZERO_HASH,
            require_empty_event_directory=True,
        )

    @classmethod
    def _start_turn(
        cls,
        *,
        checkpoint_path: Path,
        normalized_event_directory: Path,
        task_id: str,
        codex_thread_id: str | None,
        policy: ExecutionBudgetPolicyV1,
        baseline_cumulative_usage: CodexTurnUsageV1 | None,
        starting_after_supervisor_event_sequence: int,
        normalized_event_journal_base_sequence: int,
        normalized_event_journal_base_sha256: str,
        normalized_event_journal_prefix_sha256: str,
        require_empty_event_directory: bool,
    ) -> LiveExecutionBudgetControllerV1:
        state = initial_execution_budget_state(
            policy=policy,
            baseline_cumulative_usage=baseline_cumulative_usage,
            starting_after_supervisor_event_sequence=(
                starting_after_supervisor_event_sequence
            ),
        )
        checkpoint = partial_execution_budget_checkpoint(
            task_id=task_id,
            codex_thread_id=codex_thread_id,
            state=state,
            normalized_event_journal_base_sequence=(
                normalized_event_journal_base_sequence
            ),
            normalized_event_journal_base_sha256=(
                normalized_event_journal_base_sha256
            ),
            normalized_event_journal_prefix_sha256=(
                normalized_event_journal_prefix_sha256
            ),
        )
        if checkpoint_path.exists() or checkpoint_path.is_symlink():
            raise ExecutionBudgetEnforcementConfigurationError(
                "execution-budget checkpoint already exists; restore required"
            )
        _prepare_event_directory(normalized_event_directory)
        if require_empty_event_directory and any(normalized_event_directory.iterdir()):
            raise ExecutionBudgetEnforcementConfigurationError(
                "fresh normalized event stream is not empty"
            )
        write_partial_execution_budget_checkpoint(checkpoint_path, checkpoint)
        return cls(
            checkpoint_path=checkpoint_path,
            normalized_event_directory=normalized_event_directory,
            _checkpoint=checkpoint,
            _outcome=_outcome_for_checkpoint(checkpoint, event_admitted=False),
        )

    @classmethod
    def start_later_turn(
        cls,
        *,
        previous_checkpoint: PartialExecutionBudgetCheckpointV1,
        checkpoint_path: Path,
        normalized_event_directory: Path,
        task_id: str | None = None,
        policy: ExecutionBudgetPolicyV1 | None = None,
    ) -> LiveExecutionBudgetControllerV1:
        """Start the next turn with only prior cumulative usage and cursor."""
        previous_state = previous_checkpoint.state
        if previous_state.completion_kind is None and not previous_state.reached_hard_limits:
            raise ExecutionBudgetEnforcementConfigurationError(
                "a later turn requires a completed or budget-stopped prior turn"
            )
        baseline = previous_state.latest_authoritative_cumulative_usage
        if baseline is None:
            raise ExecutionBudgetEnforcementConfigurationError(
                "a later turn requires prior authoritative cumulative usage"
            )
        if previous_checkpoint.codex_thread_id is None:
            raise ExecutionBudgetEnforcementConfigurationError(
                "a later turn requires a bound Codex thread identity"
            )
        _prepare_event_directory(normalized_event_directory)
        try:
            durable_events = _load_durable_events(normalized_event_directory)
            _verify_checkpoint_journal_prefix(previous_checkpoint, durable_events)
        except (OSError, ValueError) as exc:
            raise ExecutionBudgetEnforcementConfigurationError(
                "normalized event journal must be recovered before a later turn"
            ) from exc
        if any(
            event.sequence_position > previous_state.last_supervisor_event_sequence
            for event in durable_events
        ):
            raise ExecutionBudgetEnforcementConfigurationError(
                "prior turn has an unreconciled normalized event"
            )
        return cls._start_turn(
            checkpoint_path=checkpoint_path,
            normalized_event_directory=normalized_event_directory,
            task_id=task_id or previous_checkpoint.task_id,
            codex_thread_id=previous_checkpoint.codex_thread_id,
            policy=policy or previous_state.policy,
            baseline_cumulative_usage=baseline,
            starting_after_supervisor_event_sequence=(
                previous_state.last_supervisor_event_sequence
            ),
            normalized_event_journal_base_sequence=(
                previous_checkpoint.normalized_event_journal_base_sequence
            ),
            normalized_event_journal_base_sha256=(
                previous_checkpoint.normalized_event_journal_base_sha256
            ),
            normalized_event_journal_prefix_sha256=(
                previous_checkpoint.normalized_event_journal_prefix_sha256
            ),
            require_empty_event_directory=False,
        )

    @classmethod
    def restore(
        cls,
        *,
        checkpoint_path: Path,
        normalized_event_directory: Path,
        expected_task_id: str | None = None,
        expected_codex_thread_id: str | None = None,
    ) -> LiveExecutionBudgetControllerV1:
        """Restore a checkpoint and deterministically replay its durable event journal."""
        checkpoint = load_partial_execution_budget_checkpoint(checkpoint_path)
        if expected_task_id is not None and checkpoint.task_id != expected_task_id:
            raise ExecutionBudgetEnforcementConfigurationError("checkpoint task identity mismatch")
        if (
            expected_codex_thread_id is not None
            and checkpoint.codex_thread_id != expected_codex_thread_id
        ):
            raise ExecutionBudgetEnforcementConfigurationError(
                "checkpoint Codex thread identity mismatch"
            )
        _prepare_event_directory(normalized_event_directory)
        controller = cls(
            checkpoint_path=checkpoint_path,
            normalized_event_directory=normalized_event_directory,
            _checkpoint=checkpoint,
            _outcome=_outcome_for_checkpoint(checkpoint, event_admitted=False),
        )
        controller._reconcile_durable_events()
        return controller

    @property
    def checkpoint(self) -> PartialExecutionBudgetCheckpointV1:
        return self._checkpoint

    @property
    def outcome(self) -> ExecutionBudgetEnforcementOutcomeV1:
        return self._outcome

    def bind_codex_thread_id(
        self,
        codex_thread_id: str,
    ) -> ExecutionBudgetEnforcementOutcomeV1:
        """Durably bind a fresh stream identity without allocating a budget event."""
        normalized = codex_thread_id.strip()
        if not normalized:
            return self._fail("identity", "Codex thread identity is empty")
        current = self._checkpoint.codex_thread_id
        if current is not None:
            if current != normalized:
                return self._fail("identity", "Codex thread identity mismatch")
            return self._outcome.model_copy(update={"event_admitted": False})
        if self._checkpoint.state.last_supervisor_event_sequence != -1:
            return self._fail("identity", "unbound stream already has normalized events")
        candidate = self._checkpoint.model_copy(update={"codex_thread_id": normalized})
        try:
            write_partial_execution_budget_checkpoint(self.checkpoint_path, candidate)
        except (ExecutionBudgetCheckpointError, OSError) as exc:
            return self._fail("durability", f"thread identity persistence failed: {exc}")
        self._checkpoint = candidate
        self._outcome = _outcome_for_checkpoint(candidate, event_admitted=False)
        return self._outcome

    def close_completion_reconciliation(
        self,
    ) -> ExecutionBudgetEnforcementOutcomeV1:
        """Durably close the one-record completion window without reopening the turn."""
        if self._checkpoint.completion_reconciliation_state != "open":
            return self._outcome.model_copy(update={"event_admitted": False})
        candidate = self._checkpoint.model_copy(
            update={"completion_reconciliation_state": "closed"}
        )
        try:
            write_partial_execution_budget_checkpoint(self.checkpoint_path, candidate)
        except (ExecutionBudgetCheckpointError, OSError) as exc:
            return self._fail(
                "durability", f"completion reconciliation persistence failed: {exc}"
            )
        self._checkpoint = candidate
        self._outcome = _outcome_for_checkpoint(candidate, event_admitted=False)
        return self._outcome

    def reconcile_last_durable_native_event(
        self,
        native_event: dict[str, object],
    ) -> ExecutionBudgetEnforcementOutcomeV1 | None:
        """Recognize the sole journal-before-source-cursor crash window."""
        cursor = self._checkpoint.state.last_supervisor_event_sequence
        if cursor <= self._checkpoint.normalized_event_journal_base_sequence:
            return None
        try:
            events = _load_durable_events(self.normalized_event_directory)
            _verify_checkpoint_journal_prefix(self._checkpoint, events)
        except (OSError, ValueError) as exc:
            return self._fail("event_sequence", str(exc))
        for event in reversed(events):
            if event.sequence_position == cursor:
                if event.native_event == native_event:
                    return self._outcome.model_copy(update={"event_admitted": False})
                return None
        return None

    def record_integrity_failure(
        self,
        kind: ExecutionBudgetIntegrityFailureKind,
        message: str,
    ) -> ExecutionBudgetEnforcementOutcomeV1:
        """Expose a fail-closed source/controller boundary to a typed observer."""
        return self._fail(kind, message)

    def observe_native_event(
        self,
        native_event: dict[str, object],
    ) -> ExecutionBudgetEnforcementOutcomeV1:
        """Allocate, durably bind, and apply exactly one newly consumed native event."""
        if self._outcome.decision == "bounded_continuation_required":
            if not (
                self._checkpoint.completion_reconciliation_state == "open"
                and _is_task_complete_event(native_event)
            ):
                return self.close_completion_reconciliation()
        elif self._outcome.decision != "continue":
            self._outcome = self._outcome.model_copy(update={"event_admitted": False})
            return self._outcome

        next_position = self._checkpoint.state.last_supervisor_event_sequence + 1
        normalized = SupervisorNormalizedExecutionEventV1(
            sequence_position=next_position,
            native_event=native_event,
        )
        event_path = _event_path(self.normalized_event_directory, next_position)
        if event_path.exists() or event_path.is_symlink():
            return self._fail(
                "event_sequence",
                f"normalized event position {next_position} is already durable; restore required",
            )
        try:
            atomic_write_json(
                event_path,
                normalized.model_dump(mode="json"),
                error_factory=ExecutionBudgetEnforcementDurabilityError,
                error_message="normalized execution event could not be written",
            )
        except (ExecutionBudgetEnforcementDurabilityError, OSError) as exc:
            return self._fail("durability", f"normalized event persistence failed: {exc}")
        return self._apply_normalized_event(normalized)

    def _reconcile_durable_events(self) -> None:
        try:
            events = _load_durable_events(self.normalized_event_directory)
            _verify_checkpoint_journal_prefix(self._checkpoint, events)
        except (OSError, ValueError) as exc:
            self._fail("event_sequence", str(exc))
            return
        for normalized in events:
            if self._outcome.decision == "accounting_integrity_failure":
                return
            if (
                normalized.sequence_position
                <= self._checkpoint.state.last_supervisor_event_sequence
            ):
                continue
            if self._outcome.decision == "bounded_continuation_required":
                if not (
                    self._checkpoint.completion_reconciliation_state == "open"
                    and _is_task_complete_event(normalized.native_event)
                ):
                    self._fail(
                        "lifecycle",
                        "normalized event journal continues after terminal turn state",
                    )
                    return
            elif self._outcome.decision != "continue":
                self._fail(
                    "lifecycle",
                    "normalized event journal continues after terminal turn state",
                )
                return
            self._apply_normalized_event(normalized)

    def _apply_normalized_event(
        self,
        normalized: SupervisorNormalizedExecutionEventV1,
    ) -> ExecutionBudgetEnforcementOutcomeV1:
        previous = self._checkpoint
        try:
            codex_thread_id = _bound_codex_thread_id(previous, normalized.native_event)
        except ValueError as exc:
            return self._fail("identity", str(exc))
        try:
            state = observe_execution_event(previous.state, normalized)
        except ExecutionBudgetAccountingIntegrityError as exc:
            return self._fail("accounting", str(exc))
        except ExecutionBudgetEventCursorError as exc:
            return self._fail("event_sequence", str(exc))
        except ExecutionBudgetLifecycleError as exc:
            return self._fail("lifecycle", str(exc))

        reconciliation_state = previous.completion_reconciliation_state
        if state.completion_kind is not None:
            reconciliation_state = "closed"
        elif state.reached_hard_limits and _is_token_count_event(normalized.native_event):
            reconciliation_state = "open"
        candidate = partial_execution_budget_checkpoint(
            task_id=previous.task_id,
            codex_thread_id=codex_thread_id,
            state=state,
            normalized_event_journal_base_sequence=(
                previous.normalized_event_journal_base_sequence
            ),
            normalized_event_journal_base_sha256=(
                previous.normalized_event_journal_base_sha256
            ),
            normalized_event_journal_prefix_sha256=_advance_journal_prefix_sha256(
                previous.normalized_event_journal_prefix_sha256,
                normalized,
            ),
            completion_reconciliation_state=reconciliation_state,
        )
        try:
            write_partial_execution_budget_checkpoint(self.checkpoint_path, candidate)
        except (ExecutionBudgetCheckpointError, OSError) as exc:
            return self._fail("durability", f"checkpoint persistence failed: {exc}")
        self._checkpoint = candidate
        self._outcome = _outcome_for_checkpoint(candidate, event_admitted=True)
        return self._outcome

    def _fail(
        self,
        kind: ExecutionBudgetIntegrityFailureKind,
        message: str,
    ) -> ExecutionBudgetEnforcementOutcomeV1:
        self._outcome = ExecutionBudgetEnforcementOutcomeV1(
            decision="accounting_integrity_failure",
            checkpoint=self._checkpoint,
            event_admitted=False,
            integrity_failure_kind=kind,
            integrity_failure_message=message,
        )
        return self._outcome


def _outcome_for_checkpoint(
    checkpoint: PartialExecutionBudgetCheckpointV1,
    *,
    event_admitted: bool,
) -> ExecutionBudgetEnforcementOutcomeV1:
    state = checkpoint.state
    if state.completion_kind is not None:
        decision: ExecutionBudgetEnforcementDecision = "completed"
    elif state.reached_hard_limits:
        decision = "bounded_continuation_required"
    else:
        decision = "continue"
    return ExecutionBudgetEnforcementOutcomeV1(
        decision=decision,
        checkpoint=checkpoint,
        event_admitted=event_admitted,
    )


def _bound_codex_thread_id(
    checkpoint: PartialExecutionBudgetCheckpointV1,
    native_event: dict[str, object],
) -> str:
    current = checkpoint.codex_thread_id
    is_thread_started = native_event.get("type") == "thread.started"
    if current is None and not is_thread_started:
        raise ValueError("fresh Codex stream requires thread.started before other events")
    if not is_thread_started:
        assert current is not None
        return current
    observed = native_event.get("thread_id")
    if not isinstance(observed, str) or not observed.strip():
        raise ValueError("thread.started has an invalid Codex thread identity")
    normalized_observed = observed.strip()
    if current is not None and normalized_observed != current:
        raise ValueError("thread.started Codex thread identity mismatch")
    return normalized_observed


def _is_token_count_event(native_event: dict[str, object]) -> bool:
    payload = native_event.get("payload")
    return (
        native_event.get("type") == "event_msg"
        and isinstance(payload, dict)
        and payload.get("type") == "token_count"
    )


def _is_task_complete_event(native_event: dict[str, object]) -> bool:
    payload = native_event.get("payload")
    return native_event.get("type") == "task_complete" or (
        native_event.get("type") == "event_msg"
        and isinstance(payload, dict)
        and payload.get("type") == "task_complete"
    )


def _advance_journal_prefix_sha256(
    previous_sha256: str,
    normalized_event: SupervisorNormalizedExecutionEventV1,
) -> str:
    body = {
        "previous_prefix_sha256": previous_sha256,
        "normalized_event": normalized_event.model_dump(mode="json"),
    }
    return hashlib.sha256(canonical_json(body)).hexdigest()


def _verify_checkpoint_journal_prefix(
    checkpoint: PartialExecutionBudgetCheckpointV1,
    events: tuple[SupervisorNormalizedExecutionEventV1, ...],
) -> None:
    base = checkpoint.normalized_event_journal_base_sequence
    cursor = checkpoint.state.last_supervisor_event_sequence
    if any(event.sequence_position <= base for event in events):
        raise ValueError("normalized event journal precedes its checkpointed base")
    checkpointed_events = tuple(
        event for event in events if event.sequence_position <= cursor
    )
    expected_positions = tuple(range(base + 1, cursor + 1))
    actual_positions = tuple(event.sequence_position for event in checkpointed_events)
    if actual_positions != expected_positions:
        raise ValueError("normalized event journal does not contain the checkpointed prefix")
    anchor = checkpoint.normalized_event_journal_base_sha256
    for event in checkpointed_events:
        anchor = _advance_journal_prefix_sha256(anchor, event)
    if anchor != checkpoint.normalized_event_journal_prefix_sha256:
        raise ValueError("normalized event journal prefix does not match its checkpoint anchor")


def _checkpoint_cursor_is_token_count(
    checkpoint: PartialExecutionBudgetCheckpointV1,
    events: tuple[SupervisorNormalizedExecutionEventV1, ...],
) -> bool:
    cursor = checkpoint.state.last_supervisor_event_sequence
    return any(
        event.sequence_position == cursor and _is_token_count_event(event.native_event)
        for event in events
    )


def _prepare_event_directory(path: Path) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
        if path.is_symlink() or not path.is_dir():
            raise OSError("normalized event path is not a regular directory")
    except OSError as exc:
        raise ExecutionBudgetEnforcementConfigurationError(
            "normalized event directory is unavailable"
        ) from exc


def _event_path(directory: Path, position: int) -> Path:
    return directory / (
        f"{_EVENT_FILE_PREFIX}{position:0{_EVENT_POSITION_WIDTH}d}{_EVENT_FILE_SUFFIX}"
    )


def _load_durable_events(
    directory: Path,
) -> tuple[SupervisorNormalizedExecutionEventV1, ...]:
    found: list[SupervisorNormalizedExecutionEventV1] = []
    for path in sorted(directory.iterdir(), key=lambda candidate: candidate.name):
        if path.is_symlink() or not path.is_file():
            raise ValueError("normalized event journal contains a non-regular entry")
        name = path.name
        if not name.startswith(_EVENT_FILE_PREFIX) or not name.endswith(_EVENT_FILE_SUFFIX):
            raise ValueError("normalized event journal contains an unexpected entry")
        ordinal_text = name[len(_EVENT_FILE_PREFIX) : -len(_EVENT_FILE_SUFFIX)]
        if len(ordinal_text) != _EVENT_POSITION_WIDTH or not ordinal_text.isdigit():
            raise ValueError("normalized event journal contains a malformed position")
        position = int(ordinal_text)
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                raw = os.read(descriptor, 100 * 1024 * 1024 + 1)
            finally:
                os.close(descriptor)
            if len(raw) > 100 * 1024 * 1024:
                raise ValueError("normalized event record exceeds the supported size")
            value = json.loads(raw.decode("ascii"))
            event = SupervisorNormalizedExecutionEventV1.model_validate(value)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise ValueError("normalized event journal contains an invalid event") from exc
        if event.sequence_position != position:
            raise ValueError("normalized event filename and payload positions differ")
        found.append(event)
    for previous, current in zip(found, found[1:], strict=False):
        if current.sequence_position != previous.sequence_position + 1:
            raise ValueError("normalized event journal contains a sequence gap")
    return tuple(found)
