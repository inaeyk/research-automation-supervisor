from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from research_automation_supervisor.errors import WorkflowInputError
from research_automation_supervisor.workflow_engine import (
    WorkflowServices,
    continue_substage,
    run_substage,
    validate_substage,
)
from tests.test_workflow_engine import services
from tests.workflow_helpers import (
    codex_response,
    create_workflow_tree,
    git,
    worker_result,
)


def _replace_specification_field_with_symlink(
    spec: Path,
    project: Path,
    field: str,
    target: Path,
) -> None:
    data = yaml.safe_load(spec.read_text(encoding="utf-8"))
    link = project / "src" / f"{field}.md"
    link.symlink_to(target)
    data[field] = str(link)
    spec.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def test_symlinked_specification_is_rejected_before_resolution(tmp_path: Path) -> None:
    spec, _, _ = create_workflow_tree(tmp_path)
    link = tmp_path / "substage-link.yaml"
    link.symlink_to(spec)

    with pytest.raises(WorkflowInputError, match="must not be a symbolic link"):
        validate_substage(link)


@pytest.mark.parametrize(
    "field",
    [
        "contract_path",
        "worker_initial_prompt_path",
        "worker_repair_prompt_path",
        "auditor_prompt_path",
    ],
)
def test_every_frozen_human_input_rejects_leaf_symlinks_and_protected_bypass(
    tmp_path: Path,
    field: str,
) -> None:
    spec, project, _ = create_workflow_tree(tmp_path)
    external = tmp_path / "external-human.md"
    external.write_text("External human input.\n", encoding="utf-8")
    _replace_specification_field_with_symlink(spec, project, field, external)
    git(project, "add", "src")
    git(project, "commit", "-q", "-m", "add symlink fixture")

    with pytest.raises(WorkflowInputError, match="must not be a symbolic link"):
        validate_substage(spec)


def test_in_workspace_parent_symlink_cannot_escape_or_bypass_protection(
    tmp_path: Path,
) -> None:
    spec, project, _ = create_workflow_tree(tmp_path)
    external = tmp_path / "external"
    external.mkdir()
    (external / "prompt.md").write_text("External prompt.\n", encoding="utf-8")
    (project / "src" / "linked-parent").symlink_to(external, target_is_directory=True)
    git(project, "add", "src")
    git(project, "commit", "-q", "-m", "add parent symlink fixture")
    data = yaml.safe_load(spec.read_text(encoding="utf-8"))
    data["worker_initial_prompt_path"] = str(
        project / "src" / "linked-parent" / "prompt.md"
    )
    spec.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    with pytest.raises(WorkflowInputError):
        validate_substage(spec)


def test_symlinked_human_continuation_is_rejected_without_launch(
    tmp_path: Path,
) -> None:
    responses = [codex_response("worker", "worker", worker_result("blocked"))]
    spec, _, fake = create_workflow_tree(tmp_path, responses=responses)
    paused = run_substage(
        spec,
        runs_dir=tmp_path / "runs",
        services=services(fake),
    )
    target = tmp_path / "instruction.md"
    target.write_text("Continue exactly.\n", encoding="utf-8")
    link = tmp_path / "instruction-link.md"
    link.symlink_to(target)
    counter_before = (tmp_path / "fake-counter").read_text(encoding="ascii")

    with pytest.raises(WorkflowInputError, match="must not be a symbolic link"):
        continue_substage(
            Path(paused.artifact_directory),
            link,
            services=WorkflowServices(codex_executable=str(fake)),
        )

    assert (tmp_path / "fake-counter").read_text(encoding="ascii") == counter_before


def test_in_workspace_continuation_must_match_protected_supplied_locator(
    tmp_path: Path,
) -> None:
    responses = [codex_response("worker", "worker", worker_result("blocked"))]
    spec, project, fake = create_workflow_tree(tmp_path, responses=responses)
    paused = run_substage(spec, runs_dir=tmp_path / "runs", services=services(fake))
    instruction = project / "src" / "instruction.md"
    instruction.write_text("Continue exactly.\n", encoding="utf-8")

    with pytest.raises(WorkflowInputError, match="must match protected_paths"):
        continue_substage(
            Path(paused.artifact_directory),
            instruction,
            services=services(fake),
        )


def test_input_symlink_errors_do_not_reveal_external_locator_content(
    tmp_path: Path,
) -> None:
    spec, project, _ = create_workflow_tree(tmp_path)
    secret_fragment = "sensitive-external-locator"
    external = tmp_path / secret_fragment
    external.write_text("External prompt.\n", encoding="utf-8")
    _replace_specification_field_with_symlink(
        spec,
        project,
        "worker_initial_prompt_path",
        external,
    )
    git(project, "add", "src")
    git(project, "commit", "-q", "-m", "add secret symlink fixture")

    with pytest.raises(WorkflowInputError) as captured:
        validate_substage(spec)

    assert secret_fragment not in str(captured.value)
    assert json.dumps(str(external)) not in str(captured.value)
