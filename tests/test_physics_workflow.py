from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from research_automation_supervisor.cli import app
from research_automation_supervisor.codex_models import CodexRunResult
from research_automation_supervisor.errors import WorkflowInputError, WorkflowStateError
from research_automation_supervisor.physics_auditor_execution import PhysicsAuditorCodexRun
from research_automation_supervisor.physics_oracle_execution import run_physics_oracle
from research_automation_supervisor.physics_workflow import PhysicsWorkflowServices
from research_automation_supervisor.physics_workflow_models import (
    PHYSICS_JOURNAL_SEMANTIC_FORMS_V2,
    PhysicsWorkflowStateV2,
)
from research_automation_supervisor.workflow_engine import (
    JOURNAL_SEMANTIC_FORMS,
    WorkflowServices,
    continue_substage,
    resume_substage,
    run_substage,
    substage_status,
)
from research_automation_supervisor.workflow_models import WorkflowState
from tests.workflow_helpers import (
    auditor_result,
    codex_response,
    create_workflow_tree,
    git,
    worker_result,
)

ROOT = Path(__file__).parents[1]
SYNTHETIC = ROOT / "examples/physics_auditor/synthetic"
PYTHON = Path("/usr/bin/python3").resolve(strict=True)
BWRAP = Path("/usr/bin/bwrap")
CLI = CliRunner()


class ScriptedPhysicsAuditor:
    def __init__(self, reports: Path | list[Path], *, status: str = "succeeded") -> None:
        self.reports = [reports] if isinstance(reports, Path) else reports
        self.status = status
        self.calls = 0

    def __call__(self, **kwargs: Any) -> PhysicsAuditorCodexRun:
        self.calls += 1
        prepared = kwargs["prepared"]
        executable = Path(kwargs["codex_executable"])
        session = f"fresh-physics-session-{self.calls}"
        report = self.reports[min(self.calls - 1, len(self.reports) - 1)]
        return PhysicsAuditorCodexRun(
            adapter_result=CodexRunResult(
                run_id=prepared.request.run_id,
                status=self.status,
                exit_code=0 if self.status == "succeeded" else 1,
                started_at="2026-01-01T00:00:00Z",
                ended_at="2026-01-01T00:00:01Z",
                duration_seconds=1.0,
                artifact_directory="/synthetic/fake-codex-action",
                event_count=1,
                malformed_event_count=0,
                final_message_present=True,
                permission_evidence=False,
                summary="Scripted Physics Auditor completed.",
                error=None if self.status == "succeeded" else "scripted failure",
            ),
            model_output=report.read_bytes(),
            model_output_truncated=False,
            provider_session_id=session,
            provider_thread_started_ids=(session,),
            backend_policy_evidence_sha256=hashlib.sha256(b"fake-policy").hexdigest(),
            bubblewrap_backend_identity_sha256=hashlib.sha256(b"fake-bubblewrap").hexdigest(),
            codex_executable_sha256=hashlib.sha256(executable.read_bytes()).hexdigest(),
            codex_cli_version="scripted-test-v1",
        )


def _physics_tree(
    tmp_path: Path,
    *,
    case: str = "clean",
    responses: list[dict[str, object]] | None = None,
    max_repair_rounds: int = 2,
) -> tuple[Path, Path, Path]:
    spec_path, project, fake_codex = create_workflow_tree(
        tmp_path,
        responses=responses,
        max_repair_rounds=max_repair_rounds,
    )
    shutil.copyfile(SYNTHETIC / case / "implementation.py", project / "implementation.py")
    shutil.copyfile(SYNTHETIC / case / "derivation.md", project / "derivation.md")
    shutil.copyfile(SYNTHETIC / "clean/oracle.py", project / "tools/oracle.py")
    shutil.copyfile(SYNTHETIC / "contract.yaml", project / "control/physics-contract.yaml")
    shutil.copyfile(
        SYNTHETIC / "execution-config.yaml",
        project / "control/physics-auditor.yaml",
    )
    catalog = {
        "schema_version": 1,
        "catalog_id": "pa4-synthetic-catalog",
        "environment_profiles": [
            {
                "schema_version": 1,
                "id": "minimal-python",
                "profile": "minimal_python_v1",
            }
        ],
        "intents": [
            {
                "schema_version": 1,
                "id": "force_oracle",
                "executable": {
                    "schema_version": 1,
                    "policy": "isolated_system_python_v1",
                    "path": str(PYTHON),
                    "sha256": hashlib.sha256(PYTHON.read_bytes()).hexdigest(),
                },
                "program": {
                    "path": "tools/oracle.py",
                    "sha256": hashlib.sha256(
                        (project / "tools/oracle.py").read_bytes()
                    ).hexdigest(),
                },
                "argv": [str(PYTHON), "-I", "-S", "-B", "tools/oracle.py"],
                "execution_policy": {
                    "schema_version": 1,
                    "policy_id": "pa4-synthetic-offline",
                    "isolation_backend": "bubblewrap_unshare_all_v1",
                    "working_directory": "workspace_root",
                    "workspace_access": "read_only",
                    "scratch_output": "scratch_only",
                    "network": "disabled",
                    "environment_profile_id": "minimal-python",
                    "timeout_seconds": 30,
                    "max_stdout_bytes": 65536,
                    "max_stderr_bytes": 65536,
                    "accepted_exit_codes": [0],
                    "structured_output_schema": "physics_oracle_result_v1",
                    "required_artifacts": [],
                },
            }
        ],
    }
    (project / "control/oracle-catalog.json").write_text(
        json.dumps(catalog, indent=2, sort_keys=True) + "\n", encoding="ascii"
    )
    git(project, "add", ".")
    git(project, "commit", "-q", "-m", "add synthetic physics authority")
    specification = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    specification.update(
        {
            "schema_version": 2,
            "physics_contract_path": "../project/control/physics-contract.yaml",
            "physics": {
                "schema_version": 1,
                "enabled": True,
                "required": True,
                "trusted_oracle_catalog_path": ("../project/control/oracle-catalog.json"),
                "auditor_execution_config_path": ("../project/control/physics-auditor.yaml"),
                "max_repair_rounds": 2,
                "human_review_triggers": [
                    "convention_change",
                    "unresolved_gauge_constraint_ambiguity",
                    "new_physical_interpretation",
                    "conflicting_evidence",
                    "contract_weakening_attempt",
                ],
                "insufficient_evidence_policy": "block",
                "conflicting_evidence_policy": "human_review",
                "medium_finding_policy": "request_repair",
                "low_finding_policy": "allow_pass",
            },
        }
    )
    specification["allowed_paths"] = ["src/**", "implementation.py", "derivation.md"]
    specification["physics"]["max_repair_rounds"] = max_repair_rounds
    spec_path.write_text(yaml.safe_dump(specification, sort_keys=False), encoding="utf-8")
    return spec_path, project, fake_codex


def _result_state(result: Any) -> PhysicsWorkflowStateV2:
    return PhysicsWorkflowStateV2.model_validate(
        json.loads((Path(result.artifact_directory) / "state.json").read_text())
    )


def _insufficient_report(tmp_path: Path) -> Path:
    value = json.loads((SYNTHETIC / "reports/insufficient_evidence.json").read_text())
    value["checks"][0].update(
        {
            "status": "passed",
            "evidence_sufficiency": "sufficient",
            "rationale": "The current required oracle proof passes.",
        }
    )
    value["checks"][2].update(
        {
            "status": "passed",
            "evidence_sufficiency": "sufficient",
            "evidence": [
                {
                    "kind": "oracle",
                    "reference": "force_oracle",
                    "path": None,
                    "line_start": None,
                    "line_end": None,
                }
            ],
            "rationale": "The current zero-force proof passes.",
        }
    )
    value["findings"][0]["check_ids"] = ["check_signed_force"]
    value["findings"][0]["statement"] = "Independent mapping evidence is absent."
    value["findings"][0]["required_action"] = "Supply the declared independent mapping evidence."
    path = tmp_path / "insufficient-current-oracle.json"
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    return path


class CountingOracleRunner:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, **kwargs: Any) -> Any:
        self.calls += 1
        return run_physics_oracle(**kwargs)


class CrashOnce:
    def __init__(self, target: str) -> None:
        self.target = target
        self.crashed = False

    def __call__(self, name: str) -> None:
        if name == self.target and not self.crashed:
            self.crashed = True
            raise RuntimeError(f"injected crash at {name}")


def test_v2_models_and_journal_are_disjoint_from_frozen_v1() -> None:
    assert PhysicsWorkflowStateV2 is not WorkflowState
    assert PHYSICS_JOURNAL_SEMANTIC_FORMS_V2.isdisjoint(JOURNAL_SEMANTIC_FORMS)
    assert all(len(item) == 5 for item in PHYSICS_JOURNAL_SEMANTIC_FORMS_V2)


def test_physics_validate_and_review_cli_surfaces_are_explicit(tmp_path: Path) -> None:
    spec, _, _ = _physics_tree(tmp_path)

    validated = CLI.invoke(app, ["validate-substage", str(spec), "--json"])
    help_result = CLI.invoke(app, ["review-physics-substage", "--help"])

    assert validated.exit_code == 0
    payload = json.loads(validated.stdout)
    assert payload["physics"] == {
        "enabled": True,
        "required": True,
        "required_oracle_ids": ["force_oracle"],
    }
    assert help_result.exit_code == 0
    assert "PhysicsReviewDecisionV1" in help_result.stdout


@pytest.mark.skipif(not BWRAP.is_file(), reason="Bubblewrap PA-2 path unavailable")
def test_physics_enabled_clean_pass_completes_through_versioned_dispatch(
    tmp_path: Path,
) -> None:
    spec, _, fake_codex = _physics_tree(tmp_path)
    physics = ScriptedPhysicsAuditor(SYNTHETIC / "reports/clean.json")

    result = run_substage(
        spec,
        runs_dir=tmp_path / "runs",
        services=WorkflowServices(
            codex_executable=str(fake_codex), token_factory=lambda: "physics-clean"
        ),
        physics_services=PhysicsWorkflowServices(physics_auditor_codex_invoker=physics),
    )

    assert result.schema_version == 2
    assert result.status == "completed"
    assert result.tests_passed and result.code_auditor_passed
    assert result.required_oracle_proofs_verified
    assert result.physics_route == "pass"
    assert result.final_workspace_identity_sha256 is not None
    assert physics.calls == 1
    state = _result_state(result)
    assert state.worker_thread_id not in state.prior_physics_auditor_thread_ids
    role_policy = json.loads(
        (
            Path(state.physics_auditor_action_directory or "") / "control/codex-role-policy.json"
        ).read_text()
    )
    bubblewrap_policy = json.loads(
        (
            Path(state.physics_auditor_action_directory or "") / "control/bubblewrap-policy.json"
        ).read_text()
    )
    assert bubblewrap_policy["session_policy"] == "fresh_ephemeral_no_resume"
    assert role_policy["resume_allowed"] is False
    assert role_policy["danger_full_access_allowed"] is False
    assert role_policy["workspace_write_allowed"] is False
    assert substage_status(Path(result.artifact_directory)) == result


@pytest.mark.skipif(not BWRAP.is_file(), reason="Bubblewrap PA-2 path unavailable")
def test_repairable_sign_error_reuses_worker_refreshes_proof_and_fresh_audit(
    tmp_path: Path,
) -> None:
    clean_source = (SYNTHETIC / "clean/implementation.py").read_text()
    responses = [
        codex_response("worker", "worker-thread-1", worker_result()),
        codex_response("auditor", "code-audit-1", auditor_result()),
        codex_response(
            "worker",
            "worker-thread-1",
            worker_result(),
            expected_resume_thread_id="worker-thread-1",
            write_files={"implementation.py": clean_source},
        ),
        codex_response("auditor", "code-audit-2", auditor_result()),
    ]
    spec, _, fake_codex = _physics_tree(tmp_path, case="sign_error", responses=responses)
    physics = ScriptedPhysicsAuditor(
        [SYNTHETIC / "reports/sign_error.json", SYNTHETIC / "reports/clean.json"]
    )

    result = run_substage(
        spec,
        runs_dir=tmp_path / "runs",
        services=WorkflowServices(
            codex_executable=str(fake_codex), token_factory=lambda: "physics-repair"
        ),
        physics_services=PhysicsWorkflowServices(physics_auditor_codex_invoker=physics),
    )
    state = _result_state(result)

    assert result.status == "completed"
    assert result.repair_round == 1
    assert result.worker_thread_id == "worker-thread-1"
    assert physics.calls == 2
    assert len(set(state.prior_physics_auditor_thread_ids)) == 2
    assert state.invalidated_oracle_ids == ("force_oracle",)
    assert len(state.historical_oracle_evidence) == 1
    assert state.historical_oracle_evidence[0].status == "functional_failure"
    assert state.oracle_evidence[0].repair_round == 1
    prompt = Path(state.repair_prompt_path or "").read_text()
    assert "finding_sign" in prompt
    assert "Restore the frozen positive-force sign." in prompt
    assert "chain of thought" not in prompt.casefold()
    assert "authoritative_route" not in prompt
    assert "decision_kind" not in prompt
    assert "reason_codes" not in prompt
    assert "repair_round" not in prompt
    assert set(json.loads(prompt)) == {"findings"}


@pytest.mark.skipif(not BWRAP.is_file(), reason="Bubblewrap PA-2 path unavailable")
@pytest.mark.parametrize(
    ("report_name", "reason_code"),
    [
        ("convention_change.json", "convention_change_requires_human"),
        ("gauge_ambiguity.json", "gauge_constraint_ambiguity_requires_human"),
    ],
)
def test_mandatory_scientific_conditions_create_durable_human_pause(
    tmp_path: Path,
    report_name: str,
    reason_code: str,
) -> None:
    spec, _, fake_codex = _physics_tree(tmp_path)
    result = run_substage(
        spec,
        runs_dir=tmp_path / "runs",
        services=WorkflowServices(
            codex_executable=str(fake_codex), token_factory=lambda: f"human-{report_name[:4]}"
        ),
        physics_services=PhysicsWorkflowServices(
            physics_auditor_codex_invoker=ScriptedPhysicsAuditor(
                SYNTHETIC / "reports" / report_name
            )
        ),
    )
    state = _result_state(result)

    assert result.status == "human_review_paused"
    assert result.physics_route == "require_human_review"
    assert reason_code in state.physics_reason_codes
    assert state.human_review_packet_path is not None
    assert Path(state.human_review_packet_path).is_file()


@pytest.mark.skipif(not BWRAP.is_file(), reason="Bubblewrap PA-2 path unavailable")
def test_insufficient_evidence_pauses_without_worker_repair(tmp_path: Path) -> None:
    spec, _, fake_codex = _physics_tree(tmp_path)
    result = run_substage(
        spec,
        runs_dir=tmp_path / "runs",
        services=WorkflowServices(
            codex_executable=str(fake_codex), token_factory=lambda: "evidence-pause"
        ),
        physics_services=PhysicsWorkflowServices(
            physics_auditor_codex_invoker=ScriptedPhysicsAuditor(_insufficient_report(tmp_path))
        ),
    )

    assert result.status == "evidence_paused"
    assert result.physics_route == "block_insufficient_evidence"
    assert result.repair_round == 0
    assert result.pause_reason == "physics_evidence_insufficient"


@pytest.mark.skipif(not BWRAP.is_file(), reason="Bubblewrap PA-2 path unavailable")
def test_repair_limit_and_transport_infrastructure_are_distinct(tmp_path: Path) -> None:
    repair_spec, _, repair_fake = _physics_tree(
        tmp_path / "repair", case="sign_error", max_repair_rounds=0
    )
    repair = run_substage(
        repair_spec,
        runs_dir=tmp_path / "repair-runs",
        services=WorkflowServices(
            codex_executable=str(repair_fake), token_factory=lambda: "repair-limit"
        ),
        physics_services=PhysicsWorkflowServices(
            physics_auditor_codex_invoker=ScriptedPhysicsAuditor(
                SYNTHETIC / "reports/sign_error.json"
            )
        ),
    )
    infra_spec, _, infra_fake = _physics_tree(tmp_path / "infra")
    infrastructure = run_substage(
        infra_spec,
        runs_dir=tmp_path / "infra-runs",
        services=WorkflowServices(
            codex_executable=str(infra_fake), token_factory=lambda: "infra-stop"
        ),
        physics_services=PhysicsWorkflowServices(
            physics_auditor_codex_invoker=ScriptedPhysicsAuditor(
                SYNTHETIC / "reports/clean.json", status="process_failed"
            )
        ),
    )

    assert repair.status == "repair_limit_paused"
    assert repair.pause_reason == "physics_repair_limit_exhausted"
    assert infrastructure.status == "infrastructure_stopped"
    assert infrastructure.pause_reason == "workflow_infrastructure_failure"
    assert "not blamed" in infrastructure.summary


@pytest.mark.skipif(not BWRAP.is_file(), reason="Bubblewrap PA-2 path unavailable")
def test_additional_evidence_decision_resumes_same_worker_and_preserves_valid_proof(
    tmp_path: Path,
) -> None:
    responses = [
        codex_response("worker", "worker-thread-1", worker_result()),
        codex_response("auditor", "code-audit-1", auditor_result()),
        codex_response(
            "worker",
            "worker-thread-1",
            worker_result(),
            expected_resume_thread_id="worker-thread-1",
        ),
        codex_response("auditor", "code-audit-2", auditor_result()),
    ]
    spec, _, fake_codex = _physics_tree(tmp_path, responses=responses)
    physics = ScriptedPhysicsAuditor(
        [_insufficient_report(tmp_path), SYNTHETIC / "reports/clean.json"]
    )
    oracle = CountingOracleRunner()
    software_services = WorkflowServices(
        codex_executable=str(fake_codex), token_factory=lambda: "additional-evidence"
    )
    physics_services = PhysicsWorkflowServices(
        oracle_runner=oracle,
        physics_auditor_codex_invoker=physics,
    )
    paused = run_substage(
        spec,
        runs_dir=tmp_path / "runs",
        services=software_services,
        physics_services=physics_services,
    )
    paused_state = _result_state(paused)
    assert paused_state.human_review_packet_sha256 is not None
    decision = {
        "schema_version": 1,
        "run_token": paused.run_token,
        "review_packet_sha256": paused_state.human_review_packet_sha256,
        "decision": "request_additional_evidence",
        "reason": "Rerun with the now-confirmed declared mapping evidence.",
        "acknowledged_finding_ids": ["finding_missing_oracle"],
        "acknowledged_question_ids": [],
        "evidence_references": ["implementation_source"],
    }
    decision_path = tmp_path / "decision.yaml"
    decision_path.write_text(yaml.safe_dump(decision, sort_keys=False))

    completed = continue_substage(
        Path(paused.artifact_directory),
        decision_path,
        services=software_services,
        physics_services=physics_services,
    )
    state = _result_state(completed)

    assert completed.status == "completed"
    assert completed.repair_round == 1
    assert completed.worker_thread_id == "worker-thread-1"
    assert physics.calls == 2
    assert oracle.calls == 1
    assert state.preserved_oracle_ids == ("force_oracle",)
    assert state.invalidated_oracle_ids == ()
    assert state.oracle_evidence[0].repair_round == 0


@pytest.mark.skipif(not BWRAP.is_file(), reason="Bubblewrap PA-2 path unavailable")
@pytest.mark.parametrize(
    ("decision_name", "expected_status", "expected_reason"),
    [
        ("reject_candidate", "aborted", "human_rejected_candidate"),
        ("revise_contract", "aborted", "revised_contract_requires_new_run"),
        ("accept_with_caveat", "human_review_paused", "caveat_is_not_release_authority"),
    ],
)
def test_terminal_and_caveat_human_decisions_are_durable(
    tmp_path: Path,
    decision_name: str,
    expected_status: str,
    expected_reason: str,
) -> None:
    spec, _, fake_codex = _physics_tree(tmp_path)
    services = WorkflowServices(
        codex_executable=str(fake_codex), token_factory=lambda: "human-decision"
    )
    physics_services = PhysicsWorkflowServices(
        physics_auditor_codex_invoker=ScriptedPhysicsAuditor(
            SYNTHETIC / "reports/convention_change.json"
        )
    )
    paused = run_substage(
        spec,
        runs_dir=tmp_path / "runs",
        services=services,
        physics_services=physics_services,
    )
    state = _result_state(paused)
    decision_path = tmp_path / "decision.yaml"
    decision_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "run_token": paused.run_token,
                "review_packet_sha256": state.human_review_packet_sha256,
                "decision": decision_name,
                "reason": "Bounded human scientific decision.",
            },
            sort_keys=False,
        )
    )

    result = continue_substage(
        Path(paused.artifact_directory),
        decision_path,
        services=services,
        physics_services=physics_services,
    )
    durable = _result_state(result)

    assert result.status == expected_status
    assert result.pause_reason == expected_reason
    assert durable.human_decision_path is not None
    assert durable.human_decision_sha256 is not None


@pytest.mark.skipif(not BWRAP.is_file(), reason="Bubblewrap PA-2 path unavailable")
def test_approve_existing_contract_repairs_with_same_worker_and_fresh_audit(
    tmp_path: Path,
) -> None:
    responses = [
        codex_response("worker", "worker-thread-1", worker_result()),
        codex_response("auditor", "code-audit-1", auditor_result()),
        codex_response(
            "worker",
            "worker-thread-1",
            worker_result(),
            expected_resume_thread_id="worker-thread-1",
        ),
        codex_response("auditor", "code-audit-2", auditor_result()),
    ]
    spec, _, fake_codex = _physics_tree(tmp_path, responses=responses)
    physics = ScriptedPhysicsAuditor(
        [SYNTHETIC / "reports/convention_change.json", SYNTHETIC / "reports/clean.json"]
    )
    services = WorkflowServices(
        codex_executable=str(fake_codex), token_factory=lambda: "approve-contract"
    )
    physics_services = PhysicsWorkflowServices(physics_auditor_codex_invoker=physics)
    paused = run_substage(
        spec,
        runs_dir=tmp_path / "runs",
        services=services,
        physics_services=physics_services,
    )
    paused_state = _result_state(paused)
    decision_path = tmp_path / "approve.yaml"
    decision_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "run_token": paused.run_token,
                "review_packet_sha256": paused_state.human_review_packet_sha256,
                "decision": "approve_existing_contract",
                "reason": "Keep the frozen convention and repair only within it.",
            },
            sort_keys=False,
        )
    )

    completed = continue_substage(
        Path(paused.artifact_directory),
        decision_path,
        services=services,
        physics_services=physics_services,
    )
    state = _result_state(completed)

    assert completed.status == "completed"
    assert completed.worker_thread_id == "worker-thread-1"
    assert completed.repair_round == 1
    assert physics.calls == 2
    assert len(set(state.prior_physics_auditor_thread_ids)) == 2


@pytest.mark.skipif(not BWRAP.is_file(), reason="Bubblewrap PA-2 path unavailable")
def test_human_decision_must_bind_the_exact_current_packet(tmp_path: Path) -> None:
    spec, _, fake_codex = _physics_tree(tmp_path)
    services = WorkflowServices(
        codex_executable=str(fake_codex), token_factory=lambda: "decision-binding"
    )
    physics_services = PhysicsWorkflowServices(
        physics_auditor_codex_invoker=ScriptedPhysicsAuditor(
            SYNTHETIC / "reports/convention_change.json"
        )
    )
    paused = run_substage(
        spec,
        runs_dir=tmp_path / "runs",
        services=services,
        physics_services=physics_services,
    )
    decision_path = tmp_path / "unbound.yaml"
    decision_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "run_token": paused.run_token,
                "review_packet_sha256": "0" * 64,
                "decision": "reject_candidate",
                "reason": "This decision is deliberately not bound.",
            },
            sort_keys=False,
        )
    )

    with pytest.raises(WorkflowInputError, match="does not bind"):
        continue_substage(
            Path(paused.artifact_directory),
            decision_path,
            services=services,
            physics_services=physics_services,
        )


@pytest.mark.skipif(not BWRAP.is_file(), reason="Bubblewrap PA-2 path unavailable")
def test_attempt_to_weaken_frozen_physics_contract_reaches_human_gate(
    tmp_path: Path,
) -> None:
    responses = [
        codex_response(
            "worker",
            "worker-thread-1",
            worker_result(),
            write_files={"control/physics-contract.yaml": "schema_version: 1\n"},
        )
    ]
    spec, project, fake_codex = _physics_tree(tmp_path, responses=responses)
    original_contract = (project / "control/physics-contract.yaml").read_bytes()

    result = run_substage(
        spec,
        runs_dir=tmp_path / "runs",
        services=WorkflowServices(
            codex_executable=str(fake_codex), token_factory=lambda: "contract-weakening"
        ),
        physics_services=PhysicsWorkflowServices(
            physics_auditor_codex_invoker=ScriptedPhysicsAuditor(SYNTHETIC / "reports/clean.json")
        ),
    )

    assert result.status == "human_review_paused"
    assert result.pause_reason == "contract_weakening_attempt"
    assert (project / "control/physics-contract.yaml").read_text() == "schema_version: 1\n"
    state = _result_state(result)
    assert state.human_review_packet_path is not None
    assert state.human_review_packet_sha256 is not None

    (project / "control/physics-contract.yaml").write_bytes(original_contract)
    decision_path = tmp_path / "reject-weakening.yaml"
    decision_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "run_token": result.run_token,
                "review_packet_sha256": state.human_review_packet_sha256,
                "decision": "reject_candidate",
                "reason": "Reject the attempted contract weakening.",
            },
            sort_keys=False,
        )
    )
    rejected = continue_substage(
        Path(result.artifact_directory),
        decision_path,
        services=WorkflowServices(codex_executable=str(fake_codex)),
        physics_services=PhysicsWorkflowServices(),
    )
    assert rejected.status == "aborted"
    assert rejected.pause_reason == "human_rejected_candidate"


@pytest.mark.skipif(not BWRAP.is_file(), reason="Bubblewrap PA-2 path unavailable")
def test_completed_workspace_mutation_and_oracle_proof_tampering_are_detected(
    tmp_path: Path,
) -> None:
    mutation_spec, mutation_project, mutation_fake = _physics_tree(tmp_path / "mutation")
    mutation = run_substage(
        mutation_spec,
        runs_dir=tmp_path / "mutation-runs",
        services=WorkflowServices(
            codex_executable=str(mutation_fake), token_factory=lambda: "workspace-mutation"
        ),
        physics_services=PhysicsWorkflowServices(
            physics_auditor_codex_invoker=ScriptedPhysicsAuditor(SYNTHETIC / "reports/clean.json")
        ),
    )
    (mutation_project / "implementation.py").write_text("# mutated after acceptance\n")
    with pytest.raises(WorkflowStateError, match="accepted physics workflow"):
        substage_status(Path(mutation.artifact_directory))

    tamper_spec, _, tamper_fake = _physics_tree(tmp_path / "tamper")
    tamper = run_substage(
        tamper_spec,
        runs_dir=tmp_path / "tamper-runs",
        services=WorkflowServices(
            codex_executable=str(tamper_fake), token_factory=lambda: "proof-tampering"
        ),
        physics_services=PhysicsWorkflowServices(
            physics_auditor_codex_invoker=ScriptedPhysicsAuditor(SYNTHETIC / "reports/clean.json")
        ),
    )
    tamper_state = _result_state(tamper)
    proof_path = Path(tamper_state.oracle_evidence[0].output_directory) / "completion-proof.json"
    proof_path.write_text(proof_path.read_text() + " ")
    with pytest.raises(WorkflowStateError, match="evidence was replaced"):
        substage_status(Path(tamper.artifact_directory))


@pytest.mark.skipif(not BWRAP.is_file(), reason="Bubblewrap PA-2 path unavailable")
def test_completed_status_recursively_reverifies_pa2_and_pa3_supporting_evidence(
    tmp_path: Path,
) -> None:
    spec, _, fake_codex = _physics_tree(tmp_path)
    completed = run_substage(
        spec,
        runs_dir=tmp_path / "runs",
        services=WorkflowServices(
            codex_executable=str(fake_codex), token_factory=lambda: "recursive-proof-check"
        ),
        physics_services=PhysicsWorkflowServices(
            physics_auditor_codex_invoker=ScriptedPhysicsAuditor(SYNTHETIC / "reports/clean.json")
        ),
    )
    state = _result_state(completed)
    oracle_record = (
        Path(state.oracle_evidence[0].output_directory) / "action-records/001-intent_accepted.json"
    )
    oracle_bytes = oracle_record.read_bytes()
    oracle_record.write_bytes(oracle_bytes + b" ")
    with pytest.raises(WorkflowStateError, match="action evidence no longer verifies"):
        substage_status(Path(completed.artifact_directory))
    oracle_record.write_bytes(oracle_bytes)

    provider = Path(state.physics_auditor_action_directory or "") / "provider-observation.json"
    provider.write_bytes(provider.read_bytes() + b" ")
    with pytest.raises(WorkflowStateError, match="action evidence no longer verifies"):
        substage_status(Path(completed.artifact_directory))


@pytest.mark.skipif(not BWRAP.is_file(), reason="Bubblewrap PA-2 path unavailable")
@pytest.mark.parametrize(
    "boundary",
    [
        "initial_snapshot:after_state_replacement",
        "initial_snapshot:after_result_replacement",
        "snapshot:after_result_replacement",
        "snapshot:after_state_replacement",
        "journal:software_action_intent",
        "journal:software_action_completed",
        "journal:software_gate_verified",
        "journal:physics_oracle_action_intent",
        "oracle_force_oracle:intent_accepted",
        "journal:physics_oracle_action_completed",
        "journal:oracle_evidence_refreshed",
        "journal:required_oracle_proofs_verified",
        "journal:physics_auditor_action_intent",
        "physics_auditor:action_accepted",
        "journal:physics_auditor_action_completed",
        "journal:physics_route_verified",
        "journal:physics_completion_gate_passed",
    ],
)
def test_clean_crash_boundaries_recover_exactly_once(tmp_path: Path, boundary: str) -> None:
    spec, _, fake_codex = _physics_tree(tmp_path)
    physics = ScriptedPhysicsAuditor(SYNTHETIC / "reports/clean.json")
    crash = CrashOnce(boundary)
    software_services = WorkflowServices(
        codex_executable=str(fake_codex), token_factory=lambda: "crash-clean"
    )
    physics_services = PhysicsWorkflowServices(
        physics_auditor_codex_invoker=physics,
        checkpoint=crash,
    )

    with pytest.raises(RuntimeError, match="injected crash"):
        run_substage(
            spec,
            runs_dir=tmp_path / "runs",
            services=software_services,
            physics_services=physics_services,
        )
    run_directory = tmp_path / "runs/minimal-substage-crash-clean"
    durable = _result_state(type("Run", (), {"artifact_directory": run_directory})())
    if durable.status == "completed":
        result = substage_status(run_directory)
    else:
        result = resume_substage(
            run_directory,
            services=software_services,
            physics_services=physics_services,
        )

    assert result.status == "completed"
    assert physics.calls == 1


@pytest.mark.skipif(not BWRAP.is_file(), reason="Bubblewrap PA-2 path unavailable")
@pytest.mark.parametrize(
    "boundary",
    [
        "journal:physics_repair_requested",
        "journal:physics_worker_repair_resumed",
        "journal:stale_oracle_evidence_invalidated",
    ],
)
def test_repair_routing_and_evidence_refresh_crashes_recover(tmp_path: Path, boundary: str) -> None:
    clean_source = (SYNTHETIC / "clean/implementation.py").read_text()
    responses = [
        codex_response("worker", "worker-thread-1", worker_result()),
        codex_response("auditor", "code-audit-1", auditor_result()),
        codex_response(
            "worker",
            "worker-thread-1",
            worker_result(),
            expected_resume_thread_id="worker-thread-1",
            write_files={"implementation.py": clean_source},
        ),
        codex_response("auditor", "code-audit-2", auditor_result()),
    ]
    spec, _, fake_codex = _physics_tree(tmp_path, case="sign_error", responses=responses)
    physics = ScriptedPhysicsAuditor(
        [SYNTHETIC / "reports/sign_error.json", SYNTHETIC / "reports/clean.json"]
    )
    crash = CrashOnce(boundary)
    software_services = WorkflowServices(
        codex_executable=str(fake_codex), token_factory=lambda: "crash-repair"
    )
    physics_services = PhysicsWorkflowServices(
        physics_auditor_codex_invoker=physics,
        checkpoint=crash,
    )

    with pytest.raises(RuntimeError, match="injected crash"):
        run_substage(
            spec,
            runs_dir=tmp_path / "runs",
            services=software_services,
            physics_services=physics_services,
        )
    result = resume_substage(
        tmp_path / "runs/minimal-substage-crash-repair",
        services=software_services,
        physics_services=physics_services,
    )

    assert result.status == "completed"
    assert result.repair_round == 1
    assert physics.calls == 2


@pytest.mark.skipif(not BWRAP.is_file(), reason="Bubblewrap PA-2 path unavailable")
def test_ambiguous_physics_auditor_launch_stops_without_retry(tmp_path: Path) -> None:
    spec, _, fake_codex = _physics_tree(tmp_path)
    physics = ScriptedPhysicsAuditor(SYNTHETIC / "reports/clean.json")
    crash = CrashOnce("physics_auditor:model_launch_attempted")
    software_services = WorkflowServices(
        codex_executable=str(fake_codex), token_factory=lambda: "ambiguous-audit"
    )
    physics_services = PhysicsWorkflowServices(
        physics_auditor_codex_invoker=physics,
        checkpoint=crash,
    )

    with pytest.raises(RuntimeError, match="injected crash"):
        run_substage(
            spec,
            runs_dir=tmp_path / "runs",
            services=software_services,
            physics_services=physics_services,
        )
    result = resume_substage(
        tmp_path / "runs/minimal-substage-ambiguous-audit",
        services=software_services,
        physics_services=physics_services,
    )

    assert result.status == "infrastructure_stopped"
    assert result.pause_reason == "workflow_infrastructure_failure"
    assert physics.calls == 0


@pytest.mark.skipif(not BWRAP.is_file(), reason="Bubblewrap PA-2 path unavailable")
@pytest.mark.parametrize(
    ("report_name", "boundary", "expected_status"),
    [
        (
            "convention_change.json",
            "journal:physics_human_review_required",
            "human_review_paused",
        ),
        (
            "insufficient_evidence.json",
            "journal:physics_evidence_insufficient",
            "evidence_paused",
        ),
    ],
)
def test_durable_pause_transition_crashes_need_no_relaunch(
    tmp_path: Path,
    report_name: str,
    boundary: str,
    expected_status: str,
) -> None:
    spec, _, fake_codex = _physics_tree(tmp_path)
    report = (
        _insufficient_report(tmp_path)
        if report_name == "insufficient_evidence.json"
        else SYNTHETIC / "reports" / report_name
    )
    physics = ScriptedPhysicsAuditor(report)
    crash = CrashOnce(boundary)
    services = WorkflowServices(
        codex_executable=str(fake_codex), token_factory=lambda: "pause-crash"
    )

    with pytest.raises(RuntimeError, match="injected crash"):
        run_substage(
            spec,
            runs_dir=tmp_path / "runs",
            services=services,
            physics_services=PhysicsWorkflowServices(
                physics_auditor_codex_invoker=physics,
                checkpoint=crash,
            ),
        )

    result = substage_status(tmp_path / "runs/minimal-substage-pause-crash")
    assert result.status == expected_status
    assert physics.calls == 1


@pytest.mark.skipif(not BWRAP.is_file(), reason="Bubblewrap PA-2 path unavailable")
def test_human_decision_journal_crash_recovers_the_same_decision_once(
    tmp_path: Path,
) -> None:
    spec, _, fake_codex = _physics_tree(tmp_path)
    physics = ScriptedPhysicsAuditor(SYNTHETIC / "reports/convention_change.json")
    services = WorkflowServices(
        codex_executable=str(fake_codex), token_factory=lambda: "decision-crash"
    )
    paused = run_substage(
        spec,
        runs_dir=tmp_path / "runs",
        services=services,
        physics_services=PhysicsWorkflowServices(physics_auditor_codex_invoker=physics),
    )
    state = _result_state(paused)
    decision_path = tmp_path / "reject.yaml"
    decision_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "run_token": paused.run_token,
                "review_packet_sha256": state.human_review_packet_sha256,
                "decision": "reject_candidate",
                "reason": "Reject after scientific review.",
            },
            sort_keys=False,
        )
    )
    crash_services = PhysicsWorkflowServices(
        physics_auditor_codex_invoker=physics,
        checkpoint=CrashOnce("journal:physics_human_decision_recorded"),
    )

    with pytest.raises(RuntimeError, match="injected crash"):
        continue_substage(
            Path(paused.artifact_directory),
            decision_path,
            services=services,
            physics_services=crash_services,
        )
    result = continue_substage(
        Path(paused.artifact_directory),
        decision_path,
        services=services,
        physics_services=crash_services,
    )

    assert result.status == "aborted"
    journal = (Path(result.artifact_directory) / "physics-journal-v2.jsonl").read_text()
    assert journal.count('"reason":"physics_human_decision_recorded"') == 1
