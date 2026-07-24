from __future__ import annotations

import json
import os
from pathlib import Path

import yaml

from research_automation_supervisor.live_shadow_engine import LiveShadowServices
from tests.shadow_helpers import (
    SOURCE_AUDITOR_UUID,
    SOURCE_WORKER_UUID,
    SUPERVISOR_UUID,
    supervisor_proposal,
)
from tests.workflow_helpers import (
    auditor_result,
    codex_response,
    create_workflow_tree,
    worker_result,
)


def live_supervisor_response(
    proposal_kind: str,
    *,
    resume: bool = False,
    sleep_seconds: float = 0,
) -> dict[str, object]:
    response: dict[str, object] = {
        "require_stage4_policy": True,
        "expected_sandbox": "read-only",
        "expected_ephemeral": False,
        "stdout_lines": [
            json.dumps(
                {"type": "thread.started", "thread_id": SUPERVISOR_UUID}
            )
        ],
        "final": supervisor_proposal(proposal_kind),
    }
    if resume:
        response["expected_resume_thread_id"] = SUPERVISOR_UUID
    if sleep_seconds:
        response["sleep_seconds"] = sleep_seconds
    return response


def create_live_shadow_tree(
    tmp_path: Path,
    *,
    supervisor_responses: list[dict[str, object]] | None = None,
    stage2_responses: list[dict[str, object]] | None = None,
) -> tuple[Path, Path, Path, Path, LiveShadowServices]:
    stage2_spec, project, fake = create_workflow_tree(
        tmp_path / "stage2",
        responses=stage2_responses
        or [
            codex_response(
                "worker",
                SOURCE_WORKER_UUID,
                worker_result(),
            ),
            codex_response(
                "auditor",
                SOURCE_AUDITOR_UUID,
                auditor_result(),
            ),
        ],
    )
    control = tmp_path / "live-control"
    control.mkdir()
    policy = control / "supervisor-policy.md"
    context = control / "project-context.md"
    policy.write_text(
        "Draft quarantined prompts from only frozen live evidence.\n",
        encoding="utf-8",
    )
    context.write_text(
        "This generic fixture has no project-specific policy.\n",
        encoding="utf-8",
    )
    supervisor_config = tmp_path / "live-fake-codex.json"
    supervisor_config.write_text(
        json.dumps(
            {
                "counter_path": str(tmp_path / "live-shadow-counter"),
                "observation_path": str(tmp_path / "live-shadow-observation.json"),
                "responses": supervisor_responses
                or [
                    live_supervisor_response("worker_initial"),
                    live_supervisor_response("auditor", resume=True),
                ],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    specification = {
        "schema_version": 1,
        "live_shadow_id": "minimal-live-shadow",
        "title": "Minimal live shadow observation",
        "stage2_specification_path": str(stage2_spec),
        "supervisor_policy_path": "supervisor-policy.md",
        "project_context_paths": ["project-context.md"],
        "supervisor_model": "gpt-5.6-sol",
        "supervisor_reasoning_effort": "high",
        "supervisor_timeout_seconds": 60,
        "max_proposal_bytes": 4096,
        "observer_poll_interval_milliseconds": 50,
        "shadow_completion_timeout_seconds": 30,
        "minimum_reviewed_proposals": 2,
        "required_consecutive_acceptable": 2,
    }
    live_spec = control / "live-shadow.yaml"
    live_spec.write_text(
        yaml.safe_dump(specification, sort_keys=False),
        encoding="utf-8",
    )
    supervisor_environment = dict(os.environ)
    supervisor_environment["FAKE_CODEX_CONFIG"] = str(supervisor_config)
    services = LiveShadowServices(
        codex_executable=str(fake),
        environ=supervisor_environment,
        token_factory=lambda: "deterministic-live-token",
    )
    return live_spec, stage2_spec, project, fake, services
