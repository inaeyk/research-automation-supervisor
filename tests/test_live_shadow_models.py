from __future__ import annotations

import pytest
from pydantic import ValidationError

from research_automation_supervisor.live_shadow_models import (
    LiveShadowSpecification,
)


def valid_specification() -> dict[str, object]:
    return {
        "schema_version": 1,
        "live_shadow_id": "live-1",
        "title": "Live observation",
        "stage2_specification_path": "stage2.yaml",
        "supervisor_policy_path": "policy.md",
        "project_context_paths": ["context.md"],
        "supervisor_model": "gpt-5.6-sol",
        "supervisor_reasoning_effort": "high",
        "supervisor_timeout_seconds": 60,
        "max_proposal_bytes": 4096,
        "observer_poll_interval_milliseconds": 50,
        "shadow_completion_timeout_seconds": 30,
        "minimum_reviewed_proposals": 2,
        "required_consecutive_acceptable": 2,
    }


def test_live_specification_is_strict_and_immutable() -> None:
    value = LiveShadowSpecification.model_validate(valid_specification())
    assert value.observer_poll_interval_milliseconds == 50
    with pytest.raises(ValidationError):
        LiveShadowSpecification.model_validate(
            {**valid_specification(), "automatic_handoff": True}
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("observer_poll_interval_milliseconds", 49),
        ("observer_poll_interval_milliseconds", 5001),
        ("shadow_completion_timeout_seconds", 29),
        ("max_proposal_bytes", 1023),
    ],
)
def test_live_specification_bounds(field: str, value: int) -> None:
    invalid = valid_specification()
    invalid[field] = value
    with pytest.raises(ValidationError):
        LiveShadowSpecification.model_validate(invalid)


def test_live_review_threshold_order_is_frozen() -> None:
    invalid = valid_specification()
    invalid["required_consecutive_acceptable"] = 3
    with pytest.raises(ValidationError):
        LiveShadowSpecification.model_validate(invalid)
