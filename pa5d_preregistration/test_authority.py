from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from pa5d_preregistration.authority import (
    BASE_COMMIT,
    HUMAN_AUTHORITY_REQUIRED,
    BenchmarkScheduleAuthorityV1,
    PA5DCalibrationAuthorityV1,
    PA5DHumanDecisionsV1,
    PA5DPreregistrationReviewAuthorityV1,
    ThresholdHumanDecisionV1,
    build_review_authority,
    finalize_calibration_authority,
)
from research_automation_supervisor.durable_state import canonical_json

ROOT = Path(__file__).resolve().parent.parent


def _rehash(payload: dict[str, object], hash_field: str) -> dict[str, object]:
    unhashed = {key: value for key, value in payload.items() if key != hash_field}
    return {**unhashed, hash_field: hashlib.sha256(canonical_json(unhashed)).hexdigest()}


@pytest.fixture(scope="module")
def review() -> PA5DPreregistrationReviewAuthorityV1:
    return build_review_authority(ROOT)


def test_exact_catalog_schedule_and_gl_coverage(
    review: PA5DPreregistrationReviewAuthorityV1,
) -> None:
    assert review.status == HUMAN_AUTHORITY_REQUIRED
    assert review.benchmark_sessions_launched == 0
    assert review.gl_sessions_launched == 0
    assert review.final_approved_receipt_issued is False
    assert len(review.candidate_authorities[0].benchmark_catalog_variants) == 42
    assert (
        len({item.case_id for item in review.candidate_authorities[0].benchmark_catalog_variants})
        == 21
    )
    assert len(review.schedule_alternatives) == 2
    for schedule in review.schedule_alternatives:
        assert len(schedule.executions) == 41
        assert (
            len(
                {
                    (item.case_id, item.variant_id, item.repetition_id)
                    for item in schedule.executions
                }
            )
            == 41
        )
        assert len({item.case_id for item in schedule.executions}) == 21
        assert len({item.pa3_action_root for item in schedule.executions}) == 41
    gl = review.candidate_authorities[0].gl_pilot
    assert [item.task_id for item in gl.tasks] == [f"task_{index:03d}" for index in range(1, 11)]
    assert len({item.action_root for item in gl.tasks}) == 10


def test_neutral_schedule_rules_are_exact(
    review: PA5DPreregistrationReviewAuthorityV1,
) -> None:
    maximum, repeat = review.schedule_alternatives
    assert maximum.omitted_variant_keys == ("case_005/variant_002",)
    assert maximum.distinct_variant_count == 41
    assert maximum.repeated_execution_count == 0
    assert repeat.omitted_variant_keys == (
        "case_014/variant_001",
        "case_021/variant_002",
    )
    repeated = [item for item in repeat.executions if item.repetition_id == 2]
    assert [(item.case_id, item.variant_id) for item in repeated] == [("case_021", "variant_001")]
    assert repeat.distinct_variant_count == 40
    assert repeat.repeated_execution_count == 1


def test_fresh_roots_are_deterministic_and_absent(
    review: PA5DPreregistrationReviewAuthorityV1,
) -> None:
    rebuilt = build_review_authority(ROOT)
    assert rebuilt == review
    assert rebuilt.draft_sha256 == review.draft_sha256
    assert BASE_COMMIT in {review.qualified_sources.base_commit}
    roots = [
        Path(item.pa3_action_root)
        for schedule in review.schedule_alternatives
        for item in schedule.executions
    ] + [Path(item.action_root) for item in review.candidate_authorities[0].gl_pilot.tasks]
    assert all(str(item).startswith("runs/pa5d1-preregistered-v1/") for item in roots)
    assert all(not (ROOT / item).exists() for item in roots)


def test_all_source_blob_and_receipt_hashes_rebound(
    review: PA5DPreregistrationReviewAuthorityV1,
) -> None:
    candidate = review.candidate_authorities[0]
    for item in candidate.benchmark_catalog_variants:
        assert (
            hashlib.sha256((ROOT / item.review_receipt.path).read_bytes()).hexdigest()
            == item.review_receipt.sha256
        )
    for task in candidate.gl_pilot.tasks:
        assert (
            hashlib.sha256((ROOT / task.review_receipt.path).read_bytes()).hexdigest()
            == task.review_receipt.sha256
        )
        assert task.source_blobs
    source = ROOT.parent / "GL-with-AI"
    assert source.is_dir()
    # build_review_authority has already read and verified every exact blob from the locked commit.


def test_every_performance_threshold_requires_human_authority(
    review: PA5DPreregistrationReviewAuthorityV1,
) -> None:
    thresholds = review.candidate_authorities[0].thresholds
    performance = [item for item in thresholds if item.gate_kind == "performance_gate"]
    assert len(performance) == 20
    assert all(item.authority_status == HUMAN_AUTHORITY_REQUIRED for item in performance)
    assert all(item.decision_id for item in performance)
    assert all(
        item.authority_status == "QUALIFIED_PRE_OUTCOME"
        for item in thresholds
        if item.gate_kind == "structural_hard_gate"
    )


def test_finalization_rejects_missing_or_stale_human_authority(
    review: PA5DPreregistrationReviewAuthorityV1,
) -> None:
    with pytest.raises(ValidationError):
        PA5DHumanDecisionsV1.model_validate(
            {
                "schema_version": 1,
                "review_draft_sha256": review.draft_sha256,
                "reviewer_identity": "human",
                "decided_at": "2026-08-19T00:00:00+08:00",
                "selected_schedule_id": "schedule_maximum_variant_coverage_v1",
                "model_configuration_decision": "approve_exact_qualified_pa3_configuration",
                "execution_policy_decision": ("approve_one_shot_calibration_execution_policy_v1"),
                "metric_and_gl_scoring_decision": ("approve_metric_and_gl_scoring_policy_v1"),
                "threshold_decisions": (),
                "explicit_no_pa5b_derivation_attestation": True,
                "decision_sha256": "0" * 64,
            }
        )


def test_complete_human_decisions_convert_without_issuing_receipt(
    review: PA5DPreregistrationReviewAuthorityV1,
) -> None:
    proposed = [
        item
        for item in review.candidate_authorities[0].thresholds
        if item.authority_status == HUMAN_AUTHORITY_REQUIRED
    ]
    threshold_decisions = tuple(
        ThresholdHumanDecisionV1(
            threshold_id=item.threshold_id,
            decision="approve_proposed",
            rationale="Synthetic unit-test approval only; not persisted as human authority.",
        )
        for item in proposed
    )
    payload = {
        "schema_version": 1,
        "review_draft_sha256": review.draft_sha256,
        "reviewer_identity": "synthetic-unit-test-only",
        "decided_at": "2026-08-19T00:00:00+08:00",
        "selected_schedule_id": "schedule_maximum_variant_coverage_v1",
        "model_configuration_decision": "approve_exact_qualified_pa3_configuration",
        "execution_policy_decision": "approve_one_shot_calibration_execution_policy_v1",
        "metric_and_gl_scoring_decision": "approve_metric_and_gl_scoring_policy_v1",
        "threshold_decisions": [item.model_dump(mode="json") for item in threshold_decisions],
        "explicit_no_pa5b_derivation_attestation": True,
    }
    decision_hash = hashlib.sha256(canonical_json(payload)).hexdigest()
    decisions = PA5DHumanDecisionsV1.model_validate_json(
        canonical_json({**payload, "decision_sha256": decision_hash})
    )
    finalized = finalize_calibration_authority(review, decisions)
    assert finalized.approval_state == "HUMAN_APPROVED"
    assert finalized.schedule.status == "human_selected"
    assert finalized.model_configuration.status == "HUMAN_APPROVED"
    assert finalized.execution_policy.status == "HUMAN_APPROVED"
    assert finalized.metric_and_scoring_policy.status == "HUMAN_APPROVED"
    assert finalized.human_decision_sha256 == decisions.decision_sha256
    assert all(item.authority_status != HUMAN_AUTHORITY_REQUIRED for item in finalized.thresholds)
    assert review.final_approved_receipt_issued is False


def test_persisted_review_is_canonical_authority(
    review: PA5DPreregistrationReviewAuthorityV1,
) -> None:
    path = ROOT / "pa5d_preregistration/PA5D_PREREGISTRATION_REVIEW.json"
    if not path.exists():
        pytest.skip("generated review packet has not yet been materialized")
    loaded = PA5DPreregistrationReviewAuthorityV1.model_validate_json(path.read_bytes())
    assert loaded == review
    payload = loaded.model_dump(mode="json", exclude={"draft_sha256"})
    assert hashlib.sha256(canonical_json(payload)).hexdigest() == loaded.draft_sha256


def test_module_has_no_model_launch_surface() -> None:
    text = (ROOT / "pa5d_preregistration/authority.py").read_text(encoding="utf-8")
    forbidden = ("Popen(", "subprocess.run(", "run_physics_auditor", "run_substage(")
    assert not any(item in text for item in forbidden)


def test_pa5b_appears_only_in_contamination_history(
    review: PA5DPreregistrationReviewAuthorityV1,
) -> None:
    serialized = review.model_dump_json()
    assert serialized.count("344d55c53899f8e030826cfefa76d1438e50e4f8") == 2
    # One occurrence per complete schedule candidate, and only in each contamination register.
    for candidate in review.candidate_authorities:
        without_register = candidate.model_dump(mode="json", exclude={"contamination_register"})
        assert "344d55c53899f8e030826cfefa76d1438e50e4f8" not in json.dumps(
            without_register, sort_keys=True
        )


def test_review_rejects_hidden_non_schedule_candidate_difference(
    review: PA5DPreregistrationReviewAuthorityV1,
) -> None:
    payload = review.model_dump(mode="json")
    candidate = payload["candidate_authorities"][1]
    candidate["contamination_register"][0]["identity"] = "hidden-foreign-history"
    payload["candidate_authorities"][1] = _rehash(candidate, "authority_sha256")
    payload = _rehash(payload, "draft_sha256")
    with pytest.raises(ValidationError, match="differ outside the exact schedule"):
        PA5DPreregistrationReviewAuthorityV1.model_validate_json(canonical_json(payload))


def test_schedule_rejects_self_hashed_but_nonderived_action_root(
    review: PA5DPreregistrationReviewAuthorityV1,
) -> None:
    schedule = review.schedule_alternatives[0].model_dump(mode="json")
    schedule["executions"][0]["pa3_action_root"] += "-tampered"
    schedule["expected_child_set_sha256"] = hashlib.sha256(
        canonical_json(schedule["executions"])
    ).hexdigest()
    with pytest.raises(ValidationError, match="root or identity derivation changed"):
        BenchmarkScheduleAuthorityV1.model_validate_json(canonical_json(schedule))


def test_candidate_rejects_threshold_metric_without_definition(
    review: PA5DPreregistrationReviewAuthorityV1,
) -> None:
    candidate = review.candidate_authorities[0].model_dump(mode="json")
    referenced = candidate["thresholds"][0]["metric_id"]
    candidate["metrics"] = [
        item for item in candidate["metrics"] if item["metric_id"] != referenced
    ]
    candidate = _rehash(candidate, "authority_sha256")
    with pytest.raises(ValidationError, match="every threshold must reference"):
        PA5DCalibrationAuthorityV1.model_validate_json(canonical_json(candidate))


@pytest.mark.parametrize(
    ("reviewer_identity", "decided_at"),
    (
        ("", "2026-08-19T00:00:00+08:00"),
        ("human", "2026-08-19 00:00:00"),
        ("human", "2026-99-99T99:99:99+08:00"),
    ),
)
def test_human_identity_and_rfc3339_timestamp_are_strict(
    review: PA5DPreregistrationReviewAuthorityV1,
    reviewer_identity: str,
    decided_at: str,
) -> None:
    payload = {
        "schema_version": 1,
        "review_draft_sha256": review.draft_sha256,
        "reviewer_identity": reviewer_identity,
        "decided_at": decided_at,
        "selected_schedule_id": "schedule_maximum_variant_coverage_v1",
        "model_configuration_decision": "approve_exact_qualified_pa3_configuration",
        "execution_policy_decision": "approve_one_shot_calibration_execution_policy_v1",
        "metric_and_gl_scoring_decision": "approve_metric_and_gl_scoring_policy_v1",
        "threshold_decisions": [],
        "explicit_no_pa5b_derivation_attestation": True,
    }
    payload = _rehash(payload, "decision_sha256")
    with pytest.raises(ValidationError):
        PA5DHumanDecisionsV1.model_validate(payload)


def test_candidate_rejects_self_hashed_partial_approval(
    review: PA5DPreregistrationReviewAuthorityV1,
) -> None:
    candidate = review.candidate_authorities[0].model_dump(mode="json")
    candidate["approval_state"] = "HUMAN_APPROVED"
    candidate["human_decision_sha256"] = "1" * 64
    candidate = _rehash(candidate, "authority_sha256")
    with pytest.raises(ValidationError, match="incomplete subordinate approval state"):
        PA5DCalibrationAuthorityV1.model_validate_json(canonical_json(candidate))
