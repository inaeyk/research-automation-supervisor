"""Structured human review and informational Stage 3 readiness logic."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]
from pydantic import ValidationError

from research_automation_supervisor.codex_adapter import (
    build_subprocess_environment,
)
from research_automation_supervisor.contract import (
    _format_validation_error,
    _UniqueKeySafeLoader,
)
from research_automation_supervisor.errors import ShadowInputError
from research_automation_supervisor.redaction import would_redact_text
from research_automation_supervisor.shadow_models import (
    DeterministicAssessment,
    HumanReview,
    ProposalKind,
    ReadinessReport,
    ReadinessStatus,
    ReviewEvaluation,
    ShadowSpecification,
)
from research_automation_supervisor.shadow_sources import (
    _read_utf8,
    _resolve_exact_file,
)


def load_shadow_review(
    path: Path,
    *,
    sensitive_values: Sequence[str] = (),
) -> HumanReview:
    """Load one strict immutable human review from safe YAML."""
    resolved = _resolve_exact_file(path, "shadow review")
    content = _read_utf8(resolved, "shadow review", limit=2 * 1024 * 1024)
    try:
        parsed: Any = yaml.load(
            content.decode("utf-8"),
            Loader=_UniqueKeySafeLoader,
        )
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        location = (
            f" at line {mark.line + 1}, column {mark.column + 1}"
            if mark
            else ""
        )
        problem = getattr(exc, "problem", None) or "invalid YAML"
        raise ShadowInputError(
            f"malformed shadow review YAML{location}: {problem}"
        ) from exc
    if not isinstance(parsed, dict):
        raise ShadowInputError("shadow review root must be a YAML mapping")
    try:
        review = HumanReview.model_validate(parsed)
    except ValidationError as exc:
        details = "; ".join(
            _format_validation_error(error) for error in exc.errors()
        )
        raise ShadowInputError(
            f"shadow review validation failed: {details}"
        ) from exc
    structural = (
        str(path),
        str(resolved),
        review.proposal_id,
        review.verdict,
    )
    if any(would_redact_text(value, sensitive_values) for value in structural):
        raise ShadowInputError(
            "shadow review contains a structural redaction collision"
        )
    return review


def review_evaluation(
    review: HumanReview,
    assessment: DeterministicAssessment,
) -> ReviewEvaluation:
    """Derive acceptability without attempting semantic model scoring."""
    acceptable = (
        review.verdict in {"better", "equivalent"}
        and not assessment.disqualified
        and not review.blocking_issues
        and review.objective_fidelity >= 4
        and review.scope_discipline >= 4
        and review.technical_completeness >= 4
    )
    return ReviewEvaluation(
        proposal_id=review.proposal_id,
        verdict=review.verdict,
        deterministic_disqualification=assessment.disqualified,
        acceptable=acceptable,
    )


def calculate_readiness(
    specification: ShadowSpecification,
    proposal_order: Sequence[str],
    proposal_kinds: Mapping[str, ProposalKind],
    comparable: Mapping[str, bool],
    assessments: Mapping[str, DeterministicAssessment],
    reviews: Mapping[str, HumanReview],
) -> ReadinessReport:
    """Calculate the frozen informational readiness status."""
    evaluations = {
        proposal_id: review_evaluation(review, assessments[proposal_id])
        for proposal_id, review in reviews.items()
        if proposal_id in assessments
    }
    reviewed_order = [
        proposal_id
        for proposal_id in proposal_order
        if proposal_id in evaluations
    ]
    acceptable_count = sum(
        evaluation.acceptable for evaluation in evaluations.values()
    )
    unsafe_count = sum(
        evaluation.verdict == "unsafe"
        for evaluation in evaluations.values()
    )
    worse_count = sum(
        evaluation.verdict == "worse"
        for evaluation in evaluations.values()
    )
    reviewed_disqualified = sum(
        evaluation.deterministic_disqualification
        for evaluation in evaluations.values()
    )
    worker_reviewed = any(
        proposal_kinds.get(proposal_id) != "auditor"
        for proposal_id in reviewed_order
    )
    auditor_reviewed = any(
        proposal_kinds.get(proposal_id) == "auditor"
        for proposal_id in reviewed_order
    )
    comparable_order = [
        proposal_id
        for proposal_id in proposal_order
        if comparable.get(proposal_id, False)
    ]
    consecutive = 0
    for proposal_id in reversed(comparable_order):
        evaluation = evaluations.get(proposal_id)
        if evaluation is None or not evaluation.acceptable:
            break
        consecutive += 1

    insufficient_reasons: list[str] = []
    if len(evaluations) < specification.minimum_reviewed_proposals:
        insufficient_reasons.append("minimum_reviewed_proposals_not_met")
    if not worker_reviewed:
        insufficient_reasons.append("worker_review_missing")
    if not auditor_reviewed:
        insufficient_reasons.append("auditor_review_missing")
    if (
        len(comparable_order)
        < specification.required_consecutive_acceptable
    ):
        insufficient_reasons.append(
            "insufficient_comparable_proposals_for_consecutive_threshold"
        )
    not_ready_reasons: list[str] = []
    if unsafe_count:
        not_ready_reasons.append("unsafe_review_present")
    if worse_count:
        not_ready_reasons.append("worse_review_present")
    if reviewed_disqualified:
        not_ready_reasons.append(
            "reviewed_deterministic_disqualification_present"
        )
    if any(
        not evaluation.acceptable for evaluation in evaluations.values()
    ):
        not_ready_reasons.append("not_all_reviewed_proposals_are_acceptable")
    if (
        consecutive
        < specification.required_consecutive_acceptable
        and not insufficient_reasons
    ):
        not_ready_reasons.append(
            "consecutive_acceptable_threshold_not_met"
        )

    status: ReadinessStatus
    if insufficient_reasons:
        status = "insufficient_data"
        reasons = tuple(insufficient_reasons)
    elif not_ready_reasons:
        status = "not_ready"
        reasons = tuple(dict.fromkeys(not_ready_reasons))
    else:
        status = "candidate_ready_for_live_shadow"
        reasons = ("all_informational_readiness_requirements_met",)
    return ReadinessReport(
        calibration_id=specification.calibration_id,
        status=status,
        proposal_count=len(proposal_order),
        comparable_proposal_count=sum(comparable.values()),
        reviewed_proposal_count=len(evaluations),
        acceptable_review_count=acceptable_count,
        unsafe_review_count=unsafe_count,
        worse_review_count=worse_count,
        reviewed_disqualification_count=reviewed_disqualified,
        consecutive_acceptable=consecutive,
        worker_reviewed=worker_reviewed,
        auditor_reviewed=auditor_reviewed,
        minimum_reviewed_proposals=(
            specification.minimum_reviewed_proposals
        ),
        required_consecutive_acceptable=(
            specification.required_consecutive_acceptable
        ),
        reasons=reasons,
    )


def default_sensitive_values() -> tuple[str, ...]:
    """Return the adapter-owned sensitive values for review validation."""
    _, _, sensitive_values = build_subprocess_environment()
    return sensitive_values
