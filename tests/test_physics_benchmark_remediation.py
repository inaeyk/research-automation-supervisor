from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest

from research_automation_supervisor.errors import PhysicsBenchmarkIntegrityError
from research_automation_supervisor.physics_benchmark import (
    benchmark_scoring_observation,
    load_validated_fixture_authority,
    score_physics_benchmark,
    validate_benchmark_authority_separation,
    verify_auditor_visible_blindness,
)
from research_automation_supervisor.physics_benchmark_models import (
    PhysicsBenchmarkRunRecordV1,
    load_physics_benchmark_catalog,
)
from research_automation_supervisor.physics_gl_pilot import (
    load_physics_gl_pilot_config,
    read_gl_source_blob,
    validate_physics_gl_pilot,
)
from research_automation_supervisor.physics_gl_pilot_execution import (
    _prepare_exact_gl_workspace,
)
from tests.test_physics_benchmark import (
    BENCHMARK,
    CATALOG_PATH,
    ROOT,
    _records,
    _score_inputs,
)
from tests.test_physics_gl_pilot import CONFIG_PATH, GL_SOURCE


def test_fixture_authority_is_hash_bound_approved_and_neutral() -> None:
    catalog = load_physics_benchmark_catalog(CATALOG_PATH)
    authority = load_validated_fixture_authority(catalog, repository_root=ROOT)
    assert authority is not None

    assert all(re.fullmatch(r"case_\d{3}", item.case_id) for item in catalog.cases)
    assert {item.case_id for item in authority.manifests} == {
        item.case_id for item in catalog.cases
    }
    for case in catalog.cases:
        manifest = authority.manifest(case.case_id)
        assert manifest.approval.decision == "approved"
        assert manifest.approval.independent_from_fixture_author is True
        visible = b"\n".join(
            path.read_bytes()
            for path in sorted((ROOT / case.fixture_root).iterdir())
            if path.is_file()
        )
        assert case.seed_kind.encode() not in visible
        assert case.seeded_defect_authority.encode() not in visible
        assert manifest.approval.review_id.encode() not in visible
        assert b"expected_route" not in visible
        assert b"required_finding_categories" not in visible


def test_case_004_has_unambiguous_vector_metric_authority() -> None:
    catalog = load_physics_benchmark_catalog(CATALOG_PATH)
    case = catalog.case("case_004")
    contract = (ROOT / case.contract_path).read_text(encoding="utf-8")
    source = (ROOT / case.fixture_root / "implementation.py").read_text(encoding="utf-8")

    assert "vector components (A^r,A^theta)" in contract
    assert "r^2 (A^theta)^2" in contract
    assert "def vector_norm_sq" in source
    assert "covector_norm_sq" not in source


def test_blindness_check_rejects_semantic_and_structural_answer_key_leaks() -> None:
    catalog = load_physics_benchmark_catalog(CATALOG_PATH)
    authority = load_validated_fixture_authority(catalog, repository_root=ROOT)
    assert authority is not None
    case = catalog.case("case_002")
    manifest = authority.manifest(case.case_id)
    visible = {
        path.name: path.read_bytes()
        for path in (ROOT / case.fixture_root).iterdir()
        if path.is_file()
    }

    verify_auditor_visible_blindness(
        visible,
        prompt=b"neutral projected input\n",
        case=case,
        fixture_authority=manifest,
    )
    with pytest.raises(PhysicsBenchmarkIntegrityError, match="scorer-only"):
        verify_auditor_visible_blindness(
            {**visible, "leak.json": b'{"expected_route":"request_repair"}\n'},
            prompt=b"neutral projected input\n",
            case=case,
            fixture_authority=manifest,
        )
    with pytest.raises(PhysicsBenchmarkIntegrityError, match="scorer-only"):
        verify_auditor_visible_blindness(
            visible,
            prompt=case.seeded_defect_authority.encode(),
            case=case,
            fixture_authority=manifest,
        )


def test_pa5c_scoring_requires_mechanical_identities() -> None:
    catalog = load_physics_benchmark_catalog(CATALOG_PATH)
    records = _records(catalog)
    authority = load_validated_fixture_authority(catalog, repository_root=ROOT)
    assert authority is not None

    with pytest.raises(PhysicsBenchmarkIntegrityError, match="mechanical identity"):
        score_physics_benchmark(
            catalog,
            records,
            fixture_authority=authority,
            ordinary_nonphysics_unchanged=True,
            limitations=("scripted only",),
        )


def test_required_alternative_forbidden_and_severity_scoring_are_distinct() -> None:
    catalog = load_physics_benchmark_catalog(CATALOG_PATH)
    records = list(_records(catalog))
    case_index = next(index for index, item in enumerate(records) if item.case_id == "case_002")
    case = catalog.case("case_002")
    original = records[case_index]

    alternative = original.model_dump(mode="json")
    alternative["findings"][0]["category"] = "convention_mismatch"
    provisional = PhysicsBenchmarkRunRecordV1.model_validate(alternative)
    alternative.update(benchmark_scoring_observation(case, provisional))
    records[case_index] = PhysicsBenchmarkRunRecordV1.model_validate(alternative)
    authority, identities = _score_inputs(catalog, records)
    report = score_physics_benchmark(
        catalog,
        records,
        fixture_authority=authority,
        identity_verifications=identities,
        ordinary_nonphysics_unchanged=True,
        limitations=("scripted only",),
    )
    assert report.records[case_index].acceptable_alternative_satisfied
    assert report.aggregate.acceptable_alternative_satisfaction_rate == 1.0

    low = records[case_index].model_dump(mode="json")
    low["findings"][0]["severity"] = "low"
    low["critical_defect_detected"] = False
    provisional = PhysicsBenchmarkRunRecordV1.model_validate(low)
    low.update(benchmark_scoring_observation(case, provisional))
    records[case_index] = PhysicsBenchmarkRunRecordV1.model_validate(low)
    authority, identities = _score_inputs(catalog, records)
    report = score_physics_benchmark(
        catalog,
        records,
        fixture_authority=authority,
        identity_verifications=identities,
        ordinary_nonphysics_unchanged=True,
        limitations=("scripted only",),
    )
    assert not report.records[case_index].severity_matched
    assert report.qualification_verdict == "not_qualified"


def test_fixture_source_tamper_is_detected_before_launch(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    shutil.copytree(BENCHMARK, workspace / "examples/physics_auditor/benchmark_v1")
    copied_catalog = workspace / CATALOG_PATH.relative_to(ROOT)
    catalog = load_physics_benchmark_catalog(copied_catalog)
    source = workspace / catalog.case("case_002").fixture_root / "evidence.md"
    source.write_text(source.read_text(encoding="utf-8") + "tamper\n", encoding="utf-8")

    with pytest.raises(PhysicsBenchmarkIntegrityError, match="manifest contradicts"):
        validate_benchmark_authority_separation(
            catalog,
            repository_root=workspace,
            catalog_path=copied_catalog,
        )


def test_gl_preparation_projects_exact_commit_blobs_without_summaries(
    tmp_path: Path,
) -> None:
    config = load_physics_gl_pilot_config(CONFIG_PATH)
    validate_physics_gl_pilot(
        config,
        repository_root=ROOT,
        config_path=CONFIG_PATH,
        source_repository_root=GL_SOURCE,
    )
    task = config.task("task_006")
    workspace = _prepare_exact_gl_workspace(
        config=config,
        task=task,
        benchmark_root=ROOT,
        source_repository_root=GL_SOURCE.resolve(strict=True),
        destination=tmp_path / "prepared-workspace",
    )
    assert (
        _prepare_exact_gl_workspace(
            config=config,
            task=task,
            benchmark_root=ROOT,
            source_repository_root=GL_SOURCE.resolve(strict=True),
            destination=workspace,
        )
        == workspace
    )
    for source in task.source_refs:
        assert (workspace / "source" / source.path).read_bytes() == read_gl_source_blob(
            GL_SOURCE,
            config.source_commit,
            source.path,
        )
    tracked = {
        item
        for item in (workspace / ".git").parent.rglob("*")
        if item.is_file() and ".git" not in item.parts
    }
    assert not any(item.name == "evidence.md" for item in tracked)
    assert not (workspace / CONFIG_PATH.relative_to(ROOT)).exists()
    assert (
        workspace / "examples/physics_auditor/gl_pilot_v1/fixtures/task_006/candidate.txt"
    ).is_file()


def test_gl_source_hash_tamper_is_detected() -> None:
    raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    raw["tasks"][0]["source_refs"][0]["sha256"] = "0" * 64
    from research_automation_supervisor.physics_gl_pilot import PhysicsGLPilotConfigV1

    config = PhysicsGLPilotConfigV1.model_validate(raw)
    with pytest.raises(PhysicsBenchmarkIntegrityError, match="source hash changed"):
        validate_physics_gl_pilot(
            config,
            repository_root=ROOT,
            config_path=CONFIG_PATH,
            source_repository_root=GL_SOURCE,
        )
