from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from research_automation_supervisor.codex_adapter import run_prepared_codex
from research_automation_supervisor.errors import WorkflowInputError, WorkflowLockError
from research_automation_supervisor.test_runner import run_test_attempt
from research_automation_supervisor.workflow_engine import (
    WorkflowServices,
    _WorkflowLock,
    abort_substage,
    continue_substage,
    resume_substage,
    run_substage,
    substage_status,
)
from tests.workflow_helpers import (
    auditor_result,
    codex_response,
    create_workflow_tree,
    worker_result,
)


def services(fake_codex: Path) -> WorkflowServices:
    return WorkflowServices(codex_executable=str(fake_codex))


def test_direct_pass_uses_persistent_worker_fresh_auditor_and_equal_snapshots(
    tmp_path: Path,
) -> None:
    spec, project, fake = create_workflow_tree(tmp_path)

    result = run_substage(spec, runs_dir=tmp_path / "runs", services=services(fake))

    assert result.status == "completed"
    assert result.worker_thread_id == "worker-thread-1"
    assert result.latest_worker_action_id == "worker-r000"
    assert result.latest_audit_action_id == "auditor-r000"
    assert result.tests_passed and result.scope_compliant and result.contract_satisfied
    assert result == substage_status(Path(result.artifact_directory))
    persisted = json.loads((Path(result.artifact_directory) / "result.json").read_text())
    assert persisted == result.to_dict()
    actions = sorted((Path(result.artifact_directory) / "actions").glob("*.json"))
    assert [path.stem for path in actions] == [
        "auditor-r000",
        "test-r000-000-fixed-test-92297458",
        "worker-r000",
    ]
    worker_metadata = json.loads(
        (
            Path(result.artifact_directory)
            / "worker/codex/worker-r000/metadata.json"
        ).read_text()
    )
    audit_metadata = json.loads(
        (
            Path(result.artifact_directory)
            / "audits/codex/auditor-r000/metadata.json"
        ).read_text()
    )
    assert worker_metadata["ephemeral"] is False
    assert worker_metadata["sandbox"] == "workspace-write"
    assert audit_metadata["ephemeral"] is True
    assert audit_metadata["sandbox"] == "read-only"
    assert audit_metadata["resume_thread_id"] is None
    artifact_bytes = b"".join(
        path.read_bytes()
        for path in Path(result.artifact_directory).rglob("*")
        if path.is_file()
    )
    for human_file in (
        project / "control/contract.md",
        project / "control/worker-initial.md",
        project / "control/worker-repair.md",
        project / "control/auditor.md",
    ):
        assert human_file.read_bytes() not in artifact_bytes


def test_checkpoint_pass_returns_frozen_checkpoint_exit_state(tmp_path: Path) -> None:
    spec, _, fake = create_workflow_tree(tmp_path, checkpoint_after=True)

    result = run_substage(spec, runs_dir=tmp_path / "runs", services=services(fake))

    assert result.status == "checkpoint_paused"
    assert result.pause_reason == "auditor_passed_checkpoint"
    assert result.latest_worker_action_id == "worker-r000"
    assert result.latest_audit_action_id == "auditor-r000"
    run_directory = Path(result.artifact_directory)
    persisted = json.loads((run_directory / "result.json").read_text())
    assert persisted == result.to_dict()
    assert substage_status(run_directory) == result
    with pytest.raises(WorkflowInputError):
        resume_substage(run_directory, services=services(fake))


def test_failed_fixed_test_repairs_on_exact_worker_thread_then_audits(tmp_path: Path) -> None:
    responses = [
        codex_response("worker", "persistent-worker", worker_result()),
        codex_response(
            "worker",
            "persistent-worker",
            worker_result(),
            expected_resume_thread_id="persistent-worker",
            write_files={"src/ready.txt": "ready\n"},
        ),
        codex_response("auditor", "fresh-audit", auditor_result()),
    ]
    spec, _, fake = create_workflow_tree(
        tmp_path,
        responses=responses,
        test_requires_marker=True,
    )

    result = run_substage(spec, runs_dir=tmp_path / "runs", services=services(fake))

    assert result.status == "completed"
    assert result.repair_round == 1
    assert result.latest_worker_action_id == "worker-r001"
    assert result.latest_audit_action_id == "auditor-r001"
    repair_metadata = json.loads(
        (
            Path(result.artifact_directory)
            / "worker/codex/worker-r001/metadata.json"
        ).read_text()
    )
    command = repair_metadata["command"]
    resume_index = command.index("resume")
    assert command[resume_index + 1] == "persistent-worker"
    assert "--last" not in command and "--all" not in command
    assert repair_metadata["resume_thread_id"] == "persistent-worker"


def test_scope_failure_repairs_without_auditing_incomplete_round(tmp_path: Path) -> None:
    responses = [
        codex_response(
            "worker",
            "worker-scope",
            worker_result(),
            write_files={"outside.txt": "outside\n"},
        ),
        codex_response(
            "worker",
            "worker-scope",
            worker_result(),
            expected_resume_thread_id="worker-scope",
            delete_files=["outside.txt"],
            write_files={"src/output.txt": "fixed\n"},
        ),
        codex_response("auditor", "audit-scope", auditor_result()),
    ]
    spec, project, fake = create_workflow_tree(tmp_path, responses=responses)

    result = run_substage(spec, runs_dir=tmp_path / "runs", services=services(fake))

    assert result.status == "completed"
    assert not (project / "outside.txt").exists()
    audits = list((Path(result.artifact_directory) / "audits/codex").iterdir())
    assert [path.name for path in audits] == ["auditor-r001"]


def test_repairable_audit_gets_worker_repair_and_brand_new_reaudit(tmp_path: Path) -> None:
    responses = [
        codex_response("worker", "worker-audit", worker_result()),
        codex_response("auditor", "audit-one", auditor_result("fail_repairable")),
        codex_response(
            "worker",
            "worker-audit",
            worker_result(),
            expected_resume_thread_id="worker-audit",
            write_files={"src/output.txt": "repaired\n"},
        ),
        codex_response("auditor", "audit-two", auditor_result()),
    ]
    spec, _, fake = create_workflow_tree(tmp_path, responses=responses)

    result = run_substage(spec, runs_dir=tmp_path / "runs", services=services(fake))

    assert result.status == "completed"
    audit_dirs = sorted((Path(result.artifact_directory) / "audits/codex").iterdir())
    assert [path.name for path in audit_dirs] == ["auditor-r000", "auditor-r001"]
    for directory in audit_dirs:
        metadata = json.loads((directory / "metadata.json").read_text())
        assert metadata["ephemeral"] is True
        assert metadata["resume_thread_id"] is None


@pytest.mark.parametrize("worker_status", ["blocked", "needs_human"])
def test_worker_human_status_always_pauses(tmp_path: Path, worker_status: str) -> None:
    responses = [codex_response("worker", "worker-human", worker_result(worker_status))]
    spec, _, fake = create_workflow_tree(tmp_path, responses=responses)

    result = run_substage(spec, runs_dir=tmp_path / "runs", services=services(fake))

    assert result.status == "human_paused"
    assert result.pause_reason == f"worker_{worker_status}"
    assert result.latest_worker_action_id == "worker-r000"
    assert result.latest_audit_action_id is None
    assert (Path(result.artifact_directory) / "escalation/package.json").is_file()


def test_missing_thread_and_malformed_result_pause_without_replacement_worker(
    tmp_path: Path,
) -> None:
    no_thread = codex_response("worker", "ignored", worker_result())
    no_thread["stdout_lines"] = []
    spec, _, fake = create_workflow_tree(tmp_path / "one", responses=[no_thread])
    first = run_substage(spec, runs_dir=tmp_path / "runs-one", services=services(fake))
    assert first.status == "human_paused"
    assert first.pause_reason == "worker_thread_id_missing_or_ambiguous"

    malformed = codex_response("worker", "thread", "not-json")
    spec, _, fake = create_workflow_tree(tmp_path / "two", responses=[malformed])
    second = run_substage(spec, runs_dir=tmp_path / "runs-two", services=services(fake))
    assert second.status == "human_paused"
    assert second.pause_reason == "worker_structured_result_invalid"


def test_changed_worker_thread_and_auditor_escalation_pause(tmp_path: Path) -> None:
    changed_responses = [
        codex_response("worker", "original-thread", worker_result()),
        codex_response(
            "worker",
            "different-thread",
            worker_result(),
            write_files={"src/ready.txt": "ready\n"},
        ),
    ]
    spec, _, fake = create_workflow_tree(
        tmp_path / "changed",
        responses=changed_responses,
        test_requires_marker=True,
    )
    changed = run_substage(
        spec,
        runs_dir=tmp_path / "changed-runs",
        services=services(fake),
    )
    assert changed.status == "human_paused"
    assert changed.pause_reason == "worker_thread_id_changed_or_missing"

    escalation = json.dumps(
        {
            "schema_version": 1,
            "verdict": "escalate",
            "summary": "human decision required",
            "scope_compliant": True,
            "contract_satisfied": False,
            "findings": [],
            "human_questions": ["Decide the contract interpretation."],
        }
    )
    responses = [
        codex_response("worker", "worker-escalate", worker_result()),
        codex_response("auditor", "audit-escalate", escalation),
    ]
    spec, _, fake = create_workflow_tree(tmp_path / "escalate", responses=responses)
    escalated = run_substage(
        spec,
        runs_dir=tmp_path / "escalate-runs",
        services=services(fake),
    )
    assert escalated.status == "human_paused"
    assert escalated.pause_reason == "auditor_escalated"


def test_repair_limit_zero_pauses_and_human_continuation_uses_exact_bytes(
    tmp_path: Path,
) -> None:
    instruction = tmp_path / "human-instruction.md"
    instruction.write_text("Create the required marker exactly.\n", encoding="utf-8")
    responses = [
        codex_response("worker", "worker-limit", worker_result()),
        codex_response(
            "worker",
            "worker-limit",
            worker_result(),
            expected_resume_thread_id="worker-limit",
            write_files={"src/ready.txt": "ready\n"},
            observation_path=str(tmp_path / "continuation-observation.json"),
        ),
        codex_response("auditor", "audit-limit", auditor_result()),
    ]
    spec, _, fake = create_workflow_tree(
        tmp_path,
        responses=responses,
        max_repair_rounds=0,
        test_requires_marker=True,
    )
    paused = run_substage(spec, runs_dir=tmp_path / "runs", services=services(fake))
    assert paused.status == "repair_limit_paused"
    assert paused.pause_reason == "fixed_test_failed_repair_limit"

    result = continue_substage(
        Path(paused.artifact_directory),
        instruction,
        services=services(fake),
    )

    assert result.status == "completed"
    assert result.repair_round == 1
    observation = json.loads((tmp_path / "continuation-observation.json").read_text())
    prompt = base64.b64decode(observation["prompt_base64"])
    assert prompt.startswith(instruction.read_bytes())
    all_artifacts = b"".join(
        path.read_bytes()
        for path in Path(result.artifact_directory).rglob("*")
        if path.is_file()
    )
    assert instruction.read_bytes() not in all_artifacts


def test_abort_marks_human_pause_and_status_read_is_nonmutating(tmp_path: Path) -> None:
    responses = [codex_response("worker", "worker-abort", worker_result("blocked"))]
    spec, _, fake = create_workflow_tree(tmp_path, responses=responses)
    paused = run_substage(spec, runs_dir=tmp_path / "runs", services=services(fake))
    run_directory = Path(paused.artifact_directory)

    aborted = abort_substage(run_directory, "No longer needed", services=services(fake))

    assert aborted.status == "aborted"
    assert aborted.pause_reason == "No longer needed"
    before = (run_directory / "state.json").stat().st_mtime_ns
    assert substage_status(run_directory) == aborted
    assert (run_directory / "state.json").stat().st_mtime_ns == before


def test_uncertain_intent_pauses_on_resume_without_duplicate_launch(tmp_path: Path) -> None:
    spec, _, fake = create_workflow_tree(tmp_path)

    def interrupt(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise KeyboardInterrupt

    interrupted_services = WorkflowServices(
        codex_executable=str(fake),
        codex_invoker=interrupt,  # type: ignore[arg-type]
    )
    with pytest.raises(KeyboardInterrupt):
        run_substage(spec, runs_dir=tmp_path / "runs", services=interrupted_services)
    run_directory = next((tmp_path / "runs").iterdir())
    assert not (tmp_path / "fake-counter").exists()

    result = resume_substage(run_directory, services=services(fake))

    assert result.status == "human_paused"
    assert result.pause_reason == "uncertain_in_flight_action"
    assert not (tmp_path / "fake-counter").exists()


def test_complete_stage1_artifacts_are_recovered_without_repeating_worker(tmp_path: Path) -> None:
    spec, _, fake = create_workflow_tree(tmp_path)

    def complete_then_interrupt(prepared, **kwargs: object) -> object:
        run_prepared_codex(prepared, **kwargs)  # type: ignore[arg-type]
        raise KeyboardInterrupt

    interrupted = WorkflowServices(
        codex_executable=str(fake),
        codex_invoker=complete_then_interrupt,  # type: ignore[arg-type]
    )
    with pytest.raises(KeyboardInterrupt):
        run_substage(spec, runs_dir=tmp_path / "runs", services=interrupted)
    run_directory = next((tmp_path / "runs").iterdir())
    assert (run_directory / "worker/codex/worker-r000/result.json").is_file()
    assert (tmp_path / "fake-counter").read_text(encoding="ascii") == "1"

    result = resume_substage(run_directory, services=services(fake))

    assert result.status == "completed"
    assert (tmp_path / "fake-counter").read_text(encoding="ascii") == "2"
    assert len(list((run_directory / "worker/codex").iterdir())) == 1


def test_complete_test_artifact_is_recovered_without_relaunch(tmp_path: Path) -> None:
    spec, _, fake = create_workflow_tree(tmp_path)
    calls: list[str] = []

    def test_then_interrupt(prepared_test, artifact_directory, action_id, **kwargs):
        calls.append(action_id)
        run_test_attempt(
            prepared_test,
            artifact_directory,
            action_id,
            **kwargs,
        )
        raise KeyboardInterrupt

    interrupted = WorkflowServices(
        codex_executable=str(fake),
        test_invoker=test_then_interrupt,  # type: ignore[arg-type]
    )
    with pytest.raises(KeyboardInterrupt):
        run_substage(spec, runs_dir=tmp_path / "runs", services=interrupted)
    run_directory = next((tmp_path / "runs").iterdir())
    assert len(calls) == 1

    result = resume_substage(run_directory, services=services(fake))

    assert result.status == "completed"
    assert len(calls) == 1


def test_concurrent_lock_is_rejected_and_journal_chain_is_readable(tmp_path: Path) -> None:
    spec, _, fake = create_workflow_tree(tmp_path)
    result = run_substage(spec, runs_dir=tmp_path / "runs", services=services(fake))
    run_directory = Path(result.artifact_directory)

    with (
        _WorkflowLock(run_directory, services(fake).utc_now),
        pytest.raises(WorkflowLockError),
        _WorkflowLock(run_directory, services(fake).utc_now),
    ):
        pass
    journal = [
        json.loads(line)
        for line in (run_directory / "journal.jsonl").read_text().splitlines()
    ]
    assert [entry["sequence"] for entry in journal] == list(range(1, len(journal) + 1))
    assert journal[0]["previous_hash"] == "0" * 64
    assert all(
        journal[index]["previous_hash"] == journal[index - 1]["entry_hash"]
        for index in range(1, len(journal))
    )
