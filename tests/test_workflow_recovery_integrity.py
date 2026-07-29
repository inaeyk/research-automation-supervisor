from __future__ import annotations

import hashlib
import json
import socket
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import research_automation_supervisor.workflow_engine as workflow_engine
from research_automation_supervisor.codex_adapter import run_prepared_codex
from research_automation_supervisor.errors import (
    WorkflowInputError,
    WorkflowLockError,
    WorkflowStateError,
)
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
from tests.test_workflow_engine import services
from tests.workflow_helpers import (
    auditor_result,
    codex_response,
    create_workflow_tree,
    git,
    worker_result,
)


def _interrupted_codex_run(
    tmp_path: Path,
    *,
    interrupt_role: str,
    resumed_worker: bool = False,
) -> tuple[Path, Path]:
    responses = [
        codex_response("worker", "persistent-worker", worker_result()),
    ]
    if resumed_worker:
        responses.append(
            codex_response(
                "worker",
                "persistent-worker",
                worker_result(),
                expected_resume_thread_id="persistent-worker",
                write_files={"src/ready.txt": "ready\n"},
            )
        )
    responses.append(codex_response("auditor", "fresh-auditor", auditor_result()))
    spec, _, fake = create_workflow_tree(
        tmp_path,
        responses=responses,
        test_requires_marker=resumed_worker,
    )

    def complete_then_interrupt(prepared, **kwargs: object) -> object:
        result = run_prepared_codex(prepared, **kwargs)  # type: ignore[arg-type]
        should_interrupt = prepared.request.role == interrupt_role
        if resumed_worker:
            should_interrupt = prepared.request.run_id == "worker-r001"
        if should_interrupt:
            raise KeyboardInterrupt
        return result

    interrupted = WorkflowServices(
        codex_executable=str(fake),
        codex_invoker=complete_then_interrupt,  # type: ignore[arg-type]
    )
    with pytest.raises(KeyboardInterrupt):
        run_substage(spec, runs_dir=tmp_path / "runs", services=interrupted)
    return next((tmp_path / "runs").iterdir()), fake


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "request",
        "request_role",
        "request_workspace",
        "request_policy",
        "prompt_hash",
        "events",
        "metadata",
        "result",
        "final_message",
        "missing_completion_manifest",
        "additional_artifact",
    ],
)
@pytest.mark.parametrize("resumed_worker", [False, True])
def test_interrupted_worker_rejects_replaced_or_contradictory_stage1_artifacts(
    tmp_path: Path,
    mutation: str,
    resumed_worker: bool,
) -> None:
    run_directory, fake = _interrupted_codex_run(
        tmp_path,
        interrupt_role="worker",
        resumed_worker=resumed_worker,
    )
    action_id = "worker-r001" if resumed_worker else "worker-r000"
    artifact = run_directory / "worker" / "codex" / action_id
    if mutation == "request":
        _write_json(artifact / "request.normalized.json", {})
    elif mutation in {"request_role", "request_workspace", "request_policy"}:
        value = json.loads((artifact / "request.normalized.json").read_text())
        if mutation == "request_role":
            value["role"] = "auditor"
        elif mutation == "request_workspace":
            value["workspace"] = "/tmp"
        else:
            value["policy"]["sandbox"] = "read-only"
        _write_json(artifact / "request.normalized.json", value)
    elif mutation == "prompt_hash":
        (artifact / "prompt.sha256").write_text("0" * 64 + "\n", encoding="ascii")
    elif mutation == "events":
        (artifact / "events.jsonl").write_text('{"type":"thread.started"', encoding="ascii")
    elif mutation == "metadata":
        value = json.loads((artifact / "metadata.json").read_text())
        value["model"] = "wrong-model"
        _write_json(artifact / "metadata.json", value)
    elif mutation == "result":
        value = json.loads((artifact / "result.json").read_text())
        value["run_id"] = "wrong-run"
        _write_json(artifact / "result.json", value)
    elif mutation == "final_message":
        (artifact / "final-message.md").write_text("truncated", encoding="utf-8")
    elif mutation == "missing_completion_manifest":
        (artifact / "stage2-completion.json").unlink()
    else:
        (artifact / "unexpected.txt").write_text("unexpected\n", encoding="utf-8")
    before = (tmp_path / "fake-counter").read_text(encoding="ascii")

    result = resume_substage(run_directory, services=services(fake))

    assert result.status == "human_paused"
    assert result.pause_reason == "uncertain_in_flight_action"
    assert (tmp_path / "fake-counter").read_text(encoding="ascii") == before


@pytest.mark.parametrize(
    "mutation",
    [
        "wrong_run",
        "non_ephemeral",
        "writable",
        "missing_events",
        "final_result_mismatch",
        "worker_session_substitution",
    ],
)
def test_interrupted_auditor_requires_fresh_read_only_complete_proof(
    tmp_path: Path,
    mutation: str,
) -> None:
    run_directory, fake = _interrupted_codex_run(tmp_path, interrupt_role="auditor")
    artifact = run_directory / "audits/codex/auditor-r000"
    metadata_path = artifact / "metadata.json"
    metadata = json.loads(metadata_path.read_text())
    if mutation == "wrong_run":
        metadata["run_id"] = "auditor-r999"
    elif mutation == "non_ephemeral":
        metadata["ephemeral"] = False
    elif mutation == "writable":
        metadata["sandbox"] = "workspace-write"
    elif mutation == "missing_events":
        (artifact / "events.jsonl").unlink()
    elif mutation == "final_result_mismatch":
        fabricated = worker_result().encode("utf-8")
        (artifact / "final-message.md").write_bytes(fabricated)
        metadata["final_message_sha256"] = hashlib.sha256(fabricated).hexdigest()
    else:
        event = b'{"thread_id":"persistent-worker","type":"thread.started"}\n'
        (artifact / "events.jsonl").write_bytes(event)
        metadata["events_sha256"] = hashlib.sha256(event).hexdigest()
        metadata["thread_id"] = "persistent-worker"
        metadata["thread_started_ids"] = ["persistent-worker"]
    if metadata_path.exists():
        _write_json(metadata_path, metadata)
    before = (tmp_path / "fake-counter").read_text(encoding="ascii")

    result = resume_substage(run_directory, services=services(fake))

    assert result.status == "human_paused"
    assert (tmp_path / "fake-counter").read_text(encoding="ascii") == before


@pytest.mark.parametrize(
    "mutation",
    [
        "action_id",
        "test_id",
        "argv",
        "cwd",
        "status_passed",
        "missing_stdout",
        "altered_stderr",
        "byte_count",
        "hash",
        "configured_timeout",
        "configured_limit",
        "timeout",
        "output_limit",
    ],
)
def test_interrupted_fixed_test_requires_exact_result_and_bounded_logs(
    tmp_path: Path,
    mutation: str,
) -> None:
    spec, _, fake = create_workflow_tree(tmp_path)

    def complete_then_interrupt(prepared_test, artifact_directory, action_id, **kwargs):
        run_test_attempt(prepared_test, artifact_directory, action_id, **kwargs)
        raise KeyboardInterrupt

    interrupted = WorkflowServices(
        codex_executable=str(fake),
        test_invoker=complete_then_interrupt,  # type: ignore[arg-type]
    )
    with pytest.raises(KeyboardInterrupt):
        run_substage(spec, runs_dir=tmp_path / "runs", services=interrupted)
    run_directory = next((tmp_path / "runs").iterdir())
    artifact = next((run_directory / "tests/round-000").iterdir())
    result_path = artifact / "result.json"
    value = json.loads(result_path.read_text())
    if mutation == "action_id":
        value["action_id"] = "wrong-action"
    elif mutation == "test_id":
        value["test_id"] = "wrong-test"
    elif mutation == "argv":
        value["argv"] = ["false"]
    elif mutation == "cwd":
        value["cwd"] = "/tmp"
    elif mutation == "status_passed":
        value["status"] = "failed"
        value["passed"] = True
    elif mutation == "missing_stdout":
        (artifact / "stdout.log").unlink()
    elif mutation == "altered_stderr":
        (artifact / "stderr.log").write_text("altered\n", encoding="utf-8")
    elif mutation == "byte_count":
        value["stdout_stored_byte_count"] += 1
    elif mutation == "hash":
        value["stdout_sha256"] = "0" * 64
    elif mutation == "configured_timeout":
        value["timeout_seconds"] += 1
    elif mutation == "configured_limit":
        value["max_stdout_bytes"] += 1
    elif mutation == "timeout":
        value["timed_out"] = True
    else:
        value["output_limit_stream"] = "stdout"
    if result_path.exists():
        _write_json(result_path, value)
    before = (tmp_path / "fake-counter").read_text(encoding="ascii")

    result = resume_substage(run_directory, services=services(fake))

    assert result.status == "human_paused"
    assert (tmp_path / "fake-counter").read_text(encoding="ascii") == before


@pytest.mark.parametrize(
    "target",
    [
        "action_record",
        "structured_result",
        "test_log",
        "git_patch",
        "missing_artifact",
    ],
)
def test_status_recomputes_every_journal_and_recursive_artifact_hash(
    tmp_path: Path,
    target: str,
) -> None:
    spec, _, fake = create_workflow_tree(tmp_path)
    completed = run_substage(spec, runs_dir=tmp_path / "runs", services=services(fake))
    run_directory = Path(completed.artifact_directory)
    if target == "action_record":
        path = run_directory / "actions/worker-r000.json"
        _write_json(path, {"schema_version": 1})
    elif target == "structured_result":
        path = run_directory / "worker/worker-r000.structured.json"
        path.write_text("{", encoding="utf-8")
    elif target == "test_log":
        path = next((run_directory / "tests").glob("**/stdout.log"))
        path.write_text("modified\n", encoding="utf-8")
    elif target == "git_patch":
        path = run_directory / "git/round-000/patch.txt"
        path.write_text("modified\n", encoding="utf-8")
    else:
        path = run_directory / "audits/auditor-r000.structured.json"
        path.unlink()

    with pytest.raises(WorkflowStateError):
        substage_status(run_directory)


def _rehash_journal(run_directory: Path, mutate: Callable[[list[dict[str, Any]]], None]) -> None:
    journal_path = run_directory / "journal.jsonl"
    entries = [json.loads(line) for line in journal_path.read_text().splitlines()]
    mutate(entries)
    previous = "0" * 64
    for sequence, entry in enumerate(entries, start=1):
        entry["sequence"] = sequence
        entry["previous_hash"] = previous
        body = {key: value for key, value in entry.items() if key != "entry_hash"}
        previous = hashlib.sha256(
            (
                json.dumps(
                    body,
                    ensure_ascii=True,
                    allow_nan=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                + "\n"
            ).encode("ascii")
        ).hexdigest()
        entry["entry_hash"] = previous
    journal_path.write_text(
        "".join(
            json.dumps(entry, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
            + "\n"
            for entry in entries
        ),
        encoding="ascii",
    )
    state_path = run_directory / "state.json"
    state = json.loads(state_path.read_text())
    state["journal_sequence"] = len(entries)
    state["journal_hash"] = previous
    _write_json(state_path, state)


def _rewrite_state_and_result_field(
    run_directory: Path,
    field: str,
    value: object,
) -> None:
    for name in ("state.json", "result.json"):
        path = run_directory / name
        snapshot = json.loads(path.read_text())
        snapshot[field] = value
        _write_json(path, snapshot)


def _invoke_existing_workflow_operation(
    operation: str,
    run_directory: Path,
    fake: Path,
    instruction: Path,
    *,
    workflow_services: WorkflowServices | None = None,
) -> object:
    selected_services = workflow_services or services(fake)
    if operation == "status":
        return substage_status(run_directory)
    if operation == "resume":
        return resume_substage(run_directory, services=selected_services)
    if operation == "continue":
        return continue_substage(
            run_directory,
            instruction,
            services=selected_services,
        )
    return abort_substage(run_directory, "stop", services=selected_services)


def _replace_last_latest_action_update(
    entries: list[dict[str, Any]],
    field: str,
    value: str | None,
) -> None:
    entry = next(
        item
        for item in reversed(entries)
        if field in item["state_updates"]
    )
    entry["state_updates"][field] = value


@pytest.mark.parametrize(
    "mutation",
    ["human_sentence", "other_valid_reason"],
)
def test_rehashed_checkpoint_pause_reason_mismatch_is_rejected(
    tmp_path: Path,
    mutation: str,
) -> None:
    spec, _, fake = create_workflow_tree(tmp_path, checkpoint_after=True)
    checkpoint = run_substage(
        spec,
        runs_dir=tmp_path / "runs",
        services=services(fake),
    )
    run_directory = Path(checkpoint.artifact_directory)
    replacement = (
        "Human checkpoint required."
        if mutation == "human_sentence"
        else "auditor_passed"
    )

    def mutate(entries: list[dict[str, Any]]) -> None:
        entry = next(
            item
            for item in entries
            if item["new_state"] == "checkpoint_paused"
        )
        entry["state_updates"]["pause_reason"] = replacement
        if mutation == "other_valid_reason":
            entry["reason"] = replacement

    _rehash_journal(run_directory, mutate)
    _rewrite_state_and_result_field(run_directory, "pause_reason", replacement)
    before = (tmp_path / "fake-counter").read_text(encoding="ascii")

    with pytest.raises(WorkflowStateError):
        substage_status(run_directory)

    assert (tmp_path / "fake-counter").read_text(encoding="ascii") == before


@pytest.mark.parametrize(
    "field",
    ["latest_worker_action_id", "latest_audit_action_id"],
)
def test_rehashed_completed_workflow_rejects_erased_latest_action(
    tmp_path: Path,
    field: str,
) -> None:
    spec, _, fake = create_workflow_tree(tmp_path)
    completed = run_substage(
        spec,
        runs_dir=tmp_path / "runs",
        services=services(fake),
    )
    run_directory = Path(completed.artifact_directory)

    def mutate(entries: list[dict[str, Any]]) -> None:
        _replace_last_latest_action_update(entries, field, None)

    _rehash_journal(run_directory, mutate)
    _rewrite_state_and_result_field(run_directory, field, None)
    before = (tmp_path / "fake-counter").read_text(encoding="ascii")

    with pytest.raises(WorkflowStateError):
        substage_status(run_directory)

    assert (tmp_path / "fake-counter").read_text(encoding="ascii") == before


def test_rehashed_completed_workflow_rejects_both_latest_actions_erased(
    tmp_path: Path,
) -> None:
    spec, _, fake = create_workflow_tree(tmp_path)
    completed = run_substage(
        spec,
        runs_dir=tmp_path / "runs",
        services=services(fake),
    )
    run_directory = Path(completed.artifact_directory)

    def mutate(entries: list[dict[str, Any]]) -> None:
        _replace_last_latest_action_update(entries, "latest_worker_action_id", None)
        _replace_last_latest_action_update(entries, "latest_audit_action_id", None)

    _rehash_journal(run_directory, mutate)
    _rewrite_state_and_result_field(run_directory, "latest_worker_action_id", None)
    _rewrite_state_and_result_field(run_directory, "latest_audit_action_id", None)
    before = (tmp_path / "fake-counter").read_text(encoding="ascii")

    with pytest.raises(WorkflowStateError):
        substage_status(run_directory)

    assert (tmp_path / "fake-counter").read_text(encoding="ascii") == before


@pytest.mark.parametrize("operation", ["status", "resume", "continue", "abort"])
def test_rehashed_human_pause_rejects_erased_worker_without_launch(
    tmp_path: Path,
    operation: str,
) -> None:
    responses = [codex_response("worker", "worker", worker_result("blocked"))]
    spec, _, fake = create_workflow_tree(tmp_path, responses=responses)
    paused = run_substage(
        spec,
        runs_dir=tmp_path / "runs",
        services=services(fake),
    )
    run_directory = Path(paused.artifact_directory)
    instruction = tmp_path / "instruction.md"
    instruction.write_text("Continue exactly.\n", encoding="utf-8")

    def mutate(entries: list[dict[str, Any]]) -> None:
        _replace_last_latest_action_update(
            entries,
            "latest_worker_action_id",
            None,
        )

    _rehash_journal(run_directory, mutate)
    _rewrite_state_and_result_field(
        run_directory,
        "latest_worker_action_id",
        None,
    )
    test_launches: list[str] = []

    def unexpected_test(*args: object, **kwargs: object) -> object:
        del args, kwargs
        test_launches.append("test")
        raise AssertionError("fixed test launched after failed integrity validation")

    guarded_services = WorkflowServices(
        codex_executable=str(fake),
        test_invoker=unexpected_test,  # type: ignore[arg-type]
    )
    before = (tmp_path / "fake-counter").read_text(encoding="ascii")

    with pytest.raises(WorkflowStateError):
        _invoke_existing_workflow_operation(
            operation,
            run_directory,
            fake,
            instruction,
            workflow_services=guarded_services,
        )

    assert (tmp_path / "fake-counter").read_text(encoding="ascii") == before
    assert test_launches == []


def test_rehashed_checkpoint_pause_rejects_erased_auditor_without_launch(
    tmp_path: Path,
) -> None:
    spec, _, fake = create_workflow_tree(tmp_path, checkpoint_after=True)
    checkpoint = run_substage(
        spec,
        runs_dir=tmp_path / "runs",
        services=services(fake),
    )
    run_directory = Path(checkpoint.artifact_directory)

    def mutate(entries: list[dict[str, Any]]) -> None:
        _replace_last_latest_action_update(
            entries,
            "latest_audit_action_id",
            None,
        )

    _rehash_journal(run_directory, mutate)
    _rewrite_state_and_result_field(
        run_directory,
        "latest_audit_action_id",
        None,
    )
    before = (tmp_path / "fake-counter").read_text(encoding="ascii")

    with pytest.raises(WorkflowStateError):
        substage_status(run_directory)

    assert (tmp_path / "fake-counter").read_text(encoding="ascii") == before


def test_rehashed_nonnull_latest_auditor_before_any_audit_is_rejected(
    tmp_path: Path,
) -> None:
    responses = [codex_response("worker", "worker", worker_result("blocked"))]
    spec, _, fake = create_workflow_tree(tmp_path, responses=responses)
    paused = run_substage(
        spec,
        runs_dir=tmp_path / "runs",
        services=services(fake),
    )
    run_directory = Path(paused.artifact_directory)

    def mutate(entries: list[dict[str, Any]]) -> None:
        transition = next(
            item for item in entries if item["reason"] == "worker_blocked"
        )
        transition["state_updates"]["latest_audit_action_id"] = "auditor-r000"

    _rehash_journal(run_directory, mutate)
    _rewrite_state_and_result_field(
        run_directory,
        "latest_audit_action_id",
        "auditor-r000",
    )
    before = (tmp_path / "fake-counter").read_text(encoding="ascii")

    with pytest.raises(WorkflowStateError):
        substage_status(run_directory)

    assert (tmp_path / "fake-counter").read_text(encoding="ascii") == before


@pytest.mark.parametrize("operation", ["status", "resume", "continue", "abort"])
def test_rehashed_invalid_reason_code_is_rejected_by_every_existing_run_path(
    tmp_path: Path,
    operation: str,
) -> None:
    responses = [codex_response("worker", "worker", worker_result("blocked"))]
    spec, _, fake = create_workflow_tree(tmp_path, responses=responses)
    paused = run_substage(spec, runs_dir=tmp_path / "runs", services=services(fake))
    run_directory = Path(paused.artifact_directory)
    instruction = tmp_path / "instruction.md"
    instruction.write_text("Continue exactly.\n", encoding="utf-8")

    def mutate(entries: list[dict[str, Any]]) -> None:
        entry = next(
            item for item in entries if item["reason"] == "initial_worker_requested"
        )
        entry["reason"] = "syntactically_valid_but_undefined_reason"

    _rehash_journal(run_directory, mutate)
    before = (tmp_path / "fake-counter").read_text(encoding="ascii")

    with pytest.raises(WorkflowStateError):
        _invoke_existing_workflow_operation(
            operation,
            run_directory,
            fake,
            instruction,
        )

    assert (tmp_path / "fake-counter").read_text(encoding="ascii") == before


def test_rehashed_defined_but_wrong_evidence_reason_is_rejected(tmp_path: Path) -> None:
    responses = [codex_response("worker", "worker", worker_result("blocked"))]
    spec, _, fake = create_workflow_tree(tmp_path, responses=responses)
    paused = run_substage(spec, runs_dir=tmp_path / "runs", services=services(fake))
    run_directory = Path(paused.artifact_directory)

    def mutate(entries: list[dict[str, Any]]) -> None:
        entry = next(
            item for item in entries if item["reason"] == "worker_result_validated"
        )
        entry["reason"] = "worker_pause_evidence_saved"

    _rehash_journal(run_directory, mutate)

    with pytest.raises(WorkflowStateError):
        substage_status(run_directory)


@pytest.mark.parametrize("operation", ["status", "resume", "continue", "abort"])
def test_rehashed_worker_id_in_latest_auditor_field_is_rejected_everywhere(
    tmp_path: Path,
    operation: str,
) -> None:
    responses = [codex_response("worker", "worker", worker_result("blocked"))]
    spec, _, fake = create_workflow_tree(tmp_path, responses=responses)
    paused = run_substage(spec, runs_dir=tmp_path / "runs", services=services(fake))
    run_directory = Path(paused.artifact_directory)
    instruction = tmp_path / "instruction.md"
    instruction.write_text("Continue exactly.\n", encoding="utf-8")

    def mutate(entries: list[dict[str, Any]]) -> None:
        entry = next(
            item for item in entries if item["reason"] == "worker_result_validated"
        )
        entry["state_updates"]["latest_audit_action_id"] = "worker-r000"

    _rehash_journal(run_directory, mutate)
    _rewrite_state_and_result_field(
        run_directory,
        "latest_audit_action_id",
        "worker-r000",
    )
    before = (tmp_path / "fake-counter").read_text(encoding="ascii")

    with pytest.raises(WorkflowStateError):
        _invoke_existing_workflow_operation(
            operation,
            run_directory,
            fake,
            instruction,
        )

    assert (tmp_path / "fake-counter").read_text(encoding="ascii") == before


@pytest.mark.parametrize(
    "field,value",
    [
        ("latest_worker_action_id", "auditor-r000"),
        ("latest_audit_action_id", "nonexistent-r999"),
    ],
)
def test_rehashed_latest_action_wrong_kind_or_nonexistent_id_is_rejected(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    spec, _, fake = create_workflow_tree(tmp_path)
    completed = run_substage(spec, runs_dir=tmp_path / "runs", services=services(fake))
    run_directory = Path(completed.artifact_directory)

    def mutate(entries: list[dict[str, Any]]) -> None:
        entries[-1]["state_updates"][field] = value

    _rehash_journal(run_directory, mutate)
    _rewrite_state_and_result_field(run_directory, field, value)

    with pytest.raises(WorkflowStateError):
        substage_status(run_directory)


def test_rehashed_earlier_worker_cannot_replace_latest_completed_worker(
    tmp_path: Path,
) -> None:
    responses = [
        codex_response("worker", "worker", worker_result()),
        codex_response(
            "worker",
            "worker",
            worker_result(),
            expected_resume_thread_id="worker",
            write_files={"src/ready.txt": "ready\n"},
        ),
        codex_response("auditor", "auditor", auditor_result()),
    ]
    spec, _, fake = create_workflow_tree(
        tmp_path,
        responses=responses,
        test_requires_marker=True,
    )
    completed = run_substage(spec, runs_dir=tmp_path / "runs", services=services(fake))
    run_directory = Path(completed.artifact_directory)

    def mutate(entries: list[dict[str, Any]]) -> None:
        latest_update = next(
            item
            for item in reversed(entries)
            if "latest_worker_action_id" in item["state_updates"]
        )
        latest_update["state_updates"]["latest_worker_action_id"] = "worker-r000"

    _rehash_journal(run_directory, mutate)
    _rewrite_state_and_result_field(
        run_directory,
        "latest_worker_action_id",
        "worker-r000",
    )

    with pytest.raises(WorkflowStateError):
        substage_status(run_directory)


@pytest.mark.parametrize("corruption", ["record_kind", "completion_kind"])
def test_rehashed_action_kind_mutations_cannot_change_verified_lifecycle(
    tmp_path: Path,
    corruption: str,
) -> None:
    spec, _, fake = create_workflow_tree(tmp_path)
    completed = run_substage(spec, runs_dir=tmp_path / "runs", services=services(fake))
    run_directory = Path(completed.artifact_directory)
    worker_record = run_directory / "actions/worker-r000.json"

    if corruption == "record_kind":
        value = json.loads(worker_record.read_text())
        value["kind"] = "auditor"
        _write_json(worker_record, value)
        replacement_hash = hashlib.sha256(worker_record.read_bytes()).hexdigest()

        def mutate(entries: list[dict[str, Any]]) -> None:
            completion = next(
                item
                for item in entries
                if item["event_type"] == "action_completion"
                and item["action_id"] == "worker-r000"
            )
            completion["artifact_hashes"][str(worker_record)] = replacement_hash

    else:

        def mutate(entries: list[dict[str, Any]]) -> None:
            completion = next(
                item
                for item in entries
                if item["event_type"] == "action_completion"
                and item["action_id"] == "worker-r000"
            )
            completion["action_kind"] = "auditor"
            completion["reason"] = "auditor_action_completed"

    _rehash_journal(run_directory, mutate)

    with pytest.raises(WorkflowStateError):
        substage_status(run_directory)


@pytest.mark.parametrize(
    "corruption",
    [
        "mismatched_completion",
        "completion_without_intent",
        "duplicate_completion",
        "reordered_entries",
    ],
)
def test_validly_rehashed_but_semantically_invalid_journal_is_rejected(
    tmp_path: Path,
    corruption: str,
) -> None:
    spec, _, fake = create_workflow_tree(tmp_path)
    completed = run_substage(spec, runs_dir=tmp_path / "runs", services=services(fake))
    run_directory = Path(completed.artifact_directory)

    def mutate(entries: list[dict[str, Any]]) -> None:
        completion_index = next(
            index
            for index, entry in enumerate(entries)
            if entry["event_type"] == "action_completion"
        )
        intent_index = next(
            index
            for index, entry in enumerate(entries)
            if entry["event_type"] == "action_intent"
        )
        if corruption == "mismatched_completion":
            entries[completion_index]["action_id"] = "worker-r999"
        elif corruption == "completion_without_intent":
            entries[intent_index]["event_type"] = "evidence"
            entries[intent_index]["action_id"] = None
            entries[intent_index]["action_kind"] = None
        elif corruption == "duplicate_completion":
            entries.insert(completion_index + 1, dict(entries[completion_index]))
        else:
            entries[intent_index], entries[completion_index] = (
                entries[completion_index],
                entries[intent_index],
            )

    _rehash_journal(run_directory, mutate)

    with pytest.raises(WorkflowStateError):
        substage_status(run_directory)


@pytest.mark.parametrize("snapshot", ["journal_head", "result_snapshot"])
def test_state_journal_head_and_public_result_must_agree(
    tmp_path: Path,
    snapshot: str,
) -> None:
    spec, _, fake = create_workflow_tree(tmp_path)
    completed = run_substage(spec, runs_dir=tmp_path / "runs", services=services(fake))
    run_directory = Path(completed.artifact_directory)
    if snapshot == "journal_head":
        state_path = run_directory / "state.json"
        state = json.loads(state_path.read_text())
        state["journal_hash"] = "0" * 64
        _write_json(state_path, state)
    else:
        result_path = run_directory / "result.json"
        result = json.loads(result_path.read_text())
        result["summary"] = "replacement result snapshot"
        _write_json(result_path, result)

    with pytest.raises(WorkflowStateError):
        substage_status(run_directory)


@pytest.mark.parametrize(
    "crash_point",
    (
        "before_state_replacement",
        "after_state_replacement",
        "after_result_replacement",
    ),
)
def test_stage2_state_first_snapshot_crash_boundaries_recover_historically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    crash_point: str,
) -> None:
    spec, _, fake = create_workflow_tree(
        tmp_path,
        responses=[
            codex_response("worker", "worker", worker_result("needs_human")),
        ],
    )
    paused = run_substage(
        spec,
        runs_dir=tmp_path / "runs",
        services=services(fake),
    )
    run_directory = Path(paused.artifact_directory)
    state_path = run_directory / "state.json"
    result_path = run_directory / "result.json"
    state_before = state_path.read_bytes()
    result_before = result_path.read_bytes()
    crashed = False

    def crash(point: str) -> None:
        nonlocal crashed
        if not crashed and point == crash_point:
            crashed = True
            raise KeyboardInterrupt(point)

    monkeypatch.setattr(workflow_engine, "_snapshot_checkpoint", crash)
    with pytest.raises(KeyboardInterrupt, match=crash_point):
        abort_substage(run_directory, "stop", services=services(fake))
    assert crashed

    state_after = state_path.read_bytes()
    result_after = result_path.read_bytes()
    if crash_point == "before_state_replacement":
        assert state_after == state_before
        assert result_after == result_before
    elif crash_point == "after_state_replacement":
        assert state_after != state_before
        assert result_after == result_before
        assert json.loads(state_after)["status"] == "aborted"
    else:
        assert state_after != state_before
        assert result_after != result_before
        assert json.loads(state_after)["status"] == "aborted"
        assert json.loads(result_after)["status"] == "aborted"

    if crash_point == "after_result_replacement":
        assert substage_status(run_directory).status == "aborted"
    else:
        with pytest.raises(WorkflowStateError):
            substage_status(run_directory)

    monkeypatch.setattr(workflow_engine, "_snapshot_checkpoint", lambda _name: None)
    with pytest.raises(WorkflowInputError, match="terminal"):
        abort_substage(run_directory, "stop", services=services(fake))
    recovered = substage_status(run_directory)
    assert recovered.status == "aborted"
    assert json.loads(state_path.read_bytes())["status"] == "aborted"
    assert json.loads(result_path.read_bytes())["status"] == "aborted"
    journal = [
        json.loads(line)
        for line in (run_directory / "journal.jsonl").read_bytes().splitlines()
    ]
    assert sum(entry["reason"] == "human_abort" for entry in journal) == 1


def test_continue_and_abort_reject_altered_prior_action_evidence(tmp_path: Path) -> None:
    instruction = tmp_path / "instruction.md"
    instruction.write_text("Continue exactly.\n", encoding="utf-8")
    for command in ("continue", "abort"):
        root = tmp_path / command
        spec, _, fake = create_workflow_tree(
            root,
            responses=[
                codex_response("worker", "worker", worker_result("blocked")),
            ],
        )
        paused = run_substage(spec, runs_dir=root / "runs", services=services(fake))
        run_directory = Path(paused.artifact_directory)
        (run_directory / "actions/worker-r000.json").write_text("{}\n", encoding="utf-8")
        with pytest.raises(WorkflowStateError):
            if command == "continue":
                continue_substage(run_directory, instruction, services=services(fake))
            else:
                abort_substage(run_directory, "stop", services=services(fake))


def test_status_rejects_altered_escalation_package(tmp_path: Path) -> None:
    spec, _, fake = create_workflow_tree(
        tmp_path,
        responses=[codex_response("worker", "worker", worker_result("blocked"))],
    )
    paused = run_substage(spec, runs_dir=tmp_path / "runs", services=services(fake))
    run_directory = Path(paused.artifact_directory)
    (run_directory / "escalation/package.json").write_text("{}\n", encoding="utf-8")

    with pytest.raises(WorkflowStateError):
        substage_status(run_directory)


def test_lock_metadata_stale_live_foreign_and_malformed_boundaries(tmp_path: Path) -> None:
    spec, _, fake = create_workflow_tree(tmp_path)
    result = run_substage(spec, runs_dir=tmp_path / "runs", services=services(fake))
    run_directory = Path(result.artifact_directory)
    lock = run_directory / "workflow.lock"

    _write_json(
        lock,
        {
            "schema_version": 1,
            "pid": 999_999_999,
            "host": socket.gethostname(),
            "started_at": "2026-01-01T00:00:00Z",
        },
    )
    with _WorkflowLock(run_directory, services(fake).utc_now):
        pass

    _write_json(
        lock,
        {
            "schema_version": 1,
            "pid": 1,
            "host": socket.gethostname(),
            "started_at": "2026-01-01T00:00:00Z",
        },
    )
    with (
        pytest.raises(WorkflowLockError, match="live local"),
        _WorkflowLock(run_directory, services(fake).utc_now),
    ):
        pass

    _write_json(
        lock,
        {
            "schema_version": 1,
            "pid": 999_999_999,
            "host": "foreign.invalid",
            "started_at": "2026-01-01T00:00:00Z",
        },
    )
    with (
        pytest.raises(WorkflowLockError, match="foreign-host"),
        _WorkflowLock(run_directory, services(fake).utc_now),
    ):
        pass

    lock.write_text("{", encoding="utf-8")
    with (
        pytest.raises(WorkflowLockError, match="metadata is invalid"),
        _WorkflowLock(run_directory, services(fake).utc_now),
    ):
        pass


@pytest.mark.parametrize("kind", ["worker", "auditor"])
def test_complete_codex_proof_recovers_once_without_duplicate_launch(
    tmp_path: Path,
    kind: str,
) -> None:
    run_directory, fake = _interrupted_codex_run(tmp_path, interrupt_role=kind)
    before = (tmp_path / "fake-counter").read_text(encoding="ascii")

    result = resume_substage(run_directory, services=services(fake))

    assert result.status == "completed"
    expected = "1" if kind == "worker" else "2"
    assert before == expected
    final_count = "2" if kind == "worker" else "2"
    assert (tmp_path / "fake-counter").read_text(encoding="ascii") == final_count


def test_interrupted_auditor_recreates_same_scratch_without_duplicate_launch(
    tmp_path: Path,
) -> None:
    run_directory, fake = _interrupted_codex_run(
        tmp_path,
        interrupt_role="auditor",
    )
    artifact = run_directory / "audits/codex/auditor-r000"
    scratch = artifact / "scratch"
    before = (tmp_path / "fake-counter").read_text(encoding="ascii")
    scratch.rmdir()

    result = resume_substage(run_directory, services=services(fake))

    assert result.status == "completed"
    assert scratch.is_dir()
    assert (tmp_path / "fake-counter").read_text(encoding="ascii") == before
    assert len(list((run_directory / "actions").glob("auditor-*.json"))) == 1


@pytest.mark.parametrize("boundary", ["before_intent", "after_completion_snapshot"])
def test_worker_crash_boundaries_do_not_duplicate_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    spec, _, fake = create_workflow_tree(tmp_path)
    if boundary == "before_intent":
        original_intent = workflow_engine._codex_action_intent
        interrupted = False

        def stop_before_intent(*args: object, **kwargs: object) -> object:
            nonlocal interrupted
            if not interrupted:
                interrupted = True
                raise KeyboardInterrupt
            return original_intent(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(workflow_engine, "_codex_action_intent", stop_before_intent)
    else:
        original_completion = workflow_engine._action_completion
        interrupted = False

        def stop_after_snapshot(*args: object, **kwargs: object) -> object:
            nonlocal interrupted
            result = original_completion(*args, **kwargs)  # type: ignore[arg-type]
            record = args[2]
            if (
                not interrupted
                and isinstance(record, dict)
                and record.get("kind") == "worker"
            ):
                interrupted = True
                raise KeyboardInterrupt
            return result

        monkeypatch.setattr(workflow_engine, "_action_completion", stop_after_snapshot)
    with pytest.raises(KeyboardInterrupt):
        run_substage(spec, runs_dir=tmp_path / "runs", services=services(fake))
    monkeypatch.undo()
    run_directory = next((tmp_path / "runs").iterdir())

    result = resume_substage(run_directory, services=services(fake))

    assert result.status == "completed"
    assert (tmp_path / "fake-counter").read_text(encoding="ascii") == "2"


@pytest.mark.parametrize("kind", ["test", "auditor"])
def test_test_and_auditor_state_snapshot_boundary_does_not_repeat_action(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    spec, _, fake = create_workflow_tree(tmp_path)
    original_completion = workflow_engine._action_completion
    interrupted = False
    test_calls: list[str] = []

    def stop_after_snapshot(*args: object, **kwargs: object) -> object:
        nonlocal interrupted
        result = original_completion(*args, **kwargs)  # type: ignore[arg-type]
        record = args[2]
        if (
            not interrupted
            and isinstance(record, dict)
            and record.get("kind") == kind
        ):
            interrupted = True
            raise KeyboardInterrupt
        return result

    def counted_test(prepared_test, artifact_directory, action_id, **kwargs):
        test_calls.append(action_id)
        return run_test_attempt(
            prepared_test,
            artifact_directory,
            action_id,
            **kwargs,
        )

    monkeypatch.setattr(workflow_engine, "_action_completion", stop_after_snapshot)
    with pytest.raises(KeyboardInterrupt):
        run_substage(
            spec,
            runs_dir=tmp_path / "runs",
            services=WorkflowServices(
                codex_executable=str(fake),
                test_invoker=(
                    counted_test  # type: ignore[arg-type]
                    if kind == "test"
                    else run_test_attempt
                ),
            ),
        )
    monkeypatch.undo()
    run_directory = next((tmp_path / "runs").iterdir())

    result = resume_substage(run_directory, services=services(fake))

    assert result.status == "completed"
    assert (tmp_path / "fake-counter").read_text(encoding="ascii") == "2"
    if kind == "test":
        assert len(test_calls) == 1


@pytest.mark.parametrize("kind", ["test", "auditor"])
def test_test_and_auditor_crash_before_intent_launches_once_on_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    spec, _, fake = create_workflow_tree(tmp_path)
    interrupted = False
    if kind == "test":
        original = workflow_engine._test_action_intent

        def stop_before_test_intent(*args: object, **kwargs: object) -> object:
            nonlocal interrupted
            if not interrupted:
                interrupted = True
                raise KeyboardInterrupt
            return original(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(
            workflow_engine,
            "_test_action_intent",
            stop_before_test_intent,
        )
    else:
        original_codex = workflow_engine._codex_action_intent

        def stop_before_auditor_intent(*args: object, **kwargs: object) -> object:
            nonlocal interrupted
            if not interrupted and args[2] == "auditor":
                interrupted = True
                raise KeyboardInterrupt
            return original_codex(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(
            workflow_engine,
            "_codex_action_intent",
            stop_before_auditor_intent,
        )
    with pytest.raises(KeyboardInterrupt):
        run_substage(spec, runs_dir=tmp_path / "runs", services=services(fake))
    monkeypatch.undo()
    run_directory = next((tmp_path / "runs").iterdir())

    result = resume_substage(run_directory, services=services(fake))

    assert result.status == "completed"
    assert (tmp_path / "fake-counter").read_text(encoding="ascii") == "2"


@pytest.mark.parametrize("kind", ["test", "auditor"])
def test_test_and_auditor_intent_without_launch_pauses_without_retry(
    tmp_path: Path,
    kind: str,
) -> None:
    spec, _, fake = create_workflow_tree(tmp_path)
    attempted: list[str] = []

    def interrupt_test(prepared_test, artifact_directory, action_id, **kwargs):
        del prepared_test, artifact_directory, kwargs
        attempted.append(action_id)
        raise KeyboardInterrupt

    def interrupt_auditor(prepared, **kwargs):
        if prepared.request.role == "auditor":
            attempted.append(prepared.request.run_id)
            raise KeyboardInterrupt
        return run_prepared_codex(prepared, **kwargs)

    with pytest.raises(KeyboardInterrupt):
        run_substage(
            spec,
            runs_dir=tmp_path / "runs",
            services=WorkflowServices(
                codex_executable=str(fake),
                codex_invoker=(
                    interrupt_auditor  # type: ignore[arg-type]
                    if kind == "auditor"
                    else run_prepared_codex
                ),
                test_invoker=(
                    interrupt_test  # type: ignore[arg-type]
                    if kind == "test"
                    else run_test_attempt
                ),
            ),
        )
    run_directory = next((tmp_path / "runs").iterdir())

    result = resume_substage(run_directory, services=services(fake))

    assert result.status == "human_paused"
    assert result.pause_reason == "uncertain_in_flight_action"
    assert len(attempted) == 1
    assert (tmp_path / "fake-counter").read_text(encoding="ascii") == "1"


@pytest.mark.parametrize("kind", ["worker", "test", "auditor"])
def test_partial_external_action_artifacts_never_advance_or_retry(
    tmp_path: Path,
    kind: str,
) -> None:
    spec, _, fake = create_workflow_tree(tmp_path)
    attempted: list[str] = []

    def partial_codex(prepared, **kwargs):
        if prepared.request.role == kind:
            destination = Path(kwargs["runs_dir"]) / prepared.request.run_id
            destination.mkdir(parents=True)
            (destination / "request.normalized.json").write_text(
                "{}\n",
                encoding="utf-8",
            )
            attempted.append(prepared.request.run_id)
            raise KeyboardInterrupt
        return run_prepared_codex(prepared, **kwargs)

    def partial_test(prepared_test, artifact_directory, action_id, **kwargs):
        del prepared_test, kwargs
        artifact_directory.mkdir(parents=True)
        (artifact_directory / "stdout.log").write_text("partial\n", encoding="utf-8")
        attempted.append(action_id)
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_substage(
            spec,
            runs_dir=tmp_path / "runs",
            services=WorkflowServices(
                codex_executable=str(fake),
                codex_invoker=partial_codex,  # type: ignore[arg-type]
                test_invoker=(
                    partial_test  # type: ignore[arg-type]
                    if kind == "test"
                    else run_test_attempt
                ),
            ),
        )
    run_directory = next((tmp_path / "runs").iterdir())
    before = (
        (tmp_path / "fake-counter").read_text(encoding="ascii")
        if (tmp_path / "fake-counter").exists()
        else "0"
    )

    result = resume_substage(run_directory, services=services(fake))

    assert result.status == "human_paused"
    assert result.pause_reason == "uncertain_in_flight_action"
    assert len(attempted) == 1
    after = (
        (tmp_path / "fake-counter").read_text(encoding="ascii")
        if (tmp_path / "fake-counter").exists()
        else "0"
    )
    assert after == before


@pytest.mark.parametrize("kind", ["worker", "test", "auditor"])
def test_completion_journal_before_state_snapshot_is_replayed_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    spec, _, fake = create_workflow_tree(tmp_path)
    original_persist = workflow_engine._persist_state
    interrupted = False
    test_calls: list[str] = []
    target = {
        "worker": "worker-r000",
        "test": "test-r000-000-fixed-test-92297458",
        "auditor": "auditor-r000",
    }[kind]

    def interrupt_snapshot(run_directory: Path, state: object) -> None:
        nonlocal interrupted
        pending = getattr(state, "pending_action", object())
        completed = getattr(state, "completed_action_ids", ())
        if not interrupted and pending is None and target in completed:
            interrupted = True
            raise KeyboardInterrupt
        original_persist(run_directory, state)  # type: ignore[arg-type]

    def counted_test(prepared_test, artifact_directory, action_id, **kwargs):
        test_calls.append(action_id)
        return run_test_attempt(
            prepared_test,
            artifact_directory,
            action_id,
            **kwargs,
        )

    monkeypatch.setattr(workflow_engine, "_persist_state", interrupt_snapshot)
    with pytest.raises(KeyboardInterrupt):
        run_substage(
            spec,
            runs_dir=tmp_path / "runs",
            services=WorkflowServices(
                codex_executable=str(fake),
                test_invoker=(
                    counted_test  # type: ignore[arg-type]
                    if kind == "test"
                    else run_test_attempt
                ),
            ),
        )
    monkeypatch.undo()
    run_directory = next((tmp_path / "runs").iterdir())

    result = resume_substage(run_directory, services=services(fake))

    assert result.status == "completed"
    assert (tmp_path / "fake-counter").read_text(encoding="ascii") == "2"
    if kind == "test":
        assert len(test_calls) == 1


@pytest.mark.parametrize(
    "source",
    ["specification", "contract", "initial_prompt", "repair_prompt", "auditor_prompt"],
)
def test_frozen_input_drift_pauses_before_recovery_launch(
    tmp_path: Path,
    source: str,
) -> None:
    spec, project, fake = create_workflow_tree(tmp_path)

    def interrupt(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise KeyboardInterrupt

    interrupted = WorkflowServices(
        codex_executable=str(fake),
        codex_invoker=interrupt,  # type: ignore[arg-type]
    )
    with pytest.raises(KeyboardInterrupt):
        run_substage(spec, runs_dir=tmp_path / "runs", services=interrupted)
    paths = {
        "specification": spec,
        "contract": project / "control/contract.md",
        "initial_prompt": project / "control/worker-initial.md",
        "repair_prompt": project / "control/worker-repair.md",
        "auditor_prompt": project / "control/auditor.md",
    }
    paths[source].write_text("drifted frozen input\n", encoding="utf-8")
    run_directory = next((tmp_path / "runs").iterdir())

    result = resume_substage(run_directory, services=services(fake))

    assert result.status == "human_paused"
    assert result.pause_reason == "frozen_input_drift"
    assert not (tmp_path / "fake-counter").exists()


def test_status_rejects_frozen_specification_drift(tmp_path: Path) -> None:
    spec, _, fake = create_workflow_tree(tmp_path)
    completed = run_substage(spec, runs_dir=tmp_path / "runs", services=services(fake))
    spec.write_text("schema_version: 1\n", encoding="utf-8")

    with pytest.raises(WorkflowStateError):
        substage_status(Path(completed.artifact_directory))


@pytest.mark.parametrize("drift", ["head", "branch"])
def test_repository_head_and_branch_identity_drift_pause_recovery(
    tmp_path: Path,
    drift: str,
) -> None:
    spec, project, fake = create_workflow_tree(tmp_path)

    def interrupt(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_substage(
            spec,
            runs_dir=tmp_path / "runs",
            services=WorkflowServices(
                codex_executable=str(fake),
                codex_invoker=interrupt,  # type: ignore[arg-type]
            ),
        )
    if drift == "head":
        (project / "src/drift.txt").write_text("drift\n", encoding="utf-8")
        git(project, "add", "src/drift.txt")
        git(project, "commit", "-q", "-m", "drift")
    else:
        git(project, "switch", "-q", "-c", "identity-drift")
    run_directory = next((tmp_path / "runs").iterdir())

    result = resume_substage(run_directory, services=services(fake))

    assert result.status == "human_paused"
    assert result.pause_reason == "repository_identity_drift"
    assert not (tmp_path / "fake-counter").exists()


@pytest.mark.parametrize("artifact", ["handoff", "output_schema"])
def test_recovery_rejects_altered_engine_owned_prompt_evidence(
    tmp_path: Path,
    artifact: str,
) -> None:
    run_directory, fake = _interrupted_codex_run(tmp_path, interrupt_role="worker")
    path = (
        run_directory / "handoffs/worker-r000.json"
        if artifact == "handoff"
        else run_directory / "handoffs/worker-output-schema.json"
    )
    path.write_text("{}\n", encoding="utf-8")

    with pytest.raises(WorkflowStateError):
        resume_substage(run_directory, services=services(fake))
