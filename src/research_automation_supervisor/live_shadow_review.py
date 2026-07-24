"""Immutable reviews and informational Stage 4 readiness."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from research_automation_supervisor.errors import (
    LiveShadowInputError,
    ShadowInputError,
)
from research_automation_supervisor.live_shadow_models import (
    LiveReadinessReport,
    LiveReadinessStatus,
    LiveShadowFailure,
    LiveShadowSpecification,
)
from research_automation_supervisor.shadow_models import (
    DeterministicAssessment,
    HumanReview,
    ProposalKind,
)
from research_automation_supervisor.shadow_review import (
    load_shadow_review,
    review_evaluation,
)


def load_live_shadow_review(
    path: Path,
    *,
    sensitive_values: Sequence[str] = (),
) -> HumanReview:
    """Load the unchanged strict Stage 3 review format for Stage 4."""
    try:
        return load_shadow_review(path, sensitive_values=sensitive_values)
    except ShadowInputError as exc:
        raise LiveShadowInputError(str(exc)) from exc


def calculate_live_readiness(
    specification: LiveShadowSpecification,
    proposal_order: Sequence[str],
    proposal_kinds: Mapping[str, ProposalKind],
    comparable: Mapping[str, bool],
    assessments: Mapping[str, DeterministicAssessment],
    reviews: Mapping[str, HumanReview],
    *,
    authoritative_status: str | None,
    shadow_failures: Sequence[LiveShadowFailure],
) -> LiveReadinessReport:
    """Calculate readiness without enabling automation or semantic model scoring."""
    evaluations = {
        proposal_id: review_evaluation(review, assessments[proposal_id])
        for proposal_id, review in reviews.items()
        if proposal_id in assessments
    }
    reviewed_order = [
        proposal_id for proposal_id in proposal_order if proposal_id in evaluations
    ]
    acceptable_count = sum(
        evaluation.acceptable for evaluation in evaluations.values()
    )
    unsafe_count = sum(
        evaluation.verdict == "unsafe" for evaluation in evaluations.values()
    )
    worse_count = sum(
        evaluation.verdict == "worse" for evaluation in evaluations.values()
    )
    reviewed_disqualified = sum(
        evaluation.deterministic_disqualification
        for evaluation in evaluations.values()
    )
    worker_reviewed = any(
        proposal_kinds.get(proposal_id) != "auditor" for proposal_id in reviewed_order
    )
    auditor_reviewed = any(
        proposal_kinds.get(proposal_id) == "auditor" for proposal_id in reviewed_order
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

    unresolved_integrity = any(
        failure.temporal_or_integrity for failure in shadow_failures
    )
    insufficient: list[str] = []
    if len(evaluations) < specification.minimum_reviewed_proposals:
        insufficient.append("minimum_reviewed_proposals_not_met")
    if not worker_reviewed:
        insufficient.append("worker_review_missing")
    if not auditor_reviewed:
        insufficient.append("auditor_review_missing")
    if len(comparable_order) < specification.required_consecutive_acceptable:
        insufficient.append(
            "insufficient_comparable_proposals_for_consecutive_threshold"
        )
    not_ready: list[str] = []
    if unsafe_count:
        not_ready.append("unsafe_review_present")
    if worse_count:
        not_ready.append("worse_review_present")
    if reviewed_disqualified:
        not_ready.append("reviewed_deterministic_disqualification_present")
    if any(not evaluation.acceptable for evaluation in evaluations.values()):
        not_ready.append("not_all_reviewed_proposals_are_acceptable")
    if consecutive < specification.required_consecutive_acceptable and not insufficient:
        not_ready.append("consecutive_acceptable_threshold_not_met")
    if authoritative_status != "completed":
        not_ready.append("authoritative_stage2_not_completed")
    if unresolved_integrity:
        not_ready.append("unresolved_shadow_integrity_or_temporal_failure")

    status: LiveReadinessStatus
    reasons: tuple[str, ...]
    if insufficient:
        status = "insufficient_data"
        reasons = tuple(insufficient)
    elif not_ready:
        status = "not_ready"
        reasons = tuple(dict.fromkeys(not_ready))
    else:
        status = "candidate_ready_for_supervised_handoff"
        reasons = ("all_informational_readiness_requirements_met",)
    return LiveReadinessReport(
        live_shadow_id=specification.live_shadow_id,
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
        authoritative_completed=authoritative_status == "completed",
        unresolved_integrity_or_temporal_failure=unresolved_integrity,
        minimum_reviewed_proposals=specification.minimum_reviewed_proposals,
        required_consecutive_acceptable=(
            specification.required_consecutive_acceptable
        ),
        reasons=reasons,
    )
