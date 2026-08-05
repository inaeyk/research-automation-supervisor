from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from research_automation_supervisor.cli import app
from research_automation_supervisor.codex_models import CodexRunResult
from research_automation_supervisor.physics_auditor_execution import PhysicsAuditorCodexRun
from research_automation_supervisor.physics_benchmark import fixture_sha256
from research_automation_supervisor.physics_benchmark_models import (
    PhysicsBenchmarkFindingObservationV1,
    PhysicsBenchmarkUsageV1,
)
from research_automation_supervisor.physics_gl_pilot import (
    REQUIRED_GL_PILOT_TOPICS,
    PhysicsGLPilotRunV1,
    aggregate_physics_gl_pilot,
    gl_pilot_scoring_observation,
    load_physics_gl_pilot_config,
    locked_authority_sha256,
    validate_physics_gl_pilot,
)
from research_automation_supervisor.physics_gl_pilot_execution import (
    run_bounded_physics_gl_pilot,
)
from research_automation_supervisor.physics_models import load_physics_task_contract
from research_automation_supervisor.physics_routing import derive_physics_audit_decision
from tests.test_physics_benchmark import BWRAP, EMPTY_SHA

ROOT = Path(__file__).parents[1]
GL_SOURCE = ROOT.parent / "GL-with-AI"
PILOT = ROOT / "examples/physics_auditor/gl_pilot_v1"
CONFIG_PATH = PILOT / "config/pilot.json"
CLI = CliRunner()


def _reference(
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


def _pilot_report(task: Any) -> dict[str, Any]:
    contract = load_physics_task_contract(ROOT / task.contract_path)
    declared = next(item for item in contract.evidence if item.id.startswith("source_"))
    assert declared.path is not None
    document = (
        _reference("derivation", path=declared.path)
        if declared.kind == "derivation"
        else _reference("artifact", reference=declared.id)
    )
    oracle = _reference("oracle", reference="pilot_oracle")
    passed = task.expected_route == "pass"
    findings = [
        {
            "id": f"finding_{index + 1}",
            "severity": "critical",
            "category": category,
            "status": "open",
            "disposition": "human_review",
            "check_ids": ["check_identity", "check_oracle"],
            "forbidden_claim_ids": (
                ["no_gl_mode_claim"] if category == "unsupported_physical_claim" else []
            ),
            "evidence": [document, oracle],
            "statement": "The bounded classification requires human review.",
            "required_action": "Retain the locked human scientific gate.",
        }
        for index, category in enumerate(task.required_finding_categories)
    ]
    triggers: list[str] = []
    if task.expected_route == "require_human_review":
        triggers.append(
            "new_physical_interpretation"
            if task.topic == "seeded_boundary_localized_candidate"
            else "unresolved_gauge_constraint_ambiguity"
        )
    return {
        "schema_version": 1,
        "profile": "physics_implementation",
        "verdict": "pass" if passed else "human_review",
        "evidence_sufficiency": "sufficient",
        "summary": "Bounded GL pilot semantic result.",
        "human_gate_triggers": triggers,
        "checks": [
            {
                "id": "check_identity",
                "target_kind": "required_identity",
                "target_id": "pilot_identity",
                "status": "passed" if passed else "failed",
                "evidence_sufficiency": "sufficient",
                "evidence": [document, oracle],
                "rationale": "The locked snapshot fixes this bounded assessment.",
            },
            {
                "id": "check_limit",
                "target_kind": "limiting_case",
                "target_id": "pilot_limit",
                "status": "passed",
                "evidence_sufficiency": "sufficient",
                "evidence": [oracle],
                "rationale": "The declared control limit passes.",
            },
            {
                "id": "check_oracle",
                "target_kind": "oracle",
                "target_id": "pilot_oracle",
                "status": "passed" if passed else "failed",
                "evidence_sufficiency": "sufficient",
                "evidence": [oracle],
                "rationale": "The verified PA-2 summary fixes this result.",
            },
        ],
        "findings": findings,
        "unresolved_questions": [],
    }


def _scripted_records() -> tuple[PhysicsGLPilotRunV1, ...]:
    config = load_physics_gl_pilot_config(CONFIG_PATH)
    records: list[PhysicsGLPilotRunV1] = []
    for task in config.tasks:
        contract = load_physics_task_contract(ROOT / task.contract_path)
        report = _pilot_report(task)
        decision = derive_physics_audit_decision(contract, contract.audit_policy, report)
        assert decision.outcome == task.expected_route
        findings = tuple(
            PhysicsBenchmarkFindingObservationV1(
                finding_id=item["id"],
                category=item["category"],
                severity=item["severity"],
                status=item["status"],
            )
            for item in report["findings"]
        )
        score = gl_pilot_scoring_observation(
            task,
            findings,
            decision.outcome,
            evidence_valid=True,
        )
        records.append(
            PhysicsGLPilotRunV1(
                pilot_id=config.pilot_id,
                task_id=task.task_id,
                topic=task.topic,
                fixture_sha256=fixture_sha256(ROOT / task.fixture_root),
                contract_sha256=contract.canonical_sha256(),
                locked_authority_sha256=locked_authority_sha256(task),
                expected_route=task.expected_route,
                actual_report_verdict=report["verdict"],
                actual_route=decision.outcome,
                required_finding_categories=task.required_finding_categories,
                acceptable_alternative_categories=task.acceptable_alternative_categories,
                forbidden_finding_categories=task.forbidden_finding_categories,
                minimum_severity=task.minimum_severity,
                acceptable_alternative_routes=task.acceptable_alternative_routes,
                findings=findings,
                human_review_mandatory=task.human_review_mandatory,
                route_matched=score["route_matched"],
                category_recognized=score["category_recognized"],
                severity_matched=score["severity_matched"],
                evidence_valid=score["evidence_valid"],
                required_categories_satisfied=score["required_categories_satisfied"],
                acceptable_alternative_satisfied=score["acceptable_alternative_satisfied"],
                forbidden_category_observed=score["forbidden_category_observed"],
                forbidden_route_observed=score["forbidden_route_observed"],
                run_status="routing_completed",
                fresh_session_identity_sha256=hashlib.sha256(
                    task.task_id.encode("ascii")
                ).hexdigest(),
                prompt_sha256="1" * 64,
                projection_sha256="2" * 64,
                oracle_proof_manifest_sha256="3" * 64,
                action_proof_sha256="4" * 64,
                recovery_proof_sha256="5" * 64,
                workspace_integrity="unchanged",
                answer_key_or_oracle_exposure_detected=False,
                session_reused=False,
                yolo_inheritance_detected=False,
                pa2_pa3_proofs_verified=True,
                source_contract_projection_verified=True,
                duration_seconds=1.0,
                usage=PhysicsBenchmarkUsageV1(
                    availability="unavailable",
                    input_tokens=None,
                    output_tokens=None,
                ),
            )
        )
    return tuple(records)


def test_gl_pilot_preparation_is_explicit_bounded_and_separated() -> None:
    config = load_physics_gl_pilot_config(CONFIG_PATH)
    fixture_hashes = validate_physics_gl_pilot(
        config,
        repository_root=ROOT,
        config_path=CONFIG_PATH,
        source_repository_root=GL_SOURCE,
    )

    assert config.source_commit == "7d04b5b9882dcd476c1457b8d711ac7b5520b2c1"
    assert len(config.tasks) == 10
    assert {item.topic for item in config.tasks} == REQUIRED_GL_PILOT_TOPICS
    assert config.production_mutation_allowed is False
    assert config.open_research_questions_allowed is False
    assert len(fixture_hashes) == 10
    assert all(item.source_refs for item in config.tasks)
    assert all(
        item.expected_route == "require_human_review"
        for item in config.tasks
        if item.human_review_mandatory
    )


def test_scripted_gl_pilot_keeps_clean_and_human_routes_distinct() -> None:
    config = load_physics_gl_pilot_config(CONFIG_PATH)
    report = aggregate_physics_gl_pilot(config, _scripted_records())

    assert report.outcome == "completed_bounded"
    assert report.run_count == 10
    assert report.matched_route_count == 10
    assert report.pass_route_count == 6
    assert report.human_review_route_count == 4
    assert report.all_mandatory_human_routes_satisfied
    assert report.zero_workspace_mutations
    assert report.zero_authority_exposure
    assert report.zero_session_reuse_or_yolo
    assert "claim a GL mode" in " ".join(report.limitations)


def test_gl_pilot_validation_cli_never_launches_a_model() -> None:
    result = CLI.invoke(
        app,
        [
            "validate-gl-pilot",
            "--config",
            str(CONFIG_PATH),
            "--workspace",
            str(ROOT),
            "--source-workspace",
            str(GL_SOURCE),
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["task_count"] == 10
    assert payload["model_launched"] is False
    assert payload["production_mutation_allowed"] is False
    assert payload["open_research_questions_allowed"] is False


@pytest.mark.skipif(not BWRAP.is_file(), reason="Bubblewrap PA-2 path unavailable")
def test_gl_pilot_executor_is_sequential_fresh_and_idempotent(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    pilot_copy = workspace / "examples/physics_auditor/gl_pilot_v1"
    pilot_copy.parent.mkdir(parents=True)
    shutil.copytree(PILOT, pilot_copy)
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
        ("/usr/bin/git", "-C", workspace, "commit", "-qm", "fixture"),
        check=True,
    )
    copied_config_path = pilot_copy / "config/pilot.json"
    config = load_physics_gl_pilot_config(copied_config_path)
    sessions: list[str] = []

    def invoker(task: Any) -> Any:
        report = json.dumps(_pilot_report(task), sort_keys=True).encode("utf-8")

        def invoke(**kwargs: Any) -> PhysicsAuditorCodexRun:
            prepared = kwargs["prepared"]
            executable = Path(kwargs["codex_executable"])
            session = f"fresh-pa5b-pilot-{task.task_id}"
            sessions.append(session)
            return PhysicsAuditorCodexRun(
                adapter_result=CodexRunResult(
                    run_id=prepared.request.run_id,
                    status="succeeded",
                    exit_code=0,
                    started_at="2026-01-01T00:00:00Z",
                    ended_at="2026-01-01T00:00:01Z",
                    duration_seconds=1.0,
                    artifact_directory="/synthetic/fake-pa5b-gl-pilot-action",
                    event_count=1,
                    malformed_event_count=0,
                    final_message_present=True,
                    permission_evidence=False,
                    summary="Scripted bounded GL pilot audit completed.",
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

    output = tmp_path / "pilot-actions"
    kwargs = {
        "config": config,
        "config_path": copied_config_path,
        "execution_config_path": workspace
        / "examples/physics_auditor/synthetic/execution-config.yaml",
        "repository_root": workspace,
        "source_repository_root": GL_SOURCE,
        "output_directory": output,
        "codex_invokers": {task.task_id: invoker(task) for task in config.tasks},
    }
    interruption_points = (
        "gl_authority_and_source_verified",
        "task_001:exact_source_workspace_verified",
        "task_001:pilot_oracle:before_pa2_resume_or_launch",
        "task_001:pilot_oracle:after_pa2_proof_reverification",
        "task_001:before_pa3_resume_or_launch",
        "task_001:after_pa3_completion",
        "task_001:after_pa3_proof_reverification",
        "task_001:after_recovery_proof_finalization",
        "task_001:after_record_finalization",
    )
    interrupted: set[str] = set()
    for point in interruption_points:

        def interrupt_once(name: str, *, expected: str = point) -> None:
            if name == expected and name not in interrupted:
                interrupted.add(name)
                raise RuntimeError(f"synthetic interruption at {name}")

        with pytest.raises(RuntimeError, match="synthetic interruption"):
            run_bounded_physics_gl_pilot(**kwargs, checkpoint=interrupt_once)

    first = run_bounded_physics_gl_pilot(**kwargs)
    second = run_bounded_physics_gl_pilot(**kwargs)

    assert first == second
    assert interrupted == set(interruption_points)
    assert first.outcome == "completed_bounded"
    assert first.run_count == 10
    assert len(sessions) == 10
    assert len(set(sessions)) == 10
    assert all(not item.session_reused for item in first.records)
    assert all(item.pa2_pa3_proofs_verified for item in first.records)
