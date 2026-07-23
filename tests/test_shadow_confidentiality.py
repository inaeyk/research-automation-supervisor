from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest
import yaml

from research_automation_supervisor.codex_adapter import run_prepared_codex
from research_automation_supervisor.errors import (
    ShadowConfidentialityError,
    ShadowIntegrityError,
)
from research_automation_supervisor.shadow_confidentiality import (
    preflight_shadow_confidentiality,
)
from research_automation_supervisor.shadow_engine import (
    ShadowServices,
    record_shadow_review,
    run_shadow_calibration,
    shadow_calibration_report,
    shadow_calibration_status,
)
from research_automation_supervisor.shadow_sources import (
    load_shadow_specification,
)
from tests.shadow_helpers import (
    SUPERVISOR_UUID,
    create_shadow_tree,
    shadow_services,
    supervisor_proposal,
    supervisor_response,
    write_review,
)


@pytest.mark.parametrize(
    "sensitive",
    [
        "fixed-test",
        "minimal-substage",
        "src/**",
        "worker completed",
        "completed",
        "Frozen contract sentence.",
        "Draft advisory prompts from only the supplied frozen evidence.",
        "This is a generic local research-software calibration.",
    ],
)
def test_complete_source_preflight_covers_every_blind_string_domain(
    tmp_path: Path,
    sensitive: str,
) -> None:
    spec, _, _, _ = create_shadow_tree(tmp_path)

    with pytest.raises(ShadowConfidentialityError):
        load_shadow_specification(
            spec,
            environ={"AUDIT_TOKEN": sensitive},
        )


def test_source_collision_prevents_run_directory_creation(
    tmp_path: Path,
) -> None:
    spec, _, _, fake = create_shadow_tree(tmp_path)
    runs = tmp_path / "runs"

    with pytest.raises(ShadowConfidentialityError):
        run_shadow_calibration(
            spec,
            runs_dir=runs,
            services=ShadowServices(
                codex_executable=str(fake),
                environ={"AUDIT_TOKEN": "fixed-test"},
            ),
        )

    assert not runs.exists()
    assert not (tmp_path / "shadow-counter").exists()


def test_raw_specification_locator_is_checked_before_normalization(
    tmp_path: Path,
) -> None:
    sensitive = "RAW_SPEC_SENTINEL_9d8c"
    spec, _, _, _ = create_shadow_tree(tmp_path)
    sensitive_directory = tmp_path / sensitive
    sensitive_directory.mkdir()
    lexical = sensitive_directory / ".." / spec.relative_to(tmp_path)

    with pytest.raises(ShadowConfidentialityError):
        load_shadow_specification(
            lexical,
            environ={"AUDIT_TOKEN": sensitive},
        )

    assert sensitive not in str(spec)
    assert not (tmp_path / "runs").exists()


def test_raw_runs_directory_is_checked_before_normalization(
    tmp_path: Path,
) -> None:
    sensitive = "RAW_RUNS_SENTINEL_8b3a"
    spec, _, _, fake = create_shadow_tree(tmp_path)
    sensitive_directory = tmp_path / sensitive
    sensitive_directory.mkdir()
    lexical_runs = sensitive_directory / ".." / "runs"

    with pytest.raises(ShadowConfidentialityError):
        run_shadow_calibration(
            spec,
            runs_dir=lexical_runs,
            services=ShadowServices(
                codex_executable=str(fake),
                environ={"AUDIT_TOKEN": sensitive},
            ),
        )

    assert not (tmp_path / "runs").exists()
    assert not (tmp_path / "shadow-counter").exists()


@pytest.mark.parametrize("resolved_target", [False, True])
def test_configured_and_resolved_executable_paths_preflight_before_run(
    tmp_path: Path,
    resolved_target: bool,
) -> None:
    sensitive = "EXECUTABLE_PATH_SENTINEL_4c2f"
    spec, _, _, fake = create_shadow_tree(tmp_path)
    copied = tmp_path / "fake-codex"
    shutil.copy2(fake, copied)
    copied.chmod(0o755)
    sensitive_directory = tmp_path / sensitive
    sensitive_directory.mkdir()
    if resolved_target:
        target = sensitive_directory / "codex-real"
        shutil.copy2(fake, target)
        target.chmod(0o755)
        locator = tmp_path / "codex-link"
        locator.symlink_to(target)
    else:
        locator = sensitive_directory / ".." / copied.name

    with pytest.raises(ShadowConfidentialityError):
        run_shadow_calibration(
            spec,
            runs_dir=tmp_path / "runs",
            services=ShadowServices(
                codex_executable=str(locator),
                environ={"AUDIT_TOKEN": sensitive},
            ),
        )

    assert not (tmp_path / "runs").exists()
    assert not (tmp_path / "shadow-counter").exists()


@pytest.mark.parametrize(
    "sensitive",
    ["output-schema.json", "stage1-run"],
)
def test_derived_supervisor_dependency_paths_preflight_before_run(
    tmp_path: Path,
    sensitive: str,
) -> None:
    spec, _, _, fake = create_shadow_tree(tmp_path)

    with pytest.raises(ShadowConfidentialityError):
        run_shadow_calibration(
            spec,
            runs_dir=tmp_path / "runs",
            services=ShadowServices(
                codex_executable=str(fake),
                environ={"AUDIT_TOKEN": sensitive},
            ),
        )

    assert not (tmp_path / "runs").exists()
    assert not (tmp_path / "shadow-counter").exists()


def test_raw_review_and_run_locators_write_and_launch_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec, _, _, fake = create_shadow_tree(tmp_path)
    result = run_shadow_calibration(
        spec,
        runs_dir=tmp_path / "runs",
        services=shadow_services(fake),
    )
    run_directory = Path(result.artifact_directory)
    review = write_review(
        tmp_path / "review.yaml",
        "worker_initial-r000-a001",
    )
    sensitive = "COMMAND_PATH_SENTINEL_73ac"
    sensitive_directory = tmp_path / sensitive
    sensitive_directory.mkdir()
    lexical_review = sensitive_directory / ".." / review.name
    lexical_run = (
        sensitive_directory
        / ".."
        / run_directory.relative_to(tmp_path)
    )
    before = {
        path.relative_to(run_directory): path.read_bytes()
        for path in run_directory.rglob("*")
        if path.is_file()
    }
    counter = (tmp_path / "shadow-counter").read_text()

    with pytest.raises(ShadowConfidentialityError):
        record_shadow_review(
            run_directory,
            "worker_initial-r000-a001",
            lexical_review,
            services=ShadowServices(
                codex_executable=str(fake),
                environ={"AUDIT_TOKEN": sensitive},
            ),
        )
    monkeypatch.setenv("AUDIT_TOKEN", sensitive)
    with pytest.raises(ShadowConfidentialityError):
        shadow_calibration_status(lexical_run)
    with pytest.raises(ShadowConfidentialityError):
        shadow_calibration_report(lexical_run)

    assert (tmp_path / "shadow-counter").read_text() == counter
    assert {
        path.relative_to(run_directory): path.read_bytes()
        for path in run_directory.rglob("*")
        if path.is_file()
    } == before


@pytest.mark.parametrize(
    "domain",
    [
        "pending_action",
        "journal_entry",
        "state",
        "result",
        "escalation",
        "aggregate_report",
    ],
)
def test_recursive_persisted_structure_preflight_covers_every_domain(
    domain: str,
) -> None:
    sensitive = "PERSISTED_STRUCTURE_SENTINEL_2a91"
    value = {
        "schema_version": 1,
        "domain": domain,
        "outer": [{"inner": {"path": f"safe/{sensitive}/value"}}],
    }

    with pytest.raises(ShadowConfidentialityError):
        preflight_shadow_confidentiality(
            value,
            (sensitive,),
            label="persisted Stage 3 structure",
        )
    with pytest.raises(ShadowIntegrityError):
        preflight_shadow_confidentiality(
            value,
            (sensitive,),
            label="trusted persisted Stage 3 structure",
            integrity=True,
        )


def test_runtime_blind_collision_pauses_before_next_intent_or_launch(
    tmp_path: Path,
) -> None:
    spec, _, _, fake = create_shadow_tree(tmp_path)
    environment = dict(os.environ)
    call_count = 0

    def add_sensitive_value_after_first(prepared, **kwargs: object):
        nonlocal call_count
        result = run_prepared_codex(
            prepared, **kwargs  # type: ignore[arg-type]
        )
        call_count += 1
        if call_count == 1:
            environment["AUDIT_TOKEN"] = (
                "Inspect the current workspace directly"
            )
        return result

    result = run_shadow_calibration(
        spec,
        runs_dir=tmp_path / "runs",
        services=ShadowServices(
            codex_executable=str(fake),
            supervisor_invoker=add_sensitive_value_after_first,
            environ=environment,
        ),
    )

    assert result.status == "human_paused"
    assert result.pause_reason == "blind_input_confidentiality_collision"
    assert (tmp_path / "shadow-counter").read_text() == "1"
    run_directory = Path(result.artifact_directory)
    assert not (
        run_directory
        / "proposals/auditor-r000-a002/blind-input-manifest.json"
    ).exists()
    _assert_absent_from_artifacts(
        run_directory,
        environment["AUDIT_TOKEN"],
    )


def test_runtime_comparison_collision_pauses_before_authoritative_write(
    tmp_path: Path,
) -> None:
    spec, _, _, fake = create_shadow_tree(tmp_path)
    environment = dict(os.environ)

    def add_authoritative_sensitive_value(prepared, **kwargs: object):
        result = run_prepared_codex(
            prepared, **kwargs  # type: ignore[arg-type]
        )
        environment["AUDIT_TOKEN"] = "Implement the substage."
        return result

    result = run_shadow_calibration(
        spec,
        runs_dir=tmp_path / "runs",
        services=ShadowServices(
            codex_executable=str(fake),
            supervisor_invoker=add_authoritative_sensitive_value,
            environ=environment,
        ),
    )

    assert result.status == "human_paused"
    assert result.pause_reason == "comparison_confidentiality_collision"
    assert (tmp_path / "shadow-counter").read_text() == "1"
    run_directory = Path(result.artifact_directory)
    comparison = (
        run_directory / "comparisons/worker_initial-r000-a001"
    )
    assert not comparison.exists()
    _assert_absent_from_artifacts(
        run_directory,
        environment["AUDIT_TOKEN"],
    )


@pytest.mark.parametrize(
    ("field", "sensitive"),
    [
        ("notes", "secret review note"),
        ("unsupported_assumptions", "secret unsupported assumption"),
        ("blocking_issues", "secret blocking issue"),
    ],
)
def test_sensitive_review_strings_write_nothing(
    tmp_path: Path,
    field: str,
    sensitive: str,
) -> None:
    spec, _, _, fake = create_shadow_tree(tmp_path)
    result = run_shadow_calibration(
        spec,
        runs_dir=tmp_path / "runs",
        services=shadow_services(fake),
    )
    run_directory = Path(result.artifact_directory)
    review_value: dict[str, object] = {
        "schema_version": 1,
        "proposal_id": "worker_initial-r000-a001",
        "verdict": "equivalent",
        "objective_fidelity": 5,
        "scope_discipline": 5,
        "technical_completeness": 5,
        "evidence_use": 5,
        "actionability": 5,
        "concision": 5,
        "unsupported_assumptions": [],
        "blocking_issues": [],
        "notes": "safe note",
    }
    review_value[field] = (
        sensitive if field == "notes" else [sensitive]
    )
    review_path = tmp_path / "review.yaml"
    review_path.write_text(
        yaml.safe_dump(review_value, sort_keys=False),
        encoding="utf-8",
    )
    before = {
        path.relative_to(run_directory): path.read_bytes()
        for path in run_directory.rglob("*")
        if path.is_file()
    }

    with pytest.raises(ShadowConfidentialityError):
        record_shadow_review(
            run_directory,
            "worker_initial-r000-a001",
            review_path,
            services=ShadowServices(
                codex_executable=str(fake),
                environ={"AUDIT_TOKEN": sensitive},
            ),
        )

    assert {
        path.relative_to(run_directory): path.read_bytes()
        for path in run_directory.rglob("*")
        if path.is_file()
    } == before
    assert not (
        run_directory / "reviews/worker_initial-r000-a001.json"
    ).exists()


def test_generated_secret_shape_is_redacted_then_disqualified(
    tmp_path: Path,
) -> None:
    response = supervisor_response("worker_initial")
    proposal = json.loads(supervisor_proposal("worker_initial"))
    proposal["summary"] = "sk-generated-secret"
    response["final"] = json.dumps(proposal, sort_keys=True)
    spec, _, _, fake = create_shadow_tree(
        tmp_path,
        supervisor_responses=[
            response,
            supervisor_response(
                "auditor",
                expected_resume_thread_id=SUPERVISOR_UUID,
            ),
        ],
    )

    result = run_shadow_calibration(
        spec,
        runs_dir=tmp_path / "runs",
        services=shadow_services(fake),
    )

    assert result.status == "awaiting_reviews"
    assert result.disqualification_count == 1
    _assert_absent_from_artifacts(
        Path(result.artifact_directory),
        "sk-generated-secret",
    )


def _assert_absent_from_artifacts(
    run_directory: Path,
    sensitive: str,
) -> None:
    encoded = sensitive.encode("utf-8")
    for path in run_directory.rglob("*"):
        if path.is_file():
            assert encoded not in path.read_bytes(), path
