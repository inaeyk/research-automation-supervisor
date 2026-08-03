from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from research_automation_supervisor.codex_models import CodexRunResult
from research_automation_supervisor.durable_state import canonical_json
from research_automation_supervisor.errors import (
    PhysicsAuditorInputError,
    PhysicsAuditorIntegrityError,
)
from research_automation_supervisor.physics_auditor_execution import (
    PhysicsAuditorCodexRun,
    resume_physics_auditor,
    run_physics_auditor,
    verify_physics_auditor_action,
)
from research_automation_supervisor.physics_oracle_execution import run_physics_oracle

ROOT = Path(__file__).parents[1]
SYNTHETIC = ROOT / "examples/physics_auditor/synthetic"
BWRAP = Path("/usr/bin/bwrap")
PYTHON = Path("/usr/bin/python3").resolve(strict=True)
FAKE_CODEX = (ROOT / "tests/fixtures/fake_codex.py").resolve(strict=True)


def _git(workspace: Path, *arguments: str) -> None:
    subprocess.run(("/usr/bin/git", "-C", workspace, *arguments), check=True)


def _workspace(tmp_path: Path, case: str = "clean") -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for name in ("implementation.py", "derivation.md"):
        shutil.copyfile(SYNTHETIC / case / name, workspace / name)
    shutil.copyfile(SYNTHETIC / "clean/oracle.py", workspace / "oracle.py")
    _git(workspace, "init", "-q")
    _git(workspace, "config", "user.name", "Synthetic Test")
    _git(workspace, "config", "user.email", "synthetic@example.invalid")
    _git(workspace, "add", ".")
    subprocess.run(
        ("/usr/bin/git", "-C", workspace, "commit", "-qm", "synthetic baseline"),
        check=True,
        env={
            **os.environ,
            "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
            "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
        },
    )
    return workspace


def _catalog(tmp_path: Path, workspace: Path) -> Path:
    executable_hash = hashlib.sha256(PYTHON.read_bytes()).hexdigest()
    program_hash = hashlib.sha256((workspace / "oracle.py").read_bytes()).hexdigest()
    value = {
        "schema_version": 1,
        "catalog_id": "pa3-synthetic-catalog",
        "environment_profiles": [
            {"schema_version": 1, "id": "minimal-python", "profile": "minimal_python_v1"}
        ],
        "intents": [
            {
                "schema_version": 1,
                "id": "force_oracle",
                "executable": {
                    "schema_version": 1,
                    "policy": "isolated_system_python_v1",
                    "path": str(PYTHON),
                    "sha256": executable_hash,
                },
                "program": {"path": "oracle.py", "sha256": program_hash},
                "argv": [str(PYTHON), "-I", "-S", "-B", "oracle.py"],
                "execution_policy": {
                    "schema_version": 1,
                    "policy_id": "pa3-synthetic-offline",
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
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    return path


def _pinned_fake_config(tmp_path: Path) -> Path:
    config = yaml.safe_load((SYNTHETIC / "execution-config.yaml").read_text())
    config["trusted_executable"] = {
        "path": str(FAKE_CODEX),
        "sha256": hashlib.sha256(FAKE_CODEX.read_bytes()).hexdigest(),
    }
    path = tmp_path / "execution-config.json"
    path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n")
    return path


def _evidence(tmp_path: Path, workspace: Path) -> Path:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    run_physics_oracle(
        catalog_path=_catalog(tmp_path, workspace),
        contract_path=SYNTHETIC / "contract.yaml",
        oracle_id="force_oracle",
        task_id="synthetic-task",
        workspace=workspace,
        output_directory=evidence / "force-oracle",
    )
    return evidence


class ScriptedCodex:
    def __init__(
        self,
        report: Path,
        *,
        status: str = "succeeded",
        truncated: bool = False,
        mutate_path: str | None = None,
    ) -> None:
        self.report = report
        self.status = status
        self.truncated = truncated
        self.mutate_path = mutate_path
        self.calls = 0
        self.prepared: Any = None

    def __call__(self, **kwargs: Any) -> PhysicsAuditorCodexRun:
        self.calls += 1
        self.prepared = kwargs["prepared"]
        executable = Path(kwargs["codex_executable"])
        output = self.report.read_bytes()
        if self.mutate_path is not None:
            (self.prepared.workspace / self.mutate_path).write_text("mutated\n")
        return PhysicsAuditorCodexRun(
            adapter_result=CodexRunResult(
                run_id=self.prepared.request.run_id,
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
                summary="Codex run completed successfully.",
                error=None,
            ),
            model_output=output,
            model_output_truncated=self.truncated,
            provider_session_id="fresh-physics-session",
            provider_thread_started_ids=("fresh-physics-session",),
            backend_policy_evidence_sha256=hashlib.sha256(b"fake-policy").hexdigest(),
            codex_executable_sha256=hashlib.sha256(executable.read_bytes()).hexdigest(),
            codex_cli_version="scripted-test-v1",
        )


@pytest.mark.skipif(not BWRAP.is_file(), reason="Bubblewrap PA-2 verification unavailable")
def test_clean_fake_model_action_routes_and_proof_verifies(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    evidence = _evidence(tmp_path, workspace)
    before = subprocess.run(
        ("/usr/bin/git", "-C", workspace, "status", "--porcelain=v1", "-z"),
        check=True,
        capture_output=True,
    ).stdout
    model = ScriptedCodex(SYNTHETIC / "reports/clean.json")
    output = tmp_path / "audit-output"

    result = run_physics_auditor(
        contract_path=SYNTHETIC / "contract.yaml",
        execution_config_path=SYNTHETIC / "execution-config.yaml",
        task_id="synthetic-task",
        workspace=workspace,
        oracle_evidence_root=evidence,
        output_directory=output,
        codex_invoker=model,
    )

    assert result.status == "routing_completed"
    assert result.routing_decision is not None
    assert result.routing_decision.outcome == "pass"
    assert result.integrity_verdict == "unchanged"
    assert model.calls == 1
    assert model.prepared.policy.sandbox == "read-only"
    assert model.prepared.policy.ephemeral is True
    assert (
        verify_physics_auditor_action(
            contract_path=SYNTHETIC / "contract.yaml",
            execution_config_path=SYNTHETIC / "execution-config.yaml",
            task_id="synthetic-task",
            workspace=workspace,
            oracle_evidence_root=evidence,
            output_directory=output,
        )
        == result
    )
    assert (
        resume_physics_auditor(
            contract_path=SYNTHETIC / "contract.yaml",
            execution_config_path=SYNTHETIC / "execution-config.yaml",
            task_id="synthetic-task",
            workspace=workspace,
            oracle_evidence_root=evidence,
            output_directory=output,
            codex_invoker=model,
        )
        == result
    )
    assert model.calls == 1
    after = subprocess.run(
        ("/usr/bin/git", "-C", workspace, "status", "--porcelain=v1", "-z"),
        check=True,
        capture_output=True,
    ).stdout
    assert after == before


@pytest.mark.skipif(not BWRAP.is_file(), reason="Bubblewrap PA-2 verification unavailable")
def test_qualified_adapter_command_is_fresh_read_only_and_drops_outer_session(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    observation = tmp_path / "fake-observation.json"
    (workspace / ".fake-codex.json").write_text(
        json.dumps(
            {
                "require_stage2_policy": True,
                "expected_sandbox": "read-only",
                "expected_ephemeral": True,
                "observation_path": str(observation),
                "stdout_lines": ['{"thread_id":"fresh-physics-thread","type":"thread.started"}'],
                "final": (SYNTHETIC / "reports/clean.json").read_text(),
            }
        )
    )
    evidence = _evidence(tmp_path, workspace)

    result = run_physics_auditor(
        contract_path=SYNTHETIC / "contract.yaml",
        execution_config_path=_pinned_fake_config(tmp_path),
        task_id="synthetic-task",
        workspace=workspace,
        oracle_evidence_root=evidence,
        output_directory=tmp_path / "audit-output",
        environ={
            **os.environ,
            "CODEX_THREAD_ID": "outer-yolo-session",
            "OPENAI_API_KEY": "must-not-persist",
        },
    )

    assert result.routing_decision is not None
    assert result.routing_decision.outcome == "pass"
    observed = json.loads(observation.read_text())
    argv = observed["argv"]
    assert "--ephemeral" in argv
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    assert argv[argv.index("--ask-for-approval") + 1] == "never"
    assert "resume" not in argv
    assert "--yolo" not in argv
    assert "danger-full-access" not in argv
    assert "CODEX_THREAD_ID" not in observed["environment"]
    assert "OPENAI_API_KEY" not in observed["environment"]
    durable = b"".join(
        path.read_bytes() for path in (tmp_path / "audit-output").rglob("*") if path.is_file()
    )
    assert b"must-not-persist" not in durable
    assert b"outer-yolo-session" not in durable


@pytest.mark.skipif(not BWRAP.is_file(), reason="Bubblewrap PA-2 verification unavailable")
def test_model_command_naming_sealed_oracle_program_fails_closed(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    (workspace / ".fake-codex.json").write_text(
        json.dumps(
            {
                "require_stage2_policy": True,
                "expected_sandbox": "read-only",
                "expected_ephemeral": True,
                "observation_path": str(tmp_path / "fake-observation.json"),
                "stdout_lines": [
                    '{"thread_id":"fresh-oracle-attempt","type":"thread.started"}',
                    json.dumps(
                        {
                            "type": "item.completed",
                            "item": {
                                "type": "command_execution",
                                "command": "python oracle.py",
                                "status": "completed",
                                "exit_code": 0,
                            },
                        }
                    ),
                ],
                "final": (SYNTHETIC / "reports/clean.json").read_text(),
            }
        )
    )
    evidence = _evidence(tmp_path, workspace)
    output = tmp_path / "audit-output"

    result = run_physics_auditor(
        contract_path=SYNTHETIC / "contract.yaml",
        execution_config_path=_pinned_fake_config(tmp_path),
        task_id="synthetic-task",
        workspace=workspace,
        oracle_evidence_root=evidence,
        output_directory=output,
    )

    assert result.status == "infrastructure_failure"
    assert result.failure_reason == "oracle_execution_attempted"
    assert result.oracle_execution_detected
    assert result.routing_decision is None
    assert (
        verify_physics_auditor_action(
            contract_path=SYNTHETIC / "contract.yaml",
            execution_config_path=_pinned_fake_config(tmp_path),
            task_id="synthetic-task",
            workspace=workspace,
            oracle_evidence_root=evidence,
            output_directory=output,
        )
        == result
    )


@pytest.mark.skipif(not BWRAP.is_file(), reason="Bubblewrap PA-2 verification unavailable")
def test_missing_evidence_routes_block_without_oracle_execution_by_model(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    evidence = tmp_path / "empty-evidence"
    evidence.mkdir()
    model = ScriptedCodex(SYNTHETIC / "reports/insufficient_evidence.json")

    result = run_physics_auditor(
        contract_path=SYNTHETIC / "contract.yaml",
        execution_config_path=SYNTHETIC / "execution-config.yaml",
        task_id="synthetic-task",
        workspace=workspace,
        oracle_evidence_root=evidence,
        output_directory=tmp_path / "audit-output",
        codex_invoker=model,
    )

    assert result.routing_decision is not None
    assert result.routing_decision.outcome == "block_insufficient_evidence"
    assert model.calls == 1


@pytest.mark.skipif(not BWRAP.is_file(), reason="Bubblewrap PA-2 verification unavailable")
def test_seeded_sign_error_routes_request_repair(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, "sign_error")
    evidence = _evidence(tmp_path, workspace)
    model = ScriptedCodex(SYNTHETIC / "reports/sign_error.json")

    result = run_physics_auditor(
        contract_path=SYNTHETIC / "contract.yaml",
        execution_config_path=SYNTHETIC / "execution-config.yaml",
        task_id="synthetic-task",
        workspace=workspace,
        oracle_evidence_root=evidence,
        output_directory=tmp_path / "audit-output",
        codex_invoker=model,
    )

    assert result.routing_decision is not None
    assert result.routing_decision.outcome == "request_repair"
    assert result.integrity_verdict == "unchanged"


@pytest.mark.parametrize(
    "report_name",
    ["convention_change.json", "gauge_ambiguity.json", "unsupported_claim.json"],
)
@pytest.mark.skipif(not BWRAP.is_file(), reason="Bubblewrap PA-2 verification unavailable")
def test_human_gate_reports_are_authoritatively_routed(
    tmp_path: Path,
    report_name: str,
) -> None:
    workspace = _workspace(tmp_path)
    evidence = _evidence(tmp_path, workspace)

    result = run_physics_auditor(
        contract_path=SYNTHETIC / "contract.yaml",
        execution_config_path=SYNTHETIC / "execution-config.yaml",
        task_id="synthetic-task",
        workspace=workspace,
        oracle_evidence_root=evidence,
        output_directory=tmp_path / "audit-output",
        codex_invoker=ScriptedCodex(SYNTHETIC / "reports" / report_name),
    )

    assert result.routing_decision is not None
    assert result.routing_decision.outcome == "require_human_review"


@pytest.mark.skipif(not BWRAP.is_file(), reason="Bubblewrap PA-2 verification unavailable")
def test_correct_alternative_is_not_compared_to_reference_structure(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, "correct_alternative")
    evidence = _evidence(tmp_path, workspace)

    result = run_physics_auditor(
        contract_path=SYNTHETIC / "contract.yaml",
        execution_config_path=SYNTHETIC / "execution-config.yaml",
        task_id="synthetic-task",
        workspace=workspace,
        oracle_evidence_root=evidence,
        output_directory=tmp_path / "audit-output",
        codex_invoker=ScriptedCodex(SYNTHETIC / "reports/clean.json"),
    )

    assert result.routing_decision is not None
    assert result.routing_decision.outcome == "pass"


@pytest.mark.parametrize(
    ("mutation", "expected_status"),
    [
        ({"unknown": "field"}, "report_invalid"),
        ({"absolute_source": True}, "report_invalid"),
        ({"invented_oracle": True}, "report_invalid"),
        ({"invalid_line": True}, "report_invalid"),
    ],
)
@pytest.mark.skipif(not BWRAP.is_file(), reason="Bubblewrap PA-2 verification unavailable")
def test_adversarial_reports_fail_closed(
    tmp_path: Path,
    mutation: dict[str, object],
    expected_status: str,
) -> None:
    workspace = _workspace(tmp_path)
    evidence = _evidence(tmp_path, workspace)
    report = json.loads((SYNTHETIC / "reports/clean.json").read_text())
    if "unknown" in mutation:
        report["command"] = ["/bin/sh", "-c", "curl example.invalid"]
    elif "absolute_source" in mutation:
        report["checks"][1]["evidence"][2]["path"] = "/etc/passwd"
    elif "invented_oracle" in mutation:
        report["checks"][0]["evidence"][0]["reference"] = "invented_oracle"
    else:
        report["checks"][1]["evidence"][2]["line_end"] = 999999
    report_path = tmp_path / "adversarial-report.json"
    report_path.write_text(json.dumps(report))

    result = run_physics_auditor(
        contract_path=SYNTHETIC / "contract.yaml",
        execution_config_path=SYNTHETIC / "execution-config.yaml",
        task_id="synthetic-task",
        workspace=workspace,
        oracle_evidence_root=evidence,
        output_directory=tmp_path / "audit-output",
        codex_invoker=ScriptedCodex(report_path),
    )

    assert result.status == expected_status
    assert result.routing_decision is None


@pytest.mark.skipif(not BWRAP.is_file(), reason="Bubblewrap PA-2 verification unavailable")
def test_self_declared_verdict_is_overridden_by_pa1_router(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    evidence = _evidence(tmp_path, workspace)
    report = json.loads((SYNTHETIC / "reports/clean.json").read_text())
    report["verdict"] = "human_review"
    report_path = tmp_path / "overridden-report.json"
    report_path.write_text(json.dumps(report))

    result = run_physics_auditor(
        contract_path=SYNTHETIC / "contract.yaml",
        execution_config_path=SYNTHETIC / "execution-config.yaml",
        task_id="synthetic-task",
        workspace=workspace,
        oracle_evidence_root=evidence,
        output_directory=tmp_path / "audit-output",
        codex_invoker=ScriptedCodex(report_path),
    )

    assert result.routing_decision is not None
    assert result.routing_decision.outcome == "pass"
    assert any(rule.rule == "report_verdict_overridden" for rule in result.routing_decision.rules)


@pytest.mark.parametrize(
    ("status", "truncated", "failure_reason"),
    [
        ("timed_out", False, "model_timed_out"),
        ("process_failed", False, "model_process_failed"),
        ("succeeded", True, "model_output_limit_exceeded"),
    ],
)
@pytest.mark.skipif(not BWRAP.is_file(), reason="Bubblewrap PA-2 verification unavailable")
def test_transport_failures_are_not_audit_passes(
    tmp_path: Path,
    status: str,
    truncated: bool,
    failure_reason: str,
) -> None:
    workspace = _workspace(tmp_path)
    evidence = _evidence(tmp_path, workspace)

    result = run_physics_auditor(
        contract_path=SYNTHETIC / "contract.yaml",
        execution_config_path=SYNTHETIC / "execution-config.yaml",
        task_id="synthetic-task",
        workspace=workspace,
        oracle_evidence_root=evidence,
        output_directory=tmp_path / "audit-output",
        codex_invoker=ScriptedCodex(
            SYNTHETIC / "reports/clean.json",
            status=status,
            truncated=truncated,
        ),
    )

    assert result.status == "infrastructure_failure"
    assert result.failure_reason == failure_reason
    assert result.routing_decision is None


@pytest.mark.skipif(not BWRAP.is_file(), reason="Bubblewrap PA-2 verification unavailable")
def test_workspace_mutation_overrides_a_valid_pass_report(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    evidence = _evidence(tmp_path, workspace)

    result = run_physics_auditor(
        contract_path=SYNTHETIC / "contract.yaml",
        execution_config_path=SYNTHETIC / "execution-config.yaml",
        task_id="synthetic-task",
        workspace=workspace,
        oracle_evidence_root=evidence,
        output_directory=tmp_path / "audit-output",
        codex_invoker=ScriptedCodex(
            SYNTHETIC / "reports/clean.json",
            mutate_path="implementation.py",
        ),
    )

    assert result.status == "workspace_integrity_failure"
    assert result.integrity_verdict == "changed"
    assert result.routing_decision is None
    assert (workspace / "implementation.py").read_text() == "mutated\n"


@pytest.mark.skipif(not BWRAP.is_file(), reason="Bubblewrap PA-2 verification unavailable")
def test_workspace_is_rechecked_immediately_before_model_launch(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    evidence = _evidence(tmp_path, workspace)
    model = ScriptedCodex(SYNTHETIC / "reports/clean.json")

    def checkpoint(phase: str) -> None:
        if phase == "prompt_finalized":
            (workspace / "implementation.py").write_text("prelaunch mutation\n")

    result = run_physics_auditor(
        contract_path=SYNTHETIC / "contract.yaml",
        execution_config_path=SYNTHETIC / "execution-config.yaml",
        task_id="synthetic-task",
        workspace=workspace,
        oracle_evidence_root=evidence,
        output_directory=tmp_path / "audit-output",
        codex_invoker=model,
        checkpoint=checkpoint,
    )

    assert result.status == "workspace_integrity_failure"
    assert model.calls == 0


class MutatingCodex(ScriptedCodex):
    def __init__(self, report: Path, mutation: str) -> None:
        super().__init__(report)
        self.mutation = mutation

    def __call__(self, **kwargs: Any) -> PhysicsAuditorCodexRun:
        workspace = kwargs["prepared"].workspace
        if self.mutation == "staged":
            (workspace / "implementation.py").write_text("staged mutation\n")
            _git(workspace, "add", "implementation.py")
        elif self.mutation == "untracked":
            (workspace / "created-by-auditor.txt").write_text("forbidden\n")
        elif self.mutation == "mode":
            (workspace / "implementation.py").chmod(0o755)
        elif self.mutation == "symlink":
            link = workspace / "tracked-link"
            link.unlink()
            link.symlink_to("derivation.md")
        elif self.mutation == "submodule":
            (workspace / "vendor/nested/nested.txt").write_text("mutated submodule\n")
        return super().__call__(**kwargs)


@pytest.mark.parametrize("mutation", ["staged", "untracked", "mode", "symlink", "submodule"])
@pytest.mark.skipif(not BWRAP.is_file(), reason="Bubblewrap PA-2 verification unavailable")
def test_all_workspace_identity_mutations_fail_closed(
    tmp_path: Path,
    mutation: str,
) -> None:
    workspace = _workspace(tmp_path)
    if mutation == "symlink":
        (workspace / "tracked-link").symlink_to("implementation.py")
        _git(workspace, "add", "tracked-link")
        _git(workspace, "commit", "-qm", "add synthetic link")
    elif mutation == "submodule":
        nested = tmp_path / "nested-source"
        nested.mkdir()
        (nested / "nested.txt").write_text("clean submodule\n")
        _git(nested, "init", "-q")
        _git(nested, "config", "user.name", "Synthetic Test")
        _git(nested, "config", "user.email", "synthetic@example.invalid")
        _git(nested, "add", ".")
        _git(nested, "commit", "-qm", "nested baseline")
        _git(
            workspace,
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            "-q",
            str(nested),
            "vendor/nested",
        )
        _git(workspace, "commit", "-qam", "add synthetic submodule")
    evidence = _evidence(tmp_path, workspace)

    result = run_physics_auditor(
        contract_path=SYNTHETIC / "contract.yaml",
        execution_config_path=SYNTHETIC / "execution-config.yaml",
        task_id="synthetic-task",
        workspace=workspace,
        oracle_evidence_root=evidence,
        output_directory=tmp_path / "audit-output",
        codex_invoker=MutatingCodex(SYNTHETIC / "reports/clean.json", mutation),
    )

    assert result.status == "workspace_integrity_failure"
    assert result.integrity_verdict == "changed"


@pytest.mark.skipif(not BWRAP.is_file(), reason="Bubblewrap PA-2 verification unavailable")
def test_mode_0400_declared_input_is_read_without_chmod(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    source = workspace / "implementation.py"
    source.chmod(0o400)
    evidence = _evidence(tmp_path, workspace)

    result = run_physics_auditor(
        contract_path=SYNTHETIC / "contract.yaml",
        execution_config_path=SYNTHETIC / "execution-config.yaml",
        task_id="synthetic-task",
        workspace=workspace,
        oracle_evidence_root=evidence,
        output_directory=tmp_path / "audit-output",
        codex_invoker=ScriptedCodex(SYNTHETIC / "reports/clean.json"),
    )

    assert result.status == "routing_completed"
    assert stat.S_IMODE(source.stat().st_mode) == 0o400


@pytest.mark.skipif(not BWRAP.is_file(), reason="Bubblewrap PA-2 verification unavailable")
def test_tampered_pa2_proof_fails_before_model_launch(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    evidence = _evidence(tmp_path, workspace)
    proof_path = evidence / "force-oracle/completion-proof.json"
    proof = json.loads(proof_path.read_text())
    proof["process_status"] = "functional_failure"
    proof_path.write_text(json.dumps(proof, indent=2, sort_keys=True) + "\n")
    model = ScriptedCodex(SYNTHETIC / "reports/clean.json")

    result = run_physics_auditor(
        contract_path=SYNTHETIC / "contract.yaml",
        execution_config_path=SYNTHETIC / "execution-config.yaml",
        task_id="synthetic-task",
        workspace=workspace,
        oracle_evidence_root=evidence,
        output_directory=tmp_path / "audit-output",
        codex_invoker=model,
    )

    assert result.status == "evidence_integrity_failure"
    assert model.calls == 0


@pytest.mark.skipif(not BWRAP.is_file(), reason="Bubblewrap PA-2 verification unavailable")
def test_tampered_pa2_result_fails_before_model_launch(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    evidence = _evidence(tmp_path, workspace)
    result_path = evidence / "force-oracle/result.json"
    result = json.loads(result_path.read_text())
    result["artifact_manifest_sha256"] = "0" * 64
    result_path.write_bytes(canonical_json(result))
    model = ScriptedCodex(SYNTHETIC / "reports/clean.json")

    with pytest.raises(PhysicsAuditorIntegrityError):
        run_physics_auditor(
            contract_path=SYNTHETIC / "contract.yaml",
            execution_config_path=SYNTHETIC / "execution-config.yaml",
            task_id="synthetic-task",
            workspace=workspace,
            oracle_evidence_root=evidence,
            output_directory=tmp_path / "audit-output",
            codex_invoker=model,
        )
    assert model.calls == 0


@pytest.mark.skipif(not BWRAP.is_file(), reason="Bubblewrap PA-2 verification unavailable")
def test_declared_evidence_symlink_cannot_escape_workspace(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    outside = tmp_path / "outside.py"
    outside.write_text("def acceleration(force: float) -> float:\n    return force\n")
    (workspace / "implementation.py").unlink()
    (workspace / "implementation.py").symlink_to(outside)
    evidence = _evidence(tmp_path, workspace)
    model = ScriptedCodex(SYNTHETIC / "reports/clean.json")

    with pytest.raises(PhysicsAuditorInputError):
        run_physics_auditor(
            contract_path=SYNTHETIC / "contract.yaml",
            execution_config_path=SYNTHETIC / "execution-config.yaml",
            task_id="synthetic-task",
            workspace=workspace,
            oracle_evidence_root=evidence,
            output_directory=tmp_path / "audit-output",
            codex_invoker=model,
        )
    assert model.calls == 0


@pytest.mark.skipif(not BWRAP.is_file(), reason="Bubblewrap PA-2 verification unavailable")
def test_protected_evaluation_path_is_rejected_before_model_launch(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    evidence = _evidence(tmp_path, workspace)
    protected = workspace / "protected"
    protected.mkdir()
    (protected / "historical_gold.json").write_text("{}\n")
    model = ScriptedCodex(SYNTHETIC / "reports/clean.json")

    with pytest.raises(PhysicsAuditorInputError):
        run_physics_auditor(
            contract_path=SYNTHETIC / "contract.yaml",
            execution_config_path=SYNTHETIC / "execution-config.yaml",
            task_id="synthetic-task",
            workspace=workspace,
            oracle_evidence_root=evidence,
            output_directory=tmp_path / "audit-output",
            codex_invoker=model,
        )
    assert model.calls == 0


@pytest.mark.skipif(not BWRAP.is_file(), reason="Bubblewrap PA-2 verification unavailable")
def test_action_proof_tampering_is_detected(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    evidence = _evidence(tmp_path, workspace)
    output = tmp_path / "audit-output"
    run_physics_auditor(
        contract_path=SYNTHETIC / "contract.yaml",
        execution_config_path=SYNTHETIC / "execution-config.yaml",
        task_id="synthetic-task",
        workspace=workspace,
        oracle_evidence_root=evidence,
        output_directory=output,
        codex_invoker=ScriptedCodex(SYNTHETIC / "reports/clean.json"),
    )
    proof_path = output / "action-proof.json"
    proof = json.loads(proof_path.read_text())
    proof["model"] = "substituted-model"
    proof_path.write_text(json.dumps(proof, separators=(",", ":"), sort_keys=True) + "\n")

    with pytest.raises(PhysicsAuditorIntegrityError):
        verify_physics_auditor_action(
            contract_path=SYNTHETIC / "contract.yaml",
            execution_config_path=SYNTHETIC / "execution-config.yaml",
            task_id="synthetic-task",
            workspace=workspace,
            oracle_evidence_root=evidence,
            output_directory=output,
        )


@pytest.mark.parametrize(
    "relative_path",
    [
        "control/prompt.txt",
        "model-output.json",
        "physics-audit-report.json",
        "routing-decision.json",
    ],
)
@pytest.mark.skipif(not BWRAP.is_file(), reason="Bubblewrap PA-2 verification unavailable")
def test_bound_action_artifact_tampering_is_detected(
    tmp_path: Path,
    relative_path: str,
) -> None:
    workspace = _workspace(tmp_path)
    evidence = _evidence(tmp_path, workspace)
    output = tmp_path / "audit-output"
    run_physics_auditor(
        contract_path=SYNTHETIC / "contract.yaml",
        execution_config_path=SYNTHETIC / "execution-config.yaml",
        task_id="synthetic-task",
        workspace=workspace,
        oracle_evidence_root=evidence,
        output_directory=output,
        codex_invoker=ScriptedCodex(SYNTHETIC / "reports/clean.json"),
    )
    target = output / relative_path
    target.write_bytes(target.read_bytes() + b"\n")

    with pytest.raises(PhysicsAuditorIntegrityError):
        verify_physics_auditor_action(
            contract_path=SYNTHETIC / "contract.yaml",
            execution_config_path=SYNTHETIC / "execution-config.yaml",
            task_id="synthetic-task",
            workspace=workspace,
            oracle_evidence_root=evidence,
            output_directory=output,
        )


class InjectedCrash(RuntimeError):
    pass


@pytest.mark.parametrize(
    "crash_phase",
    [
        "action_accepted",
        "evidence_verified",
        "prompt_finalized",
        "model_launch_attempted",
        "model_exit_observed",
        "output_captured",
        "report_validated",
        "workspace_rechecked",
        "routing_completed",
        "action_proof_finalized",
    ],
)
@pytest.mark.skipif(not BWRAP.is_file(), reason="Bubblewrap PA-2 verification unavailable")
def test_recovery_at_each_durable_phase_never_relaunches_after_ambiguity(
    tmp_path: Path,
    crash_phase: str,
) -> None:
    workspace = _workspace(tmp_path)
    evidence = _evidence(tmp_path, workspace)
    output = tmp_path / "audit-output"
    model = ScriptedCodex(SYNTHETIC / "reports/clean.json")

    def checkpoint(phase: str) -> None:
        if phase == crash_phase:
            raise InjectedCrash(phase)

    with pytest.raises(InjectedCrash):
        run_physics_auditor(
            contract_path=SYNTHETIC / "contract.yaml",
            execution_config_path=SYNTHETIC / "execution-config.yaml",
            task_id="synthetic-task",
            workspace=workspace,
            oracle_evidence_root=evidence,
            output_directory=output,
            codex_invoker=model,
            checkpoint=checkpoint,
        )

    result = resume_physics_auditor(
        contract_path=SYNTHETIC / "contract.yaml",
        execution_config_path=SYNTHETIC / "execution-config.yaml",
        task_id="synthetic-task",
        workspace=workspace,
        oracle_evidence_root=evidence,
        output_directory=output,
        codex_invoker=model,
    )

    if crash_phase == "model_launch_attempted":
        assert result.status == "indeterminate_recovery"
        assert model.calls == 0
    else:
        assert result.status == "routing_completed"
        assert model.calls == 1


@pytest.mark.skipif(not BWRAP.is_file(), reason="Bubblewrap PA-2 verification unavailable")
def test_workspace_change_between_crash_and_recovery_fails_closed(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    evidence = _evidence(tmp_path, workspace)
    output = tmp_path / "audit-output"
    model = ScriptedCodex(SYNTHETIC / "reports/clean.json")

    def checkpoint(phase: str) -> None:
        if phase == "evidence_verified":
            raise InjectedCrash(phase)

    with pytest.raises(InjectedCrash):
        run_physics_auditor(
            contract_path=SYNTHETIC / "contract.yaml",
            execution_config_path=SYNTHETIC / "execution-config.yaml",
            task_id="synthetic-task",
            workspace=workspace,
            oracle_evidence_root=evidence,
            output_directory=output,
            codex_invoker=model,
            checkpoint=checkpoint,
        )
    (workspace / "implementation.py").write_text("changed during recovery\n")

    with pytest.raises(PhysicsAuditorIntegrityError):
        resume_physics_auditor(
            contract_path=SYNTHETIC / "contract.yaml",
            execution_config_path=SYNTHETIC / "execution-config.yaml",
            task_id="synthetic-task",
            workspace=workspace,
            oracle_evidence_root=evidence,
            output_directory=output,
            codex_invoker=model,
        )
    assert model.calls == 0


class RunningCrashCodex(ScriptedCodex):
    def __call__(self, **kwargs: Any) -> PhysicsAuditorCodexRun:
        child = subprocess.Popen(["/bin/sleep", "5"], start_new_session=True)
        try:
            kwargs["process_started"](child.pid)
        finally:
            child.terminate()
            child.wait(timeout=5)
        return super().__call__(**kwargs)


@pytest.mark.skipif(not BWRAP.is_file(), reason="Bubblewrap PA-2 verification unavailable")
def test_stale_model_pid_is_never_resumed_or_relaunched(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    evidence = _evidence(tmp_path, workspace)
    output = tmp_path / "audit-output"
    model = RunningCrashCodex(SYNTHETIC / "reports/clean.json")

    def checkpoint(phase: str) -> None:
        if phase == "model_running":
            raise InjectedCrash(phase)

    with pytest.raises(InjectedCrash):
        run_physics_auditor(
            contract_path=SYNTHETIC / "contract.yaml",
            execution_config_path=SYNTHETIC / "execution-config.yaml",
            task_id="synthetic-task",
            workspace=workspace,
            oracle_evidence_root=evidence,
            output_directory=output,
            codex_invoker=model,
            checkpoint=checkpoint,
        )

    result = resume_physics_auditor(
        contract_path=SYNTHETIC / "contract.yaml",
        execution_config_path=SYNTHETIC / "execution-config.yaml",
        task_id="synthetic-task",
        workspace=workspace,
        oracle_evidence_root=evidence,
        output_directory=output,
        codex_invoker=model,
    )

    assert result.status == "indeterminate_recovery"
    assert result.failure_reason == "stale_process_identity"
    assert model.calls == 0


@pytest.mark.skipif(not BWRAP.is_file(), reason="Bubblewrap PA-2 verification unavailable")
def test_reused_model_pid_identity_is_rejected_without_signalling(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    evidence = _evidence(tmp_path, workspace)
    output = tmp_path / "audit-output"
    model = RunningCrashCodex(SYNTHETIC / "reports/clean.json")

    def checkpoint(phase: str) -> None:
        if phase == "model_running":
            raise InjectedCrash(phase)

    with pytest.raises(InjectedCrash):
        run_physics_auditor(
            contract_path=SYNTHETIC / "contract.yaml",
            execution_config_path=SYNTHETIC / "execution-config.yaml",
            task_id="synthetic-task",
            workspace=workspace,
            oracle_evidence_root=evidence,
            output_directory=output,
            codex_invoker=model,
            checkpoint=checkpoint,
        )
    record_path = output / "action-records/005-model_running.json"
    record = json.loads(record_path.read_text())
    stat_fields = Path(f"/proc/{os.getpid()}/stat").read_text().split(")", 1)[1].split()
    start_ticks = int(stat_fields[19])
    record["process_identity"] = {
        "pid": os.getpid(),
        "process_group_id": os.getpid(),
        "start_ticks": start_ticks + 1,
    }
    payload = {key: value for key, value in record.items() if key != "record_sha256"}
    record["record_sha256"] = hashlib.sha256(canonical_json(payload)).hexdigest()
    record_path.write_bytes(canonical_json(record))

    result = resume_physics_auditor(
        contract_path=SYNTHETIC / "contract.yaml",
        execution_config_path=SYNTHETIC / "execution-config.yaml",
        task_id="synthetic-task",
        workspace=workspace,
        oracle_evidence_root=evidence,
        output_directory=output,
        codex_invoker=model,
    )

    assert result.status == "indeterminate_recovery"
    assert result.failure_reason == "reused_process_identity"
    assert model.calls == 0
