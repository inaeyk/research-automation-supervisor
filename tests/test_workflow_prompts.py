from __future__ import annotations

import json
from pathlib import Path

from research_automation_supervisor.git_evidence import record_git_baseline
from research_automation_supervisor.workflow_models import load_substage_specification
from research_automation_supervisor.workflow_prompts import (
    APPENDIX_HEADER,
    AUDITOR_OUTPUT_SCHEMA,
    WORKER_OUTPUT_SCHEMA,
    build_initial_worker_prompt,
    write_output_schemas,
)
from tests.workflow_helpers import create_workflow_tree


def test_initial_prompt_preserves_exact_human_prefix_and_is_stable(tmp_path: Path) -> None:
    path, _, _ = create_workflow_tree(tmp_path)
    prepared = load_substage_specification(path)
    baseline = record_git_baseline(prepared.workspace)

    first = build_initial_worker_prompt(prepared, baseline)
    second = build_initial_worker_prompt(prepared, baseline)

    assert first.content.startswith(prepared.worker_initial_prompt.content + APPENDIX_HEADER)
    assert prepared.contract.content in first.content
    assert first.content == second.content
    assert first.rendered_sha256 == second.rendered_sha256
    assert first.byte_count == len(first.content)
    assert "rendered_prompt_sha256" in first.manifest()
    assert prepared.worker_initial_prompt.content.decode("utf-8") not in str(first.manifest())
    assert prepared.contract.content.decode("utf-8") not in str(first.manifest())


def test_prompt_text_is_literal_not_a_template_language(tmp_path: Path) -> None:
    path, _, _ = create_workflow_tree(tmp_path)
    prepared = load_substage_specification(path)
    source = b"{{ 7 * 7 }} ${HOME} {% include 'other' %}\n"
    prepared.worker_initial_prompt.path.write_bytes(source)
    # Reload with cleanliness disabled because the test deliberately changes a protected prompt.
    prepared = load_substage_specification(path, require_clean=False)

    rendered = build_initial_worker_prompt(prepared, record_git_baseline(prepared.workspace))

    assert rendered.content.startswith(source)
    assert b"49" not in rendered.content[: len(source)]
    assert b"${HOME}" in rendered.content[: len(source)]


def test_worker_and_auditor_output_literal_types_are_exact(tmp_path: Path) -> None:
    worker_properties = WORKER_OUTPUT_SCHEMA["properties"]
    auditor_properties = AUDITOR_OUTPUT_SCHEMA["properties"]
    assert isinstance(worker_properties, dict)
    assert isinstance(auditor_properties, dict)

    assert worker_properties["schema_version"] == {
        "type": "integer",
        "const": 1,
    }
    assert worker_properties["status"] == {
        "type": "string",
        "enum": ["completed", "blocked", "needs_human"],
    }
    assert auditor_properties["schema_version"] == {
        "type": "integer",
        "const": 1,
    }
    assert auditor_properties["verdict"] == {
        "type": "string",
        "enum": ["pass", "fail_repairable", "escalate"],
    }
    findings = auditor_properties["findings"]
    assert isinstance(findings, dict)
    finding_items = findings["items"]
    assert isinstance(finding_items, dict)
    finding_properties = finding_items["properties"]
    assert isinstance(finding_properties, dict)
    assert finding_properties["severity"] == {
        "type": "string",
        "enum": ["critical", "high", "medium", "low"],
    }

    worker_path, auditor_path = write_output_schemas(tmp_path)
    assert worker_path.read_text(encoding="ascii").endswith("\n")
    assert auditor_path.read_text(encoding="ascii").endswith("\n")
    assert json.loads(
        worker_path.read_text(encoding="ascii")
    ) == WORKER_OUTPUT_SCHEMA
    assert json.loads(
        auditor_path.read_text(encoding="ascii")
    ) == AUDITOR_OUTPUT_SCHEMA
