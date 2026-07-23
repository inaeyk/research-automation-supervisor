from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from research_automation_supervisor.codex_adapter import run_prepared_codex
from research_automation_supervisor.errors import ShadowStateError
from research_automation_supervisor.shadow_engine import (
    ShadowServices,
    resume_shadow_calibration,
    run_shadow_calibration,
    shadow_calibration_status,
)
from tests.shadow_helpers import (
    create_shadow_tree,
    shadow_services,
    supervisor_proposal,
    supervisor_response,
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
    assert result.supervisor_session_id == "shadow-supervisor-thread"
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
    assert resumed_metadata["resume_thread_id"] == "shadow-supervisor-thread"
    command = resumed_metadata["command"]
    assert command[command.index("resume") + 1] == "shadow-supervisor-thread"
    assert "--last" not in command and "--all" not in command
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
    assert paused.pause_reason == "uncertain_supervisor_action"
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


def test_requested_change_and_oversize_are_deterministically_disqualified(
    tmp_path: Path,
) -> None:
    requested = supervisor_response("worker_initial")
    requested["final"] = supervisor_proposal(
        "worker_initial", requested_change=True
    )
    auditor = supervisor_response(
        "auditor",
        expected_resume_thread_id="shadow-supervisor-thread",
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
                expected_resume_thread_id="shadow-supervisor-thread",
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


def test_supervisor_cannot_reuse_a_source_worker_session(
    tmp_path: Path,
) -> None:
    responses = [
        supervisor_response(
            "worker_initial", thread_id="worker-thread-1"
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
