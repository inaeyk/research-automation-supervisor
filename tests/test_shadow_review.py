from __future__ import annotations

import json
from pathlib import Path

import pytest

from research_automation_supervisor.errors import ShadowInputError
from research_automation_supervisor.shadow_engine import (
    record_shadow_review,
    run_shadow_calibration,
    shadow_calibration_report,
)
from research_automation_supervisor.shadow_review import load_shadow_review
from tests.shadow_helpers import (
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

    first = record_shadow_review(
        run_directory,
        first_id,
        write_review(tmp_path / "first.yaml", first_id),
        services=shadow_services(fake),
    )
    assert first.status == "awaiting_reviews"
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
    report = shadow_calibration_report(run_directory)
    readiness = report["readiness"]
    assert readiness["informational_only"] is True  # type: ignore[index]
    assert readiness["automation_enabled"] is False  # type: ignore[index]


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
