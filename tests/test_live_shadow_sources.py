from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from research_automation_supervisor.errors import LiveShadowInputError
from research_automation_supervisor.live_shadow_sources import (
    load_live_shadow_specification,
    proposal_kind_for_intent,
)
from research_automation_supervisor.workflow_integrity import PromptHandoff
from research_automation_supervisor.workflow_models import PendingAction
from tests.live_shadow_helpers import create_live_shadow_tree


def test_validation_loads_stage2_exactly_and_writes_nothing(tmp_path: Path) -> None:
    spec, _, project, _, _ = create_live_shadow_tree(tmp_path)
    before = tuple(sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*")))
    prepared = load_live_shadow_specification(spec)
    after = tuple(sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*")))
    assert prepared.stage2.workspace == project
    assert prepared.blind_source_summary()["substage_id"] == "minimal-substage"
    assert before == after


def test_duplicate_and_unknown_fields_are_rejected(tmp_path: Path) -> None:
    spec, _, _, _, _ = create_live_shadow_tree(tmp_path)
    source = spec.read_text(encoding="utf-8")
    spec.write_text(source + "live_shadow_id: duplicate\n", encoding="utf-8")
    with pytest.raises(LiveShadowInputError, match="duplicate"):
        load_live_shadow_specification(spec)

    value = yaml.safe_load(source)
    value["handoff_enabled"] = True
    spec.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")
    with pytest.raises(LiveShadowInputError, match="handoff_enabled"):
        load_live_shadow_specification(spec)


def test_policy_symlink_is_rejected(tmp_path: Path) -> None:
    spec, _, _, _, _ = create_live_shadow_tree(tmp_path)
    policy = spec.parent / "supervisor-policy.md"
    target = spec.parent / "real-policy.md"
    policy.rename(target)
    policy.symlink_to(target.name)
    with pytest.raises(LiveShadowInputError, match="symbolic-link"):
        load_live_shadow_specification(spec)
    assert json.loads(
        (tmp_path / "stage2/project/.fake-codex.json").read_text(encoding="utf-8")
    )["responses"]


@pytest.mark.parametrize(
    ("kind", "handoff_kind", "trigger", "expected"),
    [
        ("worker", "initial_worker", None, "worker_initial"),
        ("worker", "fixed_test_or_scope_repair", "scope", "worker_scope_repair"),
        ("worker", "fixed_test_or_scope_repair", "test", "worker_test_repair"),
        ("worker", "audit_repair", None, "worker_audit_repair"),
        ("worker", "human_continuation", None, "worker_human_continuation"),
        ("auditor", "auditor", None, "auditor"),
    ],
)
def test_all_live_decision_kinds_are_typed(
    kind: str,
    handoff_kind: str,
    trigger: str | None,
    expected: str,
) -> None:
    pending = PendingAction.model_construct(kind=kind, repair_round=1)
    handoff = PromptHandoff.model_construct(kind=handoff_kind)
    assert proposal_kind_for_intent(
        pending,
        handoff,
        {"repair_trigger": trigger},
    ) == expected
