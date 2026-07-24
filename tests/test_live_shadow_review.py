from __future__ import annotations

from research_automation_supervisor.live_shadow_models import LiveShadowSpecification
from research_automation_supervisor.live_shadow_review import (
    calculate_live_readiness,
)
from tests.test_live_shadow_models import valid_specification


def test_readiness_is_informational_and_requires_completed_authority() -> None:
    specification = LiveShadowSpecification.model_validate(valid_specification())
    readiness = calculate_live_readiness(
        specification,
        (),
        {},
        {},
        {},
        {},
        authoritative_status="human_paused",
        shadow_failures=(),
    )
    assert readiness.status == "insufficient_data"
    assert readiness.informational_only
    assert readiness.automation_enabled is False
    assert readiness.authoritative_completed is False
