from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from research_automation_supervisor.token_accounting import (
    CodexTurnUsageV1,
    CodexUsageBindingV1,
    aggregate_task_receipts,
    load_verified_receipt,
    receipt_from_jsonl,
    write_receipt,
)


def _event(event_type: str, **values: object) -> dict[str, object]:
    return {"type": event_type, **values}


def _usage(
    input_tokens: int,
    cached_input_tokens: int,
    output_tokens: int,
    reasoning_output_tokens: int,
) -> dict[str, int]:
    return {
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_input_tokens,
        "output_tokens": output_tokens,
        "reasoning_output_tokens": reasoning_output_tokens,
    }


def _write_events(path: Path, *events: object) -> None:
    path.write_text(
        "".join(json.dumps(event, sort_keys=True) + "\n" for event in events),
        encoding="utf-8",
    )


def _binding(
    action_id: str,
    role: str = "worker",
    *,
    repair_or_retry: bool = False,
) -> CodexUsageBindingV1:
    return CodexUsageBindingV1(
        campaign_id="campaign-1",
        task_id="task-1",
        action_id=action_id,
        role=role,  # type: ignore[arg-type]
        repair_or_retry=repair_or_retry,
    )


def _receipt(
    tmp_path: Path,
    action_id: str,
    role: str,
    usage: dict[str, int],
    *,
    repair_or_retry: bool = False,
):
    path = tmp_path / f"{action_id}.jsonl"
    _write_events(
        path,
        _event("thread.started", thread_id=f"thread-{action_id}"),
        _event("turn.completed", usage=usage),
    )
    return receipt_from_jsonl(
        path,
        binding=_binding(action_id, role, repair_or_retry=repair_or_retry),
        model="gpt-5.6",
        codex_cli_version="codex-cli 0.147.0",
    )


def test_turn_completed_usage_requires_core_and_accepts_additive_counters() -> None:
    usage = CodexTurnUsageV1.model_validate(
        {
            **_usage(100, 40, 20, 7),
            "cache_write_input_tokens": 9,
            "future_nonnegative_counter": 3,
        }
    )

    assert usage.input_tokens == 100
    assert usage.cached_input_tokens == 40
    assert usage.cache_write_input_tokens == 9
    assert usage.output_tokens == 20
    assert usage.reasoning_output_tokens == 7
    assert usage.model_extra == {"future_nonnegative_counter": 3}

    missing_required = _usage(1, 0, 1, 0)
    del missing_required["reasoning_output_tokens"]
    with pytest.raises(ValidationError):
        CodexTurnUsageV1.model_validate(missing_required)
    with pytest.raises(ValidationError):
        CodexTurnUsageV1.model_validate({**_usage(1, 0, 1, 0), "input_tokens": -1})
    with pytest.raises(ValidationError):
        CodexTurnUsageV1.model_validate({**_usage(1, 0, 1, 0), "output_tokens": 1.0})
    with pytest.raises(ValidationError):
        CodexTurnUsageV1.model_validate(
            {**_usage(1, 0, 1, 0), "future_nonnegative_counter": -1}
        )
    with pytest.raises(ValidationError):
        CodexTurnUsageV1.model_validate(_usage(5, 6, 1, 0))


def test_multiple_completed_turns_preserve_cached_and_reasoning_submetrics(
    tmp_path: Path,
) -> None:
    path = tmp_path / "events.jsonl"
    _write_events(
        path,
        _event("thread.started", thread_id="thread-1"),
        _event(
            "turn.completed",
            usage={**_usage(100, 80, 30, 11), "cache_write_input_tokens": 5},
        ),
        _event(
            "turn.completed",
            usage={**_usage(50, 10, 20, 9), "future_nonnegative_counter": 4},
        ),
    )

    receipt = receipt_from_jsonl(
        path,
        binding=_binding("worker-r000"),
        model="gpt-5.6",
        codex_cli_version="codex-cli 0.147.0",
    )

    assert receipt.complete is True
    assert receipt.completed_turn_count == 2
    assert receipt.input_tokens == 150
    assert receipt.cached_input_tokens == 90
    assert receipt.output_tokens == 50
    assert receipt.reasoning_output_tokens == 20
    assert receipt.combined_tokens == 200


@pytest.mark.parametrize(
    ("lines", "known_malformed", "reason"),
    [
        (
            [
                _event("thread.started", thread_id="thread-1"),
                _event("turn.completed"),
            ],
            0,
            "missing_or_invalid_turn_usage",
        ),
        (
            [
                _event("thread.started", thread_id="thread-1"),
                _event("turn.completed", usage=_usage(1, 0, 1, 0)),
                _event("turn.completed", usage=_usage(1, 0, 1, 0)),
            ],
            0,
            "duplicate_turn_completed_event",
        ),
        (
            [
                _event("thread.started", thread_id="thread-1"),
                _event("turn.completed", usage=_usage(1, 0, 1, 0)),
            ],
            1,
            "malformed_jsonl",
        ),
    ],
)
def test_incomplete_event_evidence_fails_closed(
    tmp_path: Path,
    lines: list[dict[str, object]],
    known_malformed: int,
    reason: str,
) -> None:
    path = tmp_path / "events.jsonl"
    _write_events(path, *lines)

    receipt = receipt_from_jsonl(
        path,
        binding=_binding("worker-r000"),
        model="gpt-5.6",
        codex_cli_version="codex-cli 0.147.0",
        known_malformed_event_count=known_malformed,
    )

    assert receipt.complete is False
    assert reason in receipt.incomplete_reasons
    assert receipt.completed_turn_count == 0
    assert receipt.combined_tokens == 0


def test_malformed_retained_jsonl_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_bytes(
        b'{"type":"thread.started","thread_id":"thread-1"}\n'
        b"not-json\n"
        b'{"type":"turn.completed","usage":'
        b'{"input_tokens":1,"cached_input_tokens":0,"output_tokens":2,'
        b'"reasoning_output_tokens":1}}\n'
    )

    receipt = receipt_from_jsonl(
        path,
        binding=_binding("worker-r000"),
        model="gpt-5.6",
        codex_cli_version="codex-cli 0.147.0",
    )

    assert receipt.complete is False
    assert receipt.incomplete_reasons == ("malformed_jsonl",)
    assert receipt.input_tokens == receipt.output_tokens == receipt.combined_tokens == 0


def test_recovery_reuses_verified_receipt_without_double_counting(tmp_path: Path) -> None:
    events = tmp_path / "events.jsonl"
    receipt_path = tmp_path / "usage-receipt.json"
    _write_events(
        events,
        _event("thread.started", thread_id="thread-1"),
        _event("turn.completed", usage=_usage(20, 15, 8, 3)),
    )
    original = receipt_from_jsonl(
        events,
        binding=_binding("worker-r001", repair_or_retry=True),
        model="gpt-5.6",
        codex_cli_version="codex-cli 0.147.0",
    )
    write_receipt(receipt_path, original)

    recovered = load_verified_receipt(receipt_path, event_log=events)
    ledger = aggregate_task_receipts(
        [original, recovered], campaign_id="campaign-1", task_id="task-1"
    )

    assert recovered == original
    assert ledger.receipt_ids == (original.receipt_id,)
    assert ledger.total_session_count == 1
    assert ledger.total_combined_tokens == 28
    assert ledger.repairs_retries.combined_tokens == 28


def test_worker_auditor_and_retry_aggregation_has_exact_final_total(tmp_path: Path) -> None:
    worker = _receipt(tmp_path, "worker-r000", "worker", _usage(100, 60, 20, 8))
    repair = _receipt(
        tmp_path,
        "worker-r001",
        "worker",
        _usage(40, 30, 10, 4),
        repair_or_retry=True,
    )
    coding = _receipt(
        tmp_path, "auditor-r001", "coding_auditor", _usage(70, 20, 15, 6)
    )
    physics = _receipt(
        tmp_path, "physics-r001", "physics_auditor", _usage(90, 50, 25, 12)
    )
    supervisor = _receipt(
        tmp_path, "supervisor-r001", "supervisor", _usage(30, 0, 5, 2)
    )

    ledger = aggregate_task_receipts(
        [worker, repair, coding, physics, supervisor],
        campaign_id="campaign-1",
        task_id="task-1",
    )

    assert ledger.worker.model_dump() == {
        "input_tokens": 140,
        "cached_input_tokens": 90,
        "output_tokens": 30,
        "reasoning_output_tokens": 12,
        "combined_tokens": 170,
        "turn_count": 2,
        "session_count": 2,
    }
    assert ledger.coding_auditor.combined_tokens == 85
    assert ledger.physics_auditor.combined_tokens == 115
    assert ledger.repairs_retries.combined_tokens == 50
    assert ledger.other_model_sessions.combined_tokens == 35
    assert ledger.total_input_tokens == 330
    assert ledger.total_cached_input_tokens == 160
    assert ledger.total_output_tokens == 75
    assert ledger.total_reasoning_output_tokens == 32
    assert ledger.total_combined_tokens == 405


def test_conflicting_receipts_for_one_action_are_rejected(tmp_path: Path) -> None:
    first = _receipt(tmp_path, "worker-r000", "worker", _usage(10, 0, 2, 1))
    second_path = tmp_path / "second.jsonl"
    _write_events(
        second_path,
        _event("thread.started", thread_id="thread-other"),
        _event("turn.completed", usage=_usage(11, 0, 2, 1)),
    )
    second = receipt_from_jsonl(
        second_path,
        binding=_binding("worker-r000"),
        model="gpt-5.6",
        codex_cli_version="codex-cli 0.147.0",
    )

    with pytest.raises(ValueError, match="conflicting usage receipts"):
        aggregate_task_receipts(
            [first, second], campaign_id="campaign-1", task_id="task-1"
        )


def test_campaign_aggregate_includes_distinct_tasks_once(tmp_path: Path) -> None:
    first = _receipt(tmp_path, "worker-r000", "worker", _usage(10, 5, 2, 1))
    second_path = tmp_path / "task-2.jsonl"
    _write_events(
        second_path,
        _event("thread.started", thread_id="thread-task-2"),
        _event("turn.completed", usage=_usage(20, 10, 3, 2)),
    )
    second_binding = CodexUsageBindingV1(
        campaign_id="campaign-1",
        task_id="task-2",
        action_id="worker-r000",
        role="worker",
    )
    second = receipt_from_jsonl(
        second_path,
        binding=second_binding,
        model="gpt-5.6",
        codex_cli_version="codex-cli 0.147.0",
    )

    ledger = aggregate_task_receipts(
        [first, first, second], campaign_id="campaign-1", task_id="*"
    )

    assert ledger.task_id == "*"
    assert ledger.total_session_count == 2
    assert ledger.total_input_tokens == 30
    assert ledger.total_output_tokens == 5
    assert ledger.total_combined_tokens == 35
