from __future__ import annotations

from pathlib import Path

from research_automation_supervisor.shadow_prompts import (
    SHADOW_INSTRUCTION,
    build_blind_supervisor_prompt,
)
from research_automation_supervisor.shadow_sources import (
    load_shadow_specification,
)
from tests.shadow_helpers import create_shadow_tree


def test_blind_prompt_contains_frozen_allowed_inputs_but_no_authoritative_prompt(
    tmp_path: Path,
) -> None:
    spec, _, _, _ = create_shadow_tree(tmp_path)
    prepared = load_shadow_specification(spec)
    decision = prepared.source.decisions[0]

    rendered = build_blind_supervisor_prompt(prepared, decision)

    assert rendered.content.startswith(prepared.policy.content)
    assert prepared.contexts[0].content in rendered.content
    assert prepared.source.prepared.contract.content in rendered.content
    assert SHADOW_INSTRUCTION in rendered.content
    assert decision.authoritative_source is not None
    assert decision.authoritative_source.content not in rendered.content
    for reconstructed in prepared.source.decisions:
        if reconstructed.authoritative_source is not None:
            assert (
                str(reconstructed.authoritative_source.path).encode()
                not in rendered.content
            )
            assert (
                reconstructed.authoritative_source.sha256.encode()
                not in rendered.content
            )
        if reconstructed.authoritative_rendered is not None:
            assert (
                reconstructed.authoritative_rendered.rendered_sha256.encode()
                not in rendered.content
            )
    assert rendered.manifest.authoritative_sentinel_absent
    assert rendered.manifest.shadow_only
    assert rendered.manifest.automatic_send_disabled


def test_initial_blind_evidence_excludes_every_future_outcome_domain(
    tmp_path: Path,
) -> None:
    spec, _, _, _ = create_shadow_tree(tmp_path)
    prepared = load_shadow_specification(spec)
    initial = build_blind_supervisor_prompt(
        prepared, prepared.source.decisions[0]
    ).content
    auditor = build_blind_supervisor_prompt(
        prepared, prepared.source.decisions[1]
    ).content

    for future in (
        b"worker completed",
        b"auditor pass",
        b"contract_satisfied",
        b"latest_audit_result_path",
        b"comparison_available",
        b"human_review",
    ):
        assert future not in initial
    assert b"worker completed" in auditor


def test_prior_comparison_and_review_sentinels_are_not_prompt_inputs(
    tmp_path: Path,
) -> None:
    spec, _, _, _ = create_shadow_tree(tmp_path)
    prepared = load_shadow_specification(spec)
    comparison_sentinel = b"PRIOR-COMPARISON-SENTINEL"
    review_sentinel = b"PRIOR-REVIEW-SENTINEL"

    later = build_blind_supervisor_prompt(
        prepared, prepared.source.decisions[-1]
    )

    assert comparison_sentinel not in later.content
    assert review_sentinel not in later.content
