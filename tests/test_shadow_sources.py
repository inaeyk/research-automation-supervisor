from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from research_automation_supervisor.errors import (
    ShadowInputError,
    ShadowIntegrityError,
    WorkflowStateError,
)
from research_automation_supervisor.shadow_prompts import (
    build_blind_supervisor_prompt,
)
from research_automation_supervisor.shadow_sources import (
    decision_points_artifact,
    load_shadow_specification,
)
from research_automation_supervisor.workflow_engine import (
    WorkflowServices,
    _WorkflowLock,
    abort_substage,
    continue_substage,
    run_substage,
    substage_status,
)
from tests.shadow_helpers import (
    create_human_continuation_shadow_tree,
    create_shadow_specification,
    create_shadow_tree,
)
from tests.workflow_helpers import (
    auditor_result,
    codex_response,
    create_workflow_tree,
    git,
    worker_result,
)


def test_verified_source_reconstructs_order_ids_and_authoritative_hashes(
    tmp_path: Path,
) -> None:
    spec, _, _, _ = create_shadow_tree(tmp_path)

    prepared = load_shadow_specification(spec)

    assert [
        decision.point.proposal_kind
        for decision in prepared.source.decisions
    ] == ["worker_initial", "auditor"]
    assert [
        decision.point.decision_id
        for decision in prepared.source.decisions
    ] == [
        "worker_initial-r000-a001",
        "auditor-r000-a002",
    ]
    for decision in prepared.source.decisions:
        assert decision.point.comparison_available
        assert decision.authoritative_source is not None
        assert decision.authoritative_rendered is not None


def test_shadow_spec_rejects_unknown_duplicate_and_symlinked_context(
    tmp_path: Path,
) -> None:
    spec, _, _, _ = create_shadow_tree(tmp_path)
    original = spec.read_text(encoding="utf-8")
    spec.write_text(original + "unknown: true\n", encoding="utf-8")
    with pytest.raises(ShadowInputError, match="Extra"):
        load_shadow_specification(spec)

    spec.write_text(
        original + "title: duplicate\n",
        encoding="utf-8",
    )
    with pytest.raises(ShadowInputError, match="duplicate"):
        load_shadow_specification(spec)

    spec.write_text(original, encoding="utf-8")
    data = yaml.safe_load(original)
    context = spec.parent / "project-context.md"
    linked = spec.parent / "linked-context.md"
    linked.symlink_to(context)
    data["project_context_paths"] = ["linked-context.md"]
    spec.write_text(
        yaml.safe_dump(data, sort_keys=False), encoding="utf-8"
    )
    with pytest.raises(ShadowInputError, match="symbolic-link"):
        load_shadow_specification(spec)

    data["project_context_paths"] = [
        {"path": "project-context.md", "unknown": True}
    ]
    spec.write_text(
        yaml.safe_dump(data, sort_keys=False),
        encoding="utf-8",
    )
    with pytest.raises(ShadowInputError):
        load_shadow_specification(spec)


def test_source_artifact_mutation_fails_trusted_validation(
    tmp_path: Path,
) -> None:
    spec, source_run, _, _ = create_shadow_tree(tmp_path)
    action = source_run / "actions" / "worker-r000.json"
    value = json.loads(action.read_text(encoding="utf-8"))
    value["repair_round"] = 1
    action.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(ShadowIntegrityError, match="integrity"):
        load_shadow_specification(spec)


@pytest.mark.parametrize(
    "relative",
    [
        "handoffs/worker-r000.json",
        "git/round-000/evidence.json",
        "tests/round-000/suite.json",
    ],
)
def test_altered_handoff_git_or_test_evidence_is_integrity_failure(
    tmp_path: Path,
    relative: str,
) -> None:
    spec, source_run, _, _ = create_shadow_tree(tmp_path)
    path = source_run / relative
    value = json.loads(path.read_text(encoding="utf-8"))
    value["unexpected"] = True
    path.write_text(
        json.dumps(value, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ShadowIntegrityError):
        load_shadow_specification(spec)


def test_actively_locked_source_run_is_rejected(tmp_path: Path) -> None:
    spec, source_run, _, fake = create_shadow_tree(tmp_path)
    services = WorkflowServices(codex_executable=str(fake))

    with (
        _WorkflowLock(source_run, services.utc_now),
        pytest.raises(ShadowInputError, match="locked"),
    ):
        load_shadow_specification(spec)


@pytest.mark.parametrize(
    ("scenario", "expected_kind"),
    [
        ("scope", "worker_scope_repair"),
        ("test", "worker_test_repair"),
        ("audit", "worker_audit_repair"),
        ("human", "worker_human_continuation"),
    ],
)
def test_reconstructs_every_repair_decision_kind_from_pre_action_evidence(
    tmp_path: Path,
    scenario: str,
    expected_kind: str,
) -> None:
    thread = f"{scenario}-worker"
    if scenario == "scope":
        responses = [
            codex_response(
                "worker",
                thread,
                worker_result(),
                write_files={"outside.txt": "outside\n"},
            ),
            codex_response(
                "worker",
                thread,
                worker_result(),
                expected_resume_thread_id=thread,
                delete_files=["outside.txt"],
            ),
            codex_response("auditor", "scope-audit", auditor_result()),
        ]
        workflow_options = {}
    elif scenario == "test":
        responses = [
            codex_response("worker", thread, worker_result()),
            codex_response(
                "worker",
                thread,
                worker_result(),
                expected_resume_thread_id=thread,
                write_files={"src/ready.txt": "ready\n"},
            ),
            codex_response("auditor", "test-audit", auditor_result()),
        ]
        workflow_options = {"test_requires_marker": True}
    elif scenario == "audit":
        responses = [
            codex_response("worker", thread, worker_result()),
            codex_response(
                "auditor",
                "audit-one",
                auditor_result("fail_repairable"),
            ),
            codex_response(
                "worker",
                thread,
                worker_result(),
                expected_resume_thread_id=thread,
            ),
            codex_response("auditor", "audit-two", auditor_result()),
        ]
        workflow_options = {}
    else:
        responses = [
            codex_response("worker", thread, worker_result("blocked")),
            codex_response(
                "worker",
                thread,
                worker_result(),
                expected_resume_thread_id=thread,
            ),
            codex_response("auditor", "human-audit", auditor_result()),
        ]
        workflow_options = {}
    stage2_spec, project, fake = create_workflow_tree(
        tmp_path / "stage2",
        responses=responses,
        **workflow_options,
    )
    source_result = run_substage(
        stage2_spec,
        runs_dir=tmp_path / "source-runs",
        services=WorkflowServices(codex_executable=str(fake)),
    )
    if scenario == "human":
        instruction = tmp_path / "human-instruction.md"
        instruction.write_text(
            "Continue under the frozen contract.\n", encoding="utf-8"
        )
        source_result = continue_substage(
            Path(source_result.artifact_directory),
            instruction,
            services=WorkflowServices(codex_executable=str(fake)),
        )
    shadow_spec = create_shadow_specification(
        tmp_path,
        Path(source_result.artifact_directory),
        project,
    )

    prepared = load_shadow_specification(shadow_spec)

    kinds = [
        decision.point.proposal_kind
        for decision in prepared.source.decisions
    ]
    assert expected_kind in kinds
    selected = next(
        decision
        for decision in prepared.source.decisions
        if decision.point.proposal_kind == expected_kind
    )
    assert selected.point.comparison_available
    assert selected.authoritative_rendered is not None
    blind = build_blind_supervisor_prompt(prepared, selected).content
    for decision in prepared.source.decisions:
        if decision.authoritative_source is not None:
            assert decision.authoritative_source.content not in blind
            assert str(decision.authoritative_source.path).encode() not in blind
            assert decision.authoritative_source.sha256.encode() not in blind
        if decision.authoritative_rendered is not None:
            assert decision.authoritative_rendered.content not in blind
            assert (
                decision.authoritative_rendered.rendered_sha256.encode()
                not in blind
            )


def test_multi_round_decision_reconstruction_has_exact_complete_order(
    tmp_path: Path,
) -> None:
    worker_thread = "multi-round-worker"

    def worker_payload(status: str, summary: str) -> str:
        value = json.loads(worker_result(status))
        value["summary"] = summary
        return json.dumps(value, sort_keys=True)

    def auditor_payload(verdict: str, summary: str) -> str:
        value = json.loads(auditor_result(verdict))
        value["summary"] = summary
        return json.dumps(value, sort_keys=True)

    future_markers = [
        "MULTI_INITIAL_RESULT",
        "MULTI_SCOPE_REPAIR_RESULT",
        "MULTI_TEST_REPAIR_RESULT",
        "MULTI_AUDIT_ONE_RESULT",
        "MULTI_AUDIT_REPAIR_BLOCKED",
        "MULTI_HUMAN_CONTINUATION_RESULT",
        "MULTI_AUDIT_FINAL_RESULT",
    ]
    responses = [
        codex_response(
            "worker",
            worker_thread,
            worker_payload("completed", future_markers[0]),
            write_files={"outside.txt": "outside\n"},
        ),
        codex_response(
            "worker",
            worker_thread,
            worker_payload("completed", future_markers[1]),
            expected_resume_thread_id=worker_thread,
            delete_files=["outside.txt"],
        ),
        codex_response(
            "worker",
            worker_thread,
            worker_payload("completed", future_markers[2]),
            expected_resume_thread_id=worker_thread,
            write_files={"src/ready.txt": "ready\n"},
        ),
        codex_response(
            "auditor",
            "multi-auditor-one",
            auditor_payload("fail_repairable", future_markers[3]),
        ),
        codex_response(
            "worker",
            worker_thread,
            worker_payload("blocked", future_markers[4]),
            expected_resume_thread_id=worker_thread,
        ),
        codex_response(
            "worker",
            worker_thread,
            worker_payload("completed", future_markers[5]),
            expected_resume_thread_id=worker_thread,
        ),
        codex_response(
            "auditor",
            "multi-auditor-final",
            auditor_payload("pass", future_markers[6]),
        ),
    ]
    stage2_spec, project, fake = create_workflow_tree(
        tmp_path / "stage2",
        responses=responses,
        max_repair_rounds=5,
        test_requires_marker=True,
    )
    services = WorkflowServices(codex_executable=str(fake))
    source_result = run_substage(
        stage2_spec,
        runs_dir=tmp_path / "source-runs",
        services=services,
    )
    assert source_result.status == "human_paused"
    instruction = tmp_path / "multi-continuation.md"
    instruction.write_text(
        "Continue the exact frozen multi-round workflow.\n",
        encoding="utf-8",
    )
    source_result = continue_substage(
        Path(source_result.artifact_directory),
        instruction,
        services=services,
    )
    assert source_result.status == "completed"
    shadow_spec = create_shadow_specification(
        tmp_path,
        Path(source_result.artifact_directory),
        project,
    )

    first = load_shadow_specification(shadow_spec)
    second = load_shadow_specification(shadow_spec)
    actual = [
        (
            decision.point.proposal_kind,
            decision.point.decision_id,
            decision.point.source_action_id,
            decision.point.repair_round,
            decision.point.ordinal,
        )
        for decision in first.source.decisions
    ]

    assert actual == [
        (
            "worker_initial",
            "worker_initial-r000-a001",
            "worker-r000",
            0,
            1,
        ),
        (
            "worker_scope_repair",
            "worker_scope_repair-r001-a002",
            "worker-r001",
            1,
            2,
        ),
        (
            "worker_test_repair",
            "worker_test_repair-r002-a003",
            "worker-r002",
            2,
            3,
        ),
        ("auditor", "auditor-r002-a004", "auditor-r002", 2, 4),
        (
            "worker_audit_repair",
            "worker_audit_repair-r003-a005",
            "worker-r003",
            3,
            5,
        ),
        (
            "worker_human_continuation",
            "worker_human_continuation-r004-a006",
            "worker-r004",
            4,
            6,
        ),
        ("auditor", "auditor-r004-a007", "auditor-r004", 4, 7),
    ]
    assert len({item[1] for item in actual}) == len(actual)
    assert json.dumps(
        {
            "normalized": first.normalized_dict(),
            "decisions": decision_points_artifact(first.source.decisions),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8") == json.dumps(
        {
            "normalized": second.normalized_dict(),
            "decisions": decision_points_artifact(second.source.decisions),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    for index, decision in enumerate(first.source.decisions):
        evidence = json.dumps(
            decision.blind_evidence,
            sort_keys=True,
            separators=(",", ":"),
        )
        for marker in future_markers[index:]:
            assert marker not in evidence


def test_missing_continuation_source_is_the_only_unavailable_comparison(
    tmp_path: Path,
) -> None:
    spec, source_run, _, _, instruction = (
        create_human_continuation_shadow_tree(tmp_path)
    )
    instruction.unlink()

    with pytest.raises(WorkflowStateError):
        substage_status(source_run)
    prepared = load_shadow_specification(spec)
    continuation = next(
        decision
        for decision in prepared.source.decisions
        if decision.point.proposal_kind
        == "worker_human_continuation"
    )

    assert continuation.point.comparison_available is False
    assert (
        continuation.point.comparison_unavailable_reason
        == "continuation_source_unavailable"
    )
    assert continuation.authoritative_source is None
    assert continuation.authoritative_rendered is None
    assert continuation.blind_evidence["current_state"] == "human_paused"


def test_altered_continuation_source_is_an_integrity_failure(
    tmp_path: Path,
) -> None:
    spec, _, _, _, instruction = create_human_continuation_shadow_tree(
        tmp_path
    )
    instruction.write_text("Altered instruction.\n", encoding="utf-8")

    with pytest.raises(ShadowIntegrityError, match="integrity"):
        load_shadow_specification(spec)


def test_parent_component_symlink_is_rejected(
    tmp_path: Path,
) -> None:
    spec, _, _, _ = create_shadow_tree(tmp_path)
    value = yaml.safe_load(spec.read_text(encoding="utf-8"))
    linked_parent = tmp_path / "linked-shadow-control"
    linked_parent.symlink_to(spec.parent, target_is_directory=True)
    value["project_context_paths"] = [
        str(linked_parent / "project-context.md")
    ]
    spec.write_text(
        yaml.safe_dump(value, sort_keys=False),
        encoding="utf-8",
    )

    with pytest.raises(ShadowInputError, match="symbolic-link"):
        load_shadow_specification(spec)


def test_source_frozen_input_and_repository_drift_are_integrity_failures(
    tmp_path: Path,
) -> None:
    spec, _, project, _ = create_shadow_tree(tmp_path / "frozen")
    (project / "control/worker-initial.md").write_text(
        "changed\n", encoding="utf-8"
    )
    with pytest.raises(ShadowIntegrityError):
        load_shadow_specification(spec)

    spec, _, project, _ = create_shadow_tree(tmp_path / "repository")
    (project / "new.txt").write_text("new\n", encoding="utf-8")
    git(project, "add", "new.txt")
    git(project, "commit", "-q", "-m", "drift")
    with pytest.raises(ShadowIntegrityError):
        load_shadow_specification(spec)


@pytest.mark.parametrize(
    "source_status",
    [
        "completed",
        "checkpoint_paused",
        "human_paused",
        "repair_limit_paused",
        "failed",
        "aborted",
    ],
)
def test_every_contract_allowed_terminal_source_status_is_accepted(
    tmp_path: Path,
    source_status: str,
) -> None:
    options: dict[str, object] = {}
    if source_status in {"completed", "checkpoint_paused"}:
        responses = [
            codex_response("worker", "worker", worker_result()),
            codex_response("auditor", "auditor", auditor_result()),
        ]
        options["checkpoint_after"] = source_status == "checkpoint_paused"
    elif source_status in {"human_paused", "aborted"}:
        responses = [
            codex_response(
                "worker", "worker", worker_result("blocked")
            )
        ]
    elif source_status == "repair_limit_paused":
        responses = [
            codex_response(
                "worker",
                "worker",
                worker_result(),
                write_files={"outside.txt": "outside\n"},
            )
        ]
        options["max_repair_rounds"] = 0
    else:
        responses = None
    stage2_spec, project, fake = create_workflow_tree(
        tmp_path / "stage2",
        responses=responses,
        **options,  # type: ignore[arg-type]
    )
    services = WorkflowServices(codex_executable=str(fake))
    if source_status == "failed":

        def fail_invariant(*args: object, **kwargs: object) -> object:
            del args, kwargs
            raise WorkflowStateError("injected local invariant")

        services = WorkflowServices(
            codex_executable=str(fake),
            codex_invoker=fail_invariant,  # type: ignore[arg-type]
        )
    result = run_substage(
        stage2_spec,
        runs_dir=tmp_path / "source-runs",
        services=services,
    )
    if source_status == "aborted":
        result = abort_substage(
            Path(result.artifact_directory),
            "stop",
            services=WorkflowServices(codex_executable=str(fake)),
        )
    assert result.status == source_status
    shadow_spec = create_shadow_specification(
        tmp_path,
        Path(result.artifact_directory),
        project,
    )

    prepared = load_shadow_specification(shadow_spec)

    assert prepared.source.state.status == source_status
