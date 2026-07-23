from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from research_automation_supervisor.errors import WorkflowInputError
from research_automation_supervisor.workflow_models import (
    AuditorModelResult,
    SubstageSpecification,
    WorkerModelResult,
    load_substage_specification,
    normalize_path_pattern,
    normalize_relative_path,
    parse_auditor_result,
    parse_worker_result,
)
from tests.workflow_helpers import create_workflow_tree, git


def test_valid_specification_resolves_all_files_and_fixed_argv(tmp_path: Path) -> None:
    path, project, _ = create_workflow_tree(tmp_path)

    prepared = load_substage_specification(path)

    assert prepared.workspace == project.resolve()
    assert prepared.specification.max_repair_rounds == 2
    assert prepared.acceptance_tests[0].specification.argv[1] == "tools/acceptance.py"
    assert prepared.acceptance_tests[0].cwd == project.resolve()
    assert len(prepared.specification_sha256) == 64


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("worker_timeout_seconds", 29),
        ("worker_timeout_seconds", 14_401),
        ("auditor_timeout_seconds", 29),
        ("max_repair_rounds", -1),
        ("max_repair_rounds", 11),
    ],
)
def test_specification_boundaries_reject_out_of_range_values(
    tmp_path: Path, field: str, value: int
) -> None:
    path, _, _ = create_workflow_tree(tmp_path)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data[field] = value
    path.write_text(yaml.safe_dump(data), encoding="utf-8")

    with pytest.raises(WorkflowInputError):
        load_substage_specification(path)


def test_unknown_duplicate_and_nested_invalid_fields_are_rejected(tmp_path: Path) -> None:
    path, _, _ = create_workflow_tree(tmp_path)
    source = path.read_text(encoding="utf-8")
    path.write_text(source + "unknown: true\n", encoding="utf-8")
    with pytest.raises(WorkflowInputError, match="Extra"):
        load_substage_specification(path)

    path.write_text(source + "title: duplicate\n", encoding="utf-8")
    with pytest.raises(WorkflowInputError, match="duplicate"):
        load_substage_specification(path)

    data = yaml.safe_load(source)
    data["acceptance_tests"][0]["shell"] = True
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(WorkflowInputError, match="Extra"):
        load_substage_specification(path)


def test_dirty_workspace_including_untracked_is_rejected(tmp_path: Path) -> None:
    path, project, _ = create_workflow_tree(tmp_path)
    (project / "untracked.txt").write_text("dirty", encoding="utf-8")

    with pytest.raises(WorkflowInputError, match="clean"):
        load_substage_specification(path)


def test_prompt_inside_workspace_must_be_protected(tmp_path: Path) -> None:
    path, _, _ = create_workflow_tree(tmp_path)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["protected_paths"] = ["tools/**", ".fake-codex.json"]
    path.write_text(yaml.safe_dump(data), encoding="utf-8")

    with pytest.raises(WorkflowInputError, match="protected_paths"):
        load_substage_specification(path)


def test_invalid_utf8_oversized_prompt_and_outside_test_cwd_are_rejected(
    tmp_path: Path,
) -> None:
    path, project, _ = create_workflow_tree(tmp_path)
    prompt = project / "control/worker-initial.md"
    prompt.write_bytes(b"\xff")
    git(project, "add", ".")
    git(project, "commit", "-q", "-m", "invalid prompt")
    with pytest.raises(WorkflowInputError, match="UTF-8"):
        load_substage_specification(path)

    prompt.write_bytes(b"x" * (2 * 1024 * 1024 + 1))
    git(project, "add", ".")
    git(project, "commit", "-q", "-m", "large prompt")
    with pytest.raises(WorkflowInputError, match="limit"):
        load_substage_specification(path)

    prompt.write_text("restored\n", encoding="utf-8")
    git(project, "add", ".")
    git(project, "commit", "-q", "-m", "restored prompt")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["acceptance_tests"][0]["cwd"] = ".."
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(WorkflowInputError, match="inside the workspace"):
        load_substage_specification(path)


def test_structural_redaction_collision_is_rejected(tmp_path: Path) -> None:
    path, _, _ = create_workflow_tree(tmp_path)

    with pytest.raises(WorkflowInputError, match="redaction collision"):
        load_substage_specification(path, sensitive_values=("minimal-substage",))


@pytest.mark.parametrize("value", ["/absolute/**", "../escape/**", "a/../../b", " "])
def test_path_patterns_reject_absolute_traversal_and_empty(value: str) -> None:
    with pytest.raises(ValueError):
        normalize_path_pattern(value)


def test_paths_normalize_to_posix_and_reject_workspace_root() -> None:
    assert normalize_relative_path(r"src\module.py") == "src/module.py"
    assert normalize_path_pattern(r"src\**") == "src/**"
    with pytest.raises(ValueError):
        normalize_relative_path(".")


def test_structured_worker_and_auditor_results_are_strict() -> None:
    worker = parse_worker_result(
        json.dumps(
            {
                "schema_version": 1,
                "status": "completed",
                "summary": "done",
                "changed_files": ["src/a.py"],
                "assumptions": [],
                "questions": [],
            }
        )
    )
    assert isinstance(worker, WorkerModelResult)
    with pytest.raises(WorkflowInputError):
        parse_worker_result('{"schema_version":1,"status":"completed"}')

    audit = parse_auditor_result(
        json.dumps(
            {
                "schema_version": 1,
                "verdict": "pass",
                "summary": "pass",
                "scope_compliant": True,
                "contract_satisfied": True,
                "findings": [],
                "human_questions": [],
            }
        )
    )
    assert isinstance(audit, AuditorModelResult)
    invalid = audit.model_dump(mode="json")
    invalid["findings"] = [
        {
            "id": "x",
            "severity": "low",
            "category": "style",
            "file": None,
            "line": None,
            "evidence": "e",
            "required_fix": "f",
        }
    ]
    with pytest.raises(ValidationError):
        AuditorModelResult.model_validate(invalid)


def test_strict_spec_model_rejects_shell_string_argv() -> None:
    with pytest.raises(ValidationError):
        SubstageSpecification.model_validate(
            {
                "schema_version": 1,
                "substage_id": "x",
                "title": "x",
                "workspace": "x",
                "contract_path": "x",
                "worker_initial_prompt_path": "x",
                "worker_repair_prompt_path": "x",
                "auditor_prompt_path": "x",
                "worker_model": "gpt-5.6-sol",
                "worker_reasoning_effort": "high",
                "worker_timeout_seconds": 30,
                "auditor_model": "gpt-5.6-sol",
                "auditor_reasoning_effort": "high",
                "auditor_timeout_seconds": 30,
                "acceptance_tests": [
                    {
                        "id": "x",
                        "argv": "pytest -q",
                        "cwd": "x",
                        "timeout_seconds": 1,
                        "max_stdout_bytes": 1,
                        "max_stderr_bytes": 1,
                    }
                ],
                "allowed_paths": ["src/**"],
                "protected_paths": ["control/**"],
                "max_repair_rounds": 0,
                "checkpoint_after": False,
            }
        )
