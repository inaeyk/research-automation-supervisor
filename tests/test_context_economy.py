from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
from pydantic import ValidationError

from research_automation_supervisor.codex_adapter import build_codex_command
from research_automation_supervisor.codex_models import (
    ROLE_POLICIES,
    CodexRunRequest,
    PreparedCodexRequest,
)
from research_automation_supervisor.context_economy import (
    CONTEXT_ECONOMY_PROFILES,
    ContextEconomyOverrideV1,
    context_economy_receipt_from_events,
    handoff_disposition,
    preserve_large_tool_output,
    retrieve_preserved_tool_output,
    tool_cycle_disposition,
)
from research_automation_supervisor.replay_campaign_prompts import material_prompt_delta
from research_automation_supervisor.token_accounting import (
    CodexUsageBindingV1,
    receipt_from_jsonl,
)


def _prepared(
    tmp_path: Path,
    *,
    profile: str = "B4",
    override: object = None,
) -> PreparedCodexRequest:
    prompt = b"Goal\nDo the bounded task.\n"
    request = CodexRunRequest.model_validate(
        {
            "schema_version": 1,
            "run_id": "economy-test",
            "role": "supervisor",
            "workspace": str(tmp_path),
            "prompt_path": str(tmp_path / "prompt.md"),
            "model": "gpt-5.6-sol",
            "reasoning_effort": "high",
            "brevity_profile": profile,
            "context_economy_override": override,
            "timeout_seconds": 30,
        }
    )
    return PreparedCodexRequest(
        request_path=tmp_path / "request.yaml",
        request=request,
        workspace=tmp_path,
        prompt_path=tmp_path / "prompt.md",
        prompt_bytes=prompt,
        prompt_sha256="0" * 64,
        policy=ROLE_POLICIES["supervisor"],
    )


def test_profiles_have_exact_prompt_and_compaction_thresholds() -> None:
    expected = {
        "B5": (300, 48_000),
        "B4": (700, 64_000),
        "B3": (1_200, 80_000),
        "B2": (2_000, 120_000),
        "B1": (3_500, 160_000),
        "B0": (None, None),
    }
    assert {
        name: (profile.supervisor_prompt_target_tokens, profile.model_auto_compact_token_limit)
        for name, profile in CONTEXT_ECONOMY_PROFILES.items()
    } == expected


def test_codex_command_uses_early_compaction_without_faking_context_window(tmp_path: Path) -> None:
    command = build_codex_command(_prepared(tmp_path), "codex", tmp_path / "final.md")
    rendered = " ".join(command)
    assert "model_auto_compact_token_limit=64000" in command
    assert "tool_output_token_limit=2048" in command
    assert "model_context_window" not in rendered

    b0 = build_codex_command(
        _prepared(
            tmp_path,
            profile="B0",
            override={"justification": "Provider default is required for this exceptional run."},
        ),
        "codex",
        tmp_path / "b0.md",
    )
    assert not any("model_auto_compact_token_limit" in item for item in b0)
    assert not any("tool_output_token_limit" in item for item in b0)


def test_b0_requires_durable_justification(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="B0 requires"):
        _prepared(tmp_path, profile="B0")


def test_tool_cycle_budget_has_terminal_non_retrying_exhaustion() -> None:
    assert tool_cycle_disposition("B4", 19).state == "ok"
    assert tool_cycle_disposition("B4", 20).state == "warning"
    assert tool_cycle_disposition("B4", 30).state == "strong_warning"
    exhausted = tool_cycle_disposition("B4", 40)
    assert exhausted.state == "handoff_required"
    assert exhausted.permit_new_tool_call is False
    assert exhausted.retry_rejected_call is False
    assert exhausted.next_action == "compact_handoff_or_stop"
    overridden = tool_cycle_disposition(
        "B4",
        40,
        ContextEconomyOverrideV1(
            justification="A durable test override raises this bounded action budget.",
            tool_call_exhaustion=50,
        ),
    )
    assert overridden.permit_new_tool_call is True


def test_large_output_is_preserved_hashed_bounded_and_failure_faithful(tmp_path: Path) -> None:
    output = "normal line\n" * 2_000 + "FAILED: exact regression marker\n" + "tail\n" * 2_000
    target = tmp_path / "raw" / "command-001.log"
    record = preserve_large_tool_output(
        output,
        destination=target,
        visible_char_limit=8_192,
        exit_code=7,
    )
    assert target.read_text(encoding="utf-8") == output
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert record.failed is True
    assert record.exit_code == 7
    assert record.visible_char_count <= 8_192
    assert "status=FAILED exit_code=7" in record.summary
    assert "FAILED: exact regression marker" in record.summary
    assert retrieve_preserved_tool_output(record, start=0, length=12) == b"normal line\n"


def test_context_receipt_uses_deterministic_fake_events(tmp_path: Path) -> None:
    events = [
        {"type": "thread.started", "thread_id": "thread-1"},
        {
            "type": "item.completed",
            "item": {"type": "command_execution", "aggregated_output": "12345"},
        },
        {"type": "context.compacted"},
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 100,
                "cached_input_tokens": 80,
                "output_tokens": 10,
                "reasoning_output_tokens": 4,
            },
        },
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 300,
                "cached_input_tokens": 250,
                "output_tokens": 20,
                "reasoning_output_tokens": 8,
            },
        },
    ]
    event_log = tmp_path / "events.jsonl"
    event_log.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
    usage = receipt_from_jsonl(
        event_log,
        binding=CodexUsageBindingV1(
            campaign_id="campaign",
            task_id="task",
            action_id="action",
            role="supervisor",
        ),
        model="gpt-5.6-sol",
        codex_cli_version="codex-cli 0.test",
    )
    receipt = context_economy_receipt_from_events(
        event_log,
        prompt_bytes=42,
        profile="B4",
        usage_receipt=usage,
    )
    assert receipt.input_tokens == 400
    assert receipt.cached_input_tokens == 330
    assert receipt.uncached_input_tokens == 70
    assert receipt.output_tokens == 30
    assert receipt.combined_tokens == 430
    assert receipt.inference_token_sample_count is None
    assert receipt.tool_call_count == 1
    assert receipt.model_visible_tool_output_chars == 5
    assert receipt.compaction_count is None
    assert receipt.max_inference_input_tokens is None
    assert receipt.median_inference_input_tokens is None


def test_context_receipt_uses_only_genuine_inference_samples(tmp_path: Path) -> None:
    events = [
        {"type": "thread.started", "thread_id": "thread-1"},
        {"type": "inference.token_count", "input_tokens": 100},
        {"type": "inference.token_count", "input_tokens": 300},
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 900,
                "cached_input_tokens": 700,
                "output_tokens": 40,
                "reasoning_output_tokens": 10,
            },
        },
    ]
    event_log = tmp_path / "events.jsonl"
    event_log.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
    usage = receipt_from_jsonl(
        event_log,
        binding=CodexUsageBindingV1(
            campaign_id="campaign",
            task_id="task",
            action_id="action",
            role="supervisor",
        ),
        model="gpt-5.6-sol",
        codex_cli_version="codex-cli 0.test",
    )
    receipt = context_economy_receipt_from_events(
        event_log,
        prompt_bytes=42,
        profile="B4",
        usage_receipt=usage,
    )
    assert receipt.input_tokens == 900
    assert receipt.inference_token_sample_count == 2
    assert receipt.median_inference_input_tokens == 100
    assert receipt.max_inference_input_tokens == 300
    assert receipt.compaction_count == 0


def test_material_prompt_dedup_uses_path_hash_and_detects_changes() -> None:
    blocks = {"authority/a": {"fixed": [1, 2]}, "authority/b": "unchanged"}
    first_delta, refs, ledger, repeated = material_prompt_delta(blocks, {})
    assert first_delta == blocks
    assert repeated == 0
    assert [item["path"] for item in refs] == ["authority/a", "authority/b"]

    second_delta, second_refs, second_ledger, repeated = material_prompt_delta(blocks, ledger)
    assert second_delta == {}
    assert second_refs == refs
    assert second_ledger == ledger
    assert repeated == 2

    changed = {**blocks, "authority/b": "changed"}
    third_delta, _, _, repeated = material_prompt_delta(changed, ledger)
    assert third_delta == {"authority/b": "changed"}
    assert repeated == 1


def test_handoff_prefers_fresh_session_but_preserves_qualified_recovery_identity() -> None:
    boundary = handoff_disposition("task_boundary")
    assert boundary.start_fresh_session is True
    assert boundary.compact_handoff_required is True
    continued_boundary = handoff_disposition("task_boundary", continue_same_epoch=True)
    assert continued_boundary.start_fresh_session is False
    assert continued_boundary.preserve_original_session_identity is True
    assert continued_boundary.compact_handoff_required is True
    recovery = handoff_disposition("qualified_recovery")
    assert recovery.start_fresh_session is False
    assert recovery.preserve_original_session_identity is True
