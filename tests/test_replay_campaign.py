from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import UUID

import yaml

from research_automation_supervisor.codex_models import CodexRunResult
from research_automation_supervisor.replay_campaign_engine import (
    ReplayCampaignServices,
    replay_campaign_status,
    resume_replay_campaign,
    run_replay_campaign,
)
from research_automation_supervisor.workflow_engine import WorkflowServices
from tests.workflow_helpers import (
    auditor_result,
    codex_response,
    create_workflow_tree,
    worker_result,
)

SUPERVISOR_UUID = str(UUID("12345678-1234-5678-9234-567812345678"))


class FakeSupervisor:
    def __init__(self, actions: list[dict[str, object]]) -> None:
        self.actions = actions
        self.resume_ids: list[str | None] = []

    def __call__(self, prepared, **kwargs: object) -> CodexRunResult:
        resume_id = kwargs.get("resume_thread_id")
        assert resume_id is None or resume_id == SUPERVISOR_UUID
        self.resume_ids.append(resume_id if isinstance(resume_id, str) else None)
        index = len(self.resume_ids) - 1
        action = self.actions[index]
        runs_dir = kwargs["runs_dir"]
        assert isinstance(runs_dir, Path)
        artifact = runs_dir / prepared.request.run_id
        artifact.mkdir(parents=True)
        (artifact / "metadata.json").write_text(
            json.dumps({"thread_started_ids": [SUPERVISOR_UUID]}),
            encoding="utf-8",
        )
        (artifact / "final-message.md").write_text(
            json.dumps(action),
            encoding="utf-8",
        )
        return CodexRunResult(
            run_id=prepared.request.run_id,
            status="succeeded",
            exit_code=0,
            started_at="2026-01-01T00:00:00.000000Z",
            ended_at="2026-01-01T00:00:01.000000Z",
            duration_seconds=1.0,
            artifact_directory=str(artifact),
            event_count=1,
            malformed_event_count=0,
            final_message_present=True,
            permission_evidence=False,
            summary="fake supervisor succeeded",
            error=None,
        )


def supervisor_action(
    action: str,
    *,
    prompt: str | None = None,
    unauthorized: bool = False,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "action": action,
        "prompt": (
            prompt
            if prompt is not None
            else (
                "Complete this task and return the required structured JSON."
                if action in {"worker_prompt", "auditor_prompt", "repair_prompt"}
                else ""
            )
        ),
        "summary": f"supervisor selected {action}",
        "referenced_paths": [],
        "required_checks": [],
        "assumptions": [],
        "questions": [],
        "contract_change_requested": unauthorized,
        "scope_expansion_requested": False,
        "permission_change_requested": False,
        "acceptance_change_requested": False,
        "convention_change_requested": False,
    }


def create_campaign(
    tmp_path: Path,
    task_responses: list[list[dict[str, object]]],
) -> tuple[Path, Path]:
    tasks: list[dict[str, Any]] = []
    first_fake: Path | None = None
    for index, responses in enumerate(task_responses):
        task_root = tmp_path / f"task-{index + 1}"
        spec, project, fake = create_workflow_tree(
            task_root,
            responses=responses,
            max_repair_rounds=1,
        )
        first_fake = first_fake or fake
        raw = yaml.safe_load(spec.read_text(encoding="utf-8"))
        task_id = f"replay-task-{index + 1}"
        raw["substage_id"] = task_id
        spec.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
        gold_root = tmp_path / f"gold-{index + 1}"
        gold_root.mkdir()
        tasks.append(
            {
                "task_id": task_id,
                "title": f"Replay task {index + 1}",
                "stage2_specification_path": str(spec),
                "project_context_paths": [],
                "gold_evaluations": [
                    {
                        "id": f"gold-{index + 1}",
                        "argv": [
                            "python",
                            "-c",
                            "raise SystemExit(0)",
                        ],
                        "cwd": str(project),
                        "timeout_seconds": 5,
                        "max_stdout_bytes": 4096,
                        "max_stderr_bytes": 4096,
                    }
                ],
                "gold_artifact_roots": [str(gold_root)],
                "production_profile": {
                    "hot_path": ["src/**"],
                    "post_update": [],
                    "validation_only": ["tools/**"],
                },
            }
        )
    policy = tmp_path / "supervisor-policy.md"
    policy.write_text("Create prompts under immutable manifest authority.\n", encoding="utf-8")
    manifest = tmp_path / "campaign.yaml"
    manifest.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "campaign_id": "historical-replay",
                "title": "Historical replay",
                "supervisor_policy_path": str(policy),
                "supervisor_model": "gpt-5.6-sol",
                "supervisor_reasoning_effort": "high",
                "supervisor_timeout_seconds": 60,
                "tasks": tasks,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    assert first_fake is not None
    return manifest, first_fake


def campaign_services(
    fake: Path,
    supervisor: FakeSupervisor,
    notifications: list[dict[str, str]],
) -> ReplayCampaignServices:
    def notify(payload: object) -> None:
        assert isinstance(payload, dict)
        notifications.append(payload)

    return ReplayCampaignServices(
        codex_executable=str(fake),
        supervisor_invoker=supervisor,
        workflow_services=WorkflowServices(codex_executable=str(fake)),
        notification_invoker=notify,  # type: ignore[arg-type]
        token_factory=lambda: "campaign-token",
    )


def test_two_task_autonomous_pass_uses_persistent_supervisor_and_workers(
    tmp_path: Path,
) -> None:
    responses = [
        [
            codex_response("worker", "worker-one", worker_result()),
            codex_response("auditor", "auditor-one", auditor_result()),
        ],
        [
            codex_response("worker", "worker-two", worker_result()),
            codex_response("auditor", "auditor-two", auditor_result()),
        ],
    ]
    manifest, fake = create_campaign(tmp_path, responses)
    supervisor = FakeSupervisor(
        [
            supervisor_action("worker_prompt"),
            supervisor_action("auditor_prompt"),
            supervisor_action("finish"),
            supervisor_action("worker_prompt"),
            supervisor_action("auditor_prompt"),
            supervisor_action("finish"),
        ]
    )
    notifications: list[dict[str, str]] = []

    result = run_replay_campaign(
        manifest,
        runs_dir=tmp_path / "runs",
        services=campaign_services(fake, supervisor, notifications),
    )

    assert result.status == "completed"
    assert result.completed_task_ids == ("replay-task-1", "replay-task-2")
    assert result.supervisor_session_id == SUPERVISOR_UUID
    assert result.task_worker_session_ids == {
        "replay-task-1": "worker-one",
        "replay-task-2": "worker-two",
    }
    assert supervisor.resume_ids == [None, *([SUPERVISOR_UUID] * 5)]
    assert notifications[-1]["reason_category"] == "campaign_completed"
    run = next((tmp_path / "runs").iterdir())
    report = json.loads((run / "campaign-report.json").read_text())
    assert len(report["tasks"]) == 2
    assert all(task["model_turns_after_gold_reveal"] == 0 for task in report["tasks"])
    assert replay_campaign_status(run) == result


def test_auditor_finding_gets_one_supervisor_repair_and_fresh_auditor(
    tmp_path: Path,
) -> None:
    responses = [
        [
            codex_response("worker", "worker-repair", worker_result()),
            codex_response("auditor", "auditor-first", auditor_result("fail_repairable")),
            codex_response(
                "worker",
                "worker-repair",
                worker_result(),
                expected_resume_thread_id="worker-repair",
            ),
            codex_response("auditor", "auditor-second", auditor_result()),
        ]
    ]
    manifest, fake = create_campaign(tmp_path, responses)
    supervisor = FakeSupervisor(
        [
            supervisor_action("worker_prompt"),
            supervisor_action("auditor_prompt"),
            supervisor_action("repair_prompt"),
            supervisor_action("auditor_prompt"),
            supervisor_action("finish"),
        ]
    )

    result = run_replay_campaign(
        manifest,
        runs_dir=tmp_path / "runs",
        services=campaign_services(fake, supervisor, []),
    )

    assert result.status == "completed"
    run = next((tmp_path / "runs").iterdir())
    task = json.loads(
        (run / "tasks/replay-task-1/task-report.json").read_text(encoding="utf-8")
    )
    assert task["repair_rounds"] == 1
    assert task["uuids"]["auditors"] == [["auditor-first"], ["auditor-second"]]


def test_unauthorized_supervisor_action_pauses_and_exact_decision_resumes(
    tmp_path: Path,
) -> None:
    responses = [
        [
            codex_response("worker", "worker-resume", worker_result()),
            codex_response("auditor", "auditor-resume", auditor_result()),
        ]
    ]
    manifest, fake = create_campaign(tmp_path, responses)
    supervisor = FakeSupervisor(
        [
            supervisor_action("worker_prompt", unauthorized=True),
            supervisor_action("worker_prompt"),
            supervisor_action("auditor_prompt"),
            supervisor_action("finish"),
        ]
    )
    notifications: list[dict[str, str]] = []
    services = campaign_services(fake, supervisor, notifications)

    paused = run_replay_campaign(
        manifest,
        runs_dir=tmp_path / "runs",
        services=services,
    )
    run = next((tmp_path / "runs").iterdir())

    assert paused.status == "human_paused"
    assert (run / "human-review-packet.md").is_file()
    assert notifications[-1]["reason_category"] == "human_review_required"
    decision = tmp_path / "decision.yaml"
    decision.write_text(
        "schema_version: 1\ndecision: resume\nnote: Authority remains unchanged.\n",
        encoding="utf-8",
    )
    resumed = resume_replay_campaign(run, decision_path=decision, services=services)
    assert resumed.status == "completed"
    assert resumed.human_assisted_task_ids == ("replay-task-1",)
    assert resumed.supervisor_session_id == SUPERVISOR_UUID


def test_notification_failure_does_not_change_completion(tmp_path: Path) -> None:
    manifest, fake = create_campaign(
        tmp_path,
        [
            [
                codex_response("worker", "worker-notify", worker_result()),
                codex_response("auditor", "auditor-notify", auditor_result()),
            ]
        ],
    )
    supervisor = FakeSupervisor(
        [
            supervisor_action("worker_prompt"),
            supervisor_action("auditor_prompt"),
            supervisor_action("finish"),
        ]
    )

    def fail_notification(payload: object) -> None:
        del payload
        raise OSError("notification unavailable")

    services = ReplayCampaignServices(
        codex_executable=str(fake),
        supervisor_invoker=supervisor,
        workflow_services=WorkflowServices(codex_executable=str(fake)),
        notification_invoker=fail_notification,  # type: ignore[arg-type]
        token_factory=lambda: "campaign-token",
    )
    result = run_replay_campaign(
        manifest,
        runs_dir=tmp_path / "runs",
        services=services,
    )
    run = next((tmp_path / "runs").iterdir())
    notification = json.loads(
        next((run / "notifications").iterdir()).read_text(encoding="utf-8")
    )
    assert result.status == "completed"
    assert notification["status"] == "failed"
