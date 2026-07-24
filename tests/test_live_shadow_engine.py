from __future__ import annotations

import base64
import json
import threading
import time
from pathlib import Path

import pytest

from research_automation_supervisor.errors import (
    LiveShadowInputError,
    LiveShadowStateError,
)
from research_automation_supervisor.live_shadow_engine import (
    abort_live_shadow,
    live_shadow_report,
    live_shadow_status,
    record_live_shadow_review,
    run_live_shadow,
)
from research_automation_supervisor.workflow_engine import substage_status
from tests.live_shadow_helpers import create_live_shadow_tree
from tests.shadow_helpers import write_review


def test_live_run_preserves_authority_and_quarantines_two_proposals(
    tmp_path: Path,
) -> None:
    spec, _, project, _, services = create_live_shadow_tree(tmp_path)
    result = run_live_shadow(
        spec,
        runs_dir=tmp_path / "live-runs",
        stage2_runs_dir=tmp_path / "stage2-runs",
        services=services,
    )
    assert result.status == "awaiting_reviews"
    assert result.authoritative_stage2_status == "completed"
    assert result.observed_decision_count == 2
    assert result.proposal_count == 2
    assert result.comparison_count == 2
    assert result.review_count == 0
    assert result.automation_enabled is False
    run_directory = Path(result.artifact_directory)
    authoritative = substage_status(
        Path(result.authoritative_stage2_run or "")
    )
    assert authoritative.status == "completed"
    assert not tuple((run_directory / "quarantine").iterdir())
    observations = json.loads(
        (tmp_path / "live-shadow-observation.json").read_text(encoding="utf-8")
    )
    assert observations["cwd"] == str(run_directory / "quarantine")
    prompt = base64.b64decode(observations["prompt_base64"])
    assert str(project).encode("utf-8") not in prompt
    assert str(result.authoritative_stage2_run).encode("utf-8") not in prompt
    assert (run_directory / "decisions/worker_initial-r000-a001/envelope.json").is_file()
    assert (run_directory / "comparisons/auditor-r000-a002/comparison.json").is_file()
    assert live_shadow_status(run_directory) == result
    report = live_shadow_report(run_directory)
    assert report["automation_enabled"] is False


def test_shadow_delay_does_not_prevent_authoritative_completion(
    tmp_path: Path,
) -> None:
    from tests.live_shadow_helpers import live_supervisor_response

    spec, _, _, _, services = create_live_shadow_tree(
        tmp_path,
        supervisor_responses=[
            live_supervisor_response("worker_initial", sleep_seconds=0.2),
            live_supervisor_response("auditor", resume=True),
        ],
    )
    result = run_live_shadow(
        spec,
        runs_dir=tmp_path / "live-runs",
        stage2_runs_dir=tmp_path / "stage2-runs",
        services=services,
    )
    assert result.authoritative_stage2_status == "completed"
    terminal = json.loads(
        (
            Path(result.artifact_directory) / "authoritative/result.json"
        ).read_text(encoding="utf-8")
    )
    first_supervisor = json.loads(
        (
            Path(result.artifact_directory)
            / "proposals/worker_initial-r000-a001/stage1-run/metadata.json"
        ).read_text(encoding="utf-8")
    )
    authoritative_result = json.loads(
        (
            Path(terminal["run_directory"]) / "result.json"
        ).read_text(encoding="utf-8")
    )
    assert authoritative_result["updated_at"] <= first_supervisor["ended_at"]


def test_live_reviews_are_immutable_and_readiness_stays_informational(
    tmp_path: Path,
) -> None:
    spec, _, _, _, services = create_live_shadow_tree(tmp_path)
    result = run_live_shadow(
        spec,
        runs_dir=tmp_path / "live-runs",
        stage2_runs_dir=tmp_path / "stage2-runs",
        services=services,
    )
    run_directory = Path(result.artifact_directory)
    for index, proposal_id in enumerate(
        ("worker_initial-r000-a001", "auditor-r000-a002"),
        start=1,
    ):
        review = write_review(tmp_path / f"review-{index}.yaml", proposal_id)
        result = record_live_shadow_review(
            run_directory,
            proposal_id,
            review,
            services=services,
        )
    assert result.status == "completed"
    assert result.readiness == "candidate_ready_for_supervised_handoff"
    assert result.automation_enabled is False
    report = live_shadow_report(run_directory)
    assert report["readiness"]["informational_only"] is True
    assert report["readiness"]["automation_enabled"] is False
    assert all(
        assessment["review_status"] == "reviewed"
        for assessment in report["assessments"]
    )
    with pytest.raises(LiveShadowInputError, match="already"):
        record_live_shadow_review(
            run_directory,
            "worker_initial-r000-a001",
            tmp_path / "review-1.yaml",
            services=services,
        )


def test_supervisor_session_failure_is_isolated_from_authoritative_stage2(
    tmp_path: Path,
) -> None:
    from tests.live_shadow_helpers import live_supervisor_response
    from tests.shadow_helpers import SOURCE_WORKER_UUID

    response = live_supervisor_response("worker_initial")
    response["stdout_lines"] = [
        json.dumps({"type": "thread.started", "thread_id": SOURCE_WORKER_UUID})
    ]
    proposal = json.loads(str(response["final"]))
    proposal["prompt"] = "QUARANTINED-SHADOW-ONLY-SENTINEL"
    response["final"] = json.dumps(proposal, sort_keys=True)
    spec, _, project, _, services = create_live_shadow_tree(
        tmp_path,
        supervisor_responses=[response],
    )
    result = run_live_shadow(
        spec,
        runs_dir=tmp_path / "live-runs",
        stage2_runs_dir=tmp_path / "stage2-runs",
        services=services,
    )
    assert result.status == "shadow_degraded"
    assert result.authoritative_stage2_status == "completed"
    assert result.shadow_failure_count >= 1
    authoritative_observation = json.loads(
        (project.parent / "fake-observation.json").read_text(encoding="utf-8")
    )
    authoritative_prompt = base64.b64decode(
        authoritative_observation["prompt_base64"]
    )
    assert b"QUARANTINED-SHADOW-ONLY-SENTINEL" not in authoritative_prompt


def test_artifact_mutation_is_rejected_by_read_only_status(tmp_path: Path) -> None:
    spec, _, _, _, services = create_live_shadow_tree(tmp_path)
    result = run_live_shadow(
        spec,
        runs_dir=tmp_path / "live-runs",
        stage2_runs_dir=tmp_path / "stage2-runs",
        services=services,
    )
    run_directory = Path(result.artifact_directory)
    candidate = (
        run_directory
        / "proposals/worker_initial-r000-a001/candidate-prompt.md"
    )
    candidate.write_text("replacement\n", encoding="utf-8")
    with pytest.raises(LiveShadowStateError, match="replaced evidence"):
        live_shadow_status(run_directory)


def test_abort_stops_only_observation_and_stage2_finishes(
    tmp_path: Path,
) -> None:
    from tests.shadow_helpers import SOURCE_AUDITOR_UUID, SOURCE_WORKER_UUID
    from tests.workflow_helpers import (
        auditor_result,
        codex_response,
        worker_result,
    )

    delayed_worker = codex_response(
        "worker",
        SOURCE_WORKER_UUID,
        worker_result(),
        sleep_seconds=0.6,
    )
    spec, _, _, _, services = create_live_shadow_tree(
        tmp_path,
        stage2_responses=[
            delayed_worker,
            codex_response(
                "auditor",
                SOURCE_AUDITOR_UUID,
                auditor_result(),
            ),
        ],
    )
    holder: dict[str, object] = {}

    def invoke() -> None:
        holder["result"] = run_live_shadow(
            spec,
            runs_dir=tmp_path / "live-runs",
            stage2_runs_dir=tmp_path / "stage2-runs",
            services=services,
        )

    observer = threading.Thread(target=invoke)
    observer.start()
    run_directory: Path | None = None
    authoritative_run: Path | None = None
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        candidates = tuple((tmp_path / "live-runs").glob("*"))
        if candidates and (candidates[0] / "state.json").is_file():
            state = json.loads(
                (candidates[0] / "state.json").read_text(encoding="utf-8")
            )
            if state["authoritative_run_directory"] is not None:
                run_directory = candidates[0]
                authoritative_run = Path(state["authoritative_run_directory"])
                break
        time.sleep(0.02)
    assert run_directory is not None
    assert authoritative_run is not None
    aborted = abort_live_shadow(
        run_directory,
        "operator stopped observation",
        services=services,
    )
    assert aborted.status == "aborted"
    observer.join(timeout=2)
    assert not observer.is_alive()
    assert holder["result"] == aborted

    deadline = time.monotonic() + 5
    authoritative_status = None
    while time.monotonic() < deadline:
        authoritative_status = json.loads(
            (authoritative_run / "result.json").read_text(encoding="utf-8")
        )["status"]
        if authoritative_status == "completed":
            break
        time.sleep(0.02)
    assert authoritative_status == "completed"
    assert substage_status(authoritative_run).status == "completed"
    assert live_shadow_status(run_directory).status == "aborted"


def test_mutating_review_recovers_a_fsynced_journal_ahead_of_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import research_automation_supervisor.live_shadow_engine as engine

    spec, _, _, _, services = create_live_shadow_tree(tmp_path)
    result = run_live_shadow(
        spec,
        runs_dir=tmp_path / "live-runs",
        stage2_runs_dir=tmp_path / "stage2-runs",
        services=services,
    )
    run_directory = Path(result.artifact_directory)
    first = write_review(
        tmp_path / "review-1.yaml",
        "worker_initial-r000-a001",
    )
    record_live_shadow_review(
        run_directory,
        "worker_initial-r000-a001",
        first,
        services=services,
    )
    second = write_review(tmp_path / "review-2.yaml", "auditor-r000-a002")
    original_persist = engine._persist_state

    def crash_after_journal(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError("simulated crash after journal fsync")

    monkeypatch.setattr(engine, "_persist_state", crash_after_journal)
    with pytest.raises(RuntimeError, match="simulated crash"):
        record_live_shadow_review(
            run_directory,
            "auditor-r000-a002",
            second,
            services=services,
        )
    with pytest.raises(
        LiveShadowStateError,
        match="journal head",
    ):
        live_shadow_status(run_directory)

    monkeypatch.setattr(engine, "_persist_state", original_persist)
    with pytest.raises(LiveShadowInputError, match="already"):
        record_live_shadow_review(
            run_directory,
            "auditor-r000-a002",
            second,
            services=services,
        )
    recovered = live_shadow_status(run_directory)
    assert recovered.status == "completed"
    assert recovered.review_count == 2
