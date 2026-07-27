from __future__ import annotations

import hashlib
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from research_automation_supervisor.codex_adapter import run_prepared_codex
from research_automation_supervisor.errors import (
    ShadowInputError,
    ShadowIntegrityError,
    ShadowLockError,
    ShadowStateError,
)
from research_automation_supervisor.shadow_engine import (
    ShadowServices,
    _ShadowLock,
    abort_shadow_calibration,
    record_shadow_review,
    resume_shadow_calibration,
    run_shadow_calibration,
    shadow_calibration_report,
    shadow_calibration_status,
)
from research_automation_supervisor.workflow_engine import (
    WorkflowServices,
    run_substage,
)
from tests.shadow_helpers import (
    SECOND_SUPERVISOR_UUID,
    SOURCE_AUDITOR_UUID,
    SOURCE_WORKER_UUID,
    SUPERVISOR_UUID,
    create_human_continuation_shadow_tree,
    create_shadow_specification,
    create_shadow_tree,
    shadow_services,
    supervisor_proposal,
    supervisor_response,
    write_review,
)
from tests.workflow_helpers import (
    auditor_result,
    codex_response,
    create_workflow_tree,
    worker_result,
)


def test_run_uses_one_exact_id_read_only_session_and_never_persists_blind_input(
    tmp_path: Path,
) -> None:
    spec, source_run, project, fake = create_shadow_tree(tmp_path)
    source_before = {
        path.relative_to(source_run): path.read_bytes()
        for path in source_run.rglob("*")
        if path.is_file()
    }

    result = run_shadow_calibration(
        spec,
        runs_dir=tmp_path / "shadow-runs",
        services=shadow_services(fake),
    )

    assert result.status == "awaiting_reviews"
    assert result.proposal_count == 2
    assert result.comparison_count == 2
    assert result.supervisor_session_id == SUPERVISOR_UUID
    run_directory = Path(result.artifact_directory)
    initial_metadata = json.loads(
        (
            run_directory
            / "proposals/worker_initial-r000-a001/stage1-run/metadata.json"
        ).read_text()
    )
    resumed_metadata = json.loads(
        (
            run_directory
            / "proposals/auditor-r000-a002/stage1-run/metadata.json"
        ).read_text()
    )
    assert initial_metadata["sandbox"] == "read-only"
    assert initial_metadata["ephemeral"] is False
    assert resumed_metadata["resume_thread_id"] == SUPERVISOR_UUID
    command = resumed_metadata["command"]
    assert command[command.index("resume") + 1] == SUPERVISOR_UUID
    assert "--last" not in command and "--all" not in command
    assert "--skip-git-repo-check" not in initial_metadata["command"]
    assert "--skip-git-repo-check" not in command
    assert not list(run_directory.rglob("*blind*prompt*"))
    assert {
        path.relative_to(source_run): path.read_bytes()
        for path in source_run.rglob("*")
        if path.is_file()
    } == source_before
    authoritative = project / "control/worker-initial.md"
    stage1_bytes = b"".join(
        path.read_bytes()
        for path in run_directory.glob(
            "proposals/*/stage1-run/*"
        )
        if path.is_file()
    )
    assert authoritative.read_bytes() not in stage1_bytes
    assert shadow_calibration_status(run_directory) == result


def test_authoritative_comparison_does_not_exist_before_supervisor_returns(
    tmp_path: Path,
) -> None:
    spec, _, _, fake = create_shadow_tree(tmp_path)

    def asserting_invoker(prepared, **kwargs: object):
        proposal_directory = Path(str(kwargs["runs_dir"]))
        run_root = proposal_directory.parents[1]
        comparison = run_root / "comparisons" / proposal_directory.name
        assert not any(comparison.rglob("authoritative-*.md"))
        assert b"Implement the substage." not in prepared.prompt_bytes
        return run_prepared_codex(prepared, **kwargs)  # type: ignore[arg-type]

    result = run_shadow_calibration(
        spec,
        runs_dir=tmp_path / "runs",
        services=ShadowServices(
            codex_executable=str(fake),
            supervisor_invoker=asserting_invoker,
        ),
    )
    assert result.status == "awaiting_reviews"


def test_complete_stage1_action_recovers_without_duplicate_launch(
    tmp_path: Path,
) -> None:
    spec, _, _, fake = create_shadow_tree(tmp_path)
    interrupted = False

    def complete_then_interrupt(prepared, **kwargs: object):
        nonlocal interrupted
        result = run_prepared_codex(
            prepared, **kwargs  # type: ignore[arg-type]
        )
        if not interrupted:
            interrupted = True
            raise KeyboardInterrupt
        return result

    services = ShadowServices(
        codex_executable=str(fake),
        supervisor_invoker=complete_then_interrupt,
    )
    with pytest.raises(KeyboardInterrupt):
        run_shadow_calibration(
            spec,
            runs_dir=tmp_path / "runs",
            services=services,
        )
    run_directory = next((tmp_path / "runs").iterdir())
    assert (tmp_path / "shadow-counter").read_text() == "1"

    result = resume_shadow_calibration(
        run_directory, services=shadow_services(fake)
    )

    assert result.status == "awaiting_reviews"
    assert (tmp_path / "shadow-counter").read_text() == "2"


def test_uncertain_action_pauses_and_malformed_result_pauses(
    tmp_path: Path,
) -> None:
    spec, _, _, fake = create_shadow_tree(tmp_path / "uncertain")

    def interrupt(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_shadow_calibration(
            spec,
            runs_dir=tmp_path / "uncertain-runs",
            services=ShadowServices(
                codex_executable=str(fake),
                supervisor_invoker=interrupt,  # type: ignore[arg-type]
            ),
        )
    run_directory = next((tmp_path / "uncertain-runs").iterdir())
    paused = resume_shadow_calibration(
        run_directory, services=shadow_services(fake)
    )
    assert paused.status == "human_paused"
    assert (
        paused.pause_reason
        == "supervisor_action_completion_unprovable"
    )
    assert not (tmp_path / "uncertain/shadow-counter").exists()

    responses = [
        supervisor_response("worker_initial", final="not-json"),
    ]
    spec, _, _, fake = create_shadow_tree(
        tmp_path / "malformed",
        supervisor_responses=responses,
    )
    malformed = run_shadow_calibration(
        spec,
        runs_dir=tmp_path / "malformed-runs",
        services=shadow_services(fake),
    )
    assert malformed.status == "human_paused"
    assert malformed.pause_reason == "supervisor_result_malformed"

    schema_invalid_value = json.loads(
        supervisor_proposal("worker_initial")
    )
    schema_invalid_value.pop("required_checks")
    spec, _, _, fake = create_shadow_tree(
        tmp_path / "schema-invalid",
        supervisor_responses=[
            supervisor_response(
                "worker_initial",
                final=json.dumps(schema_invalid_value, sort_keys=True),
            )
        ],
    )
    schema_invalid = run_shadow_calibration(
        spec,
        runs_dir=tmp_path / "schema-invalid-runs",
        services=shadow_services(fake),
    )
    assert schema_invalid.status == "human_paused"
    assert (
        schema_invalid.pause_reason == "supervisor_result_malformed"
    )


def test_mutated_completed_assessment_fails_status_integrity(
    tmp_path: Path,
) -> None:
    spec, _, _, fake = create_shadow_tree(tmp_path)
    result = run_shadow_calibration(
        spec,
        runs_dir=tmp_path / "runs",
        services=shadow_services(fake),
    )
    run_directory = Path(result.artifact_directory)
    assessment = (
        run_directory
        / "proposals/worker_initial-r000-a001/assessment.json"
    )
    assessment.write_text("{}\n", encoding="utf-8")

    with pytest.raises(ShadowStateError):
        shadow_calibration_status(run_directory)


def test_stage3_journal_and_every_finalized_artifact_are_hash_bound(
    tmp_path: Path,
) -> None:
    spec, _, _, fake = create_shadow_tree(tmp_path)
    result = run_shadow_calibration(
        spec,
        runs_dir=tmp_path / "runs",
        services=shadow_services(fake),
    )
    run_directory = Path(result.artifact_directory)
    paths = [
        run_directory / "journal.jsonl",
        run_directory / "decision-points.json",
        run_directory
        / "supervisor/supervisor-worker_initial-r000-a001.json",
        run_directory
        / "proposals/worker_initial-r000-a001"
        / "blind-input-manifest.json",
        run_directory
        / "proposals/worker_initial-r000-a001/assessment.json",
        run_directory
        / "comparisons/worker_initial-r000-a001/comparison.json",
    ]
    for path in paths:
        original = path.read_bytes()
        path.write_bytes(original + b" ")
        with pytest.raises(ShadowStateError):
            shadow_calibration_status(run_directory)
        path.write_bytes(original)


def test_requested_change_and_oversize_are_deterministically_disqualified(
    tmp_path: Path,
) -> None:
    requested = supervisor_response("worker_initial")
    requested["final"] = supervisor_proposal(
        "worker_initial", requested_change=True
    )
    auditor = supervisor_response(
        "auditor",
        expected_resume_thread_id=SUPERVISOR_UUID,
    )
    spec, _, _, fake = create_shadow_tree(
        tmp_path / "requested",
        supervisor_responses=[requested, auditor],
    )
    result = run_shadow_calibration(
        spec,
        runs_dir=tmp_path / "requested-runs",
        services=shadow_services(fake),
    )
    assert result.disqualification_count == 1
    assessment = json.loads(
        (
            Path(result.artifact_directory)
            / "proposals/worker_initial-r000-a001/assessment.json"
        ).read_text()
    )
    assert assessment["disqualified"] is True
    assert (
        "contract_change_requested"
        in assessment["disqualification_reasons"]
    )

    oversized = supervisor_response("worker_initial")
    oversized_value = json.loads(supervisor_proposal("worker_initial"))
    oversized_value["prompt"] = "x" * 1500
    oversized["final"] = json.dumps(oversized_value, sort_keys=True)
    spec, _, _, fake = create_shadow_tree(
        tmp_path / "oversized",
        supervisor_responses=[
            oversized,
            supervisor_response(
                "auditor",
                expected_resume_thread_id=SUPERVISOR_UUID,
            ),
        ],
    )
    specification = yaml.safe_load(spec.read_text(encoding="utf-8"))
    specification["max_proposal_bytes"] = 1024
    spec.write_text(
        yaml.safe_dump(specification, sort_keys=False),
        encoding="utf-8",
    )
    result = run_shadow_calibration(
        spec,
        runs_dir=tmp_path / "oversized-runs",
        services=shadow_services(fake),
    )
    oversized_assessment = json.loads(
        (
            Path(result.artifact_directory)
            / "proposals/worker_initial-r000-a001/assessment.json"
        ).read_text()
    )
    assert oversized_assessment["size_compliant"] is False
    assert "proposal_size_exceeded" in (
        oversized_assessment["disqualification_reasons"]
    )


@pytest.mark.parametrize(
    "observed",
    [
        "friendly-session-name",
        "alias:supervisor",
        "not-a-uuid",
        "00000000-0000-0000-0000-000000000000",
        SUPERVISOR_UUID.upper(),
        f" {SUPERVISOR_UUID}",
        f"{SUPERVISOR_UUID} ",
    ],
)
def test_initial_supervisor_identity_rejects_every_noncanonical_selector(
    tmp_path: Path,
    observed: str,
) -> None:
    responses = [
        supervisor_response(
            "worker_initial", thread_id=observed
        )
    ]
    spec, _, _, fake = create_shadow_tree(
        tmp_path, supervisor_responses=responses
    )

    result = run_shadow_calibration(
        spec,
        runs_dir=tmp_path / "runs",
        services=shadow_services(fake),
    )

    assert result.status == "human_paused"
    assert result.pause_reason == "supervisor_session_integrity_failed"
    assert result.supervisor_session_id is None
    assert (tmp_path / "shadow-counter").read_text() == "1"


def test_initial_supervisor_identity_rejects_missing_ambiguous_and_metadata_only(
    tmp_path: Path,
) -> None:
    variants = [
        [{"type": "item.completed", "thread_id": SUPERVISOR_UUID}],
        [
            {"type": "thread.started", "thread_id": SUPERVISOR_UUID},
            {
                "type": "thread.started",
                "thread_id": SECOND_SUPERVISOR_UUID,
            },
        ],
        [{"type": "thread.started"}],
    ]
    for index, events in enumerate(variants):
        response = supervisor_response("worker_initial")
        response["stdout_lines"] = [
            json.dumps(event) for event in events
        ]
        root = tmp_path / str(index)
        spec, _, _, fake = create_shadow_tree(
            root, supervisor_responses=[response]
        )
        result = run_shadow_calibration(
            spec,
            runs_dir=root / "runs",
            services=shadow_services(fake),
        )
        assert result.status == "human_paused"
        assert result.supervisor_session_id is None
        assert (root / "shadow-counter").read_text() == "1"


def test_resumed_supervisor_must_emit_the_same_canonical_uuid(
    tmp_path: Path,
) -> None:
    spec, _, _, fake = create_shadow_tree(
        tmp_path,
        supervisor_responses=[
            supervisor_response("worker_initial"),
            supervisor_response(
                "auditor",
                thread_id=SECOND_SUPERVISOR_UUID,
                expected_resume_thread_id=SUPERVISOR_UUID,
            ),
        ],
    )

    result = run_shadow_calibration(
        spec,
        runs_dir=tmp_path / "runs",
        services=shadow_services(fake),
    )

    assert result.status == "human_paused"
    assert result.supervisor_session_id == SUPERVISOR_UUID
    assert result.pause_reason == "supervisor_session_integrity_failed"
    assert (tmp_path / "shadow-counter").read_text() == "2"


@pytest.mark.parametrize(
    "source_uuid",
    [SOURCE_WORKER_UUID, SOURCE_AUDITOR_UUID],
)
def test_supervisor_cannot_reuse_a_source_worker_or_auditor_uuid(
    tmp_path: Path,
    source_uuid: str,
) -> None:
    stage2_spec, project, fake = create_workflow_tree(
        tmp_path / "stage2",
        responses=[
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
    source = run_substage(
        stage2_spec,
        runs_dir=tmp_path / "source-runs",
        services=WorkflowServices(codex_executable=str(fake)),
    )
    spec = create_shadow_specification(
        tmp_path,
        Path(source.artifact_directory),
        project,
        supervisor_responses=[
            supervisor_response("worker_initial", thread_id=source_uuid)
        ],
    )

    result = run_shadow_calibration(
        spec,
        runs_dir=tmp_path / "runs",
        services=shadow_services(fake),
    )

    assert result.status == "human_paused"
    assert result.supervisor_session_id is None
    assert result.pause_reason == "supervisor_session_integrity_failed"


def test_paused_non_uuid_identity_has_no_replacement_launch(
    tmp_path: Path,
) -> None:
    spec, _, _, fake = create_shadow_tree(
        tmp_path,
        supervisor_responses=[
            supervisor_response(
                "worker_initial", thread_id="friendly-name"
            )
        ],
    )
    result = run_shadow_calibration(
        spec,
        runs_dir=tmp_path / "runs",
        services=shadow_services(fake),
    )
    run_directory = Path(result.artifact_directory)
    journal_before = (run_directory / "journal.jsonl").read_bytes()

    with pytest.raises(ShadowInputError):
        resume_shadow_calibration(
            run_directory, services=shadow_services(fake)
        )

    assert (tmp_path / "shadow-counter").read_text() == "1"
    assert (run_directory / "journal.jsonl").read_bytes() == journal_before


def test_fake_stage3_mode_rejects_non_uuid_resume_selector(
    tmp_path: Path,
) -> None:
    _, _, project, fake = create_shadow_tree(tmp_path)
    (project / ".fake-codex.json").write_text(
        json.dumps(
            {
                "responses": [
                    {
                        "require_stage3_policy": True,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(fake),
            "--ask-for-approval",
            "never",
            "--sandbox",
            "read-only",
            "--cd",
            str(project),
            "exec",
            "resume",
            "friendly-name",
        ],
        cwd=project,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert "canonical UUID" in completed.stderr


@pytest.mark.parametrize(
    "mutation",
    [
        "incomplete",
        "metadata_run_id",
        "metadata_prompt",
        "metadata_schema",
        "metadata_session",
        "result",
        "events",
        "final_message",
        "completion_manifest",
    ],
)
def test_interrupted_contradictory_action_pauses_once_without_relaunch(
    tmp_path: Path,
    mutation: str,
) -> None:
    spec, _, _, fake = create_shadow_tree(tmp_path)
    call_count = 0

    def interrupt_second(prepared, **kwargs: object):
        nonlocal call_count
        call_count += 1
        result = run_prepared_codex(
            prepared, **kwargs  # type: ignore[arg-type]
        )
        if call_count == 2:
            raise KeyboardInterrupt
        return result

    with pytest.raises(KeyboardInterrupt):
        run_shadow_calibration(
            spec,
            runs_dir=tmp_path / "runs",
            services=ShadowServices(
                codex_executable=str(fake),
                supervisor_invoker=interrupt_second,
            ),
        )
    run_directory = next((tmp_path / "runs").iterdir())
    stage1 = (
        run_directory
        / "proposals/auditor-r000-a002/stage1-run"
    )
    if mutation == "incomplete":
        (stage1 / "metadata.json").unlink()
    elif mutation == "completion_manifest":
        completion = json.loads(
            (stage1 / "stage2-completion.json").read_text()
        )
        completion["run_id"] = "replaced-run"
        (stage1 / "stage2-completion.json").write_text(
            json.dumps(completion, sort_keys=True) + "\n"
        )
    elif mutation in {
        "metadata_run_id",
        "metadata_prompt",
        "metadata_schema",
        "metadata_session",
    }:
        metadata_path = stage1 / "metadata.json"
        metadata = json.loads(metadata_path.read_text())
        field = {
            "metadata_run_id": "run_id",
            "metadata_prompt": "prompt_sha256",
            "metadata_schema": "output_schema_sha256",
            "metadata_session": "resume_thread_id",
        }[mutation]
        metadata[field] = (
            SECOND_SUPERVISOR_UUID
            if mutation == "metadata_session"
            else "0" * 64
            if field.endswith("sha256")
            else "wrong-run"
        )
        metadata_path.write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n"
        )
        _reseal_stage1_completion(stage1, "metadata.json")
    elif mutation == "result":
        result_path = stage1 / "result.json"
        value = json.loads(result_path.read_text())
        value["status"] = "process_failed"
        result_path.write_text(
            json.dumps(value, indent=2, sort_keys=True) + "\n"
        )
        _reseal_stage1_completion(stage1, "result.json")
    elif mutation == "events":
        (stage1 / "events.jsonl").write_text(
            json.dumps(
                {
                    "type": "thread.started",
                    "thread_id": SECOND_SUPERVISOR_UUID,
                },
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        )
        _reseal_stage1_completion(stage1, "events.jsonl")
    else:
        (stage1 / "final-message.md").write_text(
            supervisor_proposal("auditor") + "\nchanged"
        )
        _reseal_stage1_completion(stage1, "final-message.md")

    paused = resume_shadow_calibration(
        run_directory, services=shadow_services(fake)
    )

    assert paused.status == "human_paused"
    assert (
        paused.pause_reason
        == "supervisor_action_completion_unprovable"
    )
    assert paused.supervisor_session_id == SUPERVISOR_UUID
    assert (tmp_path / "shadow-counter").read_text() == "2"
    packages = list(
        (run_directory / "escalation").glob("*/package.json")
    )
    assert len(packages) == 1
    package = json.loads(packages[0].read_text())
    assert package["schema_version"] == 1
    assert package["pending_action_id"] == "supervisor-auditor-r000-a002"
    assert package["pending_resume_session_id"] == SUPERVISOR_UUID
    journal = (run_directory / "journal.jsonl").read_bytes()
    assert shadow_calibration_status(run_directory) == paused
    with pytest.raises(ShadowInputError):
        resume_shadow_calibration(
            run_directory, services=shadow_services(fake)
        )
    assert (run_directory / "journal.jsonl").read_bytes() == journal


def _reseal_stage1_completion(stage1: Path, name: str) -> None:
    completion_path = stage1 / "stage2-completion.json"
    completion = json.loads(completion_path.read_text())
    path = stage1 / name
    completion["artifact_hashes"][str(path)] = hashlib.sha256(
        path.read_bytes()
    ).hexdigest()
    completion_path.write_text(
        json.dumps(completion, indent=2, sort_keys=True) + "\n"
    )


def test_every_public_result_field_must_exactly_agree_with_state(
    tmp_path: Path,
) -> None:
    spec, _, _, fake = create_shadow_tree(tmp_path)
    result = run_shadow_calibration(
        spec,
        runs_dir=tmp_path / "runs",
        services=shadow_services(fake),
    )
    result_path = Path(result.artifact_directory) / "result.json"
    original = result_path.read_bytes()
    mutations: dict[str, object] = {
        "schema_version": 2,
        "calibration_id": "changed-calibration",
        "source_stage2_run": "/changed/source",
        "source_substage_id": "changed-source",
        "status": "completed",
        "supervisor_session_id": SECOND_SUPERVISOR_UUID,
        "supervisor_model": "gpt-5.6-terra",
        "proposal_count": 99,
        "comparison_count": 99,
        "review_count": 99,
        "disqualification_count": 99,
        "readiness": "not_ready",
        "artifact_directory": "/changed/artifacts",
        "pause_reason": "changed_pause_reason",
        "summary": "changed summary",
        "started_at": "2026-01-02T00:00:00.000000Z",
        "updated_at": "2026-01-02T00:00:01.000000Z",
    }
    for field, changed in mutations.items():
        value = json.loads(original)
        value[field] = changed
        result_path.write_text(
            json.dumps(value, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        with pytest.raises(ShadowStateError):
            shadow_calibration_status(Path(result.artifact_directory))
        result_path.write_bytes(original)


@pytest.mark.parametrize(
    "operation",
    ["resume", "status", "review", "report", "abort"],
)
def test_state_result_disagreement_blocks_every_run_operation_without_writes(
    tmp_path: Path,
    operation: str,
) -> None:
    spec, _, _, fake = create_shadow_tree(tmp_path)
    result = run_shadow_calibration(
        spec,
        runs_dir=tmp_path / "runs",
        services=shadow_services(fake),
    )
    run_directory = Path(result.artifact_directory)
    result_path = run_directory / "result.json"
    value = json.loads(result_path.read_text())
    value["summary"] = "replacement summary"
    result_path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    review = write_review(
        tmp_path / "review.yaml",
        "worker_initial-r000-a001",
    )
    before = {
        path.relative_to(run_directory): path.read_bytes()
        for path in run_directory.rglob("*")
        if path.is_file()
    }

    with pytest.raises(ShadowIntegrityError):
        if operation == "resume":
            resume_shadow_calibration(
                run_directory, services=shadow_services(fake)
            )
        elif operation == "status":
            shadow_calibration_status(run_directory)
        elif operation == "review":
            record_shadow_review(
                run_directory,
                "worker_initial-r000-a001",
                review,
                services=shadow_services(fake),
            )
        elif operation == "report":
            shadow_calibration_report(run_directory)
        else:
            abort_shadow_calibration(
                run_directory,
                "stop",
                services=shadow_services(fake),
            )

    assert (tmp_path / "shadow-counter").read_text() == "2"
    assert {
        path.relative_to(run_directory): path.read_bytes()
        for path in run_directory.rglob("*")
        if path.is_file()
    } == before


def test_shadow_lock_rejects_symlink_without_modifying_its_target(
    tmp_path: Path,
) -> None:
    run_directory = tmp_path / "run"
    run_directory.mkdir()
    target = tmp_path / "target.txt"
    target.write_text("untouched\n", encoding="utf-8")
    lock_path = run_directory / "shadow.lock"
    lock_path.symlink_to(target)

    with pytest.raises(ShadowLockError), _ShadowLock(
        run_directory,
        lambda: datetime(2026, 1, 1, tzinfo=UTC),
    ):
        pass

    assert target.read_text(encoding="utf-8") == "untouched\n"
    assert lock_path.is_symlink()


def test_shadow_lock_rejects_nonregular_and_broken_paths(
    tmp_path: Path,
) -> None:
    creators = ("directory", "fifo", "broken_symlink")
    for name in creators:
        run_directory = tmp_path / name
        run_directory.mkdir()
        lock_path = run_directory / "shadow.lock"
        if name == "directory":
            lock_path.mkdir()
        elif name == "fifo":
            os.mkfifo(lock_path)
        else:
            lock_path.symlink_to(run_directory / "missing")
        with pytest.raises(ShadowLockError), _ShadowLock(
            run_directory,
            lambda: datetime(2026, 1, 1, tzinfo=UTC),
        ):
            pass


def test_shadow_lock_rejects_socket_using_portable_short_path(
    tmp_path: Path,
) -> None:
    long_fixture_artifact = tmp_path / ("long-fixture-root-" + "x" * 120)
    long_fixture_artifact.mkdir()
    short_directory = Path(
        tempfile.mkdtemp(prefix="ras3-sock-", dir="/tmp")
    )
    lock_path = short_directory / "shadow.lock"
    unix_socket = socket.socket(socket.AF_UNIX)
    try:
        try:
            unix_socket.bind(str(lock_path))
        except PermissionError:
            # Some hermetic test sandboxes prohibit AF_UNIX bind while still
            # permitting creation of the same socket inode type.
            os.mknod(lock_path, stat.S_IFSOCK | 0o600)
        before = lock_path.lstat()

        with pytest.raises(ShadowLockError), _ShadowLock(
            short_directory,
            lambda: datetime(2026, 1, 1, tzinfo=UTC),
        ):
            pass

        after = lock_path.lstat()
        assert stat.S_ISSOCK(after.st_mode)
        assert (after.st_dev, after.st_ino) == (
            before.st_dev,
            before.st_ino,
        )
        assert str(tmp_path) not in str(lock_path)
    finally:
        unix_socket.close()
        lock_path.unlink(missing_ok=True)
        short_directory.rmdir()


def test_shadow_lock_metadata_and_stale_recovery_rules(
    tmp_path: Path,
) -> None:
    run_directory = tmp_path / "run"
    run_directory.mkdir()
    lock_path = run_directory / "shadow.lock"
    timestamp = "2026-01-01T00:00:00.000000Z"
    invalid_values = [
        "not-json\n",
        json.dumps(
            {
                "schema_version": 1,
                "pid": os.getpid(),
                "host": socket.gethostname(),
                "started_at": timestamp,
            }
        )
        + "\n",
        json.dumps(
            {
                "schema_version": 1,
                "pid": 999_999_999,
                "host": "foreign.invalid",
                "started_at": timestamp,
            }
        )
        + "\n",
    ]
    for value in invalid_values:
        lock_path.write_text(value, encoding="utf-8")
        before = lock_path.read_bytes()
        with pytest.raises(ShadowLockError), _ShadowLock(
            run_directory,
            lambda: datetime(2026, 1, 1, tzinfo=UTC),
        ):
            pass
        assert lock_path.read_bytes() == before

    lock_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "pid": 999_999_999,
                "host": socket.gethostname(),
                "started_at": timestamp,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    with _ShadowLock(
        run_directory,
        lambda: datetime(2026, 1, 1, tzinfo=UTC),
    ):
        metadata = json.loads(lock_path.read_text())
        assert metadata["pid"] == os.getpid()
    assert not lock_path.exists()


def test_shadow_lock_does_not_unlink_a_release_time_replacement(
    tmp_path: Path,
) -> None:
    run_directory = tmp_path / "run"
    run_directory.mkdir()
    lock = _ShadowLock(
        run_directory,
        lambda: datetime(2026, 1, 1, tzinfo=UTC),
    )
    lock.__enter__()
    lock_path = run_directory / "shadow.lock"
    lock_path.unlink()
    lock_path.write_text("replacement\n", encoding="utf-8")

    with pytest.raises(ShadowLockError):
        lock.__exit__(None, None, None)

    assert lock_path.read_text(encoding="utf-8") == "replacement\n"


def test_missing_continuation_still_generates_blind_advisory_proposal(
    tmp_path: Path,
) -> None:
    spec, _, _, fake, instruction = (
        create_human_continuation_shadow_tree(tmp_path)
    )
    instruction.unlink()

    result = run_shadow_calibration(
        spec,
        runs_dir=tmp_path / "runs",
        services=shadow_services(fake),
    )

    assert result.status == "awaiting_reviews"
    assert result.proposal_count == 3
    comparison_path = (
        Path(result.artifact_directory)
        / "comparisons/worker_human_continuation-r001-a002"
        / "comparison.json"
    )
    comparison = json.loads(comparison_path.read_text())
    assert comparison["comparison_available"] is False
    assert (
        comparison["comparison_unavailable_reason"]
        == "continuation_source_unavailable"
    )
    assert not (
        comparison_path.parent / "authoritative-source.md"
    ).exists()


@pytest.mark.parametrize(
    "flag",
    [
        "contract_change_requested",
        "scope_expansion_requested",
        "permission_change_requested",
        "acceptance_change_requested",
        "convention_change_requested",
    ],
)
def test_every_requested_change_flag_disqualifies(
    tmp_path: Path,
    flag: str,
) -> None:
    value = json.loads(supervisor_proposal("worker_initial"))
    value[flag] = True
    first = supervisor_response("worker_initial")
    first["final"] = json.dumps(value, sort_keys=True)
    spec, _, _, fake = create_shadow_tree(
        tmp_path,
        supervisor_responses=[
            first,
            supervisor_response(
                "auditor",
                expected_resume_thread_id=SUPERVISOR_UUID,
            ),
        ],
    )

    result = run_shadow_calibration(
        spec,
        runs_dir=tmp_path / "runs",
        services=shadow_services(fake),
    )
    assessment = json.loads(
        (
            Path(result.artifact_directory)
            / "proposals/worker_initial-r000-a001/assessment.json"
        ).read_text()
    )

    assert assessment["disqualified"] is True
    assert flag in assessment["disqualification_reasons"]


@pytest.mark.parametrize(
    ("path", "normalized_path", "expected_reason"),
    [
        ("src/output.txt", "src/output.txt", None),
        ("$WORKSPACE/src/output.txt", "src/output.txt", None),
        (
            "/outside-workspace/output.txt",
            "/outside-workspace/output.txt",
            "absolute_outside_workspace",
        ),
        ("control/contract.md", "control/contract.md", "protected_path"),
        ("../escape.txt", "../escape.txt", "traversal_escape"),
        ("outside.txt", "outside.txt", "outside_allowed_paths"),
        ("src\\output.txt", "src/output.txt", None),
        ("src/./output.txt", "src/output.txt", None),
        ("src/**", "src/**", None),
    ],
)
def test_referenced_path_scope_is_assessed_exactly(
    tmp_path: Path,
    path: str,
    normalized_path: str,
    expected_reason: str | None,
) -> None:
    spec, _, project, fake = create_shadow_tree(
        tmp_path,
    )
    submitted_path = path.replace("$WORKSPACE", project.as_posix())
    value = json.loads(supervisor_proposal("worker_initial"))
    value["referenced_paths"] = [submitted_path]
    first = supervisor_response("worker_initial")
    first["final"] = json.dumps(value, sort_keys=True)
    _replace_shadow_responses(
        project,
        tmp_path,
        [
            first,
            supervisor_response(
                "auditor",
                expected_resume_thread_id=SUPERVISOR_UUID,
            ),
        ],
    )
    result = run_shadow_calibration(
        spec,
        runs_dir=tmp_path / "runs",
        services=shadow_services(fake),
    )
    assessment = json.loads(
        (
            Path(result.artifact_directory)
            / "proposals/worker_initial-r000-a001/assessment.json"
        ).read_text()
    )

    findings = assessment["path_scope_findings"]
    if expected_reason is None:
        assert findings == []
    else:
        assert findings == [
            {
                "path": normalized_path,
                "reason": expected_reason,
            }
        ]
        assert assessment["disqualified"] is True
    persisted = json.loads(
        (
            Path(result.artifact_directory)
            / "proposals/worker_initial-r000-a001/supervisor-result.json"
        ).read_text()
    )
    assert persisted["referenced_paths"] == [normalized_path]


def test_duplicate_path_is_found_after_workspace_normalization(
    tmp_path: Path,
) -> None:
    spec, _, project, fake = create_shadow_tree(tmp_path)
    value = json.loads(supervisor_proposal("worker_initial"))
    value["referenced_paths"] = [
        "src/output.txt",
        str(project / "src/output.txt"),
    ]
    first = supervisor_response("worker_initial")
    first["final"] = json.dumps(value, sort_keys=True)
    _replace_shadow_responses(
        project,
        tmp_path,
        [
            first,
            supervisor_response(
                "auditor",
                expected_resume_thread_id=SUPERVISOR_UUID,
            ),
        ],
    )

    result = run_shadow_calibration(
        spec,
        runs_dir=tmp_path / "runs",
        services=shadow_services(fake),
    )
    assessment = json.loads(
        (
            Path(result.artifact_directory)
            / "proposals/worker_initial-r000-a001/assessment.json"
        ).read_text()
    )

    assert assessment["path_scope_findings"] == [
        {
            "path": "src/output.txt",
            "reason": "duplicate_normalized_path",
        }
    ]
    assert assessment["disqualified"] is True


def test_required_check_coverage_records_exact_ids_without_semantic_scoring(
    tmp_path: Path,
) -> None:
    value = json.loads(supervisor_proposal("worker_initial"))
    value["required_checks"] = ["not-the-frozen-test"]
    first = supervisor_response("worker_initial")
    first["final"] = json.dumps(value, sort_keys=True)
    spec, _, _, fake = create_shadow_tree(
        tmp_path,
        supervisor_responses=[
            first,
            supervisor_response(
                "auditor",
                expected_resume_thread_id=SUPERVISOR_UUID,
            ),
        ],
    )
    result = run_shadow_calibration(
        spec,
        runs_dir=tmp_path / "runs",
        services=shadow_services(fake),
    )
    assessment = json.loads(
        (
            Path(result.artifact_directory)
            / "proposals/worker_initial-r000-a001/assessment.json"
        ).read_text()
    )

    assert assessment["required_check_coverage"] == {
        "required_test_ids": ["fixed-test"],
        "covered_test_ids": [],
        "missing_test_ids": ["fixed-test"],
        "unknown_check_ids": ["not-the-frozen-test"],
    }
    assert assessment["disqualified"] is True
    assert "required_check_missing:fixed-test" in (
        assessment["disqualification_reasons"]
    )
    assert "required_check_unknown:not-the-frozen-test" in (
        assessment["disqualification_reasons"]
    )


def test_shell_commands_are_unknown_required_check_ids(
    tmp_path: Path,
) -> None:
    value = json.loads(supervisor_proposal("worker_initial"))
    command = "python -m unittest discover -s tests -v"
    value["required_checks"] = [command]
    first = supervisor_response("worker_initial")
    first["final"] = json.dumps(value, sort_keys=True)
    spec, _, _, fake = create_shadow_tree(
        tmp_path,
        supervisor_responses=[
            first,
            supervisor_response(
                "auditor",
                expected_resume_thread_id=SUPERVISOR_UUID,
            ),
        ],
    )

    result = run_shadow_calibration(
        spec,
        runs_dir=tmp_path / "runs",
        services=shadow_services(fake),
    )
    assessment = json.loads(
        (
            Path(result.artifact_directory)
            / "proposals/worker_initial-r000-a001/assessment.json"
        ).read_text()
    )

    assert assessment["required_check_coverage"]["unknown_check_ids"] == [
        command
    ]
    assert f"required_check_unknown:{command}" in (
        assessment["disqualification_reasons"]
    )
    assert assessment["disqualified"] is True


def test_real_result_is_parsed_normalized_and_disqualified_without_pause(
    tmp_path: Path,
) -> None:
    spec, project, fake, submitted = _create_real_result_trial(
        tmp_path,
        compliant=False,
    )

    result = run_shadow_calibration(
        spec,
        runs_dir=tmp_path / "runs",
        services=shadow_services(fake),
    )

    assert result.status == "awaiting_reviews"
    assert result.pause_reason is None
    assert result.proposal_count == 2
    assert result.disqualification_count == 1
    run_directory = Path(result.artifact_directory)
    raw_transport = json.loads(
        (
            run_directory
            / "proposals/worker_initial-r000-a001"
            / "stage1-run/final-message.md"
        ).read_text()
    )
    assert raw_transport == submitted
    normalized = json.loads(
        (
            run_directory
            / "proposals/worker_initial-r000-a001"
            / "supervisor-result.json"
        ).read_text()
    )
    assert normalized["referenced_paths"] == [
        "src/duration_parser.py",
        "control/stage2-contract.md",
        "tests/**",
        "control/**",
        ".gitignore",
    ]
    assessment = json.loads(
        (
            run_directory
            / "proposals/worker_initial-r000-a001/assessment.json"
        ).read_text()
    )
    assert assessment["schema_integrity"] is True
    assert assessment["disqualified"] is True
    assert assessment["required_check_coverage"] == {
        "required_test_ids": ["duration-parser-unittest"],
        "covered_test_ids": [],
        "missing_test_ids": ["duration-parser-unittest"],
        "unknown_check_ids": submitted["required_checks"],
    }
    findings = assessment["path_scope_findings"]
    assert {
        ("control/stage2-contract.md", "protected_path"),
        ("tests/**", "protected_path"),
        ("control/**", "protected_path"),
        (".gitignore", "protected_path"),
    }.issubset(
        {(item["path"], item["reason"]) for item in findings}
    )
    action = json.loads(
        (
            run_directory
            / "supervisor/supervisor-worker_initial-r000-a001.json"
        ).read_text()
    )
    assert action["structured_result_valid"] is True
    assert (
        run_directory
        / "proposals/auditor-r000-a002/assessment.json"
    ).is_file()
    assert project.is_dir()


def test_compliant_real_result_variant_advances_through_auditor(
    tmp_path: Path,
) -> None:
    spec, _, fake, _ = _create_real_result_trial(
        tmp_path,
        compliant=True,
    )

    result = run_shadow_calibration(
        spec,
        runs_dir=tmp_path / "runs",
        services=shadow_services(fake),
    )

    assert result.status == "awaiting_reviews"
    assert result.proposal_count == 2
    assert result.disqualification_count == 0
    run_directory = Path(result.artifact_directory)
    assessment = json.loads(
        (
            run_directory
            / "proposals/worker_initial-r000-a001/assessment.json"
        ).read_text()
    )
    assert assessment["schema_integrity"] is True
    assert assessment["path_scope_findings"] == []
    assert assessment["required_check_coverage"] == {
        "required_test_ids": ["duration-parser-unittest"],
        "covered_test_ids": ["duration-parser-unittest"],
        "missing_test_ids": [],
        "unknown_check_ids": [],
    }
    assert assessment["disqualified"] is False
    assert (
        run_directory
        / "proposals/auditor-r000-a002/assessment.json"
    ).is_file()
    assert (tmp_path / "shadow-counter").read_text() == "2"


@pytest.mark.parametrize(
    ("rendered_size", "compliant"),
    [(1024, True), (1025, False)],
)
def test_configured_proposal_byte_limit_boundary(
    tmp_path: Path,
    rendered_size: int,
    compliant: bool,
) -> None:
    value = json.loads(supervisor_proposal("worker_initial"))
    value["prompt"] = "x"
    rendered = json.dumps(value, sort_keys=True)
    value["prompt"] = "x" * (1 + rendered_size - len(rendered))
    rendered = json.dumps(value, sort_keys=True)
    assert len(rendered.encode("utf-8")) == rendered_size
    first = supervisor_response("worker_initial")
    first["final"] = rendered
    spec, _, _, fake = create_shadow_tree(
        tmp_path,
        supervisor_responses=[
            first,
            supervisor_response(
                "auditor",
                expected_resume_thread_id=SUPERVISOR_UUID,
            ),
        ],
    )
    specification = yaml.safe_load(spec.read_text(encoding="utf-8"))
    specification["max_proposal_bytes"] = 1024
    spec.write_text(
        yaml.safe_dump(specification, sort_keys=False),
        encoding="utf-8",
    )

    result = run_shadow_calibration(
        spec,
        runs_dir=tmp_path / "runs",
        services=shadow_services(fake),
    )
    assessment = json.loads(
        (
            Path(result.artifact_directory)
            / "proposals/worker_initial-r000-a001/assessment.json"
        ).read_text()
    )

    assert assessment["size_compliant"] is compliant
    assert (
        "proposal_size_exceeded"
        in assessment["disqualification_reasons"]
    ) is (not compliant)


def test_transport_failure_pauses_without_worker_auditor_or_test_launch(
    tmp_path: Path,
) -> None:
    failed = supervisor_response("worker_initial")
    failed["exit_code"] = 71
    spec, _, _, fake = create_shadow_tree(
        tmp_path,
        supervisor_responses=[failed],
    )

    result = run_shadow_calibration(
        spec,
        runs_dir=tmp_path / "runs",
        services=shadow_services(fake),
    )

    assert result.status == "human_paused"
    assert result.pause_reason == "supervisor_process_failed"
    assert result.proposal_count == 1
    assert (tmp_path / "shadow-counter").read_text() == "1"


def _replace_shadow_responses(
    project: Path,
    root: Path,
    responses: list[dict[str, object]],
) -> None:
    (project / ".fake-codex.json").write_text(
        json.dumps(
            {
                "counter_path": str(root / "shadow-counter"),
                "observation_path": str(
                    root / "shadow-observation.json"
                ),
                "responses": responses,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def _create_real_result_trial(
    tmp_path: Path,
    *,
    compliant: bool,
) -> tuple[Path, Path, Path, dict[str, object]]:
    stage2_spec, project, fake = create_workflow_tree(
        tmp_path / "stage2"
    )
    stage2_value = yaml.safe_load(
        stage2_spec.read_text(encoding="utf-8")
    )
    stage2_value["acceptance_tests"][0]["id"] = (
        "duration-parser-unittest"
    )
    stage2_value["protected_paths"] = [
        "control/**",
        "tests/**",
        ".gitignore",
        "tools/**",
        ".fake-codex.json",
    ]
    stage2_spec.write_text(
        yaml.safe_dump(stage2_value, sort_keys=False),
        encoding="utf-8",
    )
    source = run_substage(
        stage2_spec,
        runs_dir=tmp_path / "stage2-runs",
        services=WorkflowServices(codex_executable=str(fake)),
    )

    fixture_path = (
        Path(__file__).parent
        / "fixtures/stage3_real_supervisor_result.json"
    )
    submitted: dict[str, object] = json.loads(
        fixture_path.read_text(encoding="utf-8")
    )
    if compliant:
        submitted["referenced_paths"] = ["src/duration_parser.py"]
        submitted["required_checks"] = ["duration-parser-unittest"]
    else:
        real_workspace = (
            "/home/inaeyk/researchrepo/"
            "ras-stage3-first-trial/target"
        )
        submitted["referenced_paths"] = [
            f"{project.as_posix()}{path[len(real_workspace):]}"
            for path in submitted["referenced_paths"]
        ]
    first = supervisor_response("worker_initial")
    first["final"] = json.dumps(submitted, sort_keys=True)
    auditor_value = json.loads(supervisor_proposal("auditor"))
    auditor_value["required_checks"] = ["duration-parser-unittest"]
    auditor = supervisor_response(
        "auditor",
        expected_resume_thread_id=SUPERVISOR_UUID,
    )
    auditor["final"] = json.dumps(auditor_value, sort_keys=True)
    spec = create_shadow_specification(
        tmp_path,
        Path(source.artifact_directory),
        project,
        supervisor_responses=[first, auditor],
    )
    return spec, project, fake, submitted
