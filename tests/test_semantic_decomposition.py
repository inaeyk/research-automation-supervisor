from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from research_automation_supervisor.semantic_decomposition import (
    AgentHandoffV1,
    ArtifactReferenceV1,
    AuthorityReferenceV1,
    PredecessorArtifactRequirementV1,
    RepositoryIdentityV1,
    SemanticSubtaskTelemetryV1,
    SemanticSubtaskV1,
    SemanticTaskPlanV1,
    SessionLaunchV1,
    ValidationReceiptV1,
    aggregate_semantic_telemetry,
    fresh_session_launch,
    measure_agent_handoff,
    validation_reuse_decision,
    write_semantic_task_plan,
)
from research_automation_supervisor.semantic_replay import (
    ControlledReplayV1,
    ReplayCommandV1,
    _completed_subtask_prefix,
    _verified_completed_task,
    load_control,
    prepare_replay,
    render_subtask_prompt,
    repository_identity,
)

AUTHORITY_HASH = "a" * 64


def _authority(path: str = "authority.md", digest: str = AUTHORITY_HASH) -> AuthorityReferenceV1:
    return AuthorityReferenceV1(path=path, sha256=digest)


def _subtask(
    sequence: int,
    *,
    role: str = "worker",
    boundary: str = "independent_component",
    coherence_key: str | None = None,
    objective: str | None = None,
) -> SemanticSubtaskV1:
    return SemanticSubtaskV1.model_validate(
        {
            "subtask_id": f"task-{sequence}",
            "sequence": sequence,
            "role": role,
            "boundary": boundary,
            "coherence_key": coherence_key or f"material-component-{sequence}",
            "objective": objective or f"Implement material component {sequence} as one unit.",
            "scope": [f"Bounded component {sequence} and its interface."],
            "authority_references": [_authority().model_dump(mode="json")],
            "required_predecessor_artifacts": [],
            "deliverables": [f"Completed component {sequence}."],
            "completion_conditions": [f"Component {sequence} is independently checkable."],
            "validation_requirements": [f"Run component {sequence} checks once."],
            "stop_conditions": [f"Stop if component {sequence} authority is unavailable."],
        }
    )


def _plan(*subtasks: SemanticSubtaskV1) -> SemanticTaskPlanV1:
    return SemanticTaskPlanV1(
        plan_id="semantic-plan",
        stage_id="substantial implementation stage",
        substantial_stage=True,
        authority_references=(_authority(),),
        subtasks=subtasks,
    )


def _repository() -> RepositoryIdentityV1:
    return RepositoryIdentityV1(
        repository="/candidate",
        base_commit="1" * 40,
        head_commit="2" * 40,
        diff_sha256="3" * 64,
        dirty=True,
    )


def _handoff(**updates: object) -> AgentHandoffV1:
    values: dict[str, object] = {
        "handoff_id": "worker-handoff",
        "completed_objective": "Implemented the bounded component.",
        "changed_paths": ["src/component.py"],
        "changed_interfaces": ["component.run"],
        "repository_identity": _repository().model_dump(mode="json"),
        "authority_references": [_authority().model_dump(mode="json")],
        "valid_evidence_receipts": [],
        "decisions_and_invariants": ["Exactly-once state remains authoritative."],
        "unresolved_findings": [],
        "remaining_work": ["Audit the candidate independently."],
        "next_subtask_requirements": ["Inspect the candidate diff and this handoff."],
        "do_not_rediscover_or_retest": ["Do not rerun the unchanged deterministic unit test."],
    }
    values.update(updates)
    return AgentHandoffV1.model_validate(values)


def _artifact(path: str, digest: str) -> ArtifactReferenceV1:
    return ArtifactReferenceV1(path=path, sha256=digest, kind="evidence")


def test_substantial_plan_requires_two_to_six_coherent_subtasks() -> None:
    assert len(_plan(_subtask(1), _subtask(2)).subtasks) == 2
    assert len(_plan(*(_subtask(index) for index in range(1, 7))).subtasks) == 6
    with pytest.raises(ValidationError, match="2-6"):
        _plan(_subtask(1))
    with pytest.raises(ValidationError):
        SemanticTaskPlanV1.model_validate(
            {
                "plan_id": "too-many",
                "stage_id": "pathological stage",
                "substantial_stage": True,
                "authority_references": [_authority().model_dump(mode="json")],
                "subtasks": [_subtask(index).model_dump(mode="json") for index in range(1, 8)],
            }
        )


def test_validated_plan_is_recorded_once_before_launch(tmp_path: Path) -> None:
    plan = _plan(_subtask(1), _subtask(2))
    destination = tmp_path / "plans" / "semantic-plan.json"
    write_semantic_task_plan(plan, destination)
    assert SemanticTaskPlanV1.model_validate_json(destination.read_bytes()) == plan
    with pytest.raises(FileExistsError):
        write_semantic_task_plan(plan, destination)


def test_plan_rejects_mechanical_and_adjacent_micro_splits() -> None:
    with pytest.raises(ValidationError, match="mechanical operation"):
        _subtask(1, objective="Grep for one symbol in the source tree.")
    first = _subtask(1, coherence_key="same-material-unit")
    second = _subtask(2, coherence_key="same-material-unit")
    with pytest.raises(ValidationError, match="pathological micro-split"):
        _plan(first, second)


def test_fresh_session_default_and_qualified_continuation_exception() -> None:
    worker = _subtask(1)
    assert fresh_session_launch(worker).fresh_session is True
    continued = SessionLaunchV1(
        subtask_id=worker.subtask_id,
        role="repair",
        fresh_session=False,
        prior_thread_id="worker-thread-123",
        continuation_reason="qualified_recovery",
        durable_reason="PA-4 recovery authority requires the exact Worker session identity.",
        authority_references=(_authority(),),
    )
    assert continued.fresh_session is False
    with pytest.raises(ValidationError, match="auditors must start"):
        SessionLaunchV1(
            subtask_id="audit",
            role="coding_auditor",
            fresh_session=False,
            prior_thread_id="worker-thread-123",
            continuation_reason="qualified_recovery",
            durable_reason="An invalid attempt to continue the Worker session for an audit.",
            authority_references=(_authority(),),
        )


def test_handoff_schema_hash_references_transcript_ban_and_size_limits() -> None:
    handoff = _handoff()
    assert handoff.authority_references[0].sha256 == AUTHORITY_HASH
    assert measure_agent_handoff(handoff, token_counter=lambda _: 1_000).target_met
    with pytest.raises(ValidationError):
        _handoff(transcript_included=True)
    with pytest.raises(ValidationError):
        AgentHandoffV1.model_validate(
            {**handoff.model_dump(mode="json"), "worker_conversation": "must not transfer"}
        )
    with pytest.raises(ValueError, match="requires justification"):
        measure_agent_handoff(handoff, token_counter=lambda _: 2_001)
    justified = handoff.model_copy(
        update={
            "oversize_justification": ("A required interface inventory exceeds the soft maximum.")
        }
    )
    assert not measure_agent_handoff(justified, token_counter=lambda _: 2_001).soft_max_met


def test_worker_audit_and_audit_repair_boundaries_transfer_only_durable_state() -> None:
    auditor = _subtask(
        1,
        role="coding_auditor",
        boundary="implementation_to_audit",
        objective="Independently audit the candidate and durable Worker evidence.",
    )
    launch = fresh_session_launch(auditor)
    assert launch.fresh_session and launch.prior_thread_id is None
    prompt = render_subtask_prompt(
        auditor,
        predecessor_artifacts=(_artifact("handoffs/worker.json", "b" * 64),),
        authority_root=Path("/authority"),
        expected_repository=_repository(),
        global_target=Path("/isolated/codex-home"),
    )
    assert "no prior conversation was supplied" in prompt
    assert "handoffs/worker.json" in prompt
    assert "Worker reasoning history" not in prompt

    audit_handoff = _handoff(
        handoff_id="audit-handoff",
        completed_objective="Audited the candidate independently.",
        changed_paths=[],
        changed_interfaces=[],
        unresolved_findings=["Finding F-1 affects src/component.py."],
        remaining_work=["Repair finding F-1 in the qualified Worker session."],
        next_subtask_requirements=["Use evidence receipt evidence/audit-f-1.json."],
    )
    assert audit_handoff.transcript_included is False
    repair = SessionLaunchV1(
        subtask_id="repair-f-1",
        role="repair",
        fresh_session=False,
        prior_thread_id="qualified-worker-thread",
        continuation_reason="qualified_recovery",
        durable_reason="Existing workflow authority binds repair to the original Worker identity.",
        authority_references=(_authority(),),
    )
    assert repair.continuation_reason == "qualified_recovery"


def test_pass_receipt_reuse_requires_every_validity_input_to_be_unchanged() -> None:
    test_code = (_artifact("tests/test_component.py", "1" * 64),)
    source = (_artifact("src/component.py", "2" * 64),)
    config = (_artifact("pyproject.toml", "3" * 64),)
    receipt = ValidationReceiptV1(
        receipt_id="component-pass",
        status="PASS",
        command=("pytest", "-q", "tests/test_component.py"),
        test_code=test_code,
        relevant_source=source,
        config=config,
        environment_assumptions=("Python 3.11-compatible runtime",),
        exit_code=0,
        evidence=_artifact("evidence/component-pass.log", "4" * 64),
    )
    reusable = validation_reuse_decision(
        receipt,
        test_code=test_code,
        relevant_source=source,
        config=config,
        environment_assumptions=("Python 3.11-compatible runtime",),
    )
    assert reusable.reusable and not reusable.rerun_required
    invalidated = validation_reuse_decision(
        receipt,
        test_code=test_code,
        relevant_source=(_artifact("src/component.py", "9" * 64),),
        config=config,
        environment_assumptions=("Python 3.11-compatible runtime",),
    )
    assert invalidated.rerun_required
    assert invalidated.reason == "relevant_source_changed"


def _telemetry(
    subtask_id: str,
    role: str,
    *,
    repair_or_retry: bool = False,
    input_tokens: int = 100,
) -> SemanticSubtaskTelemetryV1:
    return SemanticSubtaskTelemetryV1.model_validate(
        {
            "subtask_id": subtask_id,
            "role": role,
            "repair_or_retry": repair_or_retry,
            "usage_receipt_id": f"receipt-{subtask_id}",
            "accounting_complete": True,
            "input_tokens": input_tokens,
            "cached_input_tokens": 80,
            "uncached_input_tokens": input_tokens - 80,
            "output_tokens": 10,
            "reasoning_output_tokens": 4,
            "combined_tokens": input_tokens + 10,
            "inference_sample_count": 2,
            "median_inference_context_tokens": 50,
            "max_inference_context_tokens": input_tokens,
            "compactions": 1,
            "command_tool_count": 3,
            "model_visible_tool_output_chars": 400,
            "handoff_size_bytes": 600,
            "handoff_size_tokens": 150,
            "session_fresh": True,
            "continuation_reason": None,
        }
    )


def test_token_telemetry_aggregates_by_subtask_role_and_repair_without_estimates() -> None:
    worker = _telemetry("worker", "worker")
    auditor = _telemetry("auditor", "coding_auditor", input_tokens=120)
    repair = _telemetry("repair", "repair", repair_or_retry=True, input_tokens=140)
    aggregate = aggregate_semantic_telemetry((worker, auditor, repair))
    assert aggregate.by_role["worker"].combined_tokens == 110
    assert aggregate.by_role["coding_auditor"].input_tokens == 120
    assert aggregate.repair_or_retry.input_tokens == 140
    assert aggregate.total_stage.input_tokens == 360
    assert aggregate.total_stage.combined_tokens == 390
    assert aggregate.total_stage.median_inference_context_tokens is None
    with pytest.raises(ValueError, match="duplicate usage receipt"):
        aggregate_semantic_telemetry((worker, worker))
    malformed = worker.model_dump(mode="json")
    malformed["combined_tokens"] = 999
    with pytest.raises(ValidationError, match="combined tokens"):
        SemanticSubtaskTelemetryV1.model_validate(malformed)


def test_frozen_bootstrap_replay_plan_has_five_material_fresh_boundaries() -> None:
    root = Path(__file__).resolve().parents[1]
    control = load_control(
        root / "docs/validation/semantic_decomposition_v1/bootstrap_replay_control.json"
    )
    assert len(control.plan.subtasks) == 5
    assert control.plan.subtasks[-1].role == "coding_auditor"
    assert control.workload_authority.sha256 == (
        "2da7e7a7e3e67b4f70200846a07bf5eea9190ad800d86d691be3992776d6bbde"
    )


def test_replay_preparation_is_dry_fresh_and_exactly_once_safe(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    subprocess.run(("git", "init", "-q"), cwd=workspace, check=True)
    authority_path = workspace / "authority.md"
    authority_path.write_text("frozen workload\n", encoding="utf-8")
    subprocess.run(("git", "add", "authority.md"), cwd=workspace, check=True)
    subprocess.run(
        (
            "git",
            "-c",
            "user.name=Semantic Test",
            "-c",
            "user.email=semantic@example.invalid",
            "commit",
            "-q",
            "-m",
            "authority",
        ),
        cwd=workspace,
        check=True,
    )
    commit = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    digest = hashlib.sha256(authority_path.read_bytes()).hexdigest()
    authority = _authority(digest=digest)
    worker = _subtask(1).model_copy(update={"authority_references": (authority,)})
    auditor = _subtask(
        2,
        role="coding_auditor",
        boundary="implementation_to_audit",
        objective="Independently audit the implemented candidate and evidence.",
    ).model_copy(
        update={
            "authority_references": (authority,),
            "required_predecessor_artifacts": (
                PredecessorArtifactRequirementV1(
                    path="handoffs/task-1.json",
                    kind="handoff",
                    produced_by_subtask_id="task-1",
                ),
            ),
        }
    )
    plan = SemanticTaskPlanV1(
        plan_id="semantic-plan",
        stage_id="substantial implementation stage",
        substantial_stage=True,
        authority_references=(authority,),
        subtasks=(worker, auditor),
    )
    control = ControlledReplayV1(
        control_id="synthetic-replay",
        implementation_authority_commit=commit,
        replay_start_commit=commit,
        model="gpt-5.6-sol",
        reasoning_effort="high",
        workload_authority=authority,
        global_target_writer_subtask_ids=(worker.subtask_id,),
        plan=plan,
    )
    control_path = tmp_path / "control.json"
    control_path.write_text(json.dumps(control.model_dump(mode="json")), encoding="utf-8")
    artifact_root = tmp_path / "artifacts"
    global_target = tmp_path / "global-target"
    preparation = prepare_replay(
        control_path=control_path,
        authority_root=workspace,
        workspace=workspace,
        artifact_root=artifact_root,
        global_target=global_target,
        codex_task=Path("/fake/codex-task"),
    )
    assert not artifact_root.exists()
    assert not global_target.exists()
    assert preparation.ledger_root.endswith("/task-ledgers")
    assert len({command.task_id for command in preparation.commands}) == 2
    assert all(command.argv[1] == "run" for command in preparation.commands)
    assert all("resume" not in command.argv for command in preparation.commands)
    assert "--add-dir" in preparation.commands[0].argv
    assert str(global_target) in preparation.commands[0].argv
    assert "--add-dir" not in preparation.commands[-1].argv
    assert "--sandbox" in preparation.commands[-1].argv
    auditor_argv = preparation.commands[-1].argv
    assert auditor_argv[auditor_argv.index("--sandbox") + 1] == "read-only"

    artifact_root.mkdir()
    handoff_path = artifact_root / "handoffs" / "task-1.json"
    handoff_path.parent.mkdir()
    handoff_path.write_text("{}", encoding="utf-8")
    assert _completed_subtask_prefix(plan, artifact_root) == ()
    later_telemetry = artifact_root / "telemetry" / "task-2.json"
    later_telemetry.parent.mkdir()
    later_telemetry.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="incomplete or not a contiguous"):
        _completed_subtask_prefix(plan, artifact_root)

    recovery_root = tmp_path / "recovery-artifacts"
    recovery_handoffs = recovery_root / "handoffs"
    recovery_telemetry = recovery_root / "telemetry"
    recovery_handoffs.mkdir(parents=True)
    recovery_telemetry.mkdir()
    stored_handoff = _handoff(
        handoff_id="task-1-handoff",
        repository_identity=repository_identity(workspace, commit).model_dump(mode="json"),
        authority_references=[authority.model_dump(mode="json")],
    )
    (recovery_handoffs / "task-1.json").write_text(
        json.dumps(stored_handoff.model_dump(mode="json")), encoding="utf-8"
    )
    (recovery_telemetry / "task-1.json").write_text(
        json.dumps(_telemetry("task-1", "worker").model_dump(mode="json")),
        encoding="utf-8",
    )
    (workspace / "candidate.py").write_text("mutated after handoff\n", encoding="utf-8")
    with pytest.raises(ValueError, match="last completed handoff"):
        prepare_replay(
            control_path=control_path,
            authority_root=workspace,
            workspace=workspace,
            artifact_root=recovery_root,
            global_target=global_target,
            codex_task=Path("/fake/codex-task"),
        )


def test_completed_global_task_is_qualified_for_exactly_once_recovery(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    prompt = tmp_path / "prompt.md"
    prompt.write_text("bounded semantic prompt\n", encoding="utf-8")
    argv = (
        "/fake/codex-task",
        "run",
        "plan.task-1",
        str(workspace),
        str(prompt),
        "--sandbox",
        "workspace-write",
        "--model",
        "gpt-5.6-sol",
    )
    command = ReplayCommandV1(
        subtask_id="task-1",
        task_id="plan.task-1",
        prompt_path=str(prompt),
        handoff_path=str(tmp_path / "handoff.json"),
        launch_path=str(tmp_path / "launch.json"),
        argv=argv,
    )
    task_root = tmp_path / "ledger" / command.task_id
    turns = task_root / "turns"
    turns.mkdir(parents=True)
    (turns / "000001.events.jsonl").write_text(
        '{"type":"turn.completed","usage":{"input_tokens":1}}\n',
        encoding="utf-8",
    )
    (turns / "000001.final-message.md").write_text("{}\n", encoding="utf-8")
    (turns / "000001.prompt.sha256").write_text(
        hashlib.sha256(prompt.read_bytes()).hexdigest() + "\n", encoding="ascii"
    )
    (task_root / "task.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "task_id": command.task_id,
                "working_directory": str(workspace.resolve()),
                "model": "gpt-5.6-sol",
                "initial_options": list(argv[5:]),
                "turn_count": 1,
                "usage_complete": True,
            }
        ),
        encoding="utf-8",
    )
    events, final, _ = _verified_completed_task(
        task_root=task_root,
        command=command,
        workspace=workspace,
        expected_model="gpt-5.6-sol",
    )
    assert events.name == "000001.events.jsonl"
    assert final.name == "000001.final-message.md"
    (turns / "000001.prompt.sha256").write_text("0" * 64 + "\n", encoding="ascii")
    with pytest.raises(ValueError, match="prompt hash"):
        _verified_completed_task(
            task_root=task_root,
            command=command,
            workspace=workspace,
            expected_model="gpt-5.6-sol",
        )
