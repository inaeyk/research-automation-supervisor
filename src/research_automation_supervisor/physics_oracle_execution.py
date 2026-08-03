"""Trusted shell-free Physics Oracle execution and deterministic recovery v1."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import platform
import selectors
import signal
import stat
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import IO, Any, Literal, cast

from pydantic import ValidationError

from research_automation_supervisor.durable_state import (
    ZERO_HASH,
    atomic_write_bytes,
    render_json_bytes,
)
from research_automation_supervisor.errors import (
    PhysicsOracleDependencyError,
    PhysicsOracleInputError,
    PhysicsOracleIntegrityError,
    PhysicsOracleStateError,
)
from research_automation_supervisor.physics_models import (
    PhysicsTaskContractV1,
    load_physics_task_contract,
)
from research_automation_supervisor.physics_oracle_models import (
    MAX_ORACLE_ARTIFACT_BYTES,
    MAX_ORACLE_ARTIFACTS,
    OracleFailureReason,
    OracleStatus,
    PhysicsOracleActionRecordV1,
    PhysicsOracleArtifactV1,
    PhysicsOracleCatalogV1,
    PhysicsOracleCompletionProofV1,
    PhysicsOracleDeclaredResultV1,
    PhysicsOracleEnvironmentProfileV1,
    PhysicsOracleExecutionRequestV1,
    PhysicsOracleExecutionResultV1,
    PhysicsOracleIntentV1,
    PhysicsOracleNetworkEnforcementV1,
    PhysicsOracleProcessIdentityV1,
    PhysicsOracleStreamDigestV1,
    PhysicsOracleWorkspaceIdentityV1,
    load_physics_oracle_catalog,
    parse_physics_oracle_declared_result,
)
from research_automation_supervisor.physics_oracle_workspace import (
    collect_physics_oracle_workspace_identity,
)
from research_automation_supervisor.redaction import is_sensitive_name, redact_text

DEFAULT_BUBBLEWRAP_EXECUTABLE = Path("/usr/bin/bwrap")
TERMINATION_GRACE_SECONDS = 2.0
IO_POLL_SECONDS = 0.05
MAX_BUBBLEWRAP_PROBE_BYTES = 16 * 1024
RECORDS_DIRECTORY = "action-records"
DIAGNOSTICS_DIRECTORY = "diagnostics"
SCRATCH_DIRECTORY = "scratch"
CONTROL_DIRECTORY = "control"
SEALED_PROGRAM_FILE = "trusted-program.py"
SEALED_INTENT_FILE = "trusted-intent.json"
SEALED_ENVIRONMENT_FILE = "environment-profile.json"
RESULT_FILE = "result.json"
PROOF_FILE = "completion-proof.json"
DECLARED_RESULT_FILE = "declared-result.json"
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
MAX_TRUSTED_PROGRAM_BYTES = 2 * 1024 * 1024
MAX_UNDECLARED_ARTIFACT_BYTES = 1024 * 1024

Checkpoint = Callable[[str], None]


class _ObservedProcess:
    def __init__(
        self,
        *,
        exit_code: int,
        timed_out: bool,
        output_limit_exceeded: bool,
        termination_unproven: bool,
        stdout_raw: bytes,
        stderr_raw: bytes,
        stdout_count: int,
        stderr_count: int,
        stdout_sha256: str,
        stderr_sha256: str,
    ) -> None:
        self.exit_code = exit_code
        self.timed_out = timed_out
        self.output_limit_exceeded = output_limit_exceeded
        self.termination_unproven = termination_unproven
        self.stdout_raw = stdout_raw
        self.stderr_raw = stderr_raw
        self.stdout_count = stdout_count
        self.stderr_count = stderr_count
        self.stdout_sha256 = stdout_sha256
        self.stderr_sha256 = stderr_sha256


def run_physics_oracle(
    *,
    catalog_path: Path,
    contract_path: Path,
    oracle_id: str,
    task_id: str,
    workspace: Path,
    output_directory: Path,
    attempt_number: int = 1,
    environ: Mapping[str, str] | None = None,
    bubblewrap_executable: Path = DEFAULT_BUBBLEWRAP_EXECUTABLE,
    checkpoint: Checkpoint = lambda _name: None,
) -> PhysicsOracleExecutionResultV1:
    """Create and execute one isolated action; the explicit output must not exist."""
    catalog = load_physics_oracle_catalog(catalog_path)
    contract = load_physics_task_contract(contract_path)
    intent, profile = _select_trusted_intent(catalog, contract, oracle_id)
    workspace_root = _canonical_directory(workspace, "workspace")
    output = _new_output_directory(output_directory, workspace_root)
    try:
        initial = collect_physics_oracle_workspace_identity(workspace_root)
        _validate_trusted_executable(intent)
        _seal_trusted_authority(intent, profile, workspace_root, output)
        request = _request(
            task_id=task_id,
            contract=contract,
            intent=intent,
            initial=initial,
            attempt_number=attempt_number,
        )
        with _action_lock(output):
            record = _append_record(
                output,
                request=request,
                phase="intent_accepted",
                previous=None,
            )
            checkpoint("intent_accepted")
            return _continue_action(
                output=output,
                workspace=workspace_root,
                intent=intent,
                profile=profile,
                request=request,
                current=record,
                environ=environ,
                bubblewrap_executable=bubblewrap_executable,
                checkpoint=checkpoint,
            )
    except BaseException:
        raise


def resume_physics_oracle(
    *,
    catalog_path: Path,
    contract_path: Path,
    workspace: Path,
    output_directory: Path,
    environ: Mapping[str, str] | None = None,
    bubblewrap_executable: Path = DEFAULT_BUBBLEWRAP_EXECUTABLE,
    checkpoint: Checkpoint = lambda _name: None,
) -> PhysicsOracleExecutionResultV1:
    """Recover one existing PA-2 action without ever blindly repeating a launch."""
    output = _existing_output_directory(output_directory)
    workspace_root = _canonical_directory(workspace, "workspace")
    if _paths_overlap(output, workspace_root):
        raise PhysicsOracleInputError("oracle output and workspace must not overlap")
    with _action_lock(output):
        records = _load_records(output)
        if not records:
            raise PhysicsOracleStateError("oracle action has no durable intent")
        current = records[-1]
        request = current.request
        catalog = load_physics_oracle_catalog(catalog_path)
        contract = load_physics_task_contract(contract_path)
        intent, profile = _select_trusted_intent(catalog, contract, request.oracle_id)
        _verify_substitution_boundary(request, contract, intent)
        _validate_trusted_executable(intent)
        _verify_sealed_authority(intent, profile, output)
        if current.phase == "completion_proof_finalized":
            return verify_physics_oracle_completion(
                output,
                expected_request=request,
                expected_intent=intent,
                expected_profile=profile,
            )
        return _continue_action(
            output=output,
            workspace=workspace_root,
            intent=intent,
            profile=profile,
            request=request,
            current=current,
            environ=environ,
            bubblewrap_executable=bubblewrap_executable,
            checkpoint=checkpoint,
        )


def verify_physics_oracle_completion(
    output_directory: Path,
    *,
    expected_request: PhysicsOracleExecutionRequestV1 | None = None,
    expected_intent: PhysicsOracleIntentV1 | None = None,
    expected_profile: PhysicsOracleEnvironmentProfileV1 | None = None,
) -> PhysicsOracleExecutionResultV1:
    """Independently verify canonical result, proof, diagnostics, and artifacts."""
    output = _existing_output_directory(output_directory)
    records = _load_records(output)
    if not records or records[-1].phase != "completion_proof_finalized":
        raise PhysicsOracleIntegrityError("oracle completion boundary is absent")
    current = records[-1]
    sealed_intent = _load_exact_model(
        output / CONTROL_DIRECTORY / SEALED_INTENT_FILE,
        PhysicsOracleIntentV1,
        "sealed trusted oracle intent",
    )
    sealed_profile = _load_exact_model(
        output / CONTROL_DIRECTORY / SEALED_ENVIRONMENT_FILE,
        PhysicsOracleEnvironmentProfileV1,
        "sealed oracle environment profile",
    )
    result = _load_exact_model(
        output / RESULT_FILE,
        PhysicsOracleExecutionResultV1,
        "oracle result",
    )
    proof = _load_exact_model(
        output / PROOF_FILE,
        PhysicsOracleCompletionProofV1,
        "oracle completion proof",
    )
    proof_hash = proof.canonical_sha256()
    result_hash = result.canonical_sha256()
    if (
        current.result_sha256 != result_hash
        or current.completion_proof_sha256 != proof_hash
        or result.completion_proof_sha256 != proof_hash
    ):
        raise PhysicsOracleIntegrityError("oracle completion hashes do not close")
    _verify_final_record_matches_result(current, result)
    if expected_request is not None and result.request != expected_request:
        raise PhysicsOracleIntegrityError("oracle completion substituted its request")
    if expected_intent is not None and (
        result.request.trusted_intent_sha256 != expected_intent.canonical_sha256()
        or result.request.execution_policy_sha256 != expected_intent.execution_policy_sha256()
    ):
        raise PhysicsOracleIntegrityError("oracle completion substituted its trusted intent")
    if (
        sealed_intent.canonical_sha256() != result.request.trusted_intent_sha256
        or sealed_intent.execution_policy_sha256() != result.request.execution_policy_sha256
    ):
        raise PhysicsOracleIntegrityError("sealed trusted oracle intent was replaced")
    if expected_profile is not None and (
        result.environment_profile_sha256 != expected_profile.canonical_sha256()
    ):
        raise PhysicsOracleIntegrityError("oracle completion substituted its environment")
    if sealed_profile.canonical_sha256() != result.environment_profile_sha256:
        raise PhysicsOracleIntegrityError("sealed oracle environment profile was replaced")
    _verify_sealed_program(sealed_intent, output)
    _verify_proof_matches_result(proof, result)
    _verify_diagnostic(output / DIAGNOSTICS_DIRECTORY / "stdout.log", result.stdout)
    _verify_diagnostic(output / DIAGNOSTICS_DIRECTORY / "stderr.log", result.stderr)
    _verify_artifacts(output / SCRATCH_DIRECTORY, result.artifacts)
    if result.structured_output_status == "parsed":
        declared = _load_exact_model(
            output / DECLARED_RESULT_FILE,
            PhysicsOracleDeclaredResultV1,
            "declared oracle result",
        )
        if (
            declared.canonical_sha256() != result.structured_result_sha256
            or declared.outcome != result.declared_outcome
            or declared.oracle_id != result.request.oracle_id
        ):
            raise PhysicsOracleIntegrityError("declared oracle result was replaced")
    elif (output / DECLARED_RESULT_FILE).exists():
        raise PhysicsOracleIntegrityError("unexpected declared oracle result exists")
    return cast(PhysicsOracleExecutionResultV1, result)


def _continue_action(
    *,
    output: Path,
    workspace: Path,
    intent: PhysicsOracleIntentV1,
    profile: PhysicsOracleEnvironmentProfileV1,
    request: PhysicsOracleExecutionRequestV1,
    current: PhysicsOracleActionRecordV1,
    environ: Mapping[str, str] | None,
    bubblewrap_executable: Path,
    checkpoint: Checkpoint,
) -> PhysicsOracleExecutionResultV1:
    if current.phase in {"process_launch_attempted", "process_running"}:
        return _finalize_ambiguous_recovery(
            output=output,
            workspace=workspace,
            intent=intent,
            profile=profile,
            request=request,
            current=current,
            checkpoint=checkpoint,
        )

    if current.phase == "intent_accepted":
        current_identity = collect_physics_oracle_workspace_identity(workspace)
        if current_identity != request.initial_workspace_identity:
            return _finalize_without_process(
                output=output,
                request=request,
                intent=intent,
                profile=profile,
                current=current,
                final_identity=current_identity,
                status="workspace_integrity_failure",
                failure_reason="workspace_changed",
                network=_unavailable_network_identity(),
                checkpoint=checkpoint,
            )
        network = _preflight_bubblewrap(
            intent,
            bubblewrap_executable=bubblewrap_executable,
        )
        current = _append_record(
            output,
            request=request,
            phase="execution_prepared",
            previous=current,
            network_enforcement=network,
            environment_profile_sha256=profile.canonical_sha256(),
        )
        checkpoint("execution_prepared")
        if network.capability == "unavailable":
            final_identity = collect_physics_oracle_workspace_identity(workspace)
            return _finalize_without_process(
                output=output,
                request=request,
                intent=intent,
                profile=profile,
                current=current,
                final_identity=final_identity,
                status=(
                    "workspace_integrity_failure"
                    if final_identity != request.initial_workspace_identity
                    else "infrastructure_failure"
                ),
                failure_reason=(
                    "workspace_changed"
                    if final_identity != request.initial_workspace_identity
                    else "network_isolation_unavailable"
                ),
                network=network,
                checkpoint=checkpoint,
            )

    if current.phase == "execution_prepared":
        assert current.network_enforcement is not None
        current_identity = collect_physics_oracle_workspace_identity(workspace)
        if current_identity != request.initial_workspace_identity:
            return _finalize_without_process(
                output=output,
                request=request,
                intent=intent,
                profile=profile,
                current=current,
                final_identity=current_identity,
                status="workspace_integrity_failure",
                failure_reason="workspace_changed",
                network=current.network_enforcement,
                checkpoint=checkpoint,
            )
        current = _append_record(
            output,
            request=request,
            phase="process_launch_attempted",
            previous=current,
            network_enforcement=current.network_enforcement,
            environment_profile_sha256=profile.canonical_sha256(),
        )
        checkpoint("process_launch_attempted")
        return _launch_and_observe(
            output=output,
            workspace=workspace,
            intent=intent,
            profile=profile,
            request=request,
            current=current,
            environ=environ,
            bubblewrap_executable=bubblewrap_executable,
            checkpoint=checkpoint,
        )

    if current.phase == "process_exit_observed":
        return _capture_output_and_continue(
            output=output,
            workspace=workspace,
            intent=intent,
            profile=profile,
            request=request,
            current=current,
            checkpoint=checkpoint,
        )
    if current.phase == "output_captured":
        return _recheck_workspace_and_continue(
            output=output,
            workspace=workspace,
            intent=intent,
            profile=profile,
            request=request,
            current=current,
            checkpoint=checkpoint,
        )
    if current.phase == "workspace_rechecked":
        return _finalize_completion(
            output=output,
            intent=intent,
            profile=profile,
            request=request,
            current=current,
            checkpoint=checkpoint,
        )
    raise PhysicsOracleStateError("oracle action phase cannot be resumed")


def _launch_and_observe(
    *,
    output: Path,
    workspace: Path,
    intent: PhysicsOracleIntentV1,
    profile: PhysicsOracleEnvironmentProfileV1,
    request: PhysicsOracleExecutionRequestV1,
    current: PhysicsOracleActionRecordV1,
    environ: Mapping[str, str] | None,
    bubblewrap_executable: Path,
    checkpoint: Checkpoint,
) -> PhysicsOracleExecutionResultV1:
    scratch = output / SCRATCH_DIRECTORY
    diagnostics = output / DIAGNOSTICS_DIRECTORY
    scratch.mkdir(exist_ok=False)
    diagnostics.mkdir(exist_ok=False)
    command = _bubblewrap_command(
        bubblewrap_executable.resolve(strict=True),
        workspace=workspace,
        scratch=scratch,
        intent=intent,
        sealed_program=output / CONTROL_DIRECTORY / SEALED_PROGRAM_FILE,
    )
    environment = _oracle_environment()
    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(
            command,
            cwd=None,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            close_fds=True,
            start_new_session=True,
        )
    except (OSError, subprocess.SubprocessError):
        empty = _empty_stream()
        _write_diagnostics(diagnostics, b"", b"")
        exited = _append_record(
            output,
            request=request,
            phase="process_exit_observed",
            previous=current,
            network_enforcement=current.network_enforcement,
            environment_profile_sha256=profile.canonical_sha256(),
            process_exit_code=None,
            timed_out=False,
            stdout=empty,
            stderr=empty,
            execution_status="infrastructure_failure",
            failure_reason="launch_failed",
        )
        checkpoint("process_exit_observed")
        return _capture_output_and_continue(
            output=output,
            workspace=workspace,
            intent=intent,
            profile=profile,
            request=request,
            current=exited,
            checkpoint=checkpoint,
        )
    start_ticks = _process_start_ticks(process.pid)
    if start_ticks is None:
        _terminate_process_group(process, TERMINATION_GRACE_SECONDS)
        raise PhysicsOracleStateError("launched oracle process identity is unavailable")
    identity = PhysicsOracleProcessIdentityV1(
        pid=process.pid,
        process_group_id=process.pid,
        start_ticks=start_ticks,
    )
    running = _append_record(
        output,
        request=request,
        phase="process_running",
        previous=current,
        network_enforcement=current.network_enforcement,
        environment_profile_sha256=profile.canonical_sha256(),
        process_identity=identity,
    )
    _checkpoint_while_process_running(
        checkpoint, "after_process_launch_before_identity", process
    )
    _checkpoint_while_process_running(checkpoint, "process_running", process)
    try:
        observed = _observe_process(
            process,
            stdout_limit=intent.execution_policy.max_stdout_bytes,
            stderr_limit=intent.execution_policy.max_stderr_bytes,
            timeout_seconds=intent.execution_policy.timeout_seconds,
        )
    except BaseException:
        _terminate_process_group(process, TERMINATION_GRACE_SECONDS)
        raise
    sensitive_values = _sensitive_environment_values(environ)
    stdout_diagnostic = _redacted_bytes(observed.stdout_raw, sensitive_values)
    stderr_diagnostic = _redacted_bytes(observed.stderr_raw, sensitive_values)
    _write_diagnostics(diagnostics, stdout_diagnostic, stderr_diagnostic)
    stdout = PhysicsOracleStreamDigestV1(
        observed_byte_length=observed.stdout_count,
        observed_sha256=observed.stdout_sha256,
        captured_prefix_byte_length=len(stdout_diagnostic),
        captured_prefix_sha256=hashlib.sha256(stdout_diagnostic).hexdigest(),
        truncated=len(stdout_diagnostic) < observed.stdout_count,
    )
    stderr = PhysicsOracleStreamDigestV1(
        observed_byte_length=observed.stderr_count,
        observed_sha256=observed.stderr_sha256,
        captured_prefix_byte_length=len(stderr_diagnostic),
        captured_prefix_sha256=hashlib.sha256(stderr_diagnostic).hexdigest(),
        truncated=len(stderr_diagnostic) < observed.stderr_count,
    )
    status, reason = _process_status(observed, intent)
    exited = _append_record(
        output,
        request=request,
        phase="process_exit_observed",
        previous=running,
        network_enforcement=running.network_enforcement,
        environment_profile_sha256=profile.canonical_sha256(),
        process_identity=identity,
        process_exit_code=observed.exit_code,
        timed_out=observed.timed_out,
        stdout=stdout,
        stderr=stderr,
        execution_status=status,
        failure_reason=reason,
    )
    checkpoint("process_exit_observed")
    return _capture_output_and_continue(
        output=output,
        workspace=workspace,
        intent=intent,
        profile=profile,
        request=request,
        current=exited,
        checkpoint=checkpoint,
    )


def _checkpoint_while_process_running(
    checkpoint: Checkpoint,
    name: str,
    process: subprocess.Popen[bytes],
) -> None:
    try:
        checkpoint(name)
    except BaseException:
        _terminate_process_group(process, TERMINATION_GRACE_SECONDS)
        raise


def _capture_output_and_continue(
    *,
    output: Path,
    workspace: Path,
    intent: PhysicsOracleIntentV1,
    profile: PhysicsOracleEnvironmentProfileV1,
    request: PhysicsOracleExecutionRequestV1,
    current: PhysicsOracleActionRecordV1,
    checkpoint: Checkpoint,
) -> PhysicsOracleExecutionResultV1:
    if (
        current.stdout is None
        or current.stderr is None
        or current.execution_status is None
        or current.failure_reason is None
        or current.timed_out is None
    ):
        raise PhysicsOracleStateError("process-exit record is incomplete")
    _verify_diagnostic(output / DIAGNOSTICS_DIRECTORY / "stdout.log", current.stdout)
    _verify_diagnostic(output / DIAGNOSTICS_DIRECTORY / "stderr.log", current.stderr)
    structured_status: Literal["not_required", "parsed", "malformed", "missing"]
    structured_hash: str | None = None
    declared_outcome: Literal["passed", "functional_failure"] | None = None
    status = current.execution_status
    reason = current.failure_reason
    if intent.execution_policy.structured_output_schema == "none":
        structured_status = "not_required"
    elif current.stdout.observed_byte_length == 0:
        structured_status = "missing"
        if status == "passed":
            status = "output_contract_failure"
            reason = "structured_output_missing"
    elif current.stdout.truncated:
        structured_status = "malformed"
        if status not in {"timed_out", "infrastructure_failure"}:
            status = "output_contract_failure"
            reason = "output_limit_exceeded"
    else:
        stdout_path = output / DIAGNOSTICS_DIRECTORY / "stdout.log"
        try:
            raw = stdout_path.read_bytes()
            declared = parse_physics_oracle_declared_result(raw, request.oracle_id)
        except (OSError, PhysicsOracleInputError):
            structured_status = "malformed"
            if status not in {"timed_out", "infrastructure_failure"}:
                status = "output_contract_failure"
                reason = "structured_output_malformed"
        else:
            structured_status = "parsed"
            structured_hash = declared.canonical_sha256()
            declared_outcome = declared.outcome
            _write_once_or_verify(
                output / DECLARED_RESULT_FILE,
                render_json_bytes(declared.model_dump(mode="json")),
                "declared oracle result",
            )
            if declared.outcome == "functional_failure" and status == "passed":
                status = "functional_failure"
                reason = "declared_functional_failure"
    artifacts, artifact_ok = _collect_artifacts(
        output / SCRATCH_DIRECTORY,
        intent,
    )
    artifact_hash = _artifact_manifest_sha256(artifacts)
    if not artifact_ok and status not in {"timed_out", "infrastructure_failure"}:
        status = "output_contract_failure"
        reason = "artifact_contract_failed"
    captured = _append_record(
        output,
        request=request,
        phase="output_captured",
        previous=current,
        network_enforcement=current.network_enforcement,
        environment_profile_sha256=profile.canonical_sha256(),
        process_identity=current.process_identity,
        process_exit_code=current.process_exit_code,
        timed_out=current.timed_out,
        stdout=current.stdout,
        stderr=current.stderr,
        execution_status=status,
        failure_reason=reason,
        structured_output_status=structured_status,
        structured_result_sha256=structured_hash,
        declared_outcome=declared_outcome,
        artifacts=artifacts,
        artifact_manifest_sha256=artifact_hash,
    )
    checkpoint("output_captured")
    return _recheck_workspace_and_continue(
        output=output,
        workspace=workspace,
        intent=intent,
        profile=profile,
        request=request,
        current=captured,
        checkpoint=checkpoint,
    )


def _recheck_workspace_and_continue(
    *,
    output: Path,
    workspace: Path,
    intent: PhysicsOracleIntentV1,
    profile: PhysicsOracleEnvironmentProfileV1,
    request: PhysicsOracleExecutionRequestV1,
    current: PhysicsOracleActionRecordV1,
    checkpoint: Checkpoint,
) -> PhysicsOracleExecutionResultV1:
    _verify_captured_record(output, current)
    final_identity = collect_physics_oracle_workspace_identity(workspace)
    status = current.execution_status
    reason = current.failure_reason
    assert status is not None and reason is not None
    if final_identity != request.initial_workspace_identity:
        status = "workspace_integrity_failure"
        reason = "workspace_changed"
    rechecked = _append_record(
        output,
        request=request,
        phase="workspace_rechecked",
        previous=current,
        network_enforcement=current.network_enforcement,
        environment_profile_sha256=profile.canonical_sha256(),
        process_identity=current.process_identity,
        process_exit_code=current.process_exit_code,
        timed_out=current.timed_out,
        stdout=current.stdout,
        stderr=current.stderr,
        execution_status=status,
        failure_reason=reason,
        structured_output_status=current.structured_output_status,
        structured_result_sha256=current.structured_result_sha256,
        declared_outcome=current.declared_outcome,
        artifacts=current.artifacts,
        artifact_manifest_sha256=current.artifact_manifest_sha256,
        final_workspace_identity=final_identity,
    )
    checkpoint("workspace_rechecked")
    return _finalize_completion(
        output=output,
        intent=intent,
        profile=profile,
        request=request,
        current=rechecked,
        checkpoint=checkpoint,
    )


def _finalize_completion(
    *,
    output: Path,
    intent: PhysicsOracleIntentV1,
    profile: PhysicsOracleEnvironmentProfileV1,
    request: PhysicsOracleExecutionRequestV1,
    current: PhysicsOracleActionRecordV1,
    checkpoint: Checkpoint,
) -> PhysicsOracleExecutionResultV1:
    _verify_workspace_record(current)
    assert current.network_enforcement is not None
    assert current.environment_profile_sha256 is not None
    assert current.execution_status is not None
    assert current.failure_reason is not None
    assert current.timed_out is not None
    assert current.stdout is not None and current.stderr is not None
    assert current.structured_output_status is not None
    assert current.artifact_manifest_sha256 is not None
    assert current.final_workspace_identity is not None
    proof = PhysicsOracleCompletionProofV1(
        schema_version=1,
        task_id=request.task_id,
        physics_contract_sha256=request.contract_sha256,
        trusted_oracle_id=request.oracle_id,
        trusted_intent_sha256=request.trusted_intent_sha256,
        execution_policy_sha256=request.execution_policy_sha256,
        network_enforcement=current.network_enforcement,
        environment_profile_sha256=current.environment_profile_sha256,
        initial_workspace_identity=request.initial_workspace_identity,
        process_status=current.execution_status,
        failure_reason=current.failure_reason,
        process_exit_code=current.process_exit_code,
        timed_out=current.timed_out,
        stdout=current.stdout,
        stderr=current.stderr,
        structured_output_status=current.structured_output_status,
        structured_result_sha256=current.structured_result_sha256,
        declared_outcome=current.declared_outcome,
        artifact_manifest_sha256=current.artifact_manifest_sha256,
        final_workspace_identity=current.final_workspace_identity,
        integrity_verdict=(
            "unchanged"
            if current.final_workspace_identity == request.initial_workspace_identity
            else "changed"
        ),
    )
    proof_hash = proof.canonical_sha256()
    result = PhysicsOracleExecutionResultV1(
        schema_version=1,
        request=request,
        status=current.execution_status,
        failure_reason=current.failure_reason,
        process_exit_code=current.process_exit_code,
        timed_out=current.timed_out,
        stdout=current.stdout,
        stderr=current.stderr,
        structured_output_status=current.structured_output_status,
        structured_result_sha256=current.structured_result_sha256,
        declared_outcome=current.declared_outcome,
        artifacts=current.artifacts,
        artifact_manifest_sha256=current.artifact_manifest_sha256,
        initial_workspace_identity=request.initial_workspace_identity,
        final_workspace_identity=current.final_workspace_identity,
        integrity_verdict=proof.integrity_verdict,
        network_enforcement=current.network_enforcement,
        environment_profile_sha256=current.environment_profile_sha256,
        completion_proof_sha256=proof_hash,
    )
    _write_once_or_verify(
        output / PROOF_FILE,
        render_json_bytes(proof.model_dump(mode="json")),
        "oracle completion proof",
    )
    checkpoint("after_proof_creation_before_final_record")
    _write_once_or_verify(
        output / RESULT_FILE,
        render_json_bytes(result.model_dump(mode="json")),
        "oracle result",
    )
    final = _append_record(
        output,
        request=request,
        phase="completion_proof_finalized",
        previous=current,
        network_enforcement=current.network_enforcement,
        environment_profile_sha256=current.environment_profile_sha256,
        process_identity=current.process_identity,
        process_exit_code=current.process_exit_code,
        timed_out=current.timed_out,
        stdout=current.stdout,
        stderr=current.stderr,
        execution_status=current.execution_status,
        failure_reason=current.failure_reason,
        structured_output_status=current.structured_output_status,
        structured_result_sha256=current.structured_result_sha256,
        declared_outcome=current.declared_outcome,
        artifacts=current.artifacts,
        artifact_manifest_sha256=current.artifact_manifest_sha256,
        final_workspace_identity=current.final_workspace_identity,
        result_sha256=result.canonical_sha256(),
        completion_proof_sha256=proof_hash,
    )
    del final
    checkpoint("completion_proof_finalized")
    return verify_physics_oracle_completion(
        output,
        expected_request=request,
        expected_intent=intent,
        expected_profile=profile,
    )


def _finalize_without_process(
    *,
    output: Path,
    request: PhysicsOracleExecutionRequestV1,
    intent: PhysicsOracleIntentV1,
    profile: PhysicsOracleEnvironmentProfileV1,
    current: PhysicsOracleActionRecordV1,
    final_identity: PhysicsOracleWorkspaceIdentityV1,
    status: OracleStatus,
    failure_reason: OracleFailureReason,
    network: PhysicsOracleNetworkEnforcementV1,
    checkpoint: Checkpoint,
) -> PhysicsOracleExecutionResultV1:
    diagnostics = output / DIAGNOSTICS_DIRECTORY
    scratch = output / SCRATCH_DIRECTORY
    diagnostics.mkdir(exist_ok=True)
    scratch.mkdir(exist_ok=True)
    _write_diagnostics(diagnostics, b"", b"")
    empty = _empty_stream()
    captured = _append_record(
        output,
        request=request,
        phase="output_captured",
        previous=current,
        network_enforcement=network,
        environment_profile_sha256=profile.canonical_sha256(),
        process_exit_code=None,
        timed_out=False,
        stdout=empty,
        stderr=empty,
        execution_status=status,
        failure_reason=failure_reason,
        structured_output_status=(
            "not_required"
            if intent.execution_policy.structured_output_schema == "none"
            else "missing"
        ),
        artifacts=(),
        artifact_manifest_sha256=_artifact_manifest_sha256(()),
    )
    checkpoint("output_captured")
    rechecked = _append_record(
        output,
        request=request,
        phase="workspace_rechecked",
        previous=captured,
        network_enforcement=network,
        environment_profile_sha256=profile.canonical_sha256(),
        process_exit_code=None,
        timed_out=False,
        stdout=empty,
        stderr=empty,
        execution_status=(
            "workspace_integrity_failure"
            if final_identity != request.initial_workspace_identity
            else status
        ),
        failure_reason=(
            "workspace_changed"
            if final_identity != request.initial_workspace_identity
            else failure_reason
        ),
        structured_output_status=captured.structured_output_status,
        artifacts=(),
        artifact_manifest_sha256=captured.artifact_manifest_sha256,
        final_workspace_identity=final_identity,
    )
    checkpoint("workspace_rechecked")
    return _finalize_completion(
        output=output,
        intent=intent,
        profile=profile,
        request=request,
        current=rechecked,
        checkpoint=checkpoint,
    )


def _finalize_ambiguous_recovery(
    *,
    output: Path,
    workspace: Path,
    intent: PhysicsOracleIntentV1,
    profile: PhysicsOracleEnvironmentProfileV1,
    request: PhysicsOracleExecutionRequestV1,
    current: PhysicsOracleActionRecordV1,
    checkpoint: Checkpoint,
) -> PhysicsOracleExecutionResultV1:
    if current.phase == "process_running" and current.process_identity is not None:
        identity = current.process_identity
        if _process_identity_running(identity.pid, identity.start_ticks):
            _terminate_identity(identity)
    final_identity = collect_physics_oracle_workspace_identity(workspace)
    network = current.network_enforcement or _unavailable_network_identity()
    return _finalize_without_process(
        output=output,
        request=request,
        intent=intent,
        profile=profile,
        current=current,
        final_identity=final_identity,
        status=(
            "workspace_integrity_failure"
            if final_identity != request.initial_workspace_identity
            else "indeterminate_recovery"
        ),
        failure_reason=(
            "workspace_changed"
            if final_identity != request.initial_workspace_identity
            else "recovery_ambiguous"
        ),
        network=network,
        checkpoint=checkpoint,
    )


def _select_trusted_intent(
    catalog: PhysicsOracleCatalogV1,
    contract: PhysicsTaskContractV1,
    oracle_id: str,
) -> tuple[PhysicsOracleIntentV1, PhysicsOracleEnvironmentProfileV1]:
    if not any(item.id == oracle_id for item in contract.oracles):
        raise PhysicsOracleInputError("oracle ID is absent from the physics contract")
    intent = catalog.intent(oracle_id)
    profile = catalog.environment_profile(intent.execution_policy.environment_profile_id)
    return intent, profile


def _request(
    *,
    task_id: str,
    contract: PhysicsTaskContractV1,
    intent: PhysicsOracleIntentV1,
    initial: PhysicsOracleWorkspaceIdentityV1,
    attempt_number: int,
) -> PhysicsOracleExecutionRequestV1:
    seed = {
        "attempt_number": attempt_number,
        "contract_sha256": contract.canonical_sha256(),
        "initial_workspace_identity": initial.model_dump(mode="json"),
        "oracle_id": intent.id,
        "task_id": task_id,
        "trusted_intent_sha256": intent.canonical_sha256(),
    }
    action_id = f"oracle-{hashlib.sha256(_canonical_bytes(seed)).hexdigest()[:32]}"
    try:
        return PhysicsOracleExecutionRequestV1(
            schema_version=1,
            task_id=task_id,
            contract_sha256=contract.canonical_sha256(),
            oracle_id=intent.id,
            trusted_intent_sha256=intent.canonical_sha256(),
            execution_policy_sha256=intent.execution_policy_sha256(),
            workspace_reference="workspace",
            initial_workspace_identity=initial,
            scratch_reference="scratch",
            attempt_number=attempt_number,
            action_id=action_id,
        )
    except ValidationError as exc:
        raise PhysicsOracleInputError("oracle execution request is invalid") from exc


def _verify_substitution_boundary(
    request: PhysicsOracleExecutionRequestV1,
    contract: PhysicsTaskContractV1,
    intent: PhysicsOracleIntentV1,
) -> None:
    if (
        request.contract_sha256 != contract.canonical_sha256()
        or request.oracle_id != intent.id
        or request.trusted_intent_sha256 != intent.canonical_sha256()
        or request.execution_policy_sha256 != intent.execution_policy_sha256()
    ):
        raise PhysicsOracleIntegrityError(
            "contract, intent, or execution policy changed after intent acceptance"
        )


def _seal_trusted_authority(
    intent: PhysicsOracleIntentV1,
    profile: PhysicsOracleEnvironmentProfileV1,
    workspace: Path,
    output: Path,
) -> None:
    candidate = workspace / intent.program.path
    try:
        absolute = Path(os.path.abspath(candidate))
        resolved = candidate.resolve(strict=True)
        metadata = candidate.lstat()
    except (OSError, RuntimeError, ValueError) as exc:
        raise PhysicsOracleInputError("trusted oracle program is unavailable") from exc
    if (
        absolute != resolved
        or not resolved.is_relative_to(workspace)
        or not stat.S_ISREG(metadata.st_mode)
        or candidate.is_symlink()
    ):
        raise PhysicsOracleInputError(
            "trusted oracle program must be a canonical workspace regular file"
        )
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(candidate, flags)
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size <= 0
            or before.st_size > MAX_TRUSTED_PROGRAM_BYTES
        ):
            raise PhysicsOracleIntegrityError("trusted oracle program is invalid")
        raw = bytearray()
        while len(raw) <= MAX_TRUSTED_PROGRAM_BYTES:
            chunk = os.read(
                descriptor,
                min(64 * 1024, MAX_TRUSTED_PROGRAM_BYTES + 1 - len(raw)),
            )
            if not chunk:
                break
            raw.extend(chunk)
        after = os.fstat(descriptor)
    except PhysicsOracleIntegrityError:
        raise
    except OSError as exc:
        raise PhysicsOracleIntegrityError("trusted oracle program could not be sealed") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if (
        len(raw) > MAX_TRUSTED_PROGRAM_BYTES
        or _stat_identity(before) != _stat_identity(after)
        or hashlib.sha256(raw).hexdigest() != intent.program.sha256
    ):
        raise PhysicsOracleIntegrityError("trusted oracle program hash does not match")
    control = output / CONTROL_DIRECTORY
    control.mkdir(exist_ok=False)
    _write_once_or_verify(
        control / SEALED_INTENT_FILE,
        render_json_bytes(intent.model_dump(mode="json")),
        "sealed trusted oracle intent",
    )
    _write_once_or_verify(
        control / SEALED_ENVIRONMENT_FILE,
        render_json_bytes(profile.model_dump(mode="json")),
        "sealed oracle environment profile",
    )
    _write_once_or_verify(
        control / SEALED_PROGRAM_FILE,
        bytes(raw),
        "sealed trusted oracle program",
    )


def _verify_sealed_authority(
    intent: PhysicsOracleIntentV1,
    profile: PhysicsOracleEnvironmentProfileV1,
    output: Path,
) -> None:
    sealed_intent = _load_exact_model(
        output / CONTROL_DIRECTORY / SEALED_INTENT_FILE,
        PhysicsOracleIntentV1,
        "sealed trusted oracle intent",
    )
    sealed_profile = _load_exact_model(
        output / CONTROL_DIRECTORY / SEALED_ENVIRONMENT_FILE,
        PhysicsOracleEnvironmentProfileV1,
        "sealed oracle environment profile",
    )
    if sealed_intent != intent or sealed_profile != profile:
        raise PhysicsOracleIntegrityError("sealed oracle authority was substituted")
    _verify_sealed_program(intent, output)


def _verify_sealed_program(intent: PhysicsOracleIntentV1, output: Path) -> None:
    path = output / CONTROL_DIRECTORY / SEALED_PROGRAM_FILE
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise PhysicsOracleIntegrityError("sealed trusted oracle program is unavailable") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_size <= 0
        or metadata.st_size > MAX_TRUSTED_PROGRAM_BYTES
        or _sha256_file(path) != intent.program.sha256
    ):
        raise PhysicsOracleIntegrityError("sealed trusted oracle program was replaced")


def _validate_trusted_executable(intent: PhysicsOracleIntentV1) -> None:
    candidate = Path(intent.executable.path)
    try:
        absolute = Path(os.path.abspath(candidate))
        resolved = candidate.resolve(strict=True)
        metadata = candidate.lstat()
    except (OSError, RuntimeError, ValueError) as exc:
        raise PhysicsOracleDependencyError("trusted oracle executable is unavailable") from exc
    if (
        absolute != resolved
        or candidate.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or not os.access(candidate, os.X_OK)
    ):
        raise PhysicsOracleDependencyError(
            "trusted oracle executable must be one exact executable regular file"
        )
    if _sha256_file(candidate) != intent.executable.sha256:
        raise PhysicsOracleDependencyError("trusted oracle executable hash does not match")


def _preflight_bubblewrap(
    intent: PhysicsOracleIntentV1,
    *,
    bubblewrap_executable: Path,
) -> PhysicsOracleNetworkEnforcementV1:
    try:
        absolute = Path(os.path.abspath(bubblewrap_executable))
        resolved = bubblewrap_executable.resolve(strict=True)
        metadata = bubblewrap_executable.lstat()
        if (
            absolute != resolved
            or bubblewrap_executable.is_symlink()
            or resolved.parent not in {Path("/usr/bin"), Path("/bin")}
            or not stat.S_ISREG(metadata.st_mode)
            or not os.access(resolved, os.X_OK)
        ):
            raise OSError
        version = subprocess.run(
            (str(resolved), "--version"),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=_oracle_environment(),
            check=False,
            close_fds=True,
            timeout=10,
        )
        if (
            version.returncode != 0
            or len(version.stdout) > 128
            or not version.stdout.decode("ascii").strip().startswith("bubblewrap ")
        ):
            raise OSError
        version_text = version.stdout.decode("ascii").strip()
        with tempfile.TemporaryDirectory(prefix="ras-oracle-isolation-probe-") as raw:
            root = Path(raw)
            workspace = root / "workspace"
            scratch = root / "scratch"
            workspace.mkdir()
            scratch.mkdir()
            sentinel = workspace / "readable"
            sentinel.write_text("readable\n", encoding="ascii")
            probe_intent = intent.model_copy(
                update={
                    "argv": (
                        intent.executable.path,
                        "-I",
                        "-S",
                        "-B",
                        "-c",
                        (
                            "from pathlib import Path;"
                            "assert Path('/workspace/readable').read_text() == 'readable\\n';"
                            "Path('/scratch/writable').write_text('ok');"
                            "\ntry: Path('/workspace/forbidden').write_text('x')\n"
                            "except OSError: pass\n"
                            "else: raise SystemExit(9)"
                        ),
                    )
                }
            )
            command = _bubblewrap_command(
                resolved,
                workspace=workspace,
                scratch=scratch,
                intent=probe_intent,
                validate_program_position=False,
            )
            completed = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                env=_oracle_environment(),
                check=False,
                close_fds=True,
                timeout=15,
            )
            if (
                completed.returncode != 0
                or len(completed.stdout) > MAX_BUBBLEWRAP_PROBE_BYTES
                or len(completed.stderr) > MAX_BUBBLEWRAP_PROBE_BYTES
                or not (scratch / "writable").is_file()
                or (workspace / "forbidden").exists()
            ):
                raise OSError
        return PhysicsOracleNetworkEnforcementV1(
            schema_version=1,
            requested_policy="disabled",
            backend="bubblewrap",
            backend_policy="unshare_all_network_namespace_v1",
            capability="enforced",
            bubblewrap_version=version_text,
            bubblewrap_sha256=_sha256_file(resolved),
        )
    except (OSError, UnicodeDecodeError, subprocess.SubprocessError):
        return _unavailable_network_identity()


def _unavailable_network_identity() -> PhysicsOracleNetworkEnforcementV1:
    return PhysicsOracleNetworkEnforcementV1(
        schema_version=1,
        requested_policy="disabled",
        backend="bubblewrap",
        backend_policy="unshare_all_network_namespace_v1",
        capability="unavailable",
        bubblewrap_version=None,
        bubblewrap_sha256=None,
    )


def _bubblewrap_command(
    bwrap: Path,
    *,
    workspace: Path,
    scratch: Path,
    intent: PhysicsOracleIntentV1,
    sealed_program: Path | None = None,
    validate_program_position: bool = True,
) -> tuple[str, ...]:
    executable = Path(intent.executable.path)
    version = executable.name.removeprefix("python")
    if not re_python_version(version):
        raise PhysicsOracleDependencyError("trusted Python executable version is unsupported")
    stdlib = Path(f"/usr/lib/python{version}")
    multiarch_name = {
        "x86_64": "x86_64-linux-gnu",
        "aarch64": "aarch64-linux-gnu",
    }.get(platform.machine())
    if multiarch_name is None:
        raise PhysicsOracleDependencyError("oracle runtime architecture is unsupported")
    multiarch = Path("/usr/lib") / multiarch_name
    loader_destination = {
        "x86_64": Path("/lib64/ld-linux-x86-64.so.2"),
        "aarch64": Path("/lib/ld-linux-aarch64.so.1"),
    }[platform.machine()]
    try:
        loader = loader_destination.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise PhysicsOracleDependencyError("oracle dynamic loader is unavailable") from exc
    for path in (stdlib, multiarch, loader):
        if not path.exists():
            raise PhysicsOracleDependencyError("oracle Python runtime is unavailable")
    command: list[str] = [
        str(bwrap),
        "--unshare-all",
        "--die-with-parent",
        "--new-session",
        "--clearenv",
    ]
    for name, value in sorted(_oracle_environment().items()):
        command.extend(("--setenv", name, value))
    command.extend(
        (
            "--dir",
            "/usr",
            "--dir",
            "/usr/bin",
            "--dir",
            "/usr/lib",
            "--dir",
            f"/usr/lib/{multiarch_name}",
            "--dir",
            "/lib",
            "--dir",
            "/lib64",
            "--dir",
            "/oracle",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--tmpfs",
            "/tmp",
            "--ro-bind",
            str(executable),
            str(executable),
            "--ro-bind",
            str(stdlib),
            str(stdlib),
            "--ro-bind",
            str(multiarch),
            str(multiarch),
            "--ro-bind",
            str(loader),
            str(loader_destination),
            "--ro-bind",
            str(workspace),
            "/workspace",
            "--bind",
            str(scratch),
            "/scratch",
            "--chdir",
            "/workspace",
            "--",
        )
    )
    masked_runtime = (
        stdlib / "test",
        stdlib / f"config-{version}-{multiarch_name}",
        multiarch / "gdk-pixbuf-2.0",
        multiarch / "glib-2.0",
        multiarch / "gstreamer1.0",
        multiarch / "libgtk-3-0t64",
        multiarch / "nodejs",
    )
    workspace_mount = command.index(str(workspace)) - 1
    mask_arguments = [
        item
        for directory in masked_runtime
        if directory.is_dir()
        for item in ("--tmpfs", str(directory))
    ]
    command[workspace_mount:workspace_mount] = mask_arguments
    if validate_program_position:
        if sealed_program is None:
            raise PhysicsOracleIntegrityError("sealed trusted oracle program is unavailable")
        command[-3:-3] = (
            "--ro-bind",
            str(sealed_program),
            "/oracle/trusted-program.py",
        )
    inner = list(intent.argv)
    if validate_program_position:
        inner[4] = "/oracle/trusted-program.py"
    command.extend(inner)
    return tuple(command)


def re_python_version(value: str) -> bool:
    pieces = value.split(".")
    return len(pieces) == 2 and all(piece.isdigit() for piece in pieces)


def _oracle_environment() -> dict[str, str]:
    return {
        "HOME": "/tmp/home",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/bin",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "RAS_ORACLE_SCRATCH": "/scratch",
        "TMPDIR": "/scratch",
    }


def _sensitive_environment_values(environ: Mapping[str, str] | None) -> tuple[str, ...]:
    source = os.environ if environ is None else environ
    values = {
        value
        for name, value in source.items()
        if len(value.encode("utf-8")) >= 8 and _credential_or_transport_name(name)
    }
    return tuple(sorted(values, key=lambda item: (-len(item), item)))


def _credential_or_transport_name(name: str) -> bool:
    upper = name.upper()
    return (
        is_sensitive_name(name)
        or upper
        in {
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "NO_PROXY",
            "SSH_AUTH_SOCK",
            "SSH_AGENT_PID",
            "GIT_CONFIG_COUNT",
            "GIT_CONFIG_KEY_0",
            "GIT_CONFIG_VALUE_0",
        }
        or upper.startswith(
            ("AWS_", "AZURE_", "GOOGLE_", "GCP_", "OPENAI_", "ANTHROPIC_", "CODEX_")
        )
    )


def _observe_process(
    process: subprocess.Popen[bytes],
    *,
    stdout_limit: int,
    stderr_limit: int,
    timeout_seconds: int,
) -> _ObservedProcess:
    if process.stdout is None or process.stderr is None:
        raise PhysicsOracleStateError("oracle process pipes are unavailable")
    selector = selectors.DefaultSelector()
    streams: dict[IO[bytes], tuple[str, int]] = {
        process.stdout: ("stdout", stdout_limit),
        process.stderr: ("stderr", stderr_limit),
    }
    for stream in streams:
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ)
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    counts = {"stdout": 0, "stderr": 0}
    digests = {"stdout": hashlib.sha256(), "stderr": hashlib.sha256()}
    deadline = time.monotonic() + timeout_seconds
    timed_out = False
    limit_exceeded = False
    termination_unproven = False
    termination_started = False
    try:
        while selector.get_map() or process.poll() is None:
            if not termination_started and time.monotonic() >= deadline:
                timed_out = True
                termination_started = True
                termination_unproven = not _terminate_process_group(
                    process, TERMINATION_GRACE_SECONDS
                )
            for key, _mask in selector.select(IO_POLL_SECONDS):
                stream = cast(IO[bytes], key.fileobj)
                name, limit = streams[stream]
                try:
                    chunk = os.read(stream.fileno(), 64 * 1024)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(stream)
                    continue
                counts[name] += len(chunk)
                digests[name].update(chunk)
                remaining = max(0, limit - len(buffers[name]))
                buffers[name].extend(chunk[:remaining])
                if counts[name] > limit and not termination_started:
                    limit_exceeded = True
                    termination_started = True
                    termination_unproven = not _terminate_process_group(
                        process, TERMINATION_GRACE_SECONDS
                    )
        exit_code = process.wait()
    finally:
        selector.close()
        for stream in streams:
            with suppress(OSError):
                stream.close()
    return _ObservedProcess(
        exit_code=exit_code,
        timed_out=timed_out,
        output_limit_exceeded=limit_exceeded,
        termination_unproven=termination_unproven,
        stdout_raw=bytes(buffers["stdout"]),
        stderr_raw=bytes(buffers["stderr"]),
        stdout_count=counts["stdout"],
        stderr_count=counts["stderr"],
        stdout_sha256=digests["stdout"].hexdigest(),
        stderr_sha256=digests["stderr"].hexdigest(),
    )


def _terminate_process_group(process: subprocess.Popen[bytes], grace_seconds: float) -> bool:
    if process.poll() is not None:
        return True
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return True
        time.sleep(0.01)
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    try:
        process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        return False
    return True


def _process_status(
    observed: _ObservedProcess, intent: PhysicsOracleIntentV1
) -> tuple[OracleStatus, OracleFailureReason]:
    if observed.termination_unproven:
        return "infrastructure_failure", "termination_unproven"
    if observed.timed_out:
        return "timed_out", "timeout"
    if observed.output_limit_exceeded:
        return "output_contract_failure", "output_limit_exceeded"
    if observed.exit_code not in intent.execution_policy.accepted_exit_codes:
        return "functional_failure", "process_exit_not_accepted"
    return "passed", "none"


def _redacted_bytes(raw: bytes, sensitive_values: Sequence[str]) -> bytes:
    text = raw.decode("utf-8", errors="replace")
    return redact_text(text, sensitive_values).encode("utf-8")


def _write_diagnostics(directory: Path, stdout: bytes, stderr: bytes) -> None:
    directory.mkdir(exist_ok=True)
    _write_once_or_verify(directory / "stdout.log", stdout, "stdout diagnostic")
    _write_once_or_verify(directory / "stderr.log", stderr, "stderr diagnostic")


def _empty_stream() -> PhysicsOracleStreamDigestV1:
    return PhysicsOracleStreamDigestV1(
        observed_byte_length=0,
        observed_sha256=EMPTY_SHA256,
        captured_prefix_byte_length=0,
        captured_prefix_sha256=EMPTY_SHA256,
        truncated=False,
    )


def _collect_artifacts(
    scratch: Path, intent: PhysicsOracleIntentV1
) -> tuple[tuple[PhysicsOracleArtifactV1, ...], bool]:
    declarations = {item.path: item for item in intent.execution_policy.required_artifacts}
    try:
        records = _scratch_manifest(scratch, declarations)
    except (OSError, PhysicsOracleIntegrityError, ValidationError):
        return (), False
    by_path = {item.path: item for item in records}
    contract_ok = True
    for path, declaration in declarations.items():
        record = by_path.get(path)
        if record is None:
            contract_ok = contract_ok and not declaration.required
        elif record.kind != "regular" or record.byte_length > declaration.max_bytes:
            contract_ok = False
    for record in records:
        if record.kind in {"regular", "symlink"} and not record.declared:
            contract_ok = False
        if record.kind == "directory" and not any(
            path.startswith(f"{record.path}/") for path in declarations
        ):
            contract_ok = False
    return records, contract_ok


def _verify_artifacts(scratch: Path, artifacts: Sequence[PhysicsOracleArtifactV1]) -> None:
    try:
        declarations = {
            item.path: _ArtifactIdentity(item.id, item.byte_length)
            for item in artifacts
            if item.declared
        }
        actual = _scratch_manifest(scratch, declarations)
    except (OSError, ValidationError) as exc:
        raise PhysicsOracleIntegrityError("scratch artifacts could not be inspected") from exc
    if actual != tuple(artifacts):
        raise PhysicsOracleIntegrityError("scratch artifact manifest changed")


class _ArtifactIdentity:
    def __init__(self, identifier: str, max_bytes: int) -> None:
        self.id = identifier
        self.max_bytes = max_bytes


def _scratch_manifest(
    scratch: Path, declarations: Mapping[str, Any]
) -> tuple[PhysicsOracleArtifactV1, ...]:
    entries: list[tuple[str, Path]] = []

    def add_entry(candidate: Path) -> None:
        if len(entries) >= MAX_ORACLE_ARTIFACTS:
            raise PhysicsOracleIntegrityError("scratch entry count exceeds the v1 bound")
        entries.append((candidate.relative_to(scratch).as_posix(), candidate))

    for root, directories, files in os.walk(scratch, followlinks=False):
        root_path = Path(root)
        for name in tuple(directories):
            candidate = root_path / name
            add_entry(candidate)
            if candidate.is_symlink():
                directories.remove(name)
        for name in files:
            candidate = root_path / name
            add_entry(candidate)
    records: list[PhysicsOracleArtifactV1] = []
    total_bytes = 0
    for relative, candidate in sorted(entries):
        before = candidate.lstat()
        declaration = declarations.get(relative)
        identifier = (
            declaration.id
            if declaration is not None
            else f"undeclared-{hashlib.sha256(relative.encode('utf-8')).hexdigest()[:24]}"
        )
        if stat.S_ISREG(before.st_mode):
            kind: Literal["regular", "symlink", "directory"] = "regular"
            byte_length = before.st_size
            if declaration is not None and byte_length > declaration.max_bytes:
                raise PhysicsOracleIntegrityError("declared scratch artifact exceeds its bound")
            if declaration is None and byte_length > MAX_UNDECLARED_ARTIFACT_BYTES:
                raise PhysicsOracleIntegrityError("undeclared scratch artifact exceeds its bound")
            total_bytes += byte_length
            if total_bytes > MAX_ORACLE_ARTIFACT_BYTES:
                raise PhysicsOracleIntegrityError("aggregate scratch bytes exceed the v1 bound")
            digest = _sha256_file(candidate)
        elif stat.S_ISLNK(before.st_mode):
            kind = "symlink"
            target = os.fsencode(os.readlink(candidate))
            byte_length = len(target)
            total_bytes += byte_length
            if total_bytes > MAX_ORACLE_ARTIFACT_BYTES:
                raise PhysicsOracleIntegrityError("aggregate scratch bytes exceed the v1 bound")
            digest = hashlib.sha256(target).hexdigest()
        elif stat.S_ISDIR(before.st_mode):
            kind = "directory"
            byte_length = 0
            digest = EMPTY_SHA256
        else:
            raise PhysicsOracleIntegrityError("scratch contains a special filesystem object")
        after = candidate.lstat()
        if _stat_identity(before) != _stat_identity(after):
            raise PhysicsOracleIntegrityError("scratch changed during manifest collection")
        records.append(
            PhysicsOracleArtifactV1(
                id=identifier,
                path=relative,
                declared=declaration is not None,
                kind=kind,
                byte_length=byte_length,
                mode=stat.S_IMODE(before.st_mode),
                sha256=digest,
            )
        )
    return tuple(sorted(records, key=lambda item: item.id))


def _artifact_manifest_sha256(artifacts: Sequence[PhysicsOracleArtifactV1]) -> str:
    return hashlib.sha256(
        _canonical_bytes([item.model_dump(mode="json") for item in artifacts])
    ).hexdigest()


def _verify_diagnostic(path: Path, stream: PhysicsOracleStreamDigestV1) -> None:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise PhysicsOracleIntegrityError("oracle diagnostic is unavailable") from exc
    if (
        len(raw) != stream.captured_prefix_byte_length
        or hashlib.sha256(raw).hexdigest() != stream.captured_prefix_sha256
    ):
        raise PhysicsOracleIntegrityError("oracle diagnostic was replaced")


def _verify_captured_record(output: Path, current: PhysicsOracleActionRecordV1) -> None:
    if (
        current.phase != "output_captured"
        or current.stdout is None
        or current.stderr is None
        or current.execution_status is None
        or current.failure_reason is None
        or current.timed_out is None
        or current.structured_output_status is None
        or current.artifact_manifest_sha256 is None
    ):
        raise PhysicsOracleStateError("captured-output record is incomplete")
    _verify_diagnostic(output / DIAGNOSTICS_DIRECTORY / "stdout.log", current.stdout)
    _verify_diagnostic(output / DIAGNOSTICS_DIRECTORY / "stderr.log", current.stderr)
    _verify_artifacts(output / SCRATCH_DIRECTORY, current.artifacts)
    if _artifact_manifest_sha256(current.artifacts) != current.artifact_manifest_sha256:
        raise PhysicsOracleIntegrityError("captured artifact digest is invalid")
    if current.structured_output_status == "parsed":
        declared = _load_exact_model(
            output / DECLARED_RESULT_FILE,
            PhysicsOracleDeclaredResultV1,
            "declared oracle result",
        )
        if (
            declared.canonical_sha256() != current.structured_result_sha256
            or declared.outcome != current.declared_outcome
            or declared.oracle_id != current.request.oracle_id
        ):
            raise PhysicsOracleIntegrityError("declared oracle result was replaced")


def _verify_workspace_record(current: PhysicsOracleActionRecordV1) -> None:
    if (
        current.phase != "workspace_rechecked"
        or current.final_workspace_identity is None
        or current.execution_status is None
        or current.failure_reason is None
    ):
        raise PhysicsOracleStateError("workspace-rechecked record is incomplete")
    changed = current.final_workspace_identity != current.request.initial_workspace_identity
    if changed and (
        current.execution_status != "workspace_integrity_failure"
        or current.failure_reason != "workspace_changed"
    ):
        raise PhysicsOracleIntegrityError("workspace drift did not override the result")


def _verify_proof_matches_result(
    proof: PhysicsOracleCompletionProofV1,
    result: PhysicsOracleExecutionResultV1,
) -> None:
    expected: dict[str, object] = {
        "schema_version": 1,
        "task_id": result.request.task_id,
        "physics_contract_sha256": result.request.contract_sha256,
        "trusted_oracle_id": result.request.oracle_id,
        "trusted_intent_sha256": result.request.trusted_intent_sha256,
        "execution_policy_sha256": result.request.execution_policy_sha256,
        "network_enforcement": result.network_enforcement.model_dump(mode="json"),
        "environment_profile_sha256": result.environment_profile_sha256,
        "initial_workspace_identity": result.initial_workspace_identity.model_dump(mode="json"),
        "process_status": result.status,
        "failure_reason": result.failure_reason,
        "process_exit_code": result.process_exit_code,
        "timed_out": result.timed_out,
        "stdout": result.stdout.model_dump(mode="json"),
        "stderr": result.stderr.model_dump(mode="json"),
        "structured_output_status": result.structured_output_status,
        "structured_result_sha256": result.structured_result_sha256,
        "declared_outcome": result.declared_outcome,
        "artifact_manifest_sha256": result.artifact_manifest_sha256,
        "final_workspace_identity": result.final_workspace_identity.model_dump(mode="json"),
        "integrity_verdict": result.integrity_verdict,
    }
    if proof.model_dump(mode="json") != expected:
        raise PhysicsOracleIntegrityError("completion proof contradicts the result")


def _verify_final_record_matches_result(
    record: PhysicsOracleActionRecordV1,
    result: PhysicsOracleExecutionResultV1,
) -> None:
    expected: dict[str, object] = {
        "request": result.request,
        "network_enforcement": result.network_enforcement,
        "environment_profile_sha256": result.environment_profile_sha256,
        "process_exit_code": result.process_exit_code,
        "timed_out": result.timed_out,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "execution_status": result.status,
        "failure_reason": result.failure_reason,
        "structured_output_status": result.structured_output_status,
        "structured_result_sha256": result.structured_result_sha256,
        "declared_outcome": result.declared_outcome,
        "artifacts": result.artifacts,
        "artifact_manifest_sha256": result.artifact_manifest_sha256,
        "final_workspace_identity": result.final_workspace_identity,
    }
    if any(getattr(record, name) != value for name, value in expected.items()):
        raise PhysicsOracleIntegrityError("final action record contradicts the result")


def _append_record(
    output: Path,
    *,
    request: PhysicsOracleExecutionRequestV1,
    phase: Any,
    previous: PhysicsOracleActionRecordV1 | None,
    **updates: object,
) -> PhysicsOracleActionRecordV1:
    sequence = 1 if previous is None else previous.sequence + 1
    prior_hash = ZERO_HASH if previous is None else previous.canonical_sha256()
    try:
        record_type = cast(Any, PhysicsOracleActionRecordV1)
        draft = record_type.model_construct(
            schema_version=1,
            record_sha256=ZERO_HASH,
            sequence=sequence,
            phase=phase,
            previous_record_sha256=prior_hash,
            request=request,
            **updates,
        )
        payload = draft.model_dump(mode="json", exclude={"record_sha256"})
        record = PhysicsOracleActionRecordV1.model_validate(
            {
                **payload,
                "record_sha256": hashlib.sha256(_canonical_bytes(payload)).hexdigest(),
            }
        )
    except ValidationError as exc:
        raise PhysicsOracleStateError("oracle action boundary is contradictory") from exc
    records = output / RECORDS_DIRECTORY
    records.mkdir(exist_ok=True)
    path = records / f"{sequence:03d}-{phase}.json"
    _write_once_or_verify(
        path,
        render_json_bytes(record.model_dump(mode="json")),
        "oracle action record",
    )
    return record


def _load_records(output: Path) -> tuple[PhysicsOracleActionRecordV1, ...]:
    records_directory = output / RECORDS_DIRECTORY
    try:
        paths = sorted(records_directory.glob("*.json"))
    except OSError as exc:
        raise PhysicsOracleStateError("oracle action records are unavailable") from exc
    records: list[PhysicsOracleActionRecordV1] = []
    previous_hash = ZERO_HASH
    previous_sequence = 0
    for path in paths:
        record = _load_exact_model(path, PhysicsOracleActionRecordV1, "oracle action record")
        expected_name = f"{record.sequence:03d}-{record.phase}.json"
        if (
            path.name != expected_name
            or record.sequence != previous_sequence + 1
            or record.previous_record_sha256 != previous_hash
            or (records and record.request != records[0].request)
        ):
            raise PhysicsOracleIntegrityError("oracle action record chain is invalid")
        records.append(record)
        previous_sequence = record.sequence
        previous_hash = record.canonical_sha256()
    _validate_phase_sequence(tuple(item.phase for item in records))
    return tuple(records)


def _validate_phase_sequence(phases: tuple[str, ...]) -> None:
    if not phases or phases[0] != "intent_accepted":
        raise PhysicsOracleIntegrityError("oracle action lacks its accepted intent")
    normal = (
        "intent_accepted",
        "execution_prepared",
        "process_launch_attempted",
        "process_running",
        "process_exit_observed",
        "output_captured",
        "workspace_rechecked",
        "completion_proof_finalized",
    )
    no_process_prefixes = {
        ("intent_accepted", "output_captured"),
        ("intent_accepted", "execution_prepared", "output_captured"),
        (
            "intent_accepted",
            "execution_prepared",
            "process_launch_attempted",
            "output_captured",
        ),
        (
            "intent_accepted",
            "execution_prepared",
            "process_launch_attempted",
            "process_running",
            "output_captured",
        ),
    }
    valid = phases == normal[: len(phases)]
    for prefix in no_process_prefixes:
        tail = ("workspace_rechecked", "completion_proof_finalized")
        candidate = prefix + tail
        valid = valid or phases == candidate[: len(phases)]
    if not valid:
        raise PhysicsOracleIntegrityError("oracle action phase sequence is invalid")


def _load_exact_model(path: Path, model: type[Any], label: str) -> Any:
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("ascii"), parse_constant=_reject_constant)
        parsed = model.model_validate(value)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValidationError, ValueError) as exc:
        raise PhysicsOracleIntegrityError(f"{label} is malformed or unavailable") from exc
    if raw != render_json_bytes(parsed.model_dump(mode="json")):
        raise PhysicsOracleIntegrityError(f"{label} is not canonically encoded")
    return parsed


def _write_once_or_verify(path: Path, value: bytes, label: str) -> None:
    if path.exists():
        try:
            existing = path.read_bytes()
        except OSError as exc:
            raise PhysicsOracleStateError(f"{label} could not be verified") from exc
        if existing != value:
            raise PhysicsOracleIntegrityError(f"{label} already exists with different bytes")
        return
    atomic_write_bytes(
        path,
        value,
        error_factory=PhysicsOracleStateError,
        error_message=f"{label} could not be persisted",
    )


def _new_output_directory(path: Path, workspace: Path) -> Path:
    if ".." in path.parts or not path.name:
        raise PhysicsOracleInputError("oracle output path is invalid")
    try:
        parent = path.parent.resolve(strict=True)
        output = parent / path.name
    except (OSError, RuntimeError, ValueError) as exc:
        raise PhysicsOracleInputError("oracle output parent is unavailable") from exc
    if output.exists() or _paths_overlap(output, workspace):
        raise PhysicsOracleInputError(
            "oracle output must be new and must not overlap the workspace"
        )
    try:
        output.mkdir(mode=0o700)
    except OSError as exc:
        raise PhysicsOracleInputError("oracle output directory could not be created") from exc
    return output


def _existing_output_directory(path: Path) -> Path:
    if ".." in path.parts:
        raise PhysicsOracleInputError("oracle output path contains parent traversal")
    try:
        absolute = Path(os.path.abspath(path))
        resolved = path.resolve(strict=True)
        metadata = path.lstat()
    except (OSError, RuntimeError, ValueError) as exc:
        raise PhysicsOracleInputError("oracle output directory is unavailable") from exc
    if absolute != resolved or path.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
        raise PhysicsOracleInputError("oracle output must be a canonical directory")
    return resolved


def _canonical_directory(path: Path, label: str) -> Path:
    if ".." in path.parts:
        raise PhysicsOracleInputError(f"{label} contains parent traversal")
    try:
        absolute = Path(os.path.abspath(path))
        resolved = path.resolve(strict=True)
        metadata = path.lstat()
    except (OSError, RuntimeError, ValueError) as exc:
        raise PhysicsOracleInputError(f"{label} could not be resolved") from exc
    if absolute != resolved or path.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
        raise PhysicsOracleInputError(f"{label} must be a canonical directory")
    return resolved


@contextmanager
def _action_lock(output: Path) -> Any:
    lock_path = output / ".oracle.lock"
    descriptor = os.open(
        lock_path,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise PhysicsOracleStateError("oracle action is already locked") from exc
        yield
    finally:
        with suppress(OSError):
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _process_start_ticks(pid: int) -> int | None:
    try:
        value = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        close = value.rfind(")")
        fields = value[close + 2 :].split()
        ticks = int(fields[19])
    except (OSError, ValueError, IndexError):
        return None
    return ticks if ticks > 0 else None


def _process_identity_running(pid: int, expected_ticks: int) -> bool:
    try:
        value = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        close = value.rfind(")")
        fields = value[close + 2 :].split()
        return int(fields[19]) == expected_ticks and fields[0] != "Z"
    except (OSError, ValueError, IndexError):
        return False


def _terminate_identity(identity: PhysicsOracleProcessIdentityV1) -> None:
    if not _process_identity_running(identity.pid, identity.start_ticks):
        return
    with suppress(ProcessLookupError):
        os.killpg(identity.process_group_id, signal.SIGTERM)
    deadline = time.monotonic() + TERMINATION_GRACE_SECONDS
    while time.monotonic() < deadline:
        if not _process_identity_running(identity.pid, identity.start_ticks):
            return
        time.sleep(0.01)
    if _process_identity_running(identity.pid, identity.start_ticks):
        with suppress(ProcessLookupError):
            os.killpg(identity.process_group_id, signal.SIGKILL)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_mode


def _canonical_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("ascii")


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left.is_relative_to(right) or right.is_relative_to(left)


def _reject_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")
