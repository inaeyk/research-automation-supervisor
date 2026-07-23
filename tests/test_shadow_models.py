from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from research_automation_supervisor.shadow_models import (
    HumanReview,
    ShadowSpecification,
    SupervisorProposal,
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
