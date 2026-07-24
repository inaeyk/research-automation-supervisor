from __future__ import annotations

from pathlib import Path

from research_automation_supervisor.live_shadow_prompts import (
    LIVE_SHADOW_INSTRUCTION,
)
from research_automation_supervisor.live_shadow_sources import (
    load_live_shadow_specification,
)
from tests.live_shadow_helpers import create_live_shadow_tree


def test_fixed_live_instruction_freezes_quarantine_and_metadata_semantics() -> None:
    assert b"live shadow observation" in LIVE_SHADOW_INSTRUCTION
    assert b"will not be sent automatically" in LIVE_SHADOW_INSTRUCTION
    assert b"proceeds independently" in LIVE_SHADOW_INSTRUCTION
    assert b"Do not inspect" in LIVE_SHADOW_INSTRUCTION
    assert b"normalized workspace-relative POSIX paths" in LIVE_SHADOW_INSTRUCTION
    assert b"exact Stage 2 acceptance-test IDs" in LIVE_SHADOW_INSTRUCTION


def test_source_summary_excludes_authoritative_prompt_locators(tmp_path: Path) -> None:
    spec, _, _, _, _ = create_live_shadow_tree(tmp_path)
    prepared = load_live_shadow_specification(spec)
    rendered = str(prepared.blind_source_summary())
    assert "worker_initial_prompt_path" not in rendered
    assert "worker_repair_prompt_path" not in rendered
    assert "auditor_prompt_path" not in rendered
