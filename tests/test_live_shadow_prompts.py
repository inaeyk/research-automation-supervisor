from __future__ import annotations

import json
from pathlib import Path

from research_automation_supervisor.live_shadow_prompts import (
    LIVE_SHADOW_INSTRUCTION,
)
from research_automation_supervisor.live_shadow_sources import (
    load_live_shadow_specification,
)
from research_automation_supervisor.shadow_models import (
    DeterministicAssessment,
    HumanReview,
    RequiredCheckCoverage,
)
from research_automation_supervisor.shadow_review import review_evaluation
from tests.live_shadow_helpers import create_live_shadow_tree


def test_fixed_live_instruction_freezes_quarantine_and_metadata_semantics() -> None:
    assert b"live shadow observation" in LIVE_SHADOW_INSTRUCTION
    assert b"You are the shadow supervisor" in LIVE_SHADOW_INSTRUCTION
    assert b"supervisor-only isolation restrictions" in LIVE_SHADOW_INSTRUCTION
    assert b"candidate prompt is for the authoritative downstream" in (
        LIVE_SHADOW_INSTRUCTION
    )
    assert b"Do not transfer" in LIVE_SHADOW_INSTRUCTION
    assert b"inspect the authoritative workspace" in LIVE_SHADOW_INSTRUCTION
    assert b"modify only allowed_paths" in LIVE_SHADOW_INSTRUCTION
    assert b"run every exact acceptance-test command" in LIVE_SHADOW_INSTRUCTION
    assert b"complete diff" in LIVE_SHADOW_INSTRUCTION
    assert b"additional read-only checks" in LIVE_SHADOW_INSTRUCTION
    assert b"findings or PASS" in LIVE_SHADOW_INSTRUCTION
    assert b"auditor must never edit the workspace" in LIVE_SHADOW_INSTRUCTION
    assert b"recorded result is context" in LIVE_SHADOW_INSTRUCTION
    assert b"will not be sent automatically" in LIVE_SHADOW_INSTRUCTION
    assert b"proceeds independently" in LIVE_SHADOW_INSTRUCTION
    assert b"do not inspect, execute against" in LIVE_SHADOW_INSTRUCTION
    assert b"normalized workspace-relative POSIX paths" in LIVE_SHADOW_INSTRUCTION
    assert b"exact Stage 2 acceptance-test IDs" in LIVE_SHADOW_INSTRUCTION


def test_source_summary_excludes_authoritative_prompt_locators(tmp_path: Path) -> None:
    spec, _, _, _, _ = create_live_shadow_tree(tmp_path)
    prepared = load_live_shadow_specification(spec)
    rendered = str(prepared.blind_source_summary())
    assert "worker_initial_prompt_path" not in rendered
    assert "worker_repair_prompt_path" not in rendered
    assert "auditor_prompt_path" not in rendered


def test_real_byte_size_role_confusion_fixture_uses_human_quality_boundary() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "stage4_byte_size_role_confusion.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    former = fixture["former_candidate"]
    corrected = fixture["corrected_candidate"]
    command = fixture["acceptance_command"]

    assert fixture["former_classification"] == "role_confused"
    assert "without inspecting the live repository" in former.lower()
    assert "recorded passing result" in former.lower()
    assert "inspect the authoritative workspace" in corrected.lower()
    assert "complete git diff" in corrected.lower()
    assert f"`{command}`" in corrected
    assert "independently" in corrected.lower()
    assert "do not edit the workspace" in corrected.lower()
    for restriction in (
        "use only the supplied evidence",
        "use only the frozen evidence",
        "without inspecting the live repository",
        "do not inspect the live repository",
        "do not request or perform execution",
        "rely on the recorded passing test",
    ):
        assert restriction not in corrected.lower()

    assessment = _acceptable_auditor_assessment()
    former_review = HumanReview.model_validate(fixture["former_review"])
    corrected_review = HumanReview.model_validate(fixture["corrected_review"])
    assert review_evaluation(former_review, assessment).acceptable is False
    assert review_evaluation(corrected_review, assessment).acceptable is True


def _acceptable_auditor_assessment() -> DeterministicAssessment:
    return DeterministicAssessment(
        proposal_id="auditor-r000-a002",
        proposal_kind="auditor",
        schema_integrity=True,
        blind_input_integrity=True,
        session_integrity=True,
        size_compliant=True,
        proposal_byte_count=100,
        change_flags={},
        path_scope_findings=(),
        required_check_coverage=RequiredCheckCoverage(
            required_test_ids=("byte-size-unittest",),
            covered_test_ids=("byte-size-unittest",),
            missing_test_ids=(),
        ),
        disposition="propose",
        disqualified=False,
        disqualification_reasons=(),
        candidate_sha256="1" * 64,
        candidate_byte_count=100,
        authoritative_source_sha256="2" * 64,
        authoritative_source_byte_count=100,
        authoritative_rendered_sha256="3" * 64,
        authoritative_rendered_byte_count=100,
        comparison_available=True,
        review_status="unreviewed",
    )
