from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from research_automation_supervisor.errors import ShadowInputError
from research_automation_supervisor.shadow_sources import (
    load_shadow_specification,
)
from research_automation_supervisor.workflow_engine import (
    WorkflowServices,
    _WorkflowLock,
    continue_substage,
    run_substage,
)
from tests.shadow_helpers import (
    create_shadow_specification,
    create_shadow_tree,
)
from tests.workflow_helpers import (
    auditor_result,
    codex_response,
    create_workflow_tree,
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


def test_source_artifact_mutation_fails_trusted_validation(
    tmp_path: Path,
) -> None:
    spec, source_run, _, _ = create_shadow_tree(tmp_path)
    action = source_run / "actions" / "worker-r000.json"
    value = json.loads(action.read_text(encoding="utf-8"))
    value["repair_round"] = 1
    action.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(ShadowInputError, match="integrity"):
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
