from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from research_automation_supervisor.cli import app
from research_automation_supervisor.codex_models import CodexRunResult
from research_automation_supervisor.errors import PhysicsBenchmarkIntegrityError
from research_automation_supervisor.physics_auditor_execution import PhysicsAuditorCodexRun
from research_automation_supervisor.physics_benchmark import (
    finalize_physics_benchmark_report,
    fixture_sha256,
    render_physics_benchmark_markdown,
    score_physics_benchmark,
    seeded_authority_sha256,
    validate_benchmark_authority_separation,
    verify_projection_excludes_benchmark_authority,
)
from research_automation_supervisor.physics_benchmark_execution import (
    physics_benchmark_status,
    run_public_physics_benchmark,
)
from research_automation_supervisor.physics_benchmark_models import (
    REQUIRED_SEED_KINDS,
    PhysicsBenchmarkCatalogV1,
    PhysicsBenchmarkFindingObservationV1,
    PhysicsBenchmarkRunRecordV1,
    PhysicsBenchmarkUsageV1,
    PhysicsBenchmarkWorkerRepairV1,
    load_physics_benchmark_catalog,
    load_physics_benchmark_repair_calibration,
)
from research_automation_supervisor.physics_models import load_physics_task_contract
from research_automation_supervisor.physics_routing import derive_physics_audit_decision

ROOT = Path(__file__).parents[1]
BENCHMARK = ROOT / "examples/physics_auditor/benchmark_v1"
CATALOG_PATH = BENCHMARK / "authority/catalog.json"
REPAIR_CALIBRATION_PATH = BENCHMARK / "authority/worker-repair-calibration.json"
CLI = CliRunner()
EMPTY_SHA = hashlib.sha256(b"").hexdigest()
BWRAP = Path("/usr/bin/bwrap")
REPAIR_CALIBRATION = frozenset(
    {
        "wrong_sign",
        "missing_normalization",
        "missing_metric_factor",
        "finite_difference_stencil",
    }
)


def _evidence(
    kind: str,
    *,
    reference: str | None = None,
    path: str | None = None,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "reference": reference,
        "path": path,
        "line_start": None,
        "line_end": None,
    }


def _scripted_report(case: Any) -> dict[str, Any]:
    contract = load_physics_task_contract(ROOT / case.contract_path)
    oracle_ids = [item.id for item in contract.oracles if item.required]
    document_path = next(
        item.path for item in contract.evidence if item.id == "case_note"
    )
    document = _evidence("document", path=document_path)
    oracle_refs = [_evidence("oracle", reference=item) for item in oracle_ids]
    primary_evidence = [document, *oracle_refs]
    clean = case.expected_route == "pass"
    missing = case.seed_kind == "insufficient_evidence"
    conflict = case.seed_kind == "conflicting_evidence"
    evidence_block = case.expected_route == "block_insufficient_evidence" and not missing
    checks: list[dict[str, Any]] = []
    if missing:
        checks.extend(
            [
                {
                    "id": "check_primary",
                    "target_kind": "required_identity",
                    "target_id": "primary_identity",
                    "status": "unresolved",
                    "evidence_sufficiency": "partial",
                    "evidence": primary_evidence,
                    "rationale": "The required independent result is explicitly absent.",
                },
                {
                    "id": "check_limit",
                    "target_kind": "limiting_case",
                    "target_id": "control_limit",
                    "status": "unresolved",
                    "evidence_sufficiency": "insufficient",
                    "evidence": oracle_refs,
                    "rationale": "The limiting-case oracle is unavailable.",
                },
            ]
        )
    elif conflict:
        checks.extend(
            [
                {
                    "id": "check_primary",
                    "target_kind": "required_identity",
                    "target_id": "primary_identity",
                    "status": "unresolved",
                    "evidence_sufficiency": "conflicting",
                    "evidence": primary_evidence,
                    "rationale": "The two declared authorities conflict.",
                },
                {
                    "id": "check_limit",
                    "target_kind": "limiting_case",
                    "target_id": "control_limit",
                    "status": "passed",
                    "evidence_sufficiency": "sufficient",
                    "evidence": oracle_refs,
                    "rationale": "The control behavior is consistent.",
                },
            ]
        )
    else:
        checks.extend(
            [
                {
                    "id": "check_primary",
                    "target_kind": "required_identity",
                    "target_id": "primary_identity",
                    "status": "passed" if clean else "unresolved" if evidence_block else "failed",
                    "evidence_sufficiency": "partial" if evidence_block else "sufficient",
                    "evidence": primary_evidence,
                    "rationale": "The public evidence determines this bounded identity.",
                },
                {
                    "id": "check_limit",
                    "target_kind": "limiting_case",
                    "target_id": "control_limit",
                    "status": "passed",
                    "evidence_sufficiency": "sufficient",
                    "evidence": oracle_refs,
                    "rationale": "The independent control limit passes.",
                },
            ]
        )
    for index, oracle_id in enumerate(oracle_ids):
        oracle_pass = clean or missing or (conflict and index == 0)
        checks.append(
            {
                "id": f"check_{oracle_id}",
                "target_kind": "oracle",
                "target_id": oracle_id,
                "status": "unresolved" if missing else "passed" if oracle_pass else "failed",
                "evidence_sufficiency": (
                    "insufficient" if missing else "sufficient"
                ),
                "evidence": [_evidence("oracle", reference=oracle_id)],
                "rationale": (
                    "The required oracle is absent."
                    if missing
                    else "The verified PA-2 summary fixes this result."
                ),
            }
        )
    finding_check_ids = ["check_primary", *[f"check_{item}" for item in oracle_ids]]
    findings: list[dict[str, Any]] = []
    for index, category in enumerate(case.required_finding_categories):
        disposition = (
            "human_review"
            if case.expected_route == "require_human_review"
            else "evidence_blocking"
            if case.expected_route == "block_insufficient_evidence"
            else "repairable"
        )
        findings.append(
            {
                "id": f"finding_{index + 1}",
                "severity": "critical" if case.critical_seeded_defect else "high",
                "category": category,
                "status": "open",
                "disposition": disposition,
                "check_ids": (
                    [item["id"] for item in checks] if missing else finding_check_ids
                ),
                "forbidden_claim_ids": (
                    ["no_discovery_claim"]
                    if category == "unsupported_physical_claim"
                    else []
                ),
                "evidence": primary_evidence,
                "statement": "The bounded semantic seed is present.",
                "required_action": "Follow the frozen deterministic route.",
            }
        )
    triggers: list[str] = []
    if case.seed_kind == "convention_change_request":
        triggers.append("convention_change")
    if case.seed_kind in {"constraint_mode_claim", "gauge_mode_claim"}:
        triggers.append("unresolved_gauge_constraint_ambiguity")
    if case.seed_kind in {"boundary_localization_claim", "unsupported_interpretation"}:
        triggers.append("new_physical_interpretation")
    unresolved = (
        [
            {
                "id": "question_conflict",
                "category": "evidence_conflict",
                "question": "Which authority is valid requires human review.",
                "evidence": oracle_refs,
            }
        ]
        if conflict
        else []
    )
    verdict = {
        "pass": "pass",
        "request_repair": "fail_repairable",
        "require_human_review": "human_review",
        "block_insufficient_evidence": "blocked_insufficient_evidence",
    }[case.expected_route]
    sufficiency = (
        "insufficient"
        if missing
        else "conflicting"
        if conflict
        else "partial"
        if evidence_block
        else "sufficient"
    )
    return {
        "schema_version": 1,
        "profile": "physics_implementation",
        "verdict": verdict,
        "evidence_sufficiency": sufficiency,
        "summary": "Scripted semantic outcome for the public PA-5B fixture.",
        "human_gate_triggers": triggers,
        "checks": checks,
        "findings": findings,
        "unresolved_questions": unresolved,
    }


def _worker_repair(case: Any, repetition: int) -> PhysicsBenchmarkWorkerRepairV1:
    if not case.worker_repair_appropriate:
        return PhysicsBenchmarkWorkerRepairV1(
            applicable=False,
            attempted=False,
            same_worker_session=None,
            repair_round_count=0,
            stale_evidence_invalidated=None,
            fresh_auditor_enforced=None,
            final_route=None,
            success=None,
            final_workspace_integrity="not_available",
            final_proof_integrity="not_available",
        )
    attempted = case.seed_kind in REPAIR_CALIBRATION and repetition == 1
    return PhysicsBenchmarkWorkerRepairV1(
        applicable=True,
        attempted=attempted,
        same_worker_session=True if attempted else None,
        repair_round_count=1 if attempted else 0,
        stale_evidence_invalidated=True if attempted else None,
        fresh_auditor_enforced=True if attempted else None,
        final_route="pass" if attempted else None,
        success=True if attempted else None,
        final_workspace_integrity="unchanged" if attempted else "not_available",
        final_proof_integrity="verified" if attempted else "not_available",
    )


def _records(catalog: PhysicsBenchmarkCatalogV1) -> tuple[PhysicsBenchmarkRunRecordV1, ...]:
    records: list[PhysicsBenchmarkRunRecordV1] = []
    for case in catalog.cases:
        contract = load_physics_task_contract(ROOT / case.contract_path)
        report = _scripted_report(case)
        decision = derive_physics_audit_decision(
            contract,
            contract.audit_policy,
            report,
        )
        assert decision.outcome == case.expected_route
        findings = tuple(
            PhysicsBenchmarkFindingObservationV1(
                finding_id=item["id"],
                category=item["category"],
                severity=item["severity"],
                status=item["status"],
            )
            for item in report["findings"]
        )
        for repetition in range(1, case.repetitions + 1):
            session = hashlib.sha256(
                f"{case.case_id}:{repetition}".encode("ascii")
            ).hexdigest()
            records.append(
                PhysicsBenchmarkRunRecordV1(
                    benchmark_id=catalog.benchmark_id,
                    case_id=case.case_id,
                    category=case.category,
                    repetition=repetition,
                    fixture_sha256=fixture_sha256(ROOT / case.fixture_root),
                    contract_sha256=contract.canonical_sha256(),
                    seeded_defect_authority_sha256=seeded_authority_sha256(case),
                    expected_route=case.expected_route,
                    actual_report_verdict=report["verdict"],
                    actual_route=decision.outcome,
                    required_finding_categories=case.required_finding_categories,
                    findings=findings,
                    critical_defect_detected=case.critical_seeded_defect,
                    false_positive_finding_ids=(),
                    run_status="routing_completed",
                    malformed_report=False,
                    infrastructure_failure=False,
                    infrastructure_reason=None,
                    worker_repair=_worker_repair(case, repetition),
                    fresh_session_identity_sha256=session,
                    session_reused=False,
                    prompt_template_sha256="1" * 64,
                    prompt_sha256=hashlib.sha256(
                        f"prompt:{case.case_id}:{repetition}".encode("ascii")
                    ).hexdigest(),
                    projection_sha256="2" * 64,
                    oracle_proof_manifest_sha256="3" * 64,
                    action_proof_sha256="4" * 64,
                    recovery_proof_sha256="5" * 64,
                    duplicate_recovery_action_detected=False,
                    workspace_integrity="unchanged",
                    projection_integrity="unchanged",
                    oracle_program_access_detected=False,
                    answer_key_exposure_detected=False,
                    yolo_inheritance_detected=False,
                    pa2_proofs_verified=True,
                    pa3_proof_verified=True,
                    duration_seconds=float(10 + repetition),
                    usage=PhysicsBenchmarkUsageV1(
                        availability="provider_reported",
                        input_tokens=1_000 + repetition,
                        output_tokens=100 + repetition,
                    ),
                )
            )
    return tuple(records)


def test_catalog_covers_public_suite_and_fixed_repetition_design() -> None:
    catalog = load_physics_benchmark_catalog(CATALOG_PATH)
    repair = load_physics_benchmark_repair_calibration(REPAIR_CALIBRATION_PATH)

    assert len(catalog.cases) == 21
    assert REQUIRED_SEED_KINDS.issubset({item.seed_kind for item in catalog.cases})
    assert sum(item.repetitions for item in catalog.cases) == 41
    assert sum(item.repetitions == 3 for item in catalog.cases) == 10
    assert catalog.prompt_repair_limit == 1
    assert repair.benchmark_id == catalog.benchmark_id
    assert {item.case_id for item in repair.cases} == {
        "pa5b_case_002",
        "pa5b_case_003",
        "pa5b_case_004",
        "pa5b_case_011",
    }
    assert all(item.result.success for item in repair.cases)
    assert all(
        item.expected_route != "pass" for item in catalog.cases if item.critical_seeded_defect
    )


def test_answer_key_and_oracle_program_are_separate_from_visible_fixtures() -> None:
    catalog = load_physics_benchmark_catalog(CATALOG_PATH)
    hashes = validate_benchmark_authority_separation(
        catalog,
        repository_root=ROOT,
        catalog_path=CATALOG_PATH,
    )

    assert set(hashes) == {item.case_id for item in catalog.cases}
    assert all(len(value) == 64 for value in hashes.values())
    case = catalog.cases[0]
    visible = (
        case.contract_path,
        f"{case.fixture_root}/implementation.py",
        f"{case.fixture_root}/evidence.md",
    )
    verify_projection_excludes_benchmark_authority(
        visible,
        case=case,
        catalog_relative_path=CATALOG_PATH.relative_to(ROOT).as_posix(),
    )
    with pytest.raises(PhysicsBenchmarkIntegrityError):
        verify_projection_excludes_benchmark_authority(
            (*visible, case.oracle_program_paths[0]),
            case=case,
            catalog_relative_path=CATALOG_PATH.relative_to(ROOT).as_posix(),
        )


def test_all_scripted_cases_route_and_score_semantically_without_prose_matching(
    tmp_path: Path,
) -> None:
    catalog = load_physics_benchmark_catalog(CATALOG_PATH)
    records = _records(catalog)
    report = score_physics_benchmark(
        catalog,
        records,
        ordinary_nonphysics_unchanged=True,
        limitations=("Synthetic bounded cases only.",),
    )

    assert report.complete
    assert report.qualification_verdict == "qualified"
    assert report.aggregate.run_count == 41
    assert report.aggregate.critical_defect_detection_rate == 1.0
    assert report.aggregate.false_pass_rate == 0.0
    assert report.aggregate.clean_case_pass_rate == 1.0
    assert report.aggregate.false_critical_finding_rate == 0.0
    assert report.aggregate.correct_repair_routing_rate == 1.0
    assert report.aggregate.correct_human_escalation_rate == 1.0
    assert report.aggregate.correct_insufficient_evidence_rate == 1.0
    assert report.aggregate.malformed_report_rate == 0.0
    assert report.aggregate.infrastructure_failure_rate == 0.0
    assert report.aggregate.repair_success_rate == 1.0
    assert report.aggregate.repeated_run_route_consistency == 1.0
    assert report.aggregate.finding_category_consistency == 1.0
    assert len(report.by_category) == len({item.category for item in catalog.cases})
    markdown = render_physics_benchmark_markdown(report)
    assert "Functional correctness, report validity" in markdown
    assert "broad autonomous physics competence" in markdown
    json_path, markdown_path = finalize_physics_benchmark_report(tmp_path, report)
    assert json.loads(json_path.read_text())["qualification_verdict"] == "qualified"
    assert markdown_path.read_text() == markdown
    assert finalize_physics_benchmark_report(tmp_path, report) == (json_path, markdown_path)
    validation_json, validation_markdown = finalize_physics_benchmark_report(
        tmp_path / "validation",
        report,
        validation_layout=True,
    )
    assert validation_json.name == "physics_auditor_pa5b.json"
    assert validation_markdown.name == "physics_auditor_pa5b.md"


def test_aggregation_resume_reuses_json_and_finalizes_missing_markdown(
    tmp_path: Path,
) -> None:
    catalog = load_physics_benchmark_catalog(CATALOG_PATH)
    report = score_physics_benchmark(
        catalog,
        _records(catalog),
        ordinary_nonphysics_unchanged=True,
        limitations=("Bounded synthetic calibration only.",),
    )
    json_path, markdown_path = finalize_physics_benchmark_report(tmp_path, report)
    before = json_path.read_bytes()
    before_mtime = json_path.stat().st_mtime_ns
    markdown_path.unlink()

    recovered_json, recovered_markdown = finalize_physics_benchmark_report(tmp_path, report)

    assert recovered_json.read_bytes() == before
    assert recovered_json.stat().st_mtime_ns == before_mtime
    assert recovered_markdown.is_file()


def test_critical_pass_and_false_critical_clean_finding_fail_qualification() -> None:
    catalog = load_physics_benchmark_catalog(CATALOG_PATH)
    records = list(_records(catalog))
    critical_index = next(
        index
        for index, item in enumerate(records)
        if catalog.case(item.case_id).critical_seeded_defect
    )
    value = records[critical_index].model_dump(mode="json")
    value.update(
        {
            "actual_report_verdict": "pass",
            "actual_route": "pass",
            "findings": [],
            "critical_defect_detected": False,
        }
    )
    records[critical_index] = PhysicsBenchmarkRunRecordV1.model_validate(value)
    report = score_physics_benchmark(
        catalog,
        records,
        ordinary_nonphysics_unchanged=True,
        limitations=("Synthetic bounded cases only.",),
    )

    assert not report.hard_gates.zero_critical_defect_passes
    assert report.aggregate.false_pass_rate is not None
    assert report.aggregate.false_pass_rate > 0
    assert report.qualification_verdict == "not_qualified"


def test_strict_schemas_and_malformed_reports_fail_closed() -> None:
    raw = json.loads(CATALOG_PATH.read_text())
    raw["unexpected"] = True
    with pytest.raises(ValidationError):
        PhysicsBenchmarkCatalogV1.model_validate(raw)

    catalog = load_physics_benchmark_catalog(CATALOG_PATH)
    value = _records(catalog)[0].model_dump(mode="json")
    value.update(
        {
            "run_status": "malformed_report",
            "malformed_report": True,
            "actual_route": "pass",
        }
    )
    with pytest.raises(ValidationError):
        PhysicsBenchmarkRunRecordV1.model_validate(value)


def test_public_benchmark_validation_cli_is_model_free() -> None:
    result = CLI.invoke(
        app,
        [
            "validate-physics-benchmark",
            "--catalog",
            str(CATALOG_PATH),
            "--workspace",
            str(ROOT),
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["case_count"] == 21
    assert payload["repetition_count"] == 41
    assert payload["answer_key_projected"] is False
    assert "model" not in result.stdout.casefold()


def test_benchmark_status_and_dry_run_are_read_only(tmp_path: Path) -> None:
    catalog = load_physics_benchmark_catalog(CATALOG_PATH)
    output = tmp_path / "not-created"
    status = physics_benchmark_status(catalog=catalog, output_directory=output)

    assert status.expected_run_count == 41
    assert status.completed_run_count == 0
    assert status.next_case_id == "pa5b_case_001"
    assert status.next_repetition == 1
    assert status.safe_resume
    assert not output.exists()

    result = CLI.invoke(
        app,
        [
            "run-physics-benchmark",
            "--catalog",
            str(CATALOG_PATH),
            "--execution-config",
            str(ROOT / "examples/physics_auditor/synthetic/execution-config.yaml"),
            "--output",
            str(output),
            "--workspace",
            str(ROOT),
            "--dry-run",
            "--json",
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["dry_run"] is True
    assert payload["model_or_oracle_launched"] is False
    assert not output.exists()


@pytest.mark.skipif(not BWRAP.is_file(), reason="Bubblewrap PA-2 path unavailable")
def test_bounded_executor_uses_fresh_pa3_actions_and_resumes_without_duplicates(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    benchmark_copy = workspace / "examples/physics_auditor/benchmark_v1"
    benchmark_copy.parent.mkdir(parents=True)
    shutil.copytree(BENCHMARK, benchmark_copy)
    synthetic_copy = workspace / "examples/physics_auditor/synthetic"
    synthetic_copy.mkdir()
    shutil.copyfile(
        ROOT / "examples/physics_auditor/synthetic/execution-config.yaml",
        synthetic_copy / "execution-config.yaml",
    )
    subprocess.run(("/usr/bin/git", "-C", workspace, "init", "-q"), check=True)
    subprocess.run(
        ("/usr/bin/git", "-C", workspace, "config", "user.name", "PA5B Test"),
        check=True,
    )
    subprocess.run(
        (
            "/usr/bin/git",
            "-C",
            workspace,
            "config",
            "user.email",
            "pa5b@example.invalid",
        ),
        check=True,
    )
    subprocess.run(("/usr/bin/git", "-C", workspace, "add", "."), check=True)
    subprocess.run(
        ("/usr/bin/git", "-C", workspace, "commit", "-qm", "fixture"), check=True
    )
    copied_catalog_path = benchmark_copy / "authority/catalog.json"
    catalog = load_physics_benchmark_catalog(copied_catalog_path)
    case = catalog.case("pa5b_case_001")
    two_oracle_case = catalog.case("pa5b_case_020")
    sessions: list[str] = []

    def factory(_case: Any, repetition: int) -> Any:
        report = json.dumps(_scripted_report(_case), sort_keys=True).encode("utf-8")

        def invoke(**kwargs: Any) -> PhysicsAuditorCodexRun:
            prepared = kwargs["prepared"]
            executable = Path(kwargs["codex_executable"])
            session = f"fresh-pa5b-scripted-{_case.case_id}-{repetition}"
            sessions.append(session)
            return PhysicsAuditorCodexRun(
                adapter_result=CodexRunResult(
                    run_id=prepared.request.run_id,
                    status="succeeded",
                    exit_code=0,
                    started_at="2026-01-01T00:00:00Z",
                    ended_at="2026-01-01T00:00:01Z",
                    duration_seconds=1.0,
                    artifact_directory="/synthetic/fake-pa5b-codex-action",
                    event_count=1,
                    malformed_event_count=0,
                    final_message_present=True,
                    permission_evidence=False,
                    summary="Scripted PA-5B Physics Auditor completed.",
                    error=None,
                ),
                model_output=report,
                model_output_truncated=False,
                provider_session_id=session,
                provider_thread_started_ids=(session,),
                backend_policy_evidence_sha256=EMPTY_SHA,
                bubblewrap_backend_identity_sha256="9" * 64,
                codex_executable_sha256=hashlib.sha256(executable.read_bytes()).hexdigest(),
                codex_cli_version="scripted-test-v1",
            )

        return invoke

    output = tmp_path / "benchmark-actions"
    kwargs = {
        "catalog": catalog,
        "catalog_path": copied_catalog_path,
        "execution_config_path": workspace
        / "examples/physics_auditor/synthetic/execution-config.yaml",
        "repository_root": workspace,
        "output_directory": output,
        "case_ids": (case.case_id, two_oracle_case.case_id),
        "codex_invoker_factory": factory,
    }
    first = run_public_physics_benchmark(**kwargs)
    completed_status = physics_benchmark_status(
        catalog=catalog,
        output_directory=output,
        case_ids=(case.case_id, two_oracle_case.case_id),
    )
    second = run_public_physics_benchmark(**kwargs)

    assert first == second
    assert len(first) == 4
    assert sessions == [
        "fresh-pa5b-scripted-pa5b_case_001-1",
        "fresh-pa5b-scripted-pa5b_case_001-2",
        "fresh-pa5b-scripted-pa5b_case_001-3",
        "fresh-pa5b-scripted-pa5b_case_020-1",
    ]
    assert tuple(item.actual_route for item in first) == (
        "pass",
        "pass",
        "pass",
        "require_human_review",
    )
    assert all(item.pa2_proofs_verified and item.pa3_proof_verified for item in first)
    assert all(not item.session_reused for item in first)
    assert all(item.recovery_proof_sha256 is not None for item in first)
    assert completed_status.completed_run_count == 4
    assert completed_status.pending_run_count == 0
    assert completed_status.next_case_id is None
