from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

import research_automation_supervisor.semantic_replay as semantic_replay
from research_automation_supervisor.semantic_decomposition import (
    AgentHandoffV1,
    ArtifactReferenceV1,
    AuthorityReferenceV1,
    ContextLocalityFactsV1,
    PredecessorArtifactRequirementV1,
    RepositoryIdentityV1,
    SemanticSubtaskTelemetryV1,
    SemanticSubtaskV1,
    SemanticTaskPlanV1,
    SessionEpochPlanV1,
    SessionEpochV1,
    SessionLaunchV1,
    ValidationReceiptV1,
    aggregate_semantic_telemetry,
    choose_session_boundary,
    continued_epoch_launch,
    fresh_session_launch,
    measure_agent_handoff,
    validation_reuse_decision,
    write_agent_handoff,
    write_semantic_task_plan,
)
from research_automation_supervisor.semantic_replay import (
    ControlledReplayV1,
    ReplayCommandV1,
    _completed_subtask_prefix,
    _refuse_unqualified_turn_relaunch,
    _verified_completed_task,
    execute_replay,
    load_control,
    prepare_replay,
    render_subtask_prompt,
    repository_identity,
)
from research_automation_supervisor.token_accounting import load_verified_receipt

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


def _epoch(
    epoch_id: str,
    subtasks: tuple[SemanticSubtaskV1, ...],
    *,
    forced_freshness_reason: str,
) -> SessionEpochV1:
    return SessionEpochV1.model_validate(
        {
            "epoch_id": epoch_id,
            "subtask_ids": [subtask.subtask_id for subtask in subtasks],
            "role": subtasks[0].role,
            "continuation_policy": (
                "continue_same_thread" if len(subtasks) > 1 else "single_subtask"
            ),
            "context_economy_profile": "B4",
            "configuration": {
                "model": "gpt-5.6-sol",
                "reasoning_effort": "high",
                "sandbox": (
                    "read-only"
                    if subtasks[0].role in {"coding_auditor", "physics_auditor"}
                    else "workspace-write"
                ),
            },
            "rationale": "Durable deterministic test policy for this bounded session epoch.",
            "forced_freshness_reason": forced_freshness_reason,
        }
    )


def _epoch_plan(
    plan: SemanticTaskPlanV1,
    groups: tuple[tuple[SemanticSubtaskV1, ...], ...] | None = None,
) -> SessionEpochPlanV1:
    selected = groups or tuple((subtask,) for subtask in plan.subtasks)
    epochs = tuple(
        _epoch(
            f"epoch-{index}",
            group,
            forced_freshness_reason="initial_epoch" if index == 1 else "role_change",
        )
        for index, group in enumerate(selected, start=1)
    )
    return SessionEpochPlanV1(
        epoch_plan_id=f"{plan.plan_id}-epochs",
        semantic_plan_id=plan.plan_id,
        ordered_subtask_ids=tuple(subtask.subtask_id for subtask in plan.subtasks),
        epochs=epochs,
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
        epoch_id="worker-epoch",
        epoch_turn_index=2,
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
            epoch_id="audit-epoch",
            epoch_turn_index=2,
            subtask_id="audit",
            role="coding_auditor",
            fresh_session=False,
            prior_thread_id="worker-thread-123",
            continuation_reason="qualified_recovery",
            durable_reason="An invalid attempt to continue the Worker session for an audit.",
            authority_references=(_authority(),),
        )


def test_semantic_boundaries_are_partitioned_into_contiguous_session_epochs() -> None:
    first, second, third = (_subtask(index) for index in range(1, 4))
    plan = _plan(first, second, third)
    epoch_plan = _epoch_plan(plan, ((first,), (second, third)))
    assert epoch_plan.ordered_subtask_ids == ("task-1", "task-2", "task-3")
    assert epoch_plan.epochs[1].subtask_ids == ("task-2", "task-3")
    assert len(epoch_plan.epochs) == 2
    launch = continued_epoch_launch(
        third,
        epoch=epoch_plan.epochs[1],
        epoch_turn_index=2,
        thread_id="thread-shared",
    )
    assert launch.continuation_reason == "epoch_continuation"
    assert launch.prior_thread_id == "thread-shared"

    invalid = epoch_plan.model_dump(mode="json")
    invalid["epochs"][0]["subtask_ids"] = ["task-1", "task-3"]
    invalid["epochs"][0]["continuation_policy"] = "continue_same_thread"
    invalid["epochs"][1]["subtask_ids"] = ["task-2"]
    invalid["epochs"][1]["continuation_policy"] = "single_subtask"
    with pytest.raises(ValidationError, match="contiguous order"):
        SessionEpochPlanV1.model_validate(invalid)


def test_context_locality_policy_is_deterministic_and_role_changes_force_freshness() -> None:
    worker = _subtask(1)
    adjacent_worker = _subtask(2)
    continuation = choose_session_boundary(
        worker,
        adjacent_worker,
        ContextLocalityFactsV1(
            same_subsystem=True,
            depends_on_previous_interfaces=True,
            shared_source_test_architecture=True,
            implementation_integration_qualification_chain=True,
            repair_current_candidate=False,
            subsystem_independent=False,
            little_relevant_context=False,
            context_health_exceeded=False,
            qualified_recovery_requires_new_identity=False,
        )
    )
    assert continuation.fresh_session is False
    assert continuation.reason == "dependent_interfaces"

    auditor = _subtask(
        2,
        role="coding_auditor",
        boundary="implementation_to_audit",
        objective="Independently audit the implemented candidate and evidence.",
    )
    fresh = choose_session_boundary(
        worker,
        auditor,
        ContextLocalityFactsV1(
            same_subsystem=True,
            depends_on_previous_interfaces=True,
            shared_source_test_architecture=True,
            implementation_integration_qualification_chain=True,
            repair_current_candidate=False,
            subsystem_independent=False,
            little_relevant_context=False,
            context_health_exceeded=False,
            qualified_recovery_requires_new_identity=False,
        )
    )
    assert fresh.fresh_session is True
    assert fresh.reason == "role_change"


def test_handoff_schema_hash_references_transcript_ban_and_size_limits() -> None:
    handoff = _handoff()
    assert handoff.authority_references[0].sha256 == AUTHORITY_HASH
    bytes_only = measure_agent_handoff(handoff)
    assert bytes_only.counting_method == "utf8_bytes_only"
    assert bytes_only.exact_token_count is None
    assert bytes_only.token_upper_bound is None
    assert bytes_only.target_met is None
    assert bytes_only.soft_max_met is None
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


def test_handoff_without_exact_tokenizer_enforces_deterministic_byte_limits() -> None:
    above_byte_soft_max = _handoff(remaining_work=["x" * 5_000])
    with pytest.raises(ValueError, match="4096-byte soft maximum"):
        measure_agent_handoff(above_byte_soft_max)

    justified = above_byte_soft_max.model_copy(
        update={
            "oversize_justification": "The required durable recovery facts exceed 4096 bytes."
        }
    )
    size = measure_agent_handoff(justified)
    assert size.byte_count > size.soft_max_bytes
    assert size.exact_token_count is None
    assert size.counting_method == "utf8_bytes_only"

    above_absolute_max = justified.model_copy(update={"remaining_work": ("x" * 9_000,)})
    with pytest.raises(ValueError, match="absolute 8192-byte upper bound"):
        measure_agent_handoff(above_absolute_max)


def test_worker_audit_and_audit_repair_boundaries_transfer_only_durable_state() -> None:
    auditor = _subtask(
        1,
        role="coding_auditor",
        boundary="implementation_to_audit",
        objective="Independently audit the candidate and durable Worker evidence.",
    )
    launch = fresh_session_launch(auditor)
    assert launch.fresh_session and launch.prior_thread_id is None
    audit_epoch = _epoch("audit-epoch", (auditor,), forced_freshness_reason="initial_epoch")
    prompt = render_subtask_prompt(
        auditor,
        epoch=audit_epoch,
        epoch_turn_index=1,
        predecessor_artifacts=(_artifact("handoffs/worker.json", "b" * 64),),
        authority_root=Path("/authority"),
        expected_repository=_repository(),
        global_target=Path("/isolated/codex-home"),
    )
    assert "No conversation transcript was transferred" in prompt
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
        epoch_id="repair-epoch",
        epoch_turn_index=2,
        subtask_id="repair-f-1",
        role="repair",
        fresh_session=False,
        prior_thread_id="qualified-worker-thread",
        continuation_reason="qualified_recovery",
        durable_reason="Existing workflow authority binds repair to the original Worker identity.",
        authority_references=(_authority(),),
    )
    assert repair.continuation_reason == "qualified_recovery"


def test_continued_epoch_prompt_is_a_compact_delta_without_handoff_reinjection() -> None:
    first, second = (_subtask(index) for index in range(1, 3))
    epoch = _epoch(
        "worker-epoch",
        (first, second),
        forced_freshness_reason="initial_epoch",
    )
    prompt = render_subtask_prompt(
        second,
        epoch=epoch,
        epoch_turn_index=2,
        predecessor_artifacts=(),
        authority_root=Path("/authority"),
        expected_repository=_repository(),
        global_target=Path("/isolated/codex-home"),
    )
    assert '"semantic_delta"' in prompt
    assert '"semantic_subtask"' not in prompt
    assert '"required_predecessor_artifacts"' not in prompt
    assert '"predecessor_artifacts": []' in prompt
    assert "No full AgentHandoffV1 is injected back into this session" in prompt


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
    epoch_id: str | None = None,
    epoch_turn_index: int = 1,
    thread_id: str | None = None,
) -> SemanticSubtaskTelemetryV1:
    return SemanticSubtaskTelemetryV1.model_validate(
        {
            "subtask_id": subtask_id,
            "epoch_id": epoch_id or f"epoch-{subtask_id}",
            "epoch_turn_index": epoch_turn_index,
            "codex_thread_id": thread_id or f"thread-{subtask_id}",
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
            "session_fresh": epoch_turn_index == 1,
            "continuation_reason": (
                None if epoch_turn_index == 1 else "epoch_continuation"
            ),
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
    assert aggregate.total_stage.turn_count == 3
    assert aggregate.total_stage.session_count == 3
    assert aggregate.total_stage.median_inference_context_tokens is None
    with pytest.raises(ValueError, match="duplicate usage receipt"):
        aggregate_semantic_telemetry((worker, worker))
    malformed = worker.model_dump(mode="json")
    malformed["combined_tokens"] = 999
    with pytest.raises(ValidationError, match="combined tokens"):
        SemanticSubtaskTelemetryV1.model_validate(malformed)


def test_multi_turn_epoch_aggregates_each_receipt_once_without_fabricated_inference() -> None:
    first = _telemetry(
        "task-1", "worker", epoch_id="worker-epoch", thread_id="thread-shared"
    ).model_copy(
        update={
            "inference_sample_count": None,
            "median_inference_context_tokens": None,
            "max_inference_context_tokens": None,
            "compactions": None,
        }
    )
    second = _telemetry(
        "task-2",
        "worker",
        input_tokens=130,
        epoch_id="worker-epoch",
        epoch_turn_index=2,
        thread_id="thread-shared",
    ).model_copy(
        update={
            "inference_sample_count": None,
            "median_inference_context_tokens": None,
            "max_inference_context_tokens": None,
            "compactions": None,
        }
    )
    aggregate = aggregate_semantic_telemetry((first, second))
    assert aggregate.total_stage.input_tokens == 230
    assert aggregate.total_stage.combined_tokens == 250
    assert aggregate.total_stage.turn_count == 2
    assert aggregate.total_stage.session_count == 1
    assert aggregate.total_stage.inference_sample_count is None
    assert aggregate.total_stage.max_inference_context_tokens is None
    assert aggregate.total_stage.compactions is None

    changed_thread = second.model_copy(update={"codex_thread_id": "thread-new"})
    with pytest.raises(ValueError, match="changes Codex thread identity"):
        aggregate_semantic_telemetry((first, changed_thread))

    reused_fresh_thread = _telemetry(
        "task-3", "worker", epoch_id="fresh-epoch", thread_id="thread-shared"
    )
    with pytest.raises(ValueError, match="reused across fresh session epochs"):
        aggregate_semantic_telemetry((first, reused_fresh_thread))


def test_frozen_bootstrap_replay_plan_has_exact_three_epoch_topology() -> None:
    root = Path(__file__).resolve().parents[1]
    control = load_control(
        root / "docs/validation/semantic_decomposition_v1/bootstrap_replay_control.json"
    )
    assert len(control.plan.subtasks) == 5
    assert control.plan.subtasks[-1].role == "coding_auditor"
    assert tuple(epoch.subtask_ids for epoch in control.epoch_plan.epochs) == (
        ("01-runtime-wrapper",),
        ("02-ras-accounting", "03-launch-integration", "04-qualification"),
        ("05-coding-audit",),
    )
    assert tuple(epoch.role for epoch in control.epoch_plan.epochs) == (
        "worker",
        "worker",
        "coding_auditor",
    )
    assert control.epoch_plan.epochs[-1].configuration.sandbox == "read-only"
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
    workers = tuple(
        _subtask(index).model_copy(update={"authority_references": (authority,)})
        for index in range(1, 5)
    )
    auditor = _subtask(
        5,
        role="coding_auditor",
        boundary="implementation_to_audit",
        objective="Independently audit the implemented candidate and evidence.",
    ).model_copy(
        update={
            "authority_references": (authority,),
            "required_predecessor_artifacts": (
                PredecessorArtifactRequirementV1(
                    path="handoffs/task-4.json",
                    kind="handoff",
                    produced_by_subtask_id="task-4",
                ),
            ),
        }
    )
    plan = SemanticTaskPlanV1(
        plan_id="semantic-plan",
        stage_id="substantial implementation stage",
        substantial_stage=True,
        authority_references=(authority,),
        subtasks=(*workers, auditor),
    )
    epoch_plan = _epoch_plan(plan, ((workers[0],), workers[1:], (auditor,)))
    control = ControlledReplayV1(
        control_id="synthetic-replay",
        implementation_authority_commit=commit,
        replay_start_commit=commit,
        model="gpt-5.6-sol",
        reasoning_effort="high",
        workload_authority=authority,
        global_target_writer_subtask_ids=(workers[0].subtask_id, workers[-1].subtask_id),
        plan=plan,
        epoch_plan=epoch_plan,
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
    assert len({command.task_id for command in preparation.commands}) == 3
    assert [command.mode for command in preparation.commands] == [
        "run",
        "run",
        "resume",
        "resume",
        "run",
    ]
    assert preparation.commands[1].task_id == preparation.commands[2].task_id
    assert preparation.commands[2].task_id == preparation.commands[3].task_id
    assert preparation.commands[2].argv[1] == "resume"
    assert preparation.commands[3].argv[1] == "resume"
    assert "model_auto_compact_token_limit=64000" in preparation.commands[1].argv
    assert "tool_output_token_limit=2048" in preparation.commands[1].argv
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
        json.dumps(
            _telemetry("task-1", "worker", epoch_id="epoch-1").model_dump(mode="json")
        ),
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
        epoch_id="epoch-1",
        epoch_turn_index=1,
        subtask_id="task-1",
        task_id="plan.task-1",
        mode="run",
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
                "thread_id": "thread-task-1",
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
        epoch_run_command=command,
        workspace=workspace,
        expected_model="gpt-5.6-sol",
    )
    assert events.name == "000001.events.jsonl"
    assert final.name == "000001.final-message.md"
    (turns / "000002.events.jsonl").write_text(
        '{"type":"turn.completed","usage":{"input_tokens":1}}\n',
        encoding="utf-8",
    )
    (turns / "000002.final-message.md").write_text("{}\n", encoding="utf-8")
    (turns / "000002.prompt.sha256").write_text("1" * 64 + "\n", encoding="ascii")
    state = json.loads((task_root / "task.json").read_text(encoding="utf-8"))
    state["turn_count"] = 2
    (task_root / "task.json").write_text(json.dumps(state), encoding="utf-8")
    _verified_completed_task(
        task_root=task_root,
        command=command,
        epoch_run_command=command,
        workspace=workspace,
        expected_model="gpt-5.6-sol",
        allow_later_turns=True,
    )
    with pytest.raises(ValueError, match="expected completed turn"):
        _verified_completed_task(
            task_root=task_root,
            command=command,
            epoch_run_command=command,
            workspace=workspace,
            expected_model="gpt-5.6-sol",
        )
    (turns / "000001.prompt.sha256").write_text("0" * 64 + "\n", encoding="ascii")
    with pytest.raises(ValueError, match="prompt hash"):
        _verified_completed_task(
            task_root=task_root,
            command=command,
            epoch_run_command=command,
            workspace=workspace,
            expected_model="gpt-5.6-sol",
            allow_later_turns=True,
        )


def test_crash_artifacts_beyond_task_state_forbid_duplicate_turn_launch(
    tmp_path: Path,
) -> None:
    task_root = tmp_path / "ledger" / "epoch-task"
    turns = task_root / "turns"
    turns.mkdir(parents=True)
    (turns / "000002.events.jsonl").write_text(
        '{"type":"turn.completed","usage":{"input_tokens":1}}\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="refusing duplicate launch"):
        _refuse_unqualified_turn_relaunch(task_root, 2)

    _refuse_unqualified_turn_relaunch(task_root, 3)


@pytest.mark.parametrize("handoff_already_exists", [False, True])
def test_replay_recovers_completed_0147_task_without_relaunch_and_advances(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    handoff_already_exists: bool,
) -> None:
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
        plan_id="recovery-plan",
        stage_id="completed runtime recovery",
        substantial_stage=True,
        authority_references=(authority,),
        subtasks=(worker, auditor),
    )
    epoch_plan = _epoch_plan(plan)
    control = ControlledReplayV1(
        control_id="synthetic-recovery",
        implementation_authority_commit=commit,
        replay_start_commit=commit,
        model="gpt-5.6-sol",
        reasoning_effort="high",
        workload_authority=authority,
        global_target_writer_subtask_ids=(worker.subtask_id,),
        plan=plan,
        epoch_plan=epoch_plan,
    )
    control_path = tmp_path / "control.json"
    control_path.write_text(json.dumps(control.model_dump(mode="json")), encoding="utf-8")
    artifact_root = tmp_path / "artifacts"
    global_target = tmp_path / "global-target"
    ledger_root = tmp_path / "task-ledgers"
    codex_task = tmp_path / "fake-codex-task"
    monkeypatch.setattr(semantic_replay, "DEFAULT_LEDGER_ROOT", ledger_root)
    preparation = prepare_replay(
        control_path=control_path,
        authority_root=workspace,
        workspace=workspace,
        artifact_root=artifact_root,
        global_target=global_target,
        codex_task=codex_task,
    )
    worker_command = preparation.commands[0]
    initial_identity = repository_identity(workspace, commit)
    prompt = render_subtask_prompt(
        worker,
        epoch=epoch_plan.epochs[0],
        epoch_turn_index=1,
        predecessor_artifacts=(),
        authority_root=workspace,
        expected_repository=initial_identity,
        global_target=global_target,
    )
    prompt_path = Path(worker_command.prompt_path)
    prompt_path.parent.mkdir(parents=True)
    prompt_path.write_text(prompt, encoding="utf-8")
    launch_path = Path(worker_command.launch_path)
    launch_path.parent.mkdir(parents=True)
    launch_path.write_text(
        json.dumps(
            fresh_session_launch(worker, epoch_id=epoch_plan.epochs[0].epoch_id).model_dump(
                mode="json"
            ),
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    final_draft = {
        "schema_version": 1,
        "completed_objective": "Implemented the bounded component.",
        "changed_paths": ["src/component.py"],
        "changed_interfaces": ["component.run"],
        "valid_evidence_receipts": [],
        "decisions_and_invariants": ["Exactly-once state remains authoritative."],
        "unresolved_findings": [],
        "remaining_work": ["Audit the candidate independently."],
        "next_subtask_requirements": ["Inspect the candidate diff and this handoff."],
        "do_not_rediscover_or_retest": [
            "Do not rerun the unchanged deterministic unit test."
        ],
        "oversize_justification": None,
    }
    task_root = ledger_root / worker_command.task_id
    turns = task_root / "turns"
    turns.mkdir(parents=True)
    event_log = turns / "000001.events.jsonl"
    event_log.write_text(
        json.dumps({"type": "thread.started", "thread_id": "thread-task-1"})
        + "\n"
        + json.dumps(
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 100,
                    "cached_input_tokens": 80,
                    "cache_write_input_tokens": 0,
                    "output_tokens": 30,
                    "reasoning_output_tokens": 11,
                    "future_nonnegative_counter": 4,
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (turns / "000001.final-message.md").write_text(
        json.dumps(final_draft), encoding="utf-8"
    )
    (turns / "000001.prompt.sha256").write_text(
        hashlib.sha256(prompt_path.read_bytes()).hexdigest() + "\n", encoding="ascii"
    )
    (task_root / "task.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "task_id": worker_command.task_id,
                "thread_id": "thread-task-1",
                "working_directory": str(workspace.resolve()),
                "model": "gpt-5.6-sol",
                "codex_cli_version": "codex-cli 0.147.0",
                "initial_options": list(worker_command.argv[5:]),
                "turn_count": 1,
                "usage_complete": True,
            }
        ),
        encoding="utf-8",
    )
    authoritative_receipt = task_root / "TaskUsageReceipt.json"
    authoritative_receipt.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "receipt_type": "TaskUsageReceipt",
                "task_id": worker_command.task_id,
                "thread_id": "thread-task-1",
                "model": "gpt-5.6-sol",
                "codex_cli_version": "codex-cli 0.147.0",
                "input_tokens": 100,
                "cached_input_tokens": 80,
                "output_tokens": 30,
                "reasoning_output_tokens": 11,
                "combined_tokens": 130,
                "turn_count": 1,
                "complete": True,
                "incomplete_reasons": [],
                "source_jsonl": [
                    {
                        "path": str(event_log.resolve()),
                        "sha256": hashlib.sha256(event_log.read_bytes()).hexdigest(),
                        "completed_turn_count": 1,
                    }
                ],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    receipt_before = authoritative_receipt.read_bytes()

    if handoff_already_exists:
        write_agent_handoff(
            _handoff(
                handoff_id="task-1-handoff",
                repository_identity=initial_identity.model_dump(mode="json"),
                authority_references=[authority.model_dump(mode="json")],
            ),
            Path(worker_command.handoff_path),
        )

    assert not (artifact_root / "usage" / "task-1.json").exists()
    assert not (artifact_root / "context" / "task-1.json").exists()
    assert not (artifact_root / "telemetry" / "task-1.json").exists()

    real_run = subprocess.run
    launched_task_ids: list[str] = []

    class NextSubtaskEligible(RuntimeError):
        pass

    def intercept_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        argv = args[0]
        if isinstance(argv, tuple) and argv and argv[0] == str(codex_task.resolve()):
            launched_task_ids.append(argv[2])
            raise NextSubtaskEligible
        return real_run(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(semantic_replay.subprocess, "run", intercept_run)
    with pytest.raises(NextSubtaskEligible):
        execute_replay(
            control_path=control_path,
            authority_root=workspace,
            workspace=workspace,
            artifact_root=artifact_root,
            global_target=global_target,
            codex_task=codex_task,
        )

    assert launched_task_ids == [preparation.commands[1].task_id]
    assert json.loads((task_root / "task.json").read_text(encoding="utf-8"))["turn_count"] == 1
    assert authoritative_receipt.read_bytes() == receipt_before
    recovered_usage = load_verified_receipt(
        artifact_root / "usage" / "task-1.json", event_log=event_log
    )
    assert recovered_usage.complete
    assert recovered_usage.combined_tokens == 130
    recovered_telemetry = SemanticSubtaskTelemetryV1.model_validate_json(
        (artifact_root / "telemetry" / "task-1.json").read_bytes()
    )
    assert recovered_telemetry.combined_tokens == 130
    assert _completed_subtask_prefix(plan, artifact_root) == ("task-1",)
    assert Path(preparation.commands[1].prompt_path).is_file()
    assert Path(preparation.commands[1].launch_path).is_file()
