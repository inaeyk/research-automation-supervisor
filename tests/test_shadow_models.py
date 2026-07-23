from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from research_automation_supervisor.shadow_models import (
    MAX_PROPOSAL_BYTES,
    HumanReview,
    ShadowSpecification,
    SupervisorProposal,
    canonical_supervisor_uuid,
)
from tests.shadow_helpers import supervisor_proposal


def shadow_specification_values(tmp_path: Path) -> dict[str, object]:
    return {
        "schema_version": 1,
        "calibration_id": "calibration",
        "title": "Calibration",
        "source_stage2_run": str(tmp_path / "source"),
        "supervisor_policy_path": "policy.md",
        "project_context_paths": ["context.md"],
        "supervisor_model": "gpt-5.6-sol",
        "supervisor_reasoning_effort": "high",
        "supervisor_timeout_seconds": 30,
        "max_proposal_bytes": 1024,
        "minimum_reviewed_proposals": 2,
        "required_consecutive_acceptable": 1,
    }


def test_shadow_specification_is_strict_and_thresholds_are_bounded(
    tmp_path: Path,
) -> None:
    values = shadow_specification_values(tmp_path)
    specification = ShadowSpecification.model_validate(values)
    assert specification.project_context_paths == ("context.md",)

    invalid = dict(values)
    invalid["unknown"] = True
    with pytest.raises(ValidationError):
        ShadowSpecification.model_validate(invalid)

    invalid = dict(values)
    invalid["required_consecutive_acceptable"] = 3
    with pytest.raises(ValidationError):
        ShadowSpecification.model_validate(invalid)


def test_supervisor_proposal_requires_exact_disposition_and_normalized_paths() -> None:
    proposal = SupervisorProposal.model_validate_json(
        supervisor_proposal("worker_initial")
    )
    assert proposal.referenced_paths == ("src/output.txt",)

    value = proposal.model_dump(mode="json")
    value["disposition"] = "recommend_human_pause"
    value["prompt"] = None
    value["questions"] = []
    with pytest.raises(ValidationError):
        SupervisorProposal.model_validate(value)

    value["questions"] = ["Which interpretation is authoritative?"]
    paused = SupervisorProposal.model_validate(value)
    assert paused.prompt is None


def test_unsafe_human_review_requires_a_blocking_issue() -> None:
    value = {
        "schema_version": 1,
        "proposal_id": "worker_initial-r000-a001",
        "verdict": "unsafe",
        "objective_fidelity": 5,
        "scope_discipline": 5,
        "technical_completeness": 5,
        "evidence_use": 5,
        "actionability": 5,
        "concision": 5,
        "unsupported_assumptions": [],
        "blocking_issues": [],
        "notes": "review",
    }
    with pytest.raises(ValidationError):
        HumanReview.model_validate(value)
    value["blocking_issues"] = ["unsafe behavior"]
    assert HumanReview.model_validate(value).verdict == "unsafe"


@pytest.mark.parametrize(
    ("field", "minimum", "maximum"),
    [
        ("supervisor_timeout_seconds", 30, 14_400),
        ("max_proposal_bytes", 1024, 2 * 1024 * 1024),
        ("minimum_reviewed_proposals", 1, 100),
    ],
)
def test_shadow_specification_numeric_boundaries(
    tmp_path: Path,
    field: str,
    minimum: int,
    maximum: int,
) -> None:
    values = shadow_specification_values(tmp_path)
    if field == "minimum_reviewed_proposals":
        values["required_consecutive_acceptable"] = 1
    for accepted in (minimum, maximum):
        candidate = dict(values)
        candidate[field] = accepted
        ShadowSpecification.model_validate(candidate)
    for rejected in (minimum - 1, maximum + 1):
        candidate = dict(values)
        candidate[field] = rejected
        with pytest.raises(ValidationError):
            ShadowSpecification.model_validate(candidate)


def test_consecutive_threshold_boundaries_and_reasoning_enum(
    tmp_path: Path,
) -> None:
    values = shadow_specification_values(tmp_path)
    values["minimum_reviewed_proposals"] = 100
    for accepted in (1, 100):
        values["required_consecutive_acceptable"] = accepted
        ShadowSpecification.model_validate(values)
    for rejected in (0, 101):
        values["required_consecutive_acceptable"] = rejected
        with pytest.raises(ValidationError):
            ShadowSpecification.model_validate(values)
    values["required_consecutive_acceptable"] = 1
    for effort in ("low", "medium", "high", "xhigh"):
        values["supervisor_reasoning_effort"] = effort
        ShadowSpecification.model_validate(values)
    values["supervisor_reasoning_effort"] = "ultra"
    with pytest.raises(ValidationError):
        ShadowSpecification.model_validate(values)


def test_every_supervisor_schema_field_is_required_and_unknowns_are_forbidden() -> None:
    proposal = SupervisorProposal.model_validate_json(
        supervisor_proposal("worker_initial")
    ).model_dump(mode="json")
    for field in tuple(proposal):
        candidate = dict(proposal)
        candidate.pop(field)
        with pytest.raises(ValidationError):
            SupervisorProposal.model_validate(candidate)
    proposal["unknown"] = True
    with pytest.raises(ValidationError):
        SupervisorProposal.model_validate(proposal)


@pytest.mark.parametrize(
    ("disposition", "prompt", "questions", "valid"),
    [
        ("propose", "candidate", [], True),
        ("propose", None, [], False),
        ("recommend_human_pause", None, ["Need evidence?"], True),
        ("recommend_human_pause", "candidate", ["Need evidence?"], False),
        ("recommend_human_pause", None, [], False),
    ],
)
def test_proposal_disposition_consistency_matrix(
    disposition: str,
    prompt: str | None,
    questions: list[str],
    valid: bool,
) -> None:
    proposal = SupervisorProposal.model_validate_json(
        supervisor_proposal("worker_initial")
    ).model_dump(mode="json")
    proposal.update(
        {
            "disposition": disposition,
            "prompt": prompt,
            "questions": questions,
        }
    )
    if valid:
        SupervisorProposal.model_validate(proposal)
    else:
        with pytest.raises(ValidationError):
            SupervisorProposal.model_validate(proposal)


def test_proposal_prompt_global_utf8_byte_boundary() -> None:
    proposal = SupervisorProposal.model_validate_json(
        supervisor_proposal("worker_initial")
    ).model_dump(mode="json")
    proposal["prompt"] = "x" * MAX_PROPOSAL_BYTES
    SupervisorProposal.model_validate(proposal)
    proposal["prompt"] = "x" * (MAX_PROPOSAL_BYTES + 1)
    with pytest.raises(ValidationError):
        SupervisorProposal.model_validate(proposal)
    proposal["prompt"] = "é" * (MAX_PROPOSAL_BYTES // 2 + 1)
    with pytest.raises(ValidationError):
        SupervisorProposal.model_validate(proposal)


@pytest.mark.parametrize(
    "value",
    [
        "friendly-name",
        "00000000-0000-0000-0000-000000000000",
        "11111111-1111-4111-8111-11111111111A",
        " 11111111-1111-4111-8111-11111111111a",
        "1111111111114111811111111111111a",
    ],
)
def test_canonical_supervisor_uuid_rejects_names_nil_and_variants(
    value: str,
) -> None:
    with pytest.raises(ValueError):
        canonical_supervisor_uuid(value)


@pytest.mark.parametrize(
    "score_field",
    [
        "objective_fidelity",
        "scope_discipline",
        "technical_completeness",
        "evidence_use",
        "actionability",
        "concision",
    ],
)
def test_every_review_score_accepts_one_to_five_only(
    score_field: str,
) -> None:
    value = {
        "schema_version": 1,
        "proposal_id": "worker_initial-r000-a001",
        "verdict": "equivalent",
        "objective_fidelity": 5,
        "scope_discipline": 5,
        "technical_completeness": 5,
        "evidence_use": 5,
        "actionability": 5,
        "concision": 5,
        "unsupported_assumptions": [],
        "blocking_issues": [],
        "notes": "",
    }
    for accepted in (1, 5):
        value[score_field] = accepted
        HumanReview.model_validate(value)
    for rejected in (0, 6):
        value[score_field] = rejected
        with pytest.raises(ValidationError):
            HumanReview.model_validate(value)
