from __future__ import annotations

from pathlib import Path

from research_automation_supervisor.git_evidence import record_git_baseline
from research_automation_supervisor.workflow_models import load_substage_specification
from research_automation_supervisor.workflow_prompts import (
    APPENDIX_HEADER,
    build_initial_worker_prompt,
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
