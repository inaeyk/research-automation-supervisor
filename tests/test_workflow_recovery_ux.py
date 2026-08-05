from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

import research_automation_supervisor.workflow_engine as workflow_engine
import research_automation_supervisor.workflow_recovery as workflow_recovery
from research_automation_supervisor.cli import app
from research_automation_supervisor.codex_adapter import run_prepared_codex
from research_automation_supervisor.physics_workflow import PhysicsWorkflowServices
from research_automation_supervisor.workflow_engine import (
    WorkflowServices,
    _WorkflowLock,
    continue_substage,
    run_substage,
)
from research_automation_supervisor.workflow_recovery import (
    RUN_INDEX_FILE,
    RecoverySelectionError,
    RecoveryServices,
    build_recovery_plan,
    discover_workflow_runs,
    execute_recovery_plan,
    latest_incomplete_run,
    load_run_index,
)
from research_automation_supervisor.workflow_recovery_models import (
    RecoveryPlanV1,
)
from tests.test_physics_workflow import (
    BWRAP,
    SYNTHETIC,
    CrashOnce,
    ScriptedPhysicsAuditor,
    _physics_tree,
    _result_state,
)
from tests.workflow_helpers import codex_response, create_workflow_tree, worker_result


def _services(fake_codex: Path, token: str, now: datetime | None = None) -> WorkflowServices:
    return WorkflowServices(
        codex_executable=str(fake_codex),
        token_factory=lambda: token,
        utc_now=(lambda: now) if now is not None else WorkflowServices().utc_now,
    )


def test_missing_stale_and_corrupt_indexes_are_rebuilt_from_journals(tmp_path: Path) -> None:
    spec, _, fake = create_workflow_tree(tmp_path)
    result = run_substage(
        spec,
        runs_dir=tmp_path / "runs",
        services=_services(fake, "indexed"),
    )

    first = discover_workflow_runs(tmp_path / "runs")
    cache = tmp_path / "runs" / RUN_INDEX_FILE
    assert len(first.entries) == 1
    assert first.entries[0].run_directory == result.artifact_directory
    assert load_run_index(cache) == first

    cache.unlink()
    assert discover_workflow_runs(tmp_path / "runs").entries == first.entries
    cache.write_text("{", encoding="utf-8")
    rebuilt = discover_workflow_runs(tmp_path / "runs")
    assert rebuilt.entries == first.entries
    assert load_run_index(cache) == rebuilt

    stale = rebuilt.model_copy(update={"entries": (), "source_sha256": "0" * 64})
    cache.write_text(json.dumps(stale.model_dump(mode="json")), encoding="utf-8")
    assert discover_workflow_runs(tmp_path / "runs").entries == first.entries


def test_latest_incomplete_uses_journal_time_and_rejects_ties(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    base = datetime(2026, 8, 5, tzinfo=UTC)
    for name, token, now in (
        ("one", "older", base),
        ("two", "newer", base + timedelta(seconds=1)),
    ):
        spec, _, fake = create_workflow_tree(
            tmp_path / name,
            responses=[codex_response("worker", "worker-thread-1", worker_result("needs_human"))],
        )
        run_substage(spec, runs_dir=runs, services=_services(fake, token, now))
    selected = latest_incomplete_run(discover_workflow_runs(runs))
    assert selected.run_token == "newer"

    tied_runs = tmp_path / "tied-runs"
    for name, token in (("tie-one", "tie-one"), ("tie-two", "tie-two")):
        spec, _, fake = create_workflow_tree(
            tmp_path / name,
            responses=[codex_response("worker", "worker-thread-1", worker_result("needs_human"))],
        )
        run_substage(spec, runs_dir=tied_runs, services=_services(fake, token, base))
    with pytest.raises(RecoverySelectionError) as caught:
        latest_incomplete_run(discover_workflow_runs(tied_runs))
    assert caught.value.reason_code == "multiple_latest_runs"


def test_prelaunch_phase_resumes_and_repeated_resume_does_not_duplicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec, _, fake = create_workflow_tree(tmp_path)
    original = workflow_engine._codex_action_intent

    def stop_before_intent(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise KeyboardInterrupt

    monkeypatch.setattr(workflow_engine, "_codex_action_intent", stop_before_intent)
    services = _services(fake, "prelaunch")
    with pytest.raises(KeyboardInterrupt):
        run_substage(spec, runs_dir=tmp_path / "runs", services=services)
    run_directory = tmp_path / "runs/minimal-substage-prelaunch"
    before = tuple(run_directory.rglob("*"))

    plan = build_recovery_plan(run_directory)
    assert plan.reason_code == "safe_before_launch"
    assert plan.auto_resume_safe
    assert tuple(run_directory.rglob("*")) == before

    monkeypatch.setattr(workflow_engine, "_codex_action_intent", original)
    execution = execute_recovery_plan(
        plan,
        services=RecoveryServices(
            workflow_services=services,
            attempt_token=lambda: "prelaunch-one",
        ),
    )
    assert execution.outcome.result_status == "completed"
    assert (tmp_path / "fake-counter").read_text(encoding="ascii") == "2"

    terminal = build_recovery_plan(run_directory)
    assert terminal.disposition == "already_terminal"
    repeated = execute_recovery_plan(
        terminal,
        services=RecoveryServices(
            workflow_services=services,
            attempt_token=lambda: "prelaunch-two",
        ),
    )
    assert repeated.outcome.status == "already_terminal"
    assert (tmp_path / "fake-counter").read_text(encoding="ascii") == "2"


def test_completed_worker_output_is_captured_without_duplicate_worker(tmp_path: Path) -> None:
    spec, _, fake = create_workflow_tree(tmp_path)

    def complete_then_interrupt(prepared: object, **kwargs: object) -> object:
        run_prepared_codex(prepared, **kwargs)  # type: ignore[arg-type]
        raise KeyboardInterrupt

    interrupted = WorkflowServices(
        codex_executable=str(fake),
        codex_invoker=complete_then_interrupt,  # type: ignore[arg-type]
        token_factory=lambda: "completed-output",
    )
    with pytest.raises(KeyboardInterrupt):
        run_substage(spec, runs_dir=tmp_path / "runs", services=interrupted)
    run_directory = tmp_path / "runs/minimal-substage-completed-output"
    assert (tmp_path / "fake-counter").read_text(encoding="ascii") == "1"

    plan = build_recovery_plan(run_directory)
    assert plan.proof_reconciliation == "completed_valid"
    execution = execute_recovery_plan(
        plan,
        services=RecoveryServices(
            workflow_services=_services(fake, "unused"),
            attempt_token=lambda: "capture-one",
        ),
    )
    assert execution.outcome.result_status == "completed"
    assert (tmp_path / "fake-counter").read_text(encoding="ascii") == "2"
    assert len(list((run_directory / "worker/codex").iterdir())) == 1


def test_ambiguous_intent_is_blocked_receipted_and_never_launched(tmp_path: Path) -> None:
    spec, _, fake = create_workflow_tree(tmp_path)

    def interrupt(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise KeyboardInterrupt

    interrupted = WorkflowServices(
        codex_executable=str(fake),
        codex_invoker=interrupt,  # type: ignore[arg-type]
        token_factory=lambda: "ambiguous",
    )
    with pytest.raises(KeyboardInterrupt):
        run_substage(spec, runs_dir=tmp_path / "runs", services=interrupted)
    run_directory = tmp_path / "runs/minimal-substage-ambiguous"
    plan = build_recovery_plan(run_directory)
    assert plan.disposition == "blocked"
    assert plan.reason_code == "ambiguous_post_launch_state"

    execution = execute_recovery_plan(
        plan,
        services=RecoveryServices(
            workflow_services=_services(fake, "unused"),
            attempt_token=lambda: "blocked-one",
        ),
    )
    assert execution.outcome.status == "blocked"
    assert execution.plan_receipt_path.is_file()
    assert execution.outcome_receipt_path.is_file()
    plan_bytes = execution.plan_receipt_path.read_bytes()
    repeated = execute_recovery_plan(
        plan,
        services=RecoveryServices(
            workflow_services=_services(fake, "unused"),
            attempt_token=lambda: "blocked-two",
        ),
    )
    assert repeated.outcome.status == "blocked"
    assert execution.plan_receipt_path.read_bytes() == plan_bytes
    assert not (tmp_path / "fake-counter").exists()


def test_pause_is_reopened_without_duplicate_human_or_worker_action(tmp_path: Path) -> None:
    spec, _, fake = create_workflow_tree(
        tmp_path,
        responses=[codex_response("worker", "worker-thread-1", worker_result("needs_human"))],
    )
    result = run_substage(
        spec,
        runs_dir=tmp_path / "runs",
        services=_services(fake, "paused"),
    )
    plan = build_recovery_plan(Path(result.artifact_directory))
    assert plan.disposition == "reopen_pause"
    before = (tmp_path / "fake-counter").read_text(encoding="ascii")
    execution = execute_recovery_plan(
        plan,
        services=RecoveryServices(
            workflow_services=_services(fake, "unused"),
            attempt_token=lambda: "pause-one",
        ),
    )
    assert execution.outcome.status == "reopened"
    assert (tmp_path / "fake-counter").read_text(encoding="ascii") == before


def test_strict_models_and_safe_cli_dry_run(tmp_path: Path) -> None:
    spec, _, fake = create_workflow_tree(
        tmp_path,
        responses=[codex_response("worker", "worker-thread-1", worker_result("needs_human"))],
    )
    run_substage(
        spec,
        runs_dir=tmp_path / "runs",
        services=_services(fake, "cli-paused"),
    )
    runner = CliRunner()
    before = {path: path.stat().st_mtime_ns for path in (tmp_path / "runs").rglob("*")}
    result = runner.invoke(
        app,
        ["resume", "--runs-dir", str(tmp_path / "runs"), "--latest", "--dry-run", "--json"],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["plan"]["disposition"] == "reopen_pause"
    assert payload["plan"]["worker_session_id"] == "<REDACTED>"
    assert not (tmp_path / "runs" / RUN_INDEX_FILE).exists()
    assert {path: path.stat().st_mtime_ns for path in (tmp_path / "runs").rglob("*")} == before

    with pytest.raises(ValidationError):
        RecoveryPlanV1.model_validate({**payload["plan"], "unknown": True})

    latest = runner.invoke(
        app,
        ["latest-incomplete", "--runs-dir", str(tmp_path / "runs"), "--json"],
    )
    assert latest.exit_code == 0
    assert json.loads(latest.stdout)["run_token"] == "<REDACTED>"

    resumed = runner.invoke(
        app,
        ["resume", "--runs-dir", str(tmp_path / "runs"), "--latest", "--json"],
    )
    assert resumed.exit_code == 5
    resumed_payload = json.loads(resumed.stdout)
    assert resumed_payload["outcome"]["status"] == "reopened"
    assert Path(resumed_payload["plan_receipt_path"]).is_file()
    assert Path(resumed_payload["outcome_receipt_path"]).is_file()


@pytest.mark.skipif(not BWRAP.is_file(), reason="Bubblewrap PA-2 path unavailable")
@pytest.mark.parametrize(
    ("boundary", "expected_reason"),
    [
        ("journal:software_workflow_requested", "safe_deterministic_transition"),
        ("oracle_force_oracle:execution_prepared", "safe_before_launch"),
        ("physics_auditor:prompt_finalized", "safe_before_launch"),
        ("physics_auditor:action_proof_finalized", "finalized_proof_verified"),
    ],
)
def test_physics_safe_power_cut_boundaries_resume_once(
    tmp_path: Path, boundary: str, expected_reason: str
) -> None:
    spec, _, fake = _physics_tree(tmp_path)
    physics = ScriptedPhysicsAuditor(SYNTHETIC / "reports/clean.json")
    crash = CrashOnce(boundary)
    software = WorkflowServices(codex_executable=str(fake), token_factory=lambda: "pa5-safe")
    physics_services = PhysicsWorkflowServices(
        physics_auditor_codex_invoker=physics,
        checkpoint=crash,
    )
    with pytest.raises(RuntimeError, match="injected crash"):
        run_substage(
            spec,
            runs_dir=tmp_path / "runs",
            services=software,
            physics_services=physics_services,
        )
    run_directory = tmp_path / "runs/minimal-substage-pa5-safe"
    plan = build_recovery_plan(run_directory)
    assert plan.reason_code == expected_reason
    assert plan.auto_resume_safe
    before_calls = physics.calls
    execution = execute_recovery_plan(
        plan,
        services=RecoveryServices(
            workflow_services=software,
            physics_services=physics_services,
            attempt_token=lambda: f"safe-{boundary.split(':')[-1]}",
        ),
    )
    assert execution.outcome.result_status == "completed"
    assert physics.calls == 1
    if boundary == "physics_auditor:action_proof_finalized":
        assert physics.calls == before_calls


@pytest.mark.skipif(not BWRAP.is_file(), reason="Bubblewrap PA-2 path unavailable")
@pytest.mark.parametrize(
    "boundary",
    [
        "oracle_force_oracle:process_launch_attempted",
        "physics_auditor:model_launch_attempted",
    ],
)
def test_physics_ambiguous_launch_is_blocked_without_retry(tmp_path: Path, boundary: str) -> None:
    spec, _, fake = _physics_tree(tmp_path)
    physics = ScriptedPhysicsAuditor(SYNTHETIC / "reports/clean.json")
    software = WorkflowServices(codex_executable=str(fake), token_factory=lambda: "pa5-ambiguous")
    physics_services = PhysicsWorkflowServices(
        physics_auditor_codex_invoker=physics,
        checkpoint=CrashOnce(boundary),
    )
    with pytest.raises(RuntimeError, match="injected crash"):
        run_substage(
            spec,
            runs_dir=tmp_path / "runs",
            services=software,
            physics_services=physics_services,
        )
    run_directory = tmp_path / "runs/minimal-substage-pa5-ambiguous"
    plan = build_recovery_plan(run_directory)
    assert plan.disposition == "blocked"
    assert plan.reason_code == "process_identity_ambiguous"
    before = physics.calls
    execution = execute_recovery_plan(
        plan,
        services=RecoveryServices(
            workflow_services=software,
            physics_services=physics_services,
            attempt_token=lambda: f"blocked-{boundary.split(':')[-1]}",
        ),
    )
    assert execution.outcome.status == "blocked"
    assert physics.calls == before


@pytest.mark.skipif(not BWRAP.is_file(), reason="Bubblewrap PA-2 path unavailable")
def test_durable_physics_human_decision_continues_exactly_once(tmp_path: Path) -> None:
    spec, _, fake = _physics_tree(tmp_path)
    physics = ScriptedPhysicsAuditor(SYNTHETIC / "reports/convention_change.json")
    software = WorkflowServices(codex_executable=str(fake), token_factory=lambda: "pa5-decision")
    paused = run_substage(
        spec,
        runs_dir=tmp_path / "runs",
        services=software,
        physics_services=PhysicsWorkflowServices(physics_auditor_codex_invoker=physics),
    )
    state = _result_state(paused)
    decision = tmp_path / "decision.json"
    decision.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "run_token": paused.run_token,
                "review_packet_sha256": state.human_review_packet_sha256,
                "decision": "reject_candidate",
                "reason": "Reject after scientific review.",
                "acknowledged_finding_ids": [],
                "acknowledged_question_ids": [],
            }
        ),
        encoding="utf-8",
    )
    crash_physics = PhysicsWorkflowServices(
        physics_auditor_codex_invoker=physics,
        checkpoint=CrashOnce("journal:physics_human_decision_recorded"),
    )
    with pytest.raises(RuntimeError, match="injected crash"):
        continue_substage(
            Path(paused.artifact_directory),
            decision,
            services=software,
            physics_services=crash_physics,
        )
    plan = build_recovery_plan(Path(paused.artifact_directory))
    assert plan.operation == "replay_human_decision"
    execution = execute_recovery_plan(
        plan,
        services=RecoveryServices(
            workflow_services=software,
            physics_services=crash_physics,
            attempt_token=lambda: "decision-once",
        ),
    )
    assert execution.outcome.result_status == "aborted"
    journal = (Path(paused.artifact_directory) / "physics-journal-v2.jsonl").read_text()
    assert journal.count('"reason":"physics_human_decision_recorded"') == 1


@pytest.mark.skipif(not BWRAP.is_file(), reason="Bubblewrap PA-2 path unavailable")
def test_incomplete_physics_result_snapshot_is_finalized_from_proofs(tmp_path: Path) -> None:
    spec, _, fake = _physics_tree(tmp_path)
    physics = ScriptedPhysicsAuditor(SYNTHETIC / "reports/clean.json")
    software = WorkflowServices(codex_executable=str(fake), token_factory=lambda: "pa5-finalize")
    completed = run_substage(
        spec,
        runs_dir=tmp_path / "runs",
        services=software,
        physics_services=PhysicsWorkflowServices(physics_auditor_codex_invoker=physics),
    )
    run_directory = Path(completed.artifact_directory)
    (run_directory / "result.json").write_text("{}\n", encoding="utf-8")
    plan = build_recovery_plan(run_directory)
    assert plan.operation == "finalize_snapshots"
    execution = execute_recovery_plan(
        plan,
        services=RecoveryServices(
            workflow_services=software,
            physics_services=PhysicsWorkflowServices(physics_auditor_codex_invoker=physics),
            attempt_token=lambda: "finalize-once",
        ),
    )
    assert execution.outcome.status == "finalized"
    assert execution.outcome.result_status == "completed"
    assert physics.calls == 1


def test_active_workflow_lock_and_foreign_lock_fail_closed(tmp_path: Path) -> None:
    spec, _, fake = create_workflow_tree(
        tmp_path,
        responses=[codex_response("worker", "worker-thread-1", worker_result("needs_human"))],
    )
    result = run_substage(
        spec,
        runs_dir=tmp_path / "runs",
        services=_services(fake, "lock-check"),
    )
    run_directory = Path(result.artifact_directory)
    with _WorkflowLock(run_directory, lambda: datetime.now(UTC)):
        active = build_recovery_plan(run_directory)
    assert active.disposition == "blocked"
    assert active.reason_code == "active_matching_process"

    (run_directory / "workflow.lock").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "pid": 999_999_999,
                "host": "different-host.invalid",
                "started_at": "2026-08-05T00:00:00.000000Z",
            }
        ),
        encoding="utf-8",
    )
    foreign = build_recovery_plan(run_directory)
    assert foreign.disposition == "blocked"
    assert foreign.reason_code == "foreign_host_process_ambiguity"


@pytest.mark.skipif(not BWRAP.is_file(), reason="Bubblewrap PA-2 path unavailable")
def test_stale_and_reused_child_pid_identities_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec, _, fake = _physics_tree(tmp_path)
    software = WorkflowServices(codex_executable=str(fake), token_factory=lambda: "pid-identity")

    def exited_process_auditor(**kwargs: object) -> object:
        process = subprocess.Popen(["sleep", "60"], start_new_session=True)
        try:
            callback = kwargs["process_started"]
            assert callable(callback)
            callback(process.pid)
        finally:
            process.terminate()
            process.wait(timeout=5)
        raise AssertionError("model-running checkpoint did not interrupt")

    with pytest.raises(RuntimeError, match="injected crash"):
        run_substage(
            spec,
            runs_dir=tmp_path / "runs",
            services=software,
            physics_services=PhysicsWorkflowServices(
                physics_auditor_codex_invoker=exited_process_auditor,  # type: ignore[arg-type]
                checkpoint=CrashOnce("physics_auditor:model_running"),
            ),
        )
    run_directory = tmp_path / "runs/minimal-substage-pid-identity"
    stale = build_recovery_plan(run_directory)
    assert stale.disposition == "blocked"
    assert stale.reason_code == "stale_pid_ambiguity"
    observation = next(
        item for item in stale.process_observations if item.scope == "physics_auditor"
    )
    assert observation.expected_start_ticks is not None

    monkeypatch.setattr(
        workflow_recovery,
        "_process_start_ticks",
        lambda _pid: observation.expected_start_ticks + 1,  # type: ignore[operator]
    )
    reused = build_recovery_plan(run_directory)
    assert reused.disposition == "blocked"
    assert reused.reason_code == "reused_pid_ambiguity"


def test_authority_workspace_and_action_tampering_block_recovery(tmp_path: Path) -> None:
    spec, project, fake = create_workflow_tree(tmp_path / "authority")
    completed = run_substage(
        spec,
        runs_dir=tmp_path / "authority-runs",
        services=_services(fake, "authority"),
    )
    (project / "control/contract.md").write_text("changed contract\n", encoding="utf-8")
    authority_plan = build_recovery_plan(Path(completed.artifact_directory))
    assert authority_plan.disposition == "blocked"
    assert authority_plan.reason_code == "frozen_authority_changed"

    spec, project, fake = create_workflow_tree(tmp_path / "workspace")
    completed = run_substage(
        spec,
        runs_dir=tmp_path / "workspace-runs",
        services=_services(fake, "workspace"),
    )
    (project / "src/tampered.txt").write_text("unexpected\n", encoding="utf-8")
    workspace_plan = build_recovery_plan(Path(completed.artifact_directory))
    assert workspace_plan.disposition == "blocked"
    assert workspace_plan.reason_code == "workspace_content_changed"

    spec, _, fake = create_workflow_tree(tmp_path / "action")

    def complete_then_interrupt(prepared: object, **kwargs: object) -> object:
        run_prepared_codex(prepared, **kwargs)  # type: ignore[arg-type]
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_substage(
            spec,
            runs_dir=tmp_path / "action-runs",
            services=WorkflowServices(
                codex_executable=str(fake),
                codex_invoker=complete_then_interrupt,  # type: ignore[arg-type]
                token_factory=lambda: "action-tamper",
            ),
        )
    run_directory = tmp_path / "action-runs/minimal-substage-action-tamper"
    result_path = run_directory / "worker/codex/worker-r000/result.json"
    result_path.write_bytes(result_path.read_bytes() + b" ")
    action_plan = build_recovery_plan(run_directory)
    assert action_plan.disposition == "blocked"
    assert action_plan.reason_code == "action_proof_invalid"
    assert (tmp_path / "action/fake-counter").read_text(encoding="ascii") == "1"


def test_corrupt_discovered_run_prevents_latest_guess(tmp_path: Path) -> None:
    spec, _, fake = create_workflow_tree(
        tmp_path / "valid",
        responses=[codex_response("worker", "worker-thread-1", worker_result("needs_human"))],
    )
    run_substage(
        spec,
        runs_dir=tmp_path / "runs",
        services=_services(fake, "valid"),
    )
    corrupt = tmp_path / "runs/corrupt-run"
    corrupt.mkdir()
    (corrupt / "journal.jsonl").write_text("not-json\n", encoding="ascii")
    (corrupt / "state.json").write_text("{}\n", encoding="ascii")
    index = discover_workflow_runs(tmp_path / "runs")
    assert len(index.entries) == 1
    assert len(index.issues) == 1
    with pytest.raises(RecoverySelectionError) as caught:
        latest_incomplete_run(index)
    assert caught.value.reason_code == "run_discovery_integrity_failed"
