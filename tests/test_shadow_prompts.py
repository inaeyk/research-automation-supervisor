from __future__ import annotations

from pathlib import Path

from research_automation_supervisor.shadow_prompts import (
    SHADOW_INSTRUCTION,
    build_blind_supervisor_prompt,
)
from research_automation_supervisor.shadow_sources import (
    load_shadow_specification,
)
from tests.shadow_helpers import create_shadow_tree


def test_blind_prompt_contains_frozen_allowed_inputs_but_no_authoritative_prompt(
    tmp_path: Path,
) -> None:
    spec, _, _, _ = create_shadow_tree(tmp_path)
    prepared = load_shadow_specification(spec)
    decision = prepared.source.decisions[0]

    rendered = build_blind_supervisor_prompt(prepared, decision)

    assert rendered.content.startswith(prepared.policy.content)
    assert prepared.contexts[0].content in rendered.content
    assert prepared.source.prepared.contract.content in rendered.content
    assert SHADOW_INSTRUCTION in rendered.content
    assert decision.authoritative_source is not None
    assert decision.authoritative_source.content not in rendered.content
    assert rendered.manifest.authoritative_sentinel_absent
    assert rendered.manifest.shadow_only
    assert rendered.manifest.automatic_send_disabled
