from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
from pydantic import ValidationError

from research_automation_supervisor.execution_budget import (
    DEFAULT_EXECUTION_BUDGET_POLICY_V1,
    ExecutionBudgetAccountingIntegrityError,
    ExecutionBudgetEventCursorError,
    ExecutionBudgetLifecycleError,
    ExecutionBudgetPolicyV1,
    ExecutionBudgetStateV1,
    SupervisorNormalizedExecutionEventV1,
    initial_execution_budget_state,
    load_partial_execution_budget_checkpoint,
    partial_execution_budget_checkpoint,
    write_partial_execution_budget_checkpoint,
)
from research_automation_supervisor.execution_budget import (
    observe_execution_event as _observe_execution_event,
)
from research_automation_supervisor.token_accounting import (
    CodexTurnUsageV1,
    CodexUsageReceiptV1,
)


def _usage(
    input_tokens: int,
    *,
    cached_input_tokens: int = 0,
    cache_write_input_tokens: int | None = None,
    output_tokens: int = 0,
    reasoning_output_tokens: int = 0,
    **additive: int,
) -> dict[str, int]:
    usage = {
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_input_tokens,
        "output_tokens": output_tokens,
        "reasoning_output_tokens": reasoning_output_tokens,
        **additive,
    }
    if cache_write_input_tokens is not None:
        usage["cache_write_input_tokens"] = cache_write_input_tokens
    return usage


def _inference(usage: dict[str, int]) -> dict[str, object]:
    return {
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {"total_token_usage": usage},
        },
    }


def observe_execution_event(
    state: ExecutionBudgetStateV1,
    event: dict[str, object],
) -> ExecutionBudgetStateV1:
    """Consume the next normalized sequence position outside recovery tests."""
    return _observe_execution_event(
        state,
        SupervisorNormalizedExecutionEventV1(
            sequence_position=state.last_supervisor_event_sequence + 1,
            native_event=event,
        ),
    )


def _normalized_event(
    sequence_position: int,
    event: dict[str, object],
) -> SupervisorNormalizedExecutionEventV1:
    return SupervisorNormalizedExecutionEventV1(
        sequence_position=sequence_position,
        native_event=event,
    )


def test_default_execution_budget_limits() -> None:
    assert DEFAULT_EXECUTION_BUDGET_POLICY_V1.model_dump() == {
        "schema_version": 1,
        "max_inference_samples": 64,
        "max_tool_calls": 64,
        "max_patch_calls": 8,
        "max_compactions": 3,
        "max_input_token_delta": 3_000_000,
    }


def test_inference_token_count_increments_exactly_once() -> None:
    state = observe_execution_event(initial_execution_budget_state(), _inference(_usage(10)))

    assert state.inference_sample_count == 1
    assert state.latest_authoritative_cumulative_usage == CodexTurnUsageV1.model_validate(
        _usage(10)
    )


def test_durable_cursor_ignores_replay_and_applies_next_event_once(tmp_path: Path) -> None:
    first_event = _inference(_usage(10))
    state = _observe_execution_event(
        initial_execution_budget_state(),
        _normalized_event(0, first_event),
    )
    checkpoint = partial_execution_budget_checkpoint(
        task_id="task-cursor",
        codex_thread_id="thread-cursor",
        state=state,
    )
    path = tmp_path / "cursor-checkpoint.json"
    write_partial_execution_budget_checkpoint(path, checkpoint)
    recovered = load_partial_execution_budget_checkpoint(path).state

    replayed = _observe_execution_event(recovered, _normalized_event(0, first_event))
    advanced = _observe_execution_event(
        replayed,
        _normalized_event(1, _inference(_usage(20))),
    )

    assert recovered.last_supervisor_event_sequence == 0
    assert replayed == recovered
    assert advanced.last_supervisor_event_sequence == 1
    assert advanced.inference_sample_count == 2


@pytest.mark.parametrize("invalid_sequence", [-1, True, "0"])
def test_malformed_normalized_event_sequence_fails_closed(invalid_sequence: object) -> None:
    with pytest.raises(ValidationError):
        SupervisorNormalizedExecutionEventV1(
            sequence_position=cast(int, invalid_sequence),
            native_event={},
        )


def test_normalized_event_sequence_gap_fails_closed() -> None:
    with pytest.raises(ExecutionBudgetEventCursorError, match="expected 0, got 1"):
        _observe_execution_event(
            initial_execution_budget_state(),
            _normalized_event(1, {}),
        )


def test_new_turn_starts_after_nonzero_prior_sequence() -> None:
    baseline = CodexTurnUsageV1.model_validate(_usage(1_000, output_tokens=100))
    state = initial_execution_budget_state(
        baseline_cumulative_usage=baseline,
        starting_after_supervisor_event_sequence=179,
    )

    assert state.last_supervisor_event_sequence == 179
    assert state.inference_sample_count == 0
    assert state.tool_call_count == 0
    assert state.patch_call_count == 0
    assert state.compaction_count == 0
    assert state.reached_hard_limits == ()
    assert state.completion_kind is None
    assert _observe_execution_event(state, _normalized_event(179, {})) == state

    advanced = _observe_execution_event(
        state,
        _normalized_event(180, _inference(_usage(1_010, output_tokens=101))),
    )
    assert advanced.last_supervisor_event_sequence == 180
    assert advanced.inference_sample_count == 1
    assert advanced.current_turn_combined_tokens == 11

    with pytest.raises(ExecutionBudgetEventCursorError, match="expected 180, got 181"):
        _observe_execution_event(state, _normalized_event(181, {}))


def test_top_level_compacted_counts_once_and_paired_context_event_is_ignored() -> None:
    state = initial_execution_budget_state()
    state = observe_execution_event(state, {"type": "compacted"})
    state = observe_execution_event(
        state,
        {"type": "event_msg", "payload": {"type": "context_compacted"}},
    )

    assert state.compaction_count == 1


def test_custom_and_function_tool_call_representations_count() -> None:
    state = initial_execution_budget_state()
    state = observe_execution_event(
        state,
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "name": "exec",
                "input": '{"cmd":"pwd"}',
            },
        },
    )
    state = observe_execution_event(
        state,
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "read_file",
                "arguments": '{"path":"x"}',
            },
        },
    )

    assert state.tool_call_count == 2
    assert state.patch_call_count == 0


def test_actual_native_patch_wrapper_counts_as_tool_and_patch_once() -> None:
    state = observe_execution_event(
        initial_execution_budget_state(),
        {
            "type": "custom_tool_call",
            "name": "exec",
            "input": (
                'const patch = "*** Begin Patch\\n*** End Patch"; '
                "text(await tools.apply_patch(patch));"
            ),
        },
    )

    assert state.tool_call_count == 1
    assert state.patch_call_count == 1


def test_ordinary_shell_exec_is_not_a_patch() -> None:
    state = observe_execution_event(
        initial_execution_budget_state(),
        {"type": "custom_tool_call", "name": "exec", "input": '{"cmd":"pytest -q"}'},
    )

    assert state.tool_call_count == 1
    assert state.patch_call_count == 0


def test_shell_wrapper_containing_every_patch_marker_is_not_a_patch() -> None:
    state = observe_execution_event(
        initial_execution_budget_state(),
        {
            "type": "custom_tool_call",
            "name": "exec",
            "input": (
                'const result = await tools.exec_command({"cmd": '
                "'rg -n \\\"tools.apply_patch|const patch =|*** Begin Patch\\\" .'"
                "}); text(result.output);"
            ),
        },
    )

    assert state.tool_call_count == 1
    assert state.patch_call_count == 0


@pytest.mark.parametrize(
    "event",
    [
        {},
        {"type": "custom_tool_call", "name": "", "input": "tools.apply_patch"},
        {"type": "function_call", "arguments": "{}"},
        {"type": "event_msg", "payload": {"type": "context_compacted"}},
        {"type": "context_compacted"},
    ],
)
def test_malformed_and_unrelated_events_do_not_invent_counters(event: dict[str, object]) -> None:
    state = observe_execution_event(initial_execution_budget_state(), event)

    assert state.inference_sample_count == 0
    assert state.tool_call_count == 0
    assert state.patch_call_count == 0
    assert state.compaction_count == 0
    assert state.current_turn_usage_delta is None
    assert state.reached_hard_limits == ()


def test_fresh_usage_delta_is_latest_cumulative_snapshot_not_sum() -> None:
    state = initial_execution_budget_state()
    state = observe_execution_event(state, _inference(_usage(100, output_tokens=10)))
    state = observe_execution_event(state, _inference(_usage(250, output_tokens=30)))

    assert state.current_turn_usage_delta == CodexTurnUsageV1.model_validate(
        _usage(250, output_tokens=30)
    )
    assert state.current_turn_combined_tokens == 280


def test_resumed_usage_delta_is_current_cumulative_minus_baseline() -> None:
    baseline = CodexTurnUsageV1.model_validate(
        _usage(100, cached_input_tokens=80, output_tokens=10, reasoning_output_tokens=2)
    )
    state = initial_execution_budget_state(baseline_cumulative_usage=baseline)
    state = observe_execution_event(
        state,
        _inference(
            _usage(250, cached_input_tokens=210, output_tokens=20, reasoning_output_tokens=5)
        ),
    )

    assert state.current_turn_usage_delta == CodexTurnUsageV1.model_validate(
        _usage(150, cached_input_tokens=130, output_tokens=10, reasoning_output_tokens=3)
    )
    assert state.current_turn_combined_tokens == 160


@pytest.mark.parametrize("regressing_input_tokens", [140, 90])
def test_cumulative_regression_preserves_last_valid_usage_and_inference_count(
    regressing_input_tokens: int,
) -> None:
    baseline = CodexTurnUsageV1.model_validate(_usage(100))
    state = initial_execution_budget_state(baseline_cumulative_usage=baseline)
    state = observe_execution_event(state, _inference(_usage(150)))
    last_valid = state.latest_authoritative_cumulative_usage
    last_delta = state.current_turn_usage_delta

    with pytest.raises(ExecutionBudgetAccountingIntegrityError, match="regressed"):
        observe_execution_event(state, _inference(_usage(regressing_input_tokens)))

    assert state.inference_sample_count == 1
    assert state.latest_authoritative_cumulative_usage == last_valid
    assert state.current_turn_usage_delta == last_delta
    assert state.current_turn_usage_delta is not None
    assert state.current_turn_usage_delta.input_tokens == 50


def test_malformed_token_count_fails_without_advancing_state() -> None:
    state = initial_execution_budget_state(
        starting_after_supervisor_event_sequence=40,
    )

    with pytest.raises(ExecutionBudgetAccountingIntegrityError, match="malformed"):
        observe_execution_event(
            state,
            {"type": "event_msg", "payload": {"type": "token_count", "info": {}}},
        )

    assert state.last_supervisor_event_sequence == 40
    assert state.inference_sample_count == 0
    assert state.latest_authoritative_cumulative_usage is None


def test_additive_counters_are_tolerated_without_double_counting_submetrics() -> None:
    baseline = CodexTurnUsageV1.model_validate(
        _usage(
            100,
            cached_input_tokens=80,
            cache_write_input_tokens=4,
            output_tokens=10,
            reasoning_output_tokens=2,
            total_tokens=110,
            future_counter=7,
        )
    )
    state = initial_execution_budget_state(baseline_cumulative_usage=baseline)
    state = observe_execution_event(
        state,
        _inference(
            _usage(
                250,
                cached_input_tokens=210,
                cache_write_input_tokens=9,
                output_tokens=20,
                reasoning_output_tokens=5,
                total_tokens=270,
                future_counter=11,
            )
        ),
    )

    delta = state.current_turn_usage_delta
    assert delta is not None
    assert delta.cache_write_input_tokens == 5
    assert delta.model_extra == {"future_counter": 4, "total_tokens": 160}
    assert state.current_turn_combined_tokens == 160


def test_turn_completed_usage_is_authoritative_and_marks_completion() -> None:
    state = observe_execution_event(
        initial_execution_budget_state(),
        {"type": "turn.completed", "usage": _usage(20, output_tokens=3)},
    )

    assert state.completion_kind == "turn_completed"
    assert state.inference_sample_count == 0
    assert state.current_turn_combined_tokens == 23


def test_malformed_turn_completed_usage_does_not_mark_completion() -> None:
    state = initial_execution_budget_state()
    with pytest.raises(ExecutionBudgetAccountingIntegrityError, match="malformed"):
        observe_execution_event(
            state,
            {"type": "turn.completed", "usage": {"input_tokens": "invalid"}},
        )

    assert state.completion_kind is None
    assert state.latest_authoritative_cumulative_usage is None
    assert state.last_supervisor_event_sequence == -1


def test_regressing_turn_completed_usage_does_not_mark_completion_or_replace_usage() -> None:
    state = observe_execution_event(
        initial_execution_budget_state(),
        _inference(_usage(100, output_tokens=10)),
    )
    last_valid = state.latest_authoritative_cumulative_usage
    last_delta = state.current_turn_usage_delta

    with pytest.raises(ExecutionBudgetAccountingIntegrityError, match="regressed"):
        observe_execution_event(
            state,
            {"type": "turn.completed", "usage": _usage(90, output_tokens=10)},
        )

    assert state.completion_kind is None
    assert state.inference_sample_count == 1
    assert state.latest_authoritative_cumulative_usage == last_valid
    assert state.current_turn_usage_delta == last_delta


def test_interactive_task_complete_after_valid_token_count_marks_completion() -> None:
    state = observe_execution_event(
        initial_execution_budget_state(),
        {"type": "event_msg", "payload": {"type": "task_started"}},
    )
    state = observe_execution_event(state, _inference(_usage(20, output_tokens=3)))
    state = observe_execution_event(
        state,
        {"type": "event_msg", "payload": {"type": "task_complete"}},
    )

    assert state.completion_kind == "task_complete"
    assert state.current_turn_combined_tokens == 23


def test_interactive_task_complete_without_valid_usage_does_not_mark_completion() -> None:
    state = initial_execution_budget_state()
    with pytest.raises(ExecutionBudgetAccountingIntegrityError, match="requires prior valid"):
        observe_execution_event(
            state,
            {"type": "event_msg", "payload": {"type": "task_complete"}},
        )

    assert state.completion_kind is None
    assert state.last_supervisor_event_sequence == -1


def test_invalid_token_count_then_task_complete_cannot_use_stale_usage() -> None:
    state = observe_execution_event(
        initial_execution_budget_state(),
        _inference(_usage(10)),
    )
    malformed_token_count: dict[str, object] = {
        "type": "event_msg",
        "payload": {"type": "token_count", "info": {}},
    }

    with pytest.raises(ExecutionBudgetAccountingIntegrityError, match="malformed"):
        observe_execution_event(state, malformed_token_count)
    with pytest.raises(ExecutionBudgetEventCursorError, match="expected 1, got 2"):
        _observe_execution_event(
            state,
            _normalized_event(
                2,
                {"type": "event_msg", "payload": {"type": "task_complete"}},
            ),
        )

    assert state.last_supervisor_event_sequence == 0
    assert state.inference_sample_count == 1
    assert state.completion_kind is None


def test_completed_state_rejects_unseen_events_but_allows_replay() -> None:
    completion_event: dict[str, object] = {
        "type": "turn.completed",
        "usage": _usage(10),
    }
    state = observe_execution_event(initial_execution_budget_state(), completion_event)

    replayed = _observe_execution_event(state, _normalized_event(0, completion_event))
    with pytest.raises(ExecutionBudgetLifecycleError, match="terminal"):
        _observe_execution_event(
            state,
            _normalized_event(
                1,
                {"type": "custom_tool_call", "name": "read_file", "input": "{}"},
            ),
        )

    assert replayed == state
    assert state.tool_call_count == 0
    assert state.last_supervisor_event_sequence == 0


def test_consecutive_turns_use_fresh_counters_and_carried_baseline_cursor() -> None:
    first_turn = initial_execution_budget_state(
        starting_after_supervisor_event_sequence=179,
    )
    first_turn = observe_execution_event(
        first_turn,
        {"type": "custom_tool_call", "name": "read_file", "input": "{}"},
    )
    first_turn = observe_execution_event(first_turn, _inference(_usage(100)))
    first_turn = observe_execution_event(
        first_turn,
        {"type": "event_msg", "payload": {"type": "task_complete"}},
    )
    assert first_turn.completion_kind == "task_complete"
    assert first_turn.tool_call_count == 1
    assert first_turn.inference_sample_count == 1

    baseline = first_turn.latest_authoritative_cumulative_usage
    assert baseline is not None
    second_turn = initial_execution_budget_state(
        baseline_cumulative_usage=baseline,
        starting_after_supervisor_event_sequence=first_turn.last_supervisor_event_sequence,
    )

    assert second_turn.last_supervisor_event_sequence == 182
    assert second_turn.tool_call_count == 0
    assert second_turn.inference_sample_count == 0
    assert second_turn.reached_hard_limits == ()
    assert second_turn.completion_kind is None

    second_turn = observe_execution_event(second_turn, {"type": "compacted"})
    second_turn = observe_execution_event(second_turn, _inference(_usage(110)))
    assert second_turn.compaction_count == 1
    assert second_turn.tool_call_count == 0
    assert second_turn.inference_sample_count == 1
    assert second_turn.current_turn_usage_delta == CodexTurnUsageV1.model_validate(_usage(10))


def test_every_default_hard_ceiling_reports_reached_at_boundary() -> None:
    state = initial_execution_budget_state()
    for index in range(64):
        state = observe_execution_event(state, _inference(_usage(index + 1)))
    for _ in range(56):
        state = observe_execution_event(
            state,
            {"type": "custom_tool_call", "name": "read_file", "input": "{}"},
        )
    for _ in range(8):
        state = observe_execution_event(
            state,
            {
                "type": "custom_tool_call",
                "name": "exec",
                "input": (
                    'const patch = "*** Begin Patch\\n*** End Patch"; '
                    "text(await tools.apply_patch(patch));"
                ),
            },
        )
    for _ in range(3):
        state = observe_execution_event(state, {"type": "compacted"})
    state = observe_execution_event(state, _inference(_usage(3_000_000)))

    assert state.inference_sample_count == 65
    assert state.tool_call_count == 64
    assert state.patch_call_count == 8
    assert state.compaction_count == 3
    assert state.reached_hard_limits == (
        "max_inference_samples",
        "max_tool_calls",
        "max_patch_calls",
        "max_compactions",
        "max_input_token_delta",
    )


@pytest.mark.parametrize(
    ("policy", "events", "reason"),
    [
        (
            ExecutionBudgetPolicyV1(max_inference_samples=1),
            [_inference(_usage(1))],
            "max_inference_samples",
        ),
        (
            ExecutionBudgetPolicyV1(max_tool_calls=1),
            [{"type": "custom_tool_call", "name": "read_file", "input": "{}"}],
            "max_tool_calls",
        ),
        (
            ExecutionBudgetPolicyV1(max_patch_calls=1),
            [
                {
                    "type": "custom_tool_call",
                    "name": "exec",
                    "input": (
                        'const patch = "*** Begin Patch\\n*** End Patch"; '
                        "text(await tools.apply_patch(patch));"
                    ),
                }
            ],
            "max_patch_calls",
        ),
        (
            ExecutionBudgetPolicyV1(max_compactions=1),
            [{"type": "compacted"}],
            "max_compactions",
        ),
        (
            ExecutionBudgetPolicyV1(max_input_token_delta=1),
            [_inference(_usage(1))],
            "max_input_token_delta",
        ),
    ],
)
def test_each_hard_ceiling_is_reached_at_its_exact_boundary(
    policy: ExecutionBudgetPolicyV1,
    events: list[dict[str, object]],
    reason: str,
) -> None:
    state = initial_execution_budget_state(policy=policy)
    for event in events:
        state = observe_execution_event(state, event)

    assert reason in state.reached_hard_limits


def test_token_crossing_preserves_exact_observed_delta() -> None:
    state = observe_execution_event(
        initial_execution_budget_state(),
        _inference(_usage(3_100_007, output_tokens=9)),
    )

    assert state.current_turn_usage_delta is not None
    assert state.current_turn_usage_delta.input_tokens == 3_100_007
    assert state.current_turn_combined_tokens == 3_100_016
    assert state.reached_hard_limits == ("max_input_token_delta",)


def test_partial_checkpoint_has_deterministic_round_trip_and_is_not_a_receipt(
    tmp_path: Path,
) -> None:
    state = observe_execution_event(
        initial_execution_budget_state(),
        _inference(_usage(25, cached_input_tokens=20, output_tokens=4)),
    )
    checkpoint = partial_execution_budget_checkpoint(
        task_id="task-1",
        codex_thread_id="thread-1",
        state=state,
    )
    path = tmp_path / "partial-execution-budget.json"
    write_partial_execution_budget_checkpoint(path, checkpoint)
    first_bytes = path.read_bytes()

    loaded = load_partial_execution_budget_checkpoint(path)
    write_partial_execution_budget_checkpoint(path, loaded)

    assert loaded == checkpoint
    assert loaded.state.last_supervisor_event_sequence == 0
    assert path.read_bytes() == first_bytes
    assert loaded.checkpoint_kind == "partial_execution_budget"
    with pytest.raises(ValidationError):
        CodexUsageReceiptV1.model_validate(loaded.model_dump(mode="json"))
