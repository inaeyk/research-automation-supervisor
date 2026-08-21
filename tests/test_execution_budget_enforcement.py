from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from research_automation_supervisor.durable_state import ZERO_HASH, atomic_write_json
from research_automation_supervisor.execution_budget import (
    ExecutionBudgetPolicyV1,
    PartialExecutionBudgetCheckpointV1,
    SupervisorNormalizedExecutionEventV1,
    initial_execution_budget_state,
    load_partial_execution_budget_checkpoint,
    observe_execution_event,
    write_partial_execution_budget_checkpoint,
)
from research_automation_supervisor.execution_budget_enforcement import (
    LiveExecutionBudgetControllerV1,
)
from research_automation_supervisor.token_accounting import (
    CodexTurnUsageV1,
    CodexUsageReceiptV1,
)


def _usage(input_tokens: int, *, output_tokens: int = 0) -> dict[str, int]:
    return {
        "input_tokens": input_tokens,
        "cached_input_tokens": 0,
        "cache_write_input_tokens": 0,
        "output_tokens": output_tokens,
        "reasoning_output_tokens": 0,
    }


def _token_count(input_tokens: int, *, output_tokens: int = 0) -> dict[str, object]:
    return {
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "total_token_usage": _usage(input_tokens, output_tokens=output_tokens)
            },
        },
    }


def _tool() -> dict[str, object]:
    return {
        "type": "custom_tool_call",
        "name": "exec",
        "input": 'text(await tools.exec_command({"cmd":"pwd"}));',
    }


def _patch() -> dict[str, object]:
    return {
        "type": "custom_tool_call",
        "name": "exec",
        "input": (
            'const patch = "*** Begin Patch\\n*** End Patch"; '
            "text(await tools.apply_patch(patch));"
        ),
    }


def _thread_started(thread_id: str = "thread-1") -> dict[str, object]:
    return {"type": "thread.started", "thread_id": thread_id}


def _controller(
    tmp_path: Path,
    *,
    policy: ExecutionBudgetPolicyV1 | None = None,
    checkpoint_name: str = "budget.json",
) -> LiveExecutionBudgetControllerV1:
    return LiveExecutionBudgetControllerV1.start_new_turn(
        checkpoint_path=tmp_path / checkpoint_name,
        normalized_event_directory=tmp_path / "normalized-events",
        task_id="task-1",
        codex_thread_id="thread-1",
        policy=policy or ExecutionBudgetPolicyV1(),
    )


def _small_policy(**changes: int) -> ExecutionBudgetPolicyV1:
    values = {
        "max_inference_samples": 100,
        "max_tool_calls": 100,
        "max_patch_calls": 100,
        "max_compactions": 100,
        "max_input_token_delta": 100_000_000,
        **changes,
    }
    return ExecutionBudgetPolicyV1(
        max_inference_samples=values["max_inference_samples"],
        max_tool_calls=values["max_tool_calls"],
        max_patch_calls=values["max_patch_calls"],
        max_compactions=values["max_compactions"],
        max_input_token_delta=values["max_input_token_delta"],
    )


def _event_file(directory: Path, position: int) -> Path:
    return directory / f"normalized-event-{position:020d}.json"


def test_normal_under_budget_interactive_stream_completes(tmp_path: Path) -> None:
    controller = _controller(tmp_path)

    assert controller.observe_native_event(_tool()).decision == "continue"
    assert controller.observe_native_event(_token_count(50)).decision == "continue"
    outcome = controller.observe_native_event({"type": "task_complete"})

    assert outcome.decision == "completed"
    assert outcome.checkpoint.state.completion_kind == "task_complete"


@pytest.mark.parametrize(
    ("event", "count", "field", "reason"),
    [
        (_token_count(1), 64, "inference_sample_count", "max_inference_samples"),
        (_tool(), 64, "tool_call_count", "max_tool_calls"),
        (_patch(), 8, "patch_call_count", "max_patch_calls"),
        ({"type": "compacted"}, 3, "compaction_count", "max_compactions"),
    ],
)
def test_default_count_ceiling_stops_exactly_at_boundary(
    tmp_path: Path,
    event: dict[str, object],
    count: int,
    field: str,
    reason: str,
) -> None:
    controller = _controller(tmp_path)

    for index in range(count):
        current = _token_count(index + 1) if event["type"] == "event_msg" else event
        outcome = controller.observe_native_event(current)

    assert outcome.decision == "bounded_continuation_required"
    assert getattr(outcome.checkpoint.state, field) == count
    assert outcome.checkpoint.state.reached_hard_limits == (reason,)


def test_token_crossing_preserves_exact_observed_delta_and_stops(tmp_path: Path) -> None:
    controller = _controller(tmp_path)

    outcome = controller.observe_native_event(_token_count(3_000_123, output_tokens=17))

    assert outcome.decision == "bounded_continuation_required"
    assert outcome.checkpoint.state.current_turn_usage_delta == CodexTurnUsageV1.model_validate(
        _usage(3_000_123, output_tokens=17)
    )
    assert outcome.checkpoint.state.current_turn_combined_tokens == 3_000_140


def test_turn_completed_crossing_token_limit_completes_with_limit_telemetry(
    tmp_path: Path,
) -> None:
    controller = _controller(
        tmp_path,
        policy=_small_policy(max_input_token_delta=10),
    )

    outcome = controller.observe_native_event(
        {"type": "turn.completed", "usage": _usage(11, output_tokens=2)}
    )

    assert outcome.decision == "completed"
    assert outcome.event_admitted is True
    assert outcome.checkpoint.state.completion_kind == "turn_completed"
    assert outcome.checkpoint.state.reached_hard_limits == ("max_input_token_delta",)
    assert outcome.checkpoint.state.current_turn_usage_delta == CodexTurnUsageV1.model_validate(
        _usage(11, output_tokens=2)
    )


def test_token_limit_then_direct_task_complete_reconciles_completion(tmp_path: Path) -> None:
    controller = _controller(
        tmp_path,
        policy=_small_policy(max_input_token_delta=10),
    )
    stopped = controller.observe_native_event(_token_count(11, output_tokens=2))

    outcome = controller.observe_native_event({"type": "task_complete"})

    assert stopped.decision == "bounded_continuation_required"
    assert outcome.decision == "completed"
    assert outcome.event_admitted is True
    assert outcome.automatic_retry_or_repair is False
    assert outcome.checkpoint.state.completion_kind == "task_complete"
    assert outcome.checkpoint.state.reached_hard_limits == ("max_input_token_delta",)
    assert outcome.checkpoint.state.current_turn_usage_delta == CodexTurnUsageV1.model_validate(
        _usage(11, output_tokens=2)
    )


@pytest.mark.parametrize("later_event", [_tool(), _token_count(12)])
def test_model_or_tool_event_after_token_budget_stop_is_rejected(
    tmp_path: Path,
    later_event: dict[str, object],
) -> None:
    controller = _controller(
        tmp_path,
        policy=_small_policy(max_input_token_delta=10),
    )
    stopped = controller.observe_native_event(_token_count(11))

    rejected = controller.observe_native_event(later_event)

    assert rejected.decision == "bounded_continuation_required"
    assert rejected.event_admitted is False
    assert stopped.checkpoint.completion_reconciliation_state == "open"
    assert rejected.checkpoint.completion_reconciliation_state == "closed"
    assert rejected.checkpoint.state == stopped.checkpoint.state
    assert rejected.checkpoint.state.last_supervisor_event_sequence == 0


def test_ordinary_post_stop_event_permanently_closes_completion_window(
    tmp_path: Path,
) -> None:
    controller = _controller(
        tmp_path,
        policy=_small_policy(max_input_token_delta=10),
    )
    stopped = controller.observe_native_event(_token_count(11))

    ordinary = controller.observe_native_event({"type": "unrelated"})
    completion = controller.observe_native_event({"type": "task_complete"})

    assert ordinary.event_admitted is False
    assert completion.decision == "bounded_continuation_required"
    assert completion.event_admitted is False
    assert stopped.checkpoint.completion_reconciliation_state == "open"
    assert completion.checkpoint.completion_reconciliation_state == "closed"
    assert completion.checkpoint.state == stopped.checkpoint.state
    assert completion.checkpoint.state.completion_kind is None


def test_simultaneous_limits_use_core_deterministic_order(tmp_path: Path) -> None:
    controller = _controller(
        tmp_path,
        policy=_small_policy(max_tool_calls=1, max_patch_calls=1),
    )

    outcome = controller.observe_native_event(_patch())

    assert outcome.checkpoint.state.reached_hard_limits == (
        "max_tool_calls",
        "max_patch_calls",
    )


def test_budget_stop_is_durable_and_rejects_later_event(tmp_path: Path) -> None:
    controller = _controller(tmp_path, policy=_small_policy(max_tool_calls=1))
    stopped = controller.observe_native_event(_tool())
    durable = load_partial_execution_budget_checkpoint(tmp_path / "budget.json")
    journal_names = sorted(path.name for path in (tmp_path / "normalized-events").iterdir())

    rejected = controller.observe_native_event(_tool())

    assert durable == stopped.checkpoint
    assert rejected.decision == "bounded_continuation_required"
    assert rejected.event_admitted is False
    assert rejected.checkpoint.state.tool_call_count == 1
    assert rejected.checkpoint.state.last_supervisor_event_sequence == 0
    assert sorted(path.name for path in (tmp_path / "normalized-events").iterdir()) == journal_names


def test_budget_stop_is_not_failure_and_never_authorizes_retry(tmp_path: Path) -> None:
    controller = _controller(tmp_path, policy=_small_policy(max_tool_calls=1))

    outcome = controller.observe_native_event(_tool())

    assert outcome.decision == "bounded_continuation_required"
    assert outcome.task_failure is False
    assert outcome.automatic_retry_or_repair is False


def test_restore_does_not_absorb_unseen_event_after_budget_stop(tmp_path: Path) -> None:
    controller = _controller(tmp_path, policy=_small_policy(max_tool_calls=1))
    stopped = controller.observe_native_event(_tool())
    extra = SupervisorNormalizedExecutionEventV1(sequence_position=1, native_event=_tool())
    atomic_write_json(
        _event_file(tmp_path / "normalized-events", 1),
        extra.model_dump(mode="json"),
        error_factory=ValueError,
        error_message="test event write failed",
    )

    restored = LiveExecutionBudgetControllerV1.restore(
        checkpoint_path=tmp_path / "budget.json",
        normalized_event_directory=tmp_path / "normalized-events",
    )

    assert restored.outcome.decision == "accounting_integrity_failure"
    assert restored.outcome.integrity_failure_kind == "lifecycle"
    assert restored.checkpoint == stopped.checkpoint
    assert restored.checkpoint.state.tool_call_count == 1


def test_restore_replays_crash_window_once_without_double_count(tmp_path: Path) -> None:
    controller = _controller(tmp_path)
    initial = controller.checkpoint
    first = controller.observe_native_event(_tool())
    write_partial_execution_budget_checkpoint(tmp_path / "budget.json", initial)

    restored = LiveExecutionBudgetControllerV1.restore(
        checkpoint_path=tmp_path / "budget.json",
        normalized_event_directory=tmp_path / "normalized-events",
    )

    assert first.checkpoint.state.tool_call_count == 1
    assert restored.checkpoint.state.tool_call_count == 1
    assert restored.checkpoint.state.last_supervisor_event_sequence == 0


def test_restore_replayed_checkpointed_events_do_not_increment(tmp_path: Path) -> None:
    controller = _controller(tmp_path)
    controller.observe_native_event(_tool())

    restored = LiveExecutionBudgetControllerV1.restore(
        checkpoint_path=tmp_path / "budget.json",
        normalized_event_directory=tmp_path / "normalized-events",
    )

    assert restored.checkpoint.state.tool_call_count == 1
    assert restored.outcome.decision == "continue"


def test_restore_rejects_replaced_checkpointed_journal_event(tmp_path: Path) -> None:
    controller = _controller(tmp_path)
    admitted = controller.observe_native_event(_tool())
    replacement = SupervisorNormalizedExecutionEventV1(
        sequence_position=0,
        native_event={"type": "compacted"},
    )
    atomic_write_json(
        _event_file(tmp_path / "normalized-events", 0),
        replacement.model_dump(mode="json"),
        error_factory=ValueError,
        error_message="test event replacement failed",
    )

    restored = LiveExecutionBudgetControllerV1.restore(
        checkpoint_path=tmp_path / "budget.json",
        normalized_event_directory=tmp_path / "normalized-events",
    )

    assert restored.outcome.decision == "accounting_integrity_failure"
    assert restored.outcome.integrity_failure_kind == "event_sequence"
    assert restored.checkpoint == admitted.checkpoint
    assert restored.checkpoint.state.tool_call_count == 1
    assert restored.checkpoint.state.compaction_count == 0


def test_fresh_thread_started_binds_identity_durably(tmp_path: Path) -> None:
    controller = LiveExecutionBudgetControllerV1.start_new_turn(
        checkpoint_path=tmp_path / "budget.json",
        normalized_event_directory=tmp_path / "normalized-events",
        task_id="task-1",
    )
    assert controller.checkpoint.codex_thread_id is None

    outcome = controller.observe_native_event(_thread_started("generated-thread"))
    durable = load_partial_execution_budget_checkpoint(tmp_path / "budget.json")

    assert outcome.decision == "continue"
    assert outcome.event_admitted is True
    assert outcome.checkpoint.codex_thread_id == "generated-thread"
    assert durable.codex_thread_id == "generated-thread"
    assert durable.normalized_event_journal_prefix_sha256 != ZERO_HASH


def test_second_thread_started_with_different_identity_fails_closed(tmp_path: Path) -> None:
    controller = LiveExecutionBudgetControllerV1.start_new_turn(
        checkpoint_path=tmp_path / "budget.json",
        normalized_event_directory=tmp_path / "normalized-events",
        task_id="task-1",
    )
    bound = controller.observe_native_event(_thread_started("generated-thread"))

    mismatch = controller.observe_native_event(_thread_started("other-thread"))

    assert mismatch.decision == "accounting_integrity_failure"
    assert mismatch.integrity_failure_kind == "identity"
    assert mismatch.event_admitted is False
    assert mismatch.checkpoint == bound.checkpoint


def test_restored_known_thread_accepts_matching_thread_started(tmp_path: Path) -> None:
    _controller(tmp_path)
    restored = LiveExecutionBudgetControllerV1.restore(
        checkpoint_path=tmp_path / "budget.json",
        normalized_event_directory=tmp_path / "normalized-events",
        expected_codex_thread_id="thread-1",
    )

    outcome = restored.observe_native_event(_thread_started("thread-1"))

    assert outcome.decision == "continue"
    assert outcome.event_admitted is True
    assert outcome.checkpoint.codex_thread_id == "thread-1"


def test_restored_known_thread_rejects_mismatching_thread_started(tmp_path: Path) -> None:
    initial = _controller(tmp_path).checkpoint
    restored = LiveExecutionBudgetControllerV1.restore(
        checkpoint_path=tmp_path / "budget.json",
        normalized_event_directory=tmp_path / "normalized-events",
        expected_codex_thread_id="thread-1",
    )

    outcome = restored.observe_native_event(_thread_started("other-thread"))

    assert outcome.decision == "accounting_integrity_failure"
    assert outcome.integrity_failure_kind == "identity"
    assert outcome.event_admitted is False
    assert outcome.checkpoint == initial


def test_fresh_unbound_thread_rejects_usage_before_thread_started(tmp_path: Path) -> None:
    controller = LiveExecutionBudgetControllerV1.start_new_turn(
        checkpoint_path=tmp_path / "budget.json",
        normalized_event_directory=tmp_path / "normalized-events",
        task_id="task-1",
    )

    outcome = controller.observe_native_event(_token_count(10))

    assert outcome.decision == "accounting_integrity_failure"
    assert outcome.integrity_failure_kind == "identity"
    assert outcome.event_admitted is False
    assert outcome.checkpoint.codex_thread_id is None
    assert outcome.checkpoint.state.last_supervisor_event_sequence == -1


def test_event_sequence_gap_fails_closed(tmp_path: Path) -> None:
    _controller(tmp_path)
    gap = SupervisorNormalizedExecutionEventV1(sequence_position=1, native_event=_tool())
    atomic_write_json(
        _event_file(tmp_path / "normalized-events", 1),
        gap.model_dump(mode="json"),
        error_factory=ValueError,
        error_message="test event write failed",
    )

    restored = LiveExecutionBudgetControllerV1.restore(
        checkpoint_path=tmp_path / "budget.json",
        normalized_event_directory=tmp_path / "normalized-events",
    )

    assert restored.outcome.decision == "accounting_integrity_failure"
    assert restored.outcome.integrity_failure_kind == "event_sequence"
    assert restored.checkpoint.state.last_supervisor_event_sequence == -1


@pytest.mark.parametrize(
    "bad_event",
    [
        {
            "type": "event_msg",
            "payload": {"type": "token_count", "info": {"total_token_usage": {}}},
        },
        _token_count(9),
    ],
)
def test_bad_authoritative_usage_preserves_last_valid_checkpoint(
    tmp_path: Path,
    bad_event: dict[str, object],
) -> None:
    controller = _controller(tmp_path)
    valid = controller.observe_native_event(_token_count(10))
    before = (tmp_path / "budget.json").read_bytes()

    outcome = controller.observe_native_event(bad_event)

    assert outcome.decision == "accounting_integrity_failure"
    assert outcome.integrity_failure_kind == "accounting"
    assert outcome.event_admitted is False
    assert outcome.checkpoint == valid.checkpoint
    assert outcome.checkpoint.state.inference_sample_count == 1
    assert outcome.checkpoint.state.last_supervisor_event_sequence == 0
    assert (tmp_path / "budget.json").read_bytes() == before


def test_invalid_token_then_task_complete_cannot_use_stale_usage(tmp_path: Path) -> None:
    controller = _controller(tmp_path)
    controller.observe_native_event(_token_count(10))
    invalid = controller.observe_native_event(
        {"type": "event_msg", "payload": {"type": "token_count", "info": {}}}
    )

    after = controller.observe_native_event({"type": "task_complete"})

    assert invalid.decision == "accounting_integrity_failure"
    assert after.decision == "accounting_integrity_failure"
    assert after.event_admitted is False
    assert after.checkpoint.state.completion_kind is None
    assert after.checkpoint.state.last_supervisor_event_sequence == 0


def test_valid_turn_completed_completes_normally(tmp_path: Path) -> None:
    controller = _controller(tmp_path)

    outcome = controller.observe_native_event(
        {"type": "turn.completed", "usage": _usage(25, output_tokens=5)}
    )

    assert outcome.decision == "completed"
    assert outcome.checkpoint.state.completion_kind == "turn_completed"


def test_later_turn_carries_baseline_and_cursor_but_resets_counts(tmp_path: Path) -> None:
    first = _controller(tmp_path, policy=_small_policy())
    first.observe_native_event(_tool())
    completed = first.observe_native_event(
        {"type": "turn.completed", "usage": _usage(100, output_tokens=10)}
    )

    later = LiveExecutionBudgetControllerV1.start_later_turn(
        previous_checkpoint=completed.checkpoint,
        checkpoint_path=tmp_path / "later-budget.json",
        normalized_event_directory=tmp_path / "normalized-events",
    )

    state = later.checkpoint.state
    assert state.baseline_cumulative_usage == CodexTurnUsageV1.model_validate(
        _usage(100, output_tokens=10)
    )
    assert state.last_supervisor_event_sequence == 1
    assert state.inference_sample_count == 0
    assert state.tool_call_count == 0
    assert state.patch_call_count == 0
    assert state.compaction_count == 0
    assert state.reached_hard_limits == ()
    assert state.completion_kind is None
    assert (
        later.checkpoint.normalized_event_journal_prefix_sha256
        == completed.checkpoint.normalized_event_journal_prefix_sha256
    )

    accepted = later.observe_native_event(_token_count(120, output_tokens=12))
    assert accepted.event_admitted is True
    assert accepted.checkpoint.state.last_supervisor_event_sequence == 2
    assert accepted.checkpoint.state.inference_sample_count == 1
    assert accepted.checkpoint.state.current_turn_usage_delta == CodexTurnUsageV1.model_validate(
        _usage(20, output_tokens=2)
    )
    assert (
        accepted.checkpoint.normalized_event_journal_prefix_sha256
        != completed.checkpoint.normalized_event_journal_prefix_sha256
    )


def test_completed_turn_cannot_absorb_later_turn_event(tmp_path: Path) -> None:
    first = _controller(tmp_path)
    completed = first.observe_native_event(
        {"type": "turn.completed", "usage": _usage(100)}
    )

    rejected = first.observe_native_event(_tool())
    later = LiveExecutionBudgetControllerV1.start_later_turn(
        previous_checkpoint=completed.checkpoint,
        checkpoint_path=tmp_path / "later-budget.json",
        normalized_event_directory=tmp_path / "normalized-events",
    )
    accepted = later.observe_native_event(_tool())

    assert rejected.decision == "completed"
    assert rejected.event_admitted is False
    assert rejected.checkpoint.state.tool_call_count == 0
    assert accepted.event_admitted is True
    assert accepted.checkpoint.state.tool_call_count == 1


def test_partial_budget_stop_cannot_masquerade_as_usage_receipt(tmp_path: Path) -> None:
    controller = _controller(tmp_path, policy=_small_policy(max_tool_calls=1))
    outcome = controller.observe_native_event(_tool())
    serialized = outcome.checkpoint.model_dump(mode="json")

    assert isinstance(outcome.checkpoint, PartialExecutionBudgetCheckpointV1)
    assert outcome.checkpoint.checkpoint_kind == "partial_execution_budget"
    with pytest.raises(ValidationError):
        CodexUsageReceiptV1.model_validate(serialized)


def test_restore_rejects_journal_event_bound_to_wrong_position(tmp_path: Path) -> None:
    _controller(tmp_path)
    mismatched = SupervisorNormalizedExecutionEventV1(
        sequence_position=4,
        native_event=_tool(),
    )
    atomic_write_json(
        _event_file(tmp_path / "normalized-events", 0),
        mismatched.model_dump(mode="json"),
        error_factory=ValueError,
        error_message="test event write failed",
    )

    restored = LiveExecutionBudgetControllerV1.restore(
        checkpoint_path=tmp_path / "budget.json",
        normalized_event_directory=tmp_path / "normalized-events",
    )

    assert restored.outcome.decision == "accounting_integrity_failure"
    assert restored.outcome.integrity_failure_kind == "event_sequence"


def test_starting_after_nonzero_sequence_allocates_next_once(tmp_path: Path) -> None:
    baseline = CodexTurnUsageV1.model_validate(_usage(1_000))
    prior_state = observe_execution_event(
        initial_execution_budget_state(
            starting_after_supervisor_event_sequence=178,
        ),
        SupervisorNormalizedExecutionEventV1(
            sequence_position=179,
            native_event={"type": "turn.completed", "usage": _usage(1_000)},
        ),
    )
    prior = PartialExecutionBudgetCheckpointV1(
        task_id="task-1",
        codex_thread_id="thread-1",
        normalized_event_journal_base_sequence=179,
        normalized_event_journal_base_sha256="a" * 64,
        normalized_event_journal_prefix_sha256="a" * 64,
        state=prior_state,
    )
    controller = LiveExecutionBudgetControllerV1.start_later_turn(
        previous_checkpoint=prior,
        checkpoint_path=tmp_path / "budget.json",
        normalized_event_directory=tmp_path / "normalized-events",
    )

    outcome = controller.observe_native_event(_token_count(1_010))

    assert outcome.checkpoint.state.last_supervisor_event_sequence == 180
    assert outcome.checkpoint.state.inference_sample_count == 1
    assert outcome.checkpoint.state.baseline_cumulative_usage == baseline
    record = json.loads(_event_file(tmp_path / "normalized-events", 180).read_text())
    assert record["sequence_position"] == 180
