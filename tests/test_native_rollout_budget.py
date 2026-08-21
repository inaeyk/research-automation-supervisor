from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

import research_automation_supervisor.execution_budget_enforcement as enforcement_module
import research_automation_supervisor.native_rollout_budget as observer_module
from research_automation_supervisor.execution_budget import ExecutionBudgetPolicyV1
from research_automation_supervisor.execution_budget_enforcement import (
    LiveExecutionBudgetControllerV1,
)
from research_automation_supervisor.native_rollout_budget import (
    NativeRolloutBudgetObserverCursorError,
    NativeRolloutBudgetObserverV1,
    NativeRolloutSourceCursorV1,
)

THREAD_ID = "01a00000-0000-7000-8000-000000000001"


def _jsonl(event: dict[str, object]) -> bytes:
    return json.dumps(event, separators=(",", ":"), sort_keys=True).encode() + b"\n"


def _session_meta(thread_id: str = THREAD_ID) -> dict[str, object]:
    return {"type": "session_meta", "payload": {"id": thread_id}}


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


def _task_complete() -> dict[str, object]:
    return {"type": "event_msg", "payload": {"type": "task_complete"}}


def _custom_tool(input_value: str = "tools.exec_command({cmd: 'pwd'})") -> dict[str, object]:
    return {
        "type": "response_item",
        "payload": {"type": "custom_tool_call", "name": "exec", "input": input_value},
    }


def _patch_tool() -> dict[str, object]:
    return _custom_tool(
        'const patch = "*** Begin Patch\\n*** End Patch"; tools.apply_patch(patch)'
    )


def _function_call() -> dict[str, object]:
    return {
        "type": "response_item",
        "payload": {"type": "function_call", "name": "read_file", "arguments": "{}"},
    }


def _policy(**changes: int) -> ExecutionBudgetPolicyV1:
    values = {
        "max_inference_samples": 100,
        "max_tool_calls": 100,
        "max_patch_calls": 100,
        "max_compactions": 100,
        "max_input_token_delta": 100_000_000,
        **changes,
    }
    return ExecutionBudgetPolicyV1(**values)


def _source_path(tmp_path: Path, thread_id: str = THREAD_ID) -> Path:
    path = (
        tmp_path
        / "sessions"
        / "2026"
        / "08"
        / "20"
        / f"rollout-2026-08-20T11-20-30-{thread_id}.jsonl"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _append(path: Path, *events: dict[str, object]) -> None:
    with path.open("ab") as handle:
        for event in events:
            handle.write(_jsonl(event))


def _controller(
    tmp_path: Path,
    *,
    policy: ExecutionBudgetPolicyV1 | None = None,
    bound: bool = True,
    checkpoint_name: str = "budget.json",
    task_id: str = "task-1",
    thread_id: str = THREAD_ID,
) -> LiveExecutionBudgetControllerV1:
    return LiveExecutionBudgetControllerV1.start_new_turn(
        checkpoint_path=tmp_path / checkpoint_name,
        normalized_event_directory=tmp_path / "normalized-events",
        task_id=task_id,
        codex_thread_id=thread_id if bound else None,
        policy=policy or _policy(),
    )


def _observer(
    tmp_path: Path,
    controller: LiveExecutionBudgetControllerV1,
    *,
    require_cursor: bool = False,
) -> NativeRolloutBudgetObserverV1:
    return NativeRolloutBudgetObserverV1.create(
        controller=controller,
        sessions_root=tmp_path / "sessions",
        source_cursor_directory=tmp_path / "native-source-cursors",
        require_existing_source_cursor=require_cursor,
    )


def test_complete_records_advance_cursor_and_partial_line_waits(tmp_path: Path) -> None:
    source = _source_path(tmp_path)
    first = _jsonl(_session_meta())
    token = _jsonl(_token_count(10))
    source.write_bytes(first + token[:-1])
    observer = _observer(tmp_path, _controller(tmp_path))

    observer.poll()

    assert observer.cursor is not None
    assert observer.cursor.consumed_byte_offset == len(first)
    assert observer.cursor.consumed_record_count == 1
    assert observer.controller.checkpoint.state.inference_sample_count == 0

    with source.open("ab") as handle:
        handle.write(b"\n")
    observer.poll()

    assert observer.cursor.consumed_byte_offset == len(first + token)
    assert observer.controller.checkpoint.state.inference_sample_count == 1


def test_cursor_restores_exact_offset_without_rereading_history(tmp_path: Path) -> None:
    source = _source_path(tmp_path)
    _append(source, _session_meta(), _token_count(10))
    first_controller = _controller(tmp_path)
    first_observer = _observer(tmp_path, first_controller)
    first_observer.poll()
    prior_offset = source.stat().st_size
    prior_cursor_path = first_observer.source_cursor_path

    restored_controller = LiveExecutionBudgetControllerV1.restore(
        checkpoint_path=tmp_path / "budget.json",
        normalized_event_directory=tmp_path / "normalized-events",
    )
    restored_observer = _observer(tmp_path, restored_controller, require_cursor=True)
    assert restored_observer.cursor is not None
    assert restored_observer.cursor.consumed_byte_offset == prior_offset
    assert restored_observer.source_cursor_path == prior_cursor_path

    restored_observer.poll()
    _append(source, _token_count(20))
    restored_observer.poll()

    assert restored_controller.checkpoint.state.inference_sample_count == 2
    assert restored_observer.cursor.consumed_byte_offset == source.stat().st_size


@pytest.mark.parametrize("damage", ["replacement", "truncation"])
def test_restore_rejects_consumed_prefix_damage(tmp_path: Path, damage: str) -> None:
    source = _source_path(tmp_path)
    _append(source, _session_meta(), _token_count(10))
    observer = _observer(tmp_path, _controller(tmp_path))
    observer.poll()
    original = source.read_bytes()
    if damage == "replacement":
        damaged = original.replace(b'"input_tokens":10', b'"input_tokens":11')
    else:
        damaged = original[:-1]
    source.write_bytes(damaged)
    restored_controller = LiveExecutionBudgetControllerV1.restore(
        checkpoint_path=tmp_path / "budget.json",
        normalized_event_directory=tmp_path / "normalized-events",
    )

    restored_observer = _observer(tmp_path, restored_controller, require_cursor=True)

    assert restored_observer.outcome.decision == "accounting_integrity_failure"
    assert restored_observer.outcome.integrity_failure_kind == "event_sequence"


def test_session_meta_must_match_bound_thread(tmp_path: Path) -> None:
    source = _source_path(tmp_path)
    _append(source, _session_meta("different-thread"))

    observer = _observer(tmp_path, _controller(tmp_path))
    outcome = observer.poll()

    assert outcome.decision == "accounting_integrity_failure"
    assert outcome.integrity_failure_kind == "identity"


def test_fresh_stdout_identity_binds_once_and_discovers_rollout(tmp_path: Path) -> None:
    source = _source_path(tmp_path)
    _append(source, _session_meta(), _token_count(10))
    controller = _controller(tmp_path, bound=False)
    observer = _observer(tmp_path, controller)

    outcome = observer.bind_thread(THREAD_ID)

    assert outcome.decision == "continue"
    assert controller.checkpoint.codex_thread_id == THREAD_ID
    assert controller.checkpoint.state.inference_sample_count == 1
    mismatch = observer.bind_thread("different-thread")
    assert mismatch.decision == "accounting_integrity_failure"
    assert mismatch.integrity_failure_kind == "identity"


def test_resumed_observer_validates_known_thread_and_source(tmp_path: Path) -> None:
    source = _source_path(tmp_path)
    _append(source, _session_meta(), _token_count(10))
    first = _observer(tmp_path, _controller(tmp_path))
    first.poll()
    restored_controller = LiveExecutionBudgetControllerV1.restore(
        checkpoint_path=tmp_path / "budget.json",
        normalized_event_directory=tmp_path / "normalized-events",
        expected_codex_thread_id=THREAD_ID,
    )
    resumed = _observer(tmp_path, restored_controller, require_cursor=True)

    assert resumed.bind_thread(THREAD_ID).decision == "continue"
    assert resumed.bind_thread("different-thread").decision == "accounting_integrity_failure"


def test_native_event_filter_drives_exact_budget_counters(tmp_path: Path) -> None:
    source = _source_path(tmp_path)
    _append(
        source,
        _session_meta(),
        {"type": "event_msg", "payload": {"type": "context_compacted"}},
        {"type": "item.started", "payload": {"id": "irrelevant"}},
        _token_count(10),
        _custom_tool(),
        _patch_tool(),
        _function_call(),
        {"type": "compacted", "payload": {}},
    )
    observer = _observer(tmp_path, _controller(tmp_path))

    observer.poll()

    state = observer.controller.checkpoint.state
    assert state.inference_sample_count == 1
    assert state.tool_call_count == 3
    assert state.patch_call_count == 1
    assert state.compaction_count == 1
    assert observer.cursor is not None
    assert observer.cursor.consumed_record_count == 8


def test_irrelevant_record_advances_source_not_normalized_sequence(tmp_path: Path) -> None:
    source = _source_path(tmp_path)
    _append(source, _session_meta(), {"type": "item.completed", "payload": {}})
    observer = _observer(tmp_path, _controller(tmp_path))

    observer.poll()

    assert observer.cursor is not None
    assert observer.cursor.consumed_record_count == 2
    assert observer.controller.checkpoint.state.last_supervisor_event_sequence == -1


def test_source_cursor_crash_after_journal_write_reconciles_without_double_count(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source_path(tmp_path)
    _append(source, _session_meta())
    controller = _controller(tmp_path)
    observer = _observer(tmp_path, controller)
    observer.poll()
    assert observer.cursor is not None
    offset_before = observer.cursor.consumed_byte_offset
    _append(source, _token_count(10))
    real_atomic_write = observer_module.atomic_write_json

    def fail_source_cursor(path: Path, *args: Any, **kwargs: Any) -> None:
        if path == observer.source_cursor_path:
            raise NativeRolloutBudgetObserverCursorError("synthetic cursor failure")
        real_atomic_write(path, *args, **kwargs)

    monkeypatch.setattr(observer_module, "atomic_write_json", fail_source_cursor)
    failed = observer.poll()

    assert failed.decision == "accounting_integrity_failure"
    assert observer.source_cursor_path is not None
    durable_cursor = NativeRolloutSourceCursorV1.model_validate_json(
        observer.source_cursor_path.read_bytes()
    )
    assert durable_cursor.consumed_byte_offset == offset_before
    assert controller.checkpoint.state.inference_sample_count == 1

    monkeypatch.setattr(observer_module, "atomic_write_json", real_atomic_write)
    recovered_controller = LiveExecutionBudgetControllerV1.restore(
        checkpoint_path=tmp_path / "budget.json",
        normalized_event_directory=tmp_path / "normalized-events",
    )
    recovered = _observer(tmp_path, recovered_controller, require_cursor=True)
    recovered.poll()

    assert recovered_controller.checkpoint.state.inference_sample_count == 1
    assert recovered.cursor is not None
    assert recovered.cursor.consumed_byte_offset == source.stat().st_size


def test_source_cursor_never_advances_past_nondurable_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source_path(tmp_path)
    _append(source, _session_meta())
    observer = _observer(tmp_path, _controller(tmp_path))
    observer.poll()
    assert observer.cursor is not None
    offset_before = observer.cursor.consumed_byte_offset
    _append(source, _token_count(10))

    def fail_checkpoint(*args: Any, **kwargs: Any) -> None:
        raise OSError("synthetic checkpoint failure")

    monkeypatch.setattr(
        enforcement_module,
        "write_partial_execution_budget_checkpoint",
        fail_checkpoint,
    )
    failed = observer.poll()

    assert failed.decision == "accounting_integrity_failure"
    assert observer.source_cursor_path is not None
    durable_cursor = NativeRolloutSourceCursorV1.model_validate_json(
        observer.source_cursor_path.read_bytes()
    )
    assert durable_cursor.consumed_byte_offset == offset_before


def test_live_poll_observes_appended_data_before_source_is_finished(tmp_path: Path) -> None:
    source = _source_path(tmp_path)
    _append(source, _session_meta())
    observer = _observer(tmp_path, _controller(tmp_path))
    observer.poll()
    assert observer.controller.checkpoint.state.inference_sample_count == 0

    _append(source, _token_count(10))
    observer.poll()

    assert observer.controller.checkpoint.state.inference_sample_count == 1


def test_sixty_fourth_native_sample_stops_and_sample_sixty_five_is_not_admitted(
    tmp_path: Path,
) -> None:
    source = _source_path(tmp_path)
    records = [_jsonl(_session_meta()), *(_jsonl(_token_count(index)) for index in range(1, 66))]
    source.write_bytes(b"".join(records))
    observer = _observer(tmp_path, _controller(tmp_path, policy=_policy(max_inference_samples=64)))

    outcome = observer.poll()

    assert outcome.decision == "bounded_continuation_required"
    assert outcome.checkpoint.state.inference_sample_count == 64
    assert outcome.checkpoint.state.latest_authoritative_cumulative_usage is not None
    assert outcome.checkpoint.state.latest_authoritative_cumulative_usage.input_tokens == 64
    assert outcome.checkpoint.completion_reconciliation_state == "closed"
    assert observer.cursor is not None
    sample_64_offset = sum(len(record) for record in records[:65])
    assert observer.cursor.consumed_byte_offset == sample_64_offset
    assert source.read_bytes()[sample_64_offset:] == records[65]

    repeated = observer.poll()

    assert repeated.checkpoint.state.inference_sample_count == 64
    assert observer.cursor.consumed_byte_offset == sample_64_offset


def test_native_token_crossing_preserves_exact_delta(tmp_path: Path) -> None:
    source = _source_path(tmp_path)
    _append(source, _session_meta(), _token_count(3_000_123, output_tokens=17))
    observer = _observer(
        tmp_path,
        _controller(
            tmp_path,
            policy=_policy(max_input_token_delta=3_000_000),
        ),
    )

    outcome = observer.poll()

    assert outcome.decision == "bounded_continuation_required"
    delta = outcome.checkpoint.state.current_turn_usage_delta
    assert delta is not None
    assert delta.input_tokens == 3_000_123
    assert delta.output_tokens == 17


def test_direct_task_complete_reconciles_token_stop_and_retains_limits(tmp_path: Path) -> None:
    source = _source_path(tmp_path)
    _append(source, _session_meta(), _token_count(11), _task_complete())
    observer = _observer(
        tmp_path,
        _controller(tmp_path, policy=_policy(max_input_token_delta=10)),
    )

    outcome = observer.poll()

    assert outcome.decision == "completed"
    assert outcome.checkpoint.state.completion_kind == "task_complete"
    assert outcome.checkpoint.state.reached_hard_limits == ("max_input_token_delta",)
    assert outcome.checkpoint.completion_reconciliation_state == "closed"
    assert observer.cursor is not None
    assert observer.cursor.consumed_byte_offset == source.stat().st_size


def test_intervening_record_closes_reconciliation_durably_across_recovery(
    tmp_path: Path,
) -> None:
    source = _source_path(tmp_path)
    meta = _jsonl(_session_meta())
    token = _jsonl(_token_count(11))
    intervening = _jsonl({"type": "item.started"})
    source.write_bytes(meta + token + intervening)
    controller = _controller(tmp_path, policy=_policy(max_input_token_delta=10))
    observer = _observer(tmp_path, controller)

    stopped = observer.poll()

    assert stopped.decision == "bounded_continuation_required"
    assert stopped.checkpoint.completion_reconciliation_state == "closed"
    assert observer.cursor is not None
    pinned_offset = len(meta + token)
    assert observer.cursor.consumed_byte_offset == pinned_offset
    restored_controller = LiveExecutionBudgetControllerV1.restore(
        checkpoint_path=tmp_path / "budget.json",
        normalized_event_directory=tmp_path / "normalized-events",
    )
    restored_observer = _observer(tmp_path, restored_controller, require_cursor=True)
    _append(source, _task_complete())
    after = restored_observer.poll()

    assert after.decision == "bounded_continuation_required"
    assert after.checkpoint.state.completion_kind is None
    assert after.checkpoint.completion_reconciliation_state == "closed"
    assert restored_observer.cursor is not None
    assert restored_observer.cursor.consumed_byte_offset == pinned_offset


def test_later_turn_reuses_source_cursor_with_fresh_per_turn_counts(tmp_path: Path) -> None:
    source = _source_path(tmp_path)
    _append(source, _session_meta(), _token_count(10), _task_complete())
    first = _observer(tmp_path, _controller(tmp_path))
    first.poll()
    assert first.cursor is not None
    prior_offset = first.cursor.consumed_byte_offset
    prior_cursor_path = first.source_cursor_path
    prior_checkpoint = first.controller.checkpoint
    assert len(list((tmp_path / "native-source-cursors").glob("*.json"))) == 1

    later_controller = LiveExecutionBudgetControllerV1.start_later_turn(
        previous_checkpoint=prior_checkpoint,
        checkpoint_path=tmp_path / "later-budget.json",
        normalized_event_directory=tmp_path / "normalized-events",
        task_id="task-2",
    )
    later = _observer(tmp_path, later_controller, require_cursor=True)
    assert later.cursor is not None
    assert later.cursor.consumed_byte_offset == prior_offset
    assert later.source_cursor_path == prior_cursor_path
    assert later.controller.checkpoint.task_id == "task-2"
    assert len(list((tmp_path / "native-source-cursors").glob("*.json"))) == 1
    _append(source, {"type": "event_msg", "payload": {"type": "task_started"}})
    _append(source, _token_count(15))

    outcome = later.poll()

    assert outcome.decision == "continue"
    assert outcome.checkpoint.state.inference_sample_count == 1
    assert outcome.checkpoint.state.tool_call_count == 0
    assert outcome.checkpoint.state.baseline_cumulative_usage is not None
    assert outcome.checkpoint.state.baseline_cumulative_usage.input_tokens == 10
    assert outcome.checkpoint.state.current_turn_usage_delta is not None
    assert outcome.checkpoint.state.current_turn_usage_delta.input_tokens == 5


def test_later_turn_consumes_first_record_pinned_by_prior_budget_stop(
    tmp_path: Path,
) -> None:
    source = _source_path(tmp_path)
    records = [_jsonl(_session_meta()), *(_jsonl(_token_count(index)) for index in range(1, 66))]
    source.write_bytes(b"".join(records))
    first = _observer(
        tmp_path,
        _controller(tmp_path, policy=_policy(max_inference_samples=64)),
    )
    stopped = first.poll()
    assert stopped.checkpoint.state.latest_authoritative_cumulative_usage is not None
    pinned_offset = sum(len(record) for record in records[:65])
    assert first.cursor is not None
    assert first.cursor.consumed_byte_offset == pinned_offset

    later_controller = LiveExecutionBudgetControllerV1.start_later_turn(
        previous_checkpoint=stopped.checkpoint,
        checkpoint_path=tmp_path / "later-budget.json",
        normalized_event_directory=tmp_path / "normalized-events",
        task_id="task-2",
    )
    later = _observer(tmp_path, later_controller, require_cursor=True)
    outcome = later.poll()

    assert outcome.decision == "continue"
    assert outcome.checkpoint.state.inference_sample_count == 1
    assert outcome.checkpoint.state.baseline_cumulative_usage is not None
    assert outcome.checkpoint.state.baseline_cumulative_usage.input_tokens == 64
    assert outcome.checkpoint.state.current_turn_usage_delta is not None
    assert outcome.checkpoint.state.current_turn_usage_delta.input_tokens == 1
    assert later.cursor is not None
    assert later.cursor.consumed_byte_offset == source.stat().st_size


def test_resumed_thread_requires_its_own_existing_stream_cursor(tmp_path: Path) -> None:
    controller = _controller(tmp_path)

    observer = _observer(tmp_path, controller, require_cursor=True)

    assert observer.outcome.decision == "accounting_integrity_failure"
    assert observer.outcome.integrity_failure_kind == "event_sequence"


def test_different_thread_cannot_restore_another_threads_cursor(tmp_path: Path) -> None:
    source = _source_path(tmp_path)
    _append(source, _session_meta())
    first = _observer(tmp_path, _controller(tmp_path))
    first.poll()
    assert first.source_cursor_path is not None

    other_thread = "01a00000-0000-7000-8000-000000000002"
    other_controller = _controller(
        tmp_path,
        checkpoint_name="other-budget.json",
        task_id="other-task",
        thread_id=other_thread,
    )
    other = _observer(tmp_path, other_controller)
    assert other.source_cursor_path is not None
    other.source_cursor_path.parent.mkdir(parents=True, exist_ok=True)
    other.source_cursor_path.write_bytes(first.source_cursor_path.read_bytes())

    restored = _observer(tmp_path, other_controller, require_cursor=True)

    assert restored.outcome.decision == "accounting_integrity_failure"
    assert restored.outcome.integrity_failure_kind == "event_sequence"


def test_normalized_journal_event_binds_native_source_record_digest(tmp_path: Path) -> None:
    source = _source_path(tmp_path)
    token_record = _jsonl(_token_count(10))
    source.write_bytes(_jsonl(_session_meta()) + token_record)
    observer = _observer(tmp_path, _controller(tmp_path))

    observer.poll()

    event_path = next((tmp_path / "normalized-events").iterdir())
    event = json.loads(event_path.read_text(encoding="utf-8"))
    metadata = event["native_event"]["supervisor_native_rollout_record_v1"]
    assert metadata["record_sha256"] == hashlib.sha256(token_record).hexdigest()
    assert metadata["rollout_source_identity_sha256"] == (
        observer.cursor.rollout_source_identity_sha256 if observer.cursor else None
    )
