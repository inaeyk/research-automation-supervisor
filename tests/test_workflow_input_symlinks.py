from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from research_automation_supervisor.cli import app
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

runner = CliRunner()


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

    with pytest.raises(WorkflowInputError):
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


@pytest.mark.parametrize(
    "field,filename",
    [
        ("contract_path", "contract.md"),
        ("worker_initial_prompt_path", "worker-initial.md"),
        ("worker_repair_prompt_path", "worker-repair.md"),
        ("auditor_prompt_path", "auditor.md"),
    ],
)
@pytest.mark.parametrize("target_location", ["inside", "outside"])
def test_every_frozen_human_input_rejects_parent_component_symlinks(
    tmp_path: Path,
    field: str,
    filename: str,
    target_location: str,
) -> None:
    spec, project, _ = create_workflow_tree(tmp_path)
    if target_location == "inside":
        target = project / "control"
    else:
        target = tmp_path / "external-human-parent"
        target.mkdir()
        (target / filename).write_text("External human input.\n", encoding="utf-8")
    link = project / "control" / "human-parent-link"
    link.symlink_to(target, target_is_directory=True)
    git(project, "add", "control/human-parent-link")
    git(project, "commit", "-q", "-m", "add human parent link")
    data = yaml.safe_load(spec.read_text(encoding="utf-8"))
    data[field] = f"../project/control/human-parent-link/{filename}"
    spec.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    with pytest.raises(WorkflowInputError):
        run_substage(spec, runs_dir=tmp_path / "runs")

    assert not (tmp_path / "fake-counter").exists()


@pytest.mark.parametrize(
    "link_kind",
    ["inside", "outside", "broken", "multi_level", "non_directory"],
)
def test_specification_rejects_every_parent_link_shape(
    tmp_path: Path,
    link_kind: str,
) -> None:
    spec, _, _ = create_workflow_tree(tmp_path)
    alias = tmp_path / "specification-parent"
    if link_kind == "inside":
        alias.symlink_to(spec.parent, target_is_directory=True)
    elif link_kind == "outside":
        external = tmp_path / "outside-specification"
        external.mkdir()
        (external / spec.name).write_bytes(spec.read_bytes())
        alias.symlink_to(external, target_is_directory=True)
    elif link_kind == "broken":
        alias.symlink_to(tmp_path / "missing-specification-parent", target_is_directory=True)
    elif link_kind == "multi_level":
        second = tmp_path / "second-specification-parent"
        second.symlink_to(spec.parent, target_is_directory=True)
        alias.symlink_to(second, target_is_directory=True)
    else:
        alias.write_text("not a directory\n", encoding="utf-8")

    with pytest.raises(WorkflowInputError):
        run_substage(
            alias / spec.name,
            runs_dir=tmp_path / "runs",
        )

    assert not (tmp_path / "fake-counter").exists()


def test_symlinked_relative_base_directory_is_rejected(tmp_path: Path) -> None:
    spec, _, _ = create_workflow_tree(tmp_path)
    base = tmp_path / "linked-base"
    base.symlink_to(spec.parent, target_is_directory=True)

    with pytest.raises(WorkflowInputError):
        validate_substage(base / spec.name)

    assert not (tmp_path / "fake-counter").exists()


def test_parent_symlink_cli_error_is_sanitized_exit_two(tmp_path: Path) -> None:
    spec, _, _ = create_workflow_tree(tmp_path)
    sensitive_component = "sensitive-parent-target"
    target = tmp_path / sensitive_component
    target.mkdir()
    (target / spec.name).write_bytes(spec.read_bytes())
    alias = tmp_path / "supplied-parent"
    alias.symlink_to(target, target_is_directory=True)

    result = runner.invoke(
        app,
        ["validate-substage", str(alias / spec.name), "--json"],
    )

    assert result.exit_code == 2
    assert json.loads(result.stdout)["error_kind"] == "input"
    assert sensitive_component not in result.stdout
    assert not (tmp_path / "fake-counter").exists()


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


@pytest.mark.parametrize("target_location", ["inside", "outside", "broken", "multi_level"])
def test_continuation_parent_symlink_is_rejected_without_new_launch(
    tmp_path: Path,
    target_location: str,
) -> None:
    responses = [codex_response("worker", "worker", worker_result("blocked"))]
    spec, project, fake = create_workflow_tree(tmp_path, responses=responses)
    paused = run_substage(spec, runs_dir=tmp_path / "runs", services=services(fake))
    link = tmp_path / "continuation-parent"
    if target_location == "inside":
        target = project / "control"
        filename = "worker-initial.md"
    elif target_location == "outside":
        target = tmp_path / "outside-continuation"
        target.mkdir()
        filename = "instruction.md"
        (target / filename).write_text("Continue exactly.\n", encoding="utf-8")
    elif target_location == "broken":
        target = tmp_path / "missing-continuation-parent"
        filename = "instruction.md"
    else:
        target = tmp_path / "real-continuation"
        target.mkdir()
        filename = "instruction.md"
        (target / filename).write_text("Continue exactly.\n", encoding="utf-8")
        second = tmp_path / "second-continuation-parent"
        second.symlink_to(target, target_is_directory=True)
        target = second
    link.symlink_to(target, target_is_directory=True)
    before = (tmp_path / "fake-counter").read_text(encoding="ascii")

    with pytest.raises(WorkflowInputError):
        continue_substage(
            Path(paused.artifact_directory),
            link / filename,
            services=services(fake),
        )

    assert (tmp_path / "fake-counter").read_text(encoding="ascii") == before


def test_protected_in_workspace_continuation_parent_link_is_rejected_before_launch(
    tmp_path: Path,
) -> None:
    responses = [codex_response("worker", "worker", worker_result("blocked"))]
    spec, project, fake = create_workflow_tree(tmp_path, responses=responses)
    external = tmp_path / "external-continuation-parent"
    external.mkdir()
    (external / "instruction.md").write_text("Continue exactly.\n", encoding="utf-8")
    link = project / "control" / "continuation-parent"
    link.symlink_to(external, target_is_directory=True)
    git(project, "add", "control/continuation-parent")
    git(project, "commit", "-q", "-m", "add continuation parent link")
    paused = run_substage(spec, runs_dir=tmp_path / "runs", services=services(fake))
    before = (tmp_path / "fake-counter").read_text(encoding="ascii")

    with pytest.raises(WorkflowInputError):
        continue_substage(
            Path(paused.artifact_directory),
            link / "instruction.md",
            services=services(fake),
        )

    assert (tmp_path / "fake-counter").read_text(encoding="ascii") == before


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
