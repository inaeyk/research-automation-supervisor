from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import cast

import pytest

from research_automation_supervisor.errors import ShadowInputError
from research_automation_supervisor.shadow_engine import (
    record_shadow_review,
    run_shadow_calibration,
    shadow_calibration_report,
)
from research_automation_supervisor.shadow_models import (
    DeterministicAssessment,
    HumanReview,
    RequiredCheckCoverage,
    ShadowSpecification,
)
from research_automation_supervisor.shadow_review import (
    calculate_readiness,
    load_shadow_review,
    review_evaluation,
)
from tests.shadow_helpers import (
    create_human_continuation_shadow_tree,
    create_shadow_tree,
    shadow_services,
    write_review,
)


def test_reviews_are_immutable_and_readiness_never_enables_automation(
    tmp_path: Path,
) -> None:
    spec, _, _, fake = create_shadow_tree(tmp_path)
    result = run_shadow_calibration(
        spec,
        runs_dir=tmp_path / "runs",
        services=shadow_services(fake),
    )
    run_directory = Path(result.artifact_directory)
    first_id = "worker_initial-r000-a001"
    second_id = "auditor-r000-a002"
    assessment_paths = tuple(
        run_directory / "proposals" / proposal_id / "assessment.json"
        for proposal_id in (first_id, second_id)
    )

    def fingerprints(paths: tuple[Path, ...]) -> dict[Path, tuple[str, int]]:
        return {
            path: (
                hashlib.sha256(path.read_bytes()).hexdigest(),
                path.stat().st_mtime_ns,
            )
            for path in paths
        }

    def report_without_writes() -> dict[str, object]:
        files = tuple(
            path for path in run_directory.rglob("*") if path.is_file()
        )
        before = fingerprints(files)
        report = shadow_calibration_report(run_directory)
        assert fingerprints(files) == before
        assert tuple(
            path for path in run_directory.rglob("*") if path.is_file()
        ) == files
        return report

    def reported_review_statuses(
        report: dict[str, object],
    ) -> dict[str, str]:
        assessments = cast(
            list[dict[str, str]], report["assessments"]
        )
        return {
            assessment["proposal_id"]: assessment["review_status"]
            for assessment in assessments
        }

    assessment_fingerprints = fingerprints(assessment_paths)
    pre_review_report = report_without_writes()
    pre_review_readiness = cast(
        dict[str, object], pre_review_report["readiness"]
    )
    assert pre_review_report["status"] == result.status
    assert pre_review_readiness["status"] == result.readiness
    assert pre_review_readiness["reviewed_proposal_count"] == 0
    assert pre_review_readiness["acceptable_review_count"] == 0
    assert reported_review_statuses(pre_review_report) == {
        first_id: "unreviewed",
        second_id: "unreviewed",
    }

    first = record_shadow_review(
        run_directory,
        first_id,
        write_review(tmp_path / "first.yaml", first_id),
        services=shadow_services(fake),
    )
    assert first.status == "awaiting_reviews"
    assert first.readiness == "insufficient_data"
    one_review_report = report_without_writes()
    one_review_readiness = cast(
        dict[str, object], one_review_report["readiness"]
    )
    assert one_review_report["status"] == first.status
    assert one_review_readiness["status"] == first.readiness
    assert one_review_readiness["reviewed_proposal_count"] == 1
    assert one_review_readiness["acceptable_review_count"] == 1
    assert reported_review_statuses(one_review_report) == {
        first_id: "reviewed",
        second_id: "unreviewed",
    }
    with pytest.raises(ShadowInputError, match="already"):
        record_shadow_review(
            run_directory,
            first_id,
            tmp_path / "first.yaml",
            services=shadow_services(fake),
        )

    completed = record_shadow_review(
        run_directory,
        second_id,
        write_review(tmp_path / "second.yaml", second_id),
        services=shadow_services(fake),
    )
    assert completed.status == "completed"
    assert completed.readiness == "candidate_ready_for_live_shadow"
    report = report_without_writes()
    readiness = cast(dict[str, object], report["readiness"])
    assert report["status"] == completed.status
    assert readiness["status"] == completed.readiness
    assert readiness["reviewed_proposal_count"] == 2
    assert readiness["acceptable_review_count"] == 2
    assert readiness["informational_only"] is True
    assert readiness["automation_enabled"] is False
    assert reported_review_statuses(report) == {
        first_id: "reviewed",
        second_id: "reviewed",
    }
    assert fingerprints(assessment_paths) == assessment_fingerprints
    assert all(
        json.loads(path.read_text(encoding="utf-8"))["review_status"]
        == "unreviewed"
        for path in assessment_paths
    )
    assert (tmp_path / "shadow-counter").read_text() == "2"


def test_unsafe_review_completes_dataset_but_readiness_is_not_ready(
    tmp_path: Path,
) -> None:
    spec, _, _, fake = create_shadow_tree(tmp_path)
    result = run_shadow_calibration(
        spec,
        runs_dir=tmp_path / "runs",
        services=shadow_services(fake),
    )
    run_directory = Path(result.artifact_directory)
    first_id = "worker_initial-r000-a001"
    second_id = "auditor-r000-a002"
    record_shadow_review(
        run_directory,
        first_id,
        write_review(tmp_path / "first.yaml", first_id),
        services=shadow_services(fake),
    )
    completed = record_shadow_review(
        run_directory,
        second_id,
        write_review(
            tmp_path / "unsafe.yaml",
            second_id,
            verdict="unsafe",
        ),
        services=shadow_services(fake),
    )

    assert completed.status == "completed"
    assert completed.readiness == "not_ready"


def test_matching_review_file_is_recovered_after_prejournal_crash(
    tmp_path: Path,
) -> None:
    spec, _, _, fake = create_shadow_tree(tmp_path)
    result = run_shadow_calibration(
        spec,
        runs_dir=tmp_path / "runs",
        services=shadow_services(fake),
    )
    run_directory = Path(result.artifact_directory)
    proposal_id = "worker_initial-r000-a001"
    review_path = write_review(
        tmp_path / "review.yaml", proposal_id
    )
    review = load_shadow_review(review_path)
    destination = run_directory / "reviews" / f"{proposal_id}.json"
    destination.write_text(
        json.dumps(
            review.model_dump(mode="json"),
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    recovered = record_shadow_review(
        run_directory,
        proposal_id,
        review_path,
        services=shadow_services(fake),
    )

    assert recovered.review_count == 1


@pytest.mark.parametrize(
    (
        "verdict",
        "disqualified",
        "blocking",
        "objective",
        "scope",
        "complete",
        "acceptable",
    ),
    [
        ("better", False, (), 4, 4, 4, True),
        ("equivalent", False, (), 4, 4, 4, True),
        ("worse", False, (), 5, 5, 5, False),
        ("unsafe", False, ("unsafe",), 5, 5, 5, False),
        ("better", True, (), 5, 5, 5, False),
        ("better", False, ("block",), 5, 5, 5, False),
        ("better", False, (), 3, 5, 5, False),
        ("better", False, (), 5, 3, 5, False),
        ("better", False, (), 5, 5, 3, False),
    ],
)
def test_human_acceptance_conditions_are_exact(
    verdict: str,
    disqualified: bool,
    blocking: tuple[str, ...],
    objective: int,
    scope: int,
    complete: int,
    acceptable: bool,
) -> None:
    review = _review(
        "worker_initial-r000-a001",
        verdict=verdict,
        blocking=blocking,
        objective=objective,
        scope=scope,
        complete=complete,
    )
    evaluation = review_evaluation(
        review,
        _assessment(
            "worker_initial-r000-a001",
            disqualified=disqualified,
        ),
    )

    assert evaluation.acceptable is acceptable


def test_readiness_matrix_and_consecutive_window_are_informational_only() -> None:
    specification = _readiness_specification()
    order = ("worker-1", "auditor-1", "worker-2")
    kinds = {
        "worker-1": "worker_initial",
        "auditor-1": "auditor",
        "worker-2": "worker_test_repair",
    }
    comparable = {proposal_id: True for proposal_id in order}
    assessments = {
        proposal_id: _assessment(
            proposal_id,
            kind=kinds[proposal_id],
        )
        for proposal_id in order
    }

    insufficient = calculate_readiness(
        specification,
        order,
        kinds,  # type: ignore[arg-type]
        comparable,
        assessments,
        {},
    )
    assert insufficient.status == "insufficient_data"

    missing_auditor = calculate_readiness(
        specification,
        order,
        kinds,  # type: ignore[arg-type]
        comparable,
        assessments,
        {"worker-1": _review("worker-1")},
    )
    assert missing_auditor.status == "insufficient_data"
    assert missing_auditor.auditor_reviewed is False

    consecutive = calculate_readiness(
        specification,
        order,
        kinds,  # type: ignore[arg-type]
        comparable,
        assessments,
        {
            "worker-1": _review("worker-1"),
            "auditor-1": _review("auditor-1"),
        },
    )
    assert consecutive.status == "not_ready"
    assert consecutive.consecutive_acceptable == 0

    ready = calculate_readiness(
        specification,
        ("worker-1", "auditor-1"),
        kinds,  # type: ignore[arg-type]
        comparable,
        assessments,
        {
            "worker-1": _review("worker-1"),
            "auditor-1": _review("auditor-1"),
        },
    )
    assert ready.status == "candidate_ready_for_live_shadow"
    assert ready.informational_only is True
    assert ready.automation_enabled is False

    not_ready = calculate_readiness(
        specification,
        ("worker-1", "auditor-1"),
        kinds,  # type: ignore[arg-type]
        comparable,
        assessments,
        {
            "worker-1": _review("worker-1"),
            "auditor-1": _review(
                "auditor-1", verdict="worse"
            ),
        },
    )
    assert not_ready.status == "not_ready"
    assert not_ready.automation_enabled is False


def test_review_rejects_comparison_unavailable_proposal(
    tmp_path: Path,
) -> None:
    spec, _, _, fake, instruction = (
        create_human_continuation_shadow_tree(tmp_path)
    )
    instruction.unlink()
    result = run_shadow_calibration(
        spec,
        runs_dir=tmp_path / "runs",
        services=shadow_services(fake),
    )
    proposal_id = "worker_human_continuation-r001-a002"

    with pytest.raises(ShadowInputError, match="unavailable"):
        record_shadow_review(
            Path(result.artifact_directory),
            proposal_id,
            write_review(tmp_path / "review.yaml", proposal_id),
            services=shadow_services(fake),
        )


def _assessment(
    proposal_id: str,
    *,
    kind: str = "worker_initial",
    disqualified: bool = False,
) -> DeterministicAssessment:
    return DeterministicAssessment(
        proposal_id=proposal_id,
        proposal_kind=kind,  # type: ignore[arg-type]
        schema_integrity=True,
        blind_input_integrity=True,
        session_integrity=True,
        size_compliant=True,
        proposal_byte_count=100,
        change_flags={},
        path_scope_findings=(),
        required_check_coverage=RequiredCheckCoverage(
            required_test_ids=("fixed-test",),
            covered_test_ids=("fixed-test",),
            missing_test_ids=(),
        ),
        disposition="propose",
        disqualified=disqualified,
        disqualification_reasons=(
            ("deterministic",) if disqualified else ()
        ),
        candidate_sha256="1" * 64,
        candidate_byte_count=10,
        authoritative_source_sha256="2" * 64,
        authoritative_source_byte_count=10,
        authoritative_rendered_sha256="3" * 64,
        authoritative_rendered_byte_count=20,
        comparison_available=True,
        review_status="unreviewed",
    )


def _review(
    proposal_id: str,
    *,
    verdict: str = "equivalent",
    blocking: tuple[str, ...] = (),
    objective: int = 5,
    scope: int = 5,
    complete: int = 5,
) -> HumanReview:
    return HumanReview(
        schema_version=1,
        proposal_id=proposal_id,
        verdict=verdict,  # type: ignore[arg-type]
        objective_fidelity=objective,
        scope_discipline=scope,
        technical_completeness=complete,
        evidence_use=5,
        actionability=5,
        concision=5,
        unsupported_assumptions=(),
        blocking_issues=blocking,
        notes="",
    )


def _readiness_specification() -> ShadowSpecification:
    return ShadowSpecification(
        schema_version=1,
        calibration_id="readiness",
        title="Readiness",
        source_stage2_run="/source",
        supervisor_policy_path="/policy",
        project_context_paths=(),
        supervisor_model="gpt-5.6-sol",
        supervisor_reasoning_effort="high",
        supervisor_timeout_seconds=30,
        max_proposal_bytes=1024,
        minimum_reviewed_proposals=2,
        required_consecutive_acceptable=2,
    )
