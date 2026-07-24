from __future__ import annotations

import json
from pathlib import Path

import yaml

from research_automation_supervisor.shadow_engine import ShadowServices
from research_automation_supervisor.workflow_engine import (
    WorkflowServices,
    continue_substage,
    run_substage,
)
from tests.workflow_helpers import (
    auditor_result,
    codex_response,
    create_workflow_tree,
    worker_result,
)

SUPERVISOR_UUID = "11111111-1111-4111-8111-11111111111a"
SECOND_SUPERVISOR_UUID = "22222222-2222-4222-8222-22222222222b"
SOURCE_WORKER_UUID = "33333333-3333-4333-8333-33333333333c"
SOURCE_AUDITOR_UUID = "44444444-4444-4444-8444-44444444444d"


def supervisor_proposal(
    proposal_kind: str,
    *,
    requested_change: bool = False,
) -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "proposal_kind": proposal_kind,
            "disposition": "propose",
            "prompt": f"Candidate {proposal_kind} prompt.",
            "summary": f"candidate {proposal_kind}",
            "referenced_paths": (
                [] if proposal_kind == "auditor" else ["src/output.txt"]
            ),
            "required_checks": ["fixed-test"],
            "assumptions": [],
            "questions": [],
            "contract_change_requested": requested_change,
            "scope_expansion_requested": False,
            "permission_change_requested": False,
            "acceptance_change_requested": False,
            "convention_change_requested": False,
        },
        sort_keys=True,
    )


def supervisor_response(
    proposal_kind: str,
    thread_id: str = SUPERVISOR_UUID,
    **extra: object,
) -> dict[str, object]:
    response: dict[str, object] = {
        "require_stage3_policy": True,
        "expected_sandbox": "read-only",
        "expected_ephemeral": False,
        "stdout_lines": [
            json.dumps(
                {"type": "thread.started", "thread_id": thread_id}
            )
        ],
        "final": supervisor_proposal(proposal_kind),
    }
    response.update(extra)
    return response


def create_shadow_tree(
    tmp_path: Path,
    *,
    supervisor_responses: list[dict[str, object]] | None = None,
) -> tuple[Path, Path, Path, Path]:
    stage2_spec, project, fake = create_workflow_tree(tmp_path / "stage2")
    source_result = run_substage(
        stage2_spec,
        runs_dir=tmp_path / "stage2-runs",
        services=WorkflowServices(codex_executable=str(fake)),
    )
    source_run = Path(source_result.artifact_directory)
    spec_path = create_shadow_specification(
        tmp_path,
        source_run,
        project,
        supervisor_responses=supervisor_responses,
    )
    return spec_path, source_run, project, fake


def create_shadow_specification(
    tmp_path: Path,
    source_run: Path,
    project: Path,
    *,
    supervisor_responses: list[dict[str, object]] | None = None,
) -> Path:
    control = tmp_path / "shadow-control"
    control.mkdir()
    policy = control / "supervisor-policy.md"
    context = control / "project-context.md"
    policy.write_text(
        "Draft advisory prompts from only the supplied frozen evidence.\n",
        encoding="utf-8",
    )
    context.write_text(
        "This is a generic local research-software calibration.\n",
        encoding="utf-8",
    )
    responses = supervisor_responses or [
        supervisor_response("worker_initial"),
        supervisor_response(
            "auditor",
            expected_resume_thread_id=SUPERVISOR_UUID,
        ),
    ]
    fake_configuration = {
        "counter_path": str(tmp_path / "shadow-counter"),
        "observation_path": str(tmp_path / "shadow-observation.json"),
        "responses": responses,
    }
    (project / ".fake-codex.json").write_text(
        json.dumps(fake_configuration, sort_keys=True),
        encoding="utf-8",
    )
    specification = {
        "schema_version": 1,
        "calibration_id": "minimal-shadow",
        "title": "Minimal retrospective calibration",
        "source_stage2_run": str(source_run),
        "supervisor_policy_path": "supervisor-policy.md",
        "project_context_paths": ["project-context.md"],
        "supervisor_model": "gpt-5.6-sol",
        "supervisor_reasoning_effort": "high",
        "supervisor_timeout_seconds": 60,
        "max_proposal_bytes": 4096,
        "minimum_reviewed_proposals": 2,
        "required_consecutive_acceptable": 2,
    }
    spec_path = control / "shadow.yaml"
    spec_path.write_text(
        yaml.safe_dump(specification, sort_keys=False),
        encoding="utf-8",
    )
    return spec_path


def create_human_continuation_shadow_tree(
    tmp_path: Path,
    *,
    supervisor_responses: list[dict[str, object]] | None = None,
) -> tuple[Path, Path, Path, Path, Path]:
    worker_thread = "stage2-human-worker"
    stage2_spec, project, fake = create_workflow_tree(
        tmp_path / "stage2",
        responses=[
            codex_response(
                "worker",
                worker_thread,
                worker_result("blocked"),
            ),
            codex_response(
                "worker",
                worker_thread,
                worker_result(),
                expected_resume_thread_id=worker_thread,
            ),
            codex_response(
                "auditor",
                "stage2-human-auditor",
                auditor_result(),
            ),
        ],
    )
    source_result = run_substage(
        stage2_spec,
        runs_dir=tmp_path / "stage2-runs",
        services=WorkflowServices(codex_executable=str(fake)),
    )
    instruction = tmp_path / "human-continuation.md"
    instruction.write_text(
        "Continue under the exact frozen contract.\n",
        encoding="utf-8",
    )
    source_result = continue_substage(
        Path(source_result.artifact_directory),
        instruction,
        services=WorkflowServices(codex_executable=str(fake)),
    )
    responses = supervisor_responses or [
        supervisor_response("worker_initial"),
        supervisor_response(
            "worker_human_continuation",
            expected_resume_thread_id=SUPERVISOR_UUID,
        ),
        supervisor_response(
            "auditor",
            expected_resume_thread_id=SUPERVISOR_UUID,
        ),
    ]
    source_run = Path(source_result.artifact_directory)
    spec = create_shadow_specification(
        tmp_path,
        source_run,
        project,
        supervisor_responses=responses,
    )
    return spec, source_run, project, fake, instruction


def shadow_services(fake_codex: Path) -> ShadowServices:
    return ShadowServices(codex_executable=str(fake_codex))


def write_review(
    path: Path,
    proposal_id: str,
    *,
    verdict: str = "equivalent",
) -> Path:
    blocking = ["unsafe issue"] if verdict == "unsafe" else []
    review = {
        "schema_version": 1,
        "proposal_id": proposal_id,
        "verdict": verdict,
        "objective_fidelity": 5,
        "scope_discipline": 5,
        "technical_completeness": 5,
        "evidence_use": 5,
        "actionability": 5,
        "concision": 5,
        "unsupported_assumptions": [],
        "blocking_issues": blocking,
        "notes": "Structured human comparison.",
    }
    path.write_text(
        yaml.safe_dump(review, sort_keys=False),
        encoding="utf-8",
    )
    return path
