from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from research_automation_supervisor.codex_usage import (
    TaskTokenLedgerV1,
    build_task_token_ledger,
    parse_codex_usage_jsonl,
    update_task_token_ledger,
)


def event(usage: dict[str, int] | None = None) -> dict[str, object]:
    value: dict[str, object] = {"type": "turn.completed"}
    if usage is not None:
        value["usage"] = usage
    return value


def usage(
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


def write_events(path: Path, values: list[object]) -> Path:
    path.write_bytes(
        b"".join(
            json.dumps(value, separators=(",", ":"), sort_keys=True).encode("ascii") + b"\n"
            if not isinstance(value, bytes)
            else value + b"\n"
            for value in values
        )
    )
    return path


def receipt(
    path: Path,
    *,
    action_id: str = "worker-r000",
    role: str = "worker",
    action_kind: str = "initial",
    resumed_thread_id: str | None = None,
):
    return parse_codex_usage_jsonl(
        path,
        campaign_task_id="campaign-task",
        role=role,  # type: ignore[arg-type]
        action_kind=action_kind,  # type: ignore[arg-type]
        action_id=action_id,
        model="gpt-5.6-sol",
        codex_cli_version="codex-cli 1.2.3",
        resumed_thread_id=resumed_thread_id,
        disposition="raw",
    )


def test_documented_schema_multiple_turns_cached_reasoning_and_exact_total(
    tmp_path: Path,
) -> None:
    path = write_events(
        tmp_path / "events.jsonl",
        [
            {"type": "thread.started", "thread_id": "thread-1"},
            event(usage(100, 40, 12, 7)),
            event(usage(60, 15, 8, 3)),
        ],
    )

    parsed = receipt(path)

    assert parsed.complete
    assert parsed.completed_turn_count == parsed.usage_event_count == 2
    assert parsed.input_tokens == 160
    assert parsed.cached_input_tokens == 55
    assert parsed.output_tokens == 20
    assert parsed.reasoning_output_tokens == 10
    assert parsed.combined_tokens == 180


def test_resume_requires_the_same_runtime_thread(tmp_path: Path) -> None:
    path = write_events(
        tmp_path / "resume.jsonl",
        [
            {"type": "thread.started", "thread_id": "thread-1"},
            event(usage(5, 2, 3, 1)),
        ],
    )

    assert receipt(path, resumed_thread_id="thread-1").complete
    mismatch = receipt(path, resumed_thread_id="thread-2")
    assert not mismatch.complete
    assert "resumed_thread_id_mismatch" in mismatch.incomplete_reasons


@pytest.mark.parametrize(
    ("values", "reason"),
    [
        (
            [
                {"type": "thread.started", "thread_id": "thread-1"},
                event(usage(5, 1, 2, 1)),
                event(usage(5, 1, 2, 1)),
            ],
            "duplicate_turn_completed_event",
        ),
        (
            [{"type": "thread.started", "thread_id": "thread-1"}, event()],
            "completed_turn_missing_or_invalid_usage",
        ),
        ([b"{not-json"], "malformed_jsonl"),
    ],
)
def test_duplicate_missing_usage_and_malformed_jsonl_fail_closed(
    tmp_path: Path,
    values: list[object],
    reason: str,
) -> None:
    parsed = receipt(write_events(tmp_path / "bad.jsonl", values))
    assert not parsed.complete
    assert reason in parsed.incomplete_reasons


def test_recovery_reuses_verified_receipt_without_double_counting(tmp_path: Path) -> None:
    parsed = receipt(
        write_events(
            tmp_path / "events.jsonl",
            [
                {"type": "thread.started", "thread_id": "thread-1"},
                event(usage(10, 4, 6, 2)),
            ],
        )
    )
    ledger_path = tmp_path / "ledger.json"

    first = update_task_token_ledger(ledger_path, parsed)
    recovered = update_task_token_ledger(ledger_path, parsed)

    assert recovered == first
    assert recovered.total.session_count == 1
    assert recovered.total.combined_tokens == 16


def test_worker_auditor_repairs_and_other_sessions_aggregate_exclusively(
    tmp_path: Path,
) -> None:
    roles = [
        ("worker", "worker-r000", "initial", usage(10, 2, 3, 1)),
        ("coding_auditor", "audit-r000", "initial", usage(20, 5, 4, 2)),
        ("physics_auditor", "physics-r000", "initial", usage(30, 7, 5, 3)),
        ("worker", "worker-r001", "repair_retry", usage(40, 11, 6, 4)),
        (
            "other_model_session",
            "supervisor-r000",
            "initial",
            usage(50, 13, 7, 5),
        ),
    ]
    receipts = []
    for index, (role, action_id, action_kind, counters) in enumerate(roles):
        receipts.append(
            receipt(
                write_events(
                    tmp_path / f"events-{index}.jsonl",
                    [
                        {"type": "thread.started", "thread_id": f"thread-{index}"},
                        event(counters),
                    ],
                ),
                action_id=action_id,
                role=role,
                action_kind=action_kind,
            )
        )

    ledger = build_task_token_ledger("campaign-task", receipts)

    assert ledger.worker.combined_tokens == 59
    assert ledger.coding_auditor.combined_tokens == 24
    assert ledger.physics_auditor.combined_tokens == 35
    assert ledger.repairs_retries.combined_tokens == 46
    assert ledger.other_model_sessions.combined_tokens == 57
    assert ledger.total.input_tokens == 150
    assert ledger.total.cached_input_tokens == 38
    assert ledger.total.output_tokens == 25
    assert ledger.total.reasoning_output_tokens == 15
    assert ledger.total.combined_tokens == 175


def test_strict_ledger_rejects_tampered_aggregate(tmp_path: Path) -> None:
    parsed = receipt(
        write_events(
            tmp_path / "events.jsonl",
            [
                {"type": "thread.started", "thread_id": "thread-1"},
                event(usage(10, 4, 6, 2)),
            ],
        )
    )
    value = build_task_token_ledger("campaign-task", [parsed]).model_dump()
    value["total"]["session_count"] = 99

    with pytest.raises(ValidationError, match="task token ledger"):
        TaskTokenLedgerV1.model_validate(value)
