from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from research_automation_supervisor.physics_workflow import PhysicsWorkflowServices
from research_automation_supervisor.workflow_engine import WorkflowServices, run_substage
from tests.test_physics_benchmark import (
    CATALOG_PATH,
    ROOT,
    _scripted_report,
)
from tests.test_physics_workflow import (
    BWRAP,
    PYTHON,
    ScriptedPhysicsAuditor,
    _physics_tree,
    _result_state,
)
from tests.workflow_helpers import (
    auditor_result,
    codex_response,
    git,
    worker_result,
)

CASES = {
    "wrong_sign": (
        "pa5b_case_002",
        "def acceleration(force: float) -> float:\n    return force\n",
        "implementation.acceleration(2.0) == 2.0",
    ),
    "missing_normalization": (
        "pa5b_case_003",
        "import math\n\ndef density(x: float, sigma: float) -> float:\n"
        "    return math.exp(-(x*x)/(2*sigma*sigma))/(math.sqrt(2*math.pi)*sigma)\n",
        "abs(implementation.density(0.0, 1.0) - 0.3989422804014327) < 1e-12",
    ),
    "missing_metric_factor": (
        "pa5b_case_004",
        "def covector_norm_sq(a_r: float, a_theta: float, r: float) -> float:\n"
        "    return a_r*a_r + r*r*a_theta*a_theta\n",
        "implementation.covector_norm_sq(0.0, 1.0, 2.0) == 4.0",
    ),
    "finite_difference_stencil": (
        "pa5b_case_011",
        "def centered(left: float, right: float, h: float) -> float:\n"
        "    return (right-left)/(2*h)\n",
        "implementation.centered(0.0, 2.0, 1.0) == 1.0",
    ),
}


def _rewrite_evidence_paths(value: object) -> None:
    if isinstance(value, dict):
        if value.get("kind") == "document" and value.get("path") is not None:
            value["path"] = "derivation.md"
        for item in value.values():
            _rewrite_evidence_paths(item)
    elif isinstance(value, list):
        for item in value:
            _rewrite_evidence_paths(item)


def _passing_report(value: dict[str, Any]) -> dict[str, Any]:
    result = json.loads(json.dumps(value))
    result.update(
        {
            "verdict": "pass",
            "evidence_sufficiency": "sufficient",
            "summary": "The bounded repair now satisfies every locked check.",
            "human_gate_triggers": [],
            "findings": [],
            "unresolved_questions": [],
        }
    )
    for check in result["checks"]:
        check.update(
            {
                "status": "passed",
                "evidence_sufficiency": "sufficient",
                "rationale": "The refreshed evidence verifies the repaired implementation.",
            }
        )
    return result


def _install_case_authority(
    *,
    project: Path,
    case: Any,
    oracle_expression: str,
) -> None:
    source_root = ROOT / case.fixture_root
    (project / "implementation.py").write_bytes((source_root / "implementation.py").read_bytes())
    (project / "derivation.md").write_bytes((source_root / "evidence.md").read_bytes())
    contract = yaml.safe_load((ROOT / case.contract_path).read_text())
    for evidence in contract["evidence"]:
        evidence["path"] = (
            "implementation.py" if evidence["id"] == "implementation_source" else "derivation.md"
        )
    contract_path = project / "control/physics-contract.yaml"
    contract_path.write_text(yaml.safe_dump(contract, sort_keys=False), encoding="utf-8")
    oracle_source = f'''import importlib.util
import json
from pathlib import Path

spec = importlib.util.spec_from_file_location("implementation", Path("implementation.py"))
assert spec is not None and spec.loader is not None
implementation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(implementation)
checks = {{"control": True, "primary": bool({oracle_expression})}}
print(json.dumps({{
    "schema_version": 1,
    "oracle_id": "case_oracle",
    "outcome": "passed" if all(checks.values()) else "functional_failure",
    "checks": [{{"id": key, "passed": value}} for key, value in sorted(checks.items())],
}}, sort_keys=True))
'''
    oracle_path = project / "tools/oracle.py"
    oracle_path.write_text(oracle_source, encoding="utf-8")
    catalog = {
        "schema_version": 1,
        "catalog_id": "pa5b-repair-calibration",
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
                "id": "case_oracle",
                "executable": {
                    "schema_version": 1,
                    "policy": "isolated_system_python_v1",
                    "path": str(PYTHON),
                    "sha256": hashlib.sha256(PYTHON.read_bytes()).hexdigest(),
                },
                "program": {
                    "path": "tools/oracle.py",
                    "sha256": hashlib.sha256(oracle_path.read_bytes()).hexdigest(),
                },
                "argv": [str(PYTHON), "-I", "-S", "-B", "tools/oracle.py"],
                "execution_policy": {
                    "schema_version": 1,
                    "policy_id": "pa5b-repair-oracle",
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
        json.dumps(catalog, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    git(project, "add", ".")
    git(project, "commit", "-q", "-m", f"install {case.seed_kind} calibration")


@pytest.mark.skipif(not BWRAP.is_file(), reason="Bubblewrap PA-2 path unavailable")
@pytest.mark.parametrize("seed_kind", tuple(CASES))
def test_four_bounded_pa4_worker_repairs_refresh_all_evidence(
    tmp_path: Path,
    seed_kind: str,
) -> None:
    catalog = json.loads(CATALOG_PATH.read_text())
    catalog_cases = {item["case_id"]: item for item in catalog["cases"]}
    case_id, clean_source, oracle_expression = CASES[seed_kind]
    from research_automation_supervisor.physics_benchmark_models import (
        PhysicsBenchmarkCaseAuthorityV1,
    )

    case = PhysicsBenchmarkCaseAuthorityV1.model_validate(catalog_cases[case_id])
    initial = _scripted_report(case)
    _rewrite_evidence_paths(initial)
    repaired = _passing_report(initial)
    initial_path = tmp_path / "initial-report.json"
    repaired_path = tmp_path / "repaired-report.json"
    initial_path.write_text(json.dumps(initial, indent=2, sort_keys=True) + "\n")
    repaired_path.write_text(json.dumps(repaired, indent=2, sort_keys=True) + "\n")
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
    spec, project, fake_codex = _physics_tree(tmp_path, responses=responses)
    _install_case_authority(project=project, case=case, oracle_expression=oracle_expression)
    physics = ScriptedPhysicsAuditor([initial_path, repaired_path])

    result = run_substage(
        spec,
        runs_dir=tmp_path / "runs",
        services=WorkflowServices(
            codex_executable=str(fake_codex),
            token_factory=lambda: f"pa5b-repair-{seed_kind}",
        ),
        physics_services=PhysicsWorkflowServices(physics_auditor_codex_invoker=physics),
    )
    state = _result_state(result)

    assert result.status == "completed"
    assert result.repair_round == 1
    assert result.worker_thread_id == "worker-thread-1"
    assert physics.calls == 2
    assert len(set(state.prior_physics_auditor_thread_ids)) == 2
    assert state.invalidated_oracle_ids == ("case_oracle",)
    assert len(state.historical_oracle_evidence) == 1
    assert state.historical_oracle_evidence[0].status == "functional_failure"
    assert state.oracle_evidence[0].repair_round == 1
    assert state.oracle_evidence[0].status == "passed"
    assert state.current_workspace_identity_sha256 is not None
    assert Path(project / "implementation.py").read_text() == clean_source
    prompt = json.loads(Path(state.repair_prompt_path or "").read_text())
    assert prompt["findings"][0]["id"] == "finding_1"
    assert prompt["findings"][0]["required_repair"] == (
        "Follow the frozen deterministic route."
    )
