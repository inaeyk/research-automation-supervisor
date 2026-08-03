"""Standalone fresh-session Codex Physics Auditor execution and recovery (PA-3)."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import signal
import stat
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal, Protocol, TypeAlias, cast

from pydantic import ValidationError

from research_automation_supervisor.codex_adapter import (
    AUDITOR_SANDBOX_DISPOSITION,
    AdapterLimits,
    CodexProcessLaunch,
    build_subprocess_environment,
    run_prepared_codex,
)
from research_automation_supervisor.codex_models import (
    ROLE_POLICIES,
    CodexRunRequest,
    CodexRunResult,
    PreparedCodexRequest,
)
from research_automation_supervisor.durable_state import (
    ZERO_HASH,
    atomic_write_bytes,
    canonical_json,
    render_json_bytes,
)
from research_automation_supervisor.errors import (
    CodexAdapterError,
    LiveShadowDependencyError,
    LiveShadowIntegrityError,
    PhysicsAuditError,
    PhysicsAuditorDependencyError,
    PhysicsAuditorInputError,
    PhysicsAuditorIntegrityError,
    PhysicsAuditorStateError,
)
from research_automation_supervisor.live_shadow_isolation import (
    RECORDED_AUTH_SOURCE,
    BubblewrapBackendIdentity,
    build_bubblewrap_process_launch,
    load_backend_identity,
    preflight_bubblewrap_isolation,
    reset_runtime_home_contents,
    verify_projected_auditor_bubblewrap_command,
    write_backend_identity,
)
from research_automation_supervisor.physics_auditor_evidence import (
    DiscoveredPhysicsAuditorEvidence,
    collect_changed_path_manifest,
    discover_physics_auditor_evidence,
    validate_report_evidence_index,
    verify_discovered_physics_auditor_evidence,
)
from research_automation_supervisor.physics_auditor_models import (
    MAX_PHYSICS_AUDITOR_OUTPUT_BYTES,
    PHYSICS_AUDITOR_BUBBLEWRAP_POLICY_V1,
    PHYSICS_AUDITOR_CODEX_ROLE_POLICY_V1,
    PhysicsAuditorActionProofV1,
    PhysicsAuditorActionRecordV1,
    PhysicsAuditorActionRequestV1,
    PhysicsAuditorActionResultV1,
    PhysicsAuditorChangedPathManifestV1,
    PhysicsAuditorExecutionConfigV1,
    PhysicsAuditorFailureReason,
    PhysicsAuditorProcessIdentityV1,
    PhysicsAuditorProviderObservationV1,
    PhysicsAuditorStatus,
    load_physics_auditor_execution_config,
    validate_trusted_codex_executable,
    zero_sha256,
)
from research_automation_supervisor.physics_auditor_projection import (
    PROJECTION_DIRECTORY,
    PROJECTION_MANIFEST_FILE,
    RUNTIME_HOME_DIRECTORY,
    PhysicsAuditorProjectionPlan,
    build_physics_auditor_projection,
    materialize_physics_auditor_projection,
    verify_physics_auditor_projection,
)
from research_automation_supervisor.physics_auditor_prompts import (
    RenderedPhysicsAuditorPrompt,
    build_physics_auditor_prompt,
)
from research_automation_supervisor.physics_models import (
    DEFAULT_PHYSICS_AUDIT_POLICY_V1,
    PHYSICS_AUDIT_REPORT_OUTPUT_SCHEMA,
    PhysicsAuditReportV1,
    PhysicsTaskContractV1,
    load_physics_task_contract,
    parse_physics_audit_report_json,
)
from research_automation_supervisor.physics_oracle_execution import (
    CONTROL_DIRECTORY as ORACLE_CONTROL_DIRECTORY,
)
from research_automation_supervisor.physics_oracle_execution import (
    SEALED_INTENT_FILE,
)
from research_automation_supervisor.physics_oracle_models import (
    PhysicsOracleIntentV1,
    PhysicsOracleWorkspaceIdentityV1,
)
from research_automation_supervisor.physics_oracle_workspace import (
    collect_physics_oracle_workspace_identity,
)
from research_automation_supervisor.physics_routing import (
    PhysicsRoutingDecisionV1,
    derive_physics_audit_decision,
)
from research_automation_supervisor.workflow_integrity import (
    CodexMetadata,
    NormalizedCodexRequest,
    Stage2CompletionManifest,
)

RECORDS_DIRECTORY = "action-records"
CONTROL_DIRECTORY = "control"
BACKEND_DIRECTORY = "codex-action"
CONFIG_FILE = "execution-config.json"
CONTRACT_FILE = "physics-contract.json"
CHANGED_PATHS_FILE = "changed-path-manifest.json"
EVIDENCE_INDEX_FILE = "evidence-index.json"
REQUEST_FILE = "action-request.json"
PROMPT_FILE = "prompt.txt"
OUTPUT_SCHEMA_FILE = "physics-audit-report-output-schema.json"
ROLE_POLICY_FILE = "codex-role-policy.json"
BUBBLEWRAP_POLICY_FILE = "bubblewrap-policy.json"
BUBBLEWRAP_BACKEND_FILE = "bubblewrap-backend-identity.json"
PROVIDER_OBSERVATION_FILE = "provider-observation.json"
MODEL_OUTPUT_FILE = "model-output.json"
REPORT_FILE = "physics-audit-report.json"
ROUTING_FILE = "routing-decision.json"
RESULT_FILE = "result.json"
PROOF_FILE = "action-proof.json"

Checkpoint = Callable[[str], None]
ProjectionIntegrity: TypeAlias = Literal["not_materialized", "unchanged", "changed"]


@dataclass(frozen=True)
class PhysicsAuditorCodexRun:
    """Codex-only test seam and production observation; not a provider abstraction."""

    adapter_result: CodexRunResult
    model_output: bytes
    model_output_truncated: bool
    provider_session_id: str | None
    provider_thread_started_ids: tuple[str, ...]
    backend_policy_evidence_sha256: str
    bubblewrap_backend_identity_sha256: str
    codex_executable_sha256: str
    codex_cli_version: str | None
    oracle_execution_detected: bool = False


class PhysicsAuditorCodexInvoker(Protocol):
    """Exact Codex action seam used by deterministic scripted tests."""

    def __call__(
        self,
        *,
        prepared: PreparedCodexRequest,
        runs_dir: Path,
        codex_executable: Path,
        config: PhysicsAuditorExecutionConfigV1,
        output_schema: Path,
        environ: Mapping[str, str] | None,
        process_started: Callable[[int], None],
        source_workspace: Path,
        oracle_evidence_root: Path,
        action_root: Path,
    ) -> PhysicsAuditorCodexRun: ...


@dataclass(frozen=True)
class PhysicsAuditorValidationSummaryV1:
    """Read-only validation summary; it is not an action result or proof."""

    task_id: str
    action_id: str
    contract_sha256: str
    execution_config_sha256: str
    workspace_identity_sha256: str
    evidence_index_sha256: str
    prompt_sha256: str
    projection_manifest_sha256: str
    missing_required_oracle_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "ok": True,
            "validate_only": True,
            "task_id": self.task_id,
            "action_id": self.action_id,
            "contract_sha256": self.contract_sha256,
            "execution_config_sha256": self.execution_config_sha256,
            "workspace_identity_sha256": self.workspace_identity_sha256,
            "evidence_index_sha256": self.evidence_index_sha256,
            "prompt_sha256": self.prompt_sha256,
            "projection_manifest_sha256": self.projection_manifest_sha256,
            "missing_required_oracle_ids": list(self.missing_required_oracle_ids),
            "model_launched": False,
        }


@dataclass(frozen=True)
class _PreparedAction:
    config: PhysicsAuditorExecutionConfigV1
    contract: PhysicsTaskContractV1
    workspace: Path
    oracle_evidence_root: Path
    initial_identity: PhysicsOracleWorkspaceIdentityV1
    changed_paths: PhysicsAuditorChangedPathManifestV1
    discovered: DiscoveredPhysicsAuditorEvidence
    prompt: RenderedPhysicsAuditorPrompt
    projection: PhysicsAuditorProjectionPlan
    request: PhysicsAuditorActionRequestV1


def validate_physics_auditor_action(
    *,
    contract_path: Path,
    execution_config_path: Path,
    task_id: str,
    workspace: Path,
    oracle_evidence_root: Path,
    action_id: str | None = None,
    attempt_number: int = 1,
) -> PhysicsAuditorValidationSummaryV1:
    """Validate all launch authority read-only, without locating or invoking Codex."""
    prepared = _prepare_action(
        contract_path=contract_path,
        execution_config_path=execution_config_path,
        task_id=task_id,
        workspace=workspace,
        oracle_evidence_root=oracle_evidence_root,
        action_id=action_id,
        attempt_number=attempt_number,
    )
    verify_discovered_physics_auditor_evidence(
        discovered=prepared.discovered,
        contract=prepared.contract,
        task_id=task_id,
        workspace_identity=prepared.initial_identity,
    )
    missing = tuple(
        item.oracle_id
        for item in prepared.discovered.index.oracle_evidence
        if item.required and item.availability == "missing"
    )
    return PhysicsAuditorValidationSummaryV1(
        task_id=task_id,
        action_id=prepared.request.action_id,
        contract_sha256=prepared.contract.canonical_sha256(),
        execution_config_sha256=prepared.config.canonical_sha256(),
        workspace_identity_sha256=prepared.initial_identity.canonical_sha256(),
        evidence_index_sha256=prepared.discovered.index.canonical_sha256(),
        prompt_sha256=prepared.prompt.rendered_sha256,
        projection_manifest_sha256=prepared.projection.manifest.canonical_sha256(),
        missing_required_oracle_ids=missing,
    )


def run_physics_auditor(
    *,
    contract_path: Path,
    execution_config_path: Path,
    task_id: str,
    workspace: Path,
    oracle_evidence_root: Path,
    output_directory: Path,
    action_id: str | None = None,
    attempt_number: int = 1,
    environ: Mapping[str, str] | None = None,
    codex_invoker: PhysicsAuditorCodexInvoker | None = None,
    checkpoint: Checkpoint = lambda _name: None,
) -> PhysicsAuditorActionResultV1:
    """Create and execute one fresh read-only standalone Physics Auditor action."""
    prepared = _prepare_action(
        contract_path=contract_path,
        execution_config_path=execution_config_path,
        task_id=task_id,
        workspace=workspace,
        oracle_evidence_root=oracle_evidence_root,
        action_id=action_id,
        attempt_number=attempt_number,
    )
    output = _new_output_directory(
        output_directory,
        prepared.workspace,
        prepared.oracle_evidence_root,
    )
    with _action_lock(output):
        _persist_accepted_control(output, prepared)
        current = _append_record(
            output,
            request=prepared.request,
            phase="action_accepted",
            previous=None,
        )
        checkpoint("action_accepted")
        return _continue_action(
            output=output,
            prepared=prepared,
            current=current,
            environ=environ,
            codex_invoker=codex_invoker or _invoke_qualified_codex,
            checkpoint=checkpoint,
        )


def resume_physics_auditor(
    *,
    contract_path: Path,
    execution_config_path: Path,
    task_id: str,
    workspace: Path,
    oracle_evidence_root: Path,
    output_directory: Path,
    action_id: str | None = None,
    attempt_number: int = 1,
    environ: Mapping[str, str] | None = None,
    codex_invoker: PhysicsAuditorCodexInvoker | None = None,
    checkpoint: Checkpoint = lambda _name: None,
) -> PhysicsAuditorActionResultV1:
    """Recover without resuming a session or blindly repeating a possible launch."""
    output = _existing_output_directory(output_directory)
    records = _load_records(output)
    if not records:
        raise PhysicsAuditorStateError("Physics Auditor action has no durable request")
    current = records[-1]
    requested_action_id = action_id or current.request.action_id
    prepared = _prepare_action(
        contract_path=contract_path,
        execution_config_path=execution_config_path,
        task_id=task_id,
        workspace=workspace,
        oracle_evidence_root=oracle_evidence_root,
        action_id=requested_action_id,
        attempt_number=attempt_number,
    )
    if prepared.request != current.request:
        raise PhysicsAuditorIntegrityError(
            "model, config, prompt, evidence, contract, or workspace authority changed"
        )
    with _action_lock(output):
        _verify_accepted_control(output, prepared)
        if current.phase == "action_proof_finalized":
            return verify_physics_auditor_action(
                contract_path=contract_path,
                execution_config_path=execution_config_path,
                task_id=task_id,
                workspace=workspace,
                oracle_evidence_root=oracle_evidence_root,
                output_directory=output,
                action_id=requested_action_id,
                attempt_number=attempt_number,
            )
        if current.phase in {"model_launch_attempted", "model_running"}:
            return _finalize_ambiguous_recovery(
                output=output,
                prepared=prepared,
                current=current,
                checkpoint=checkpoint,
            )
        return _continue_action(
            output=output,
            prepared=prepared,
            current=current,
            environ=environ,
            codex_invoker=codex_invoker or _invoke_qualified_codex,
            checkpoint=checkpoint,
        )


def verify_physics_auditor_action(
    *,
    contract_path: Path,
    execution_config_path: Path,
    task_id: str,
    workspace: Path,
    oracle_evidence_root: Path,
    output_directory: Path,
    action_id: str | None = None,
    attempt_number: int = 1,
) -> PhysicsAuditorActionResultV1:
    """Independently verify request, evidence, prompt, report, route, and proof."""
    output = _existing_output_directory(output_directory)
    records = _load_records(output)
    if not records or records[-1].phase != "action_proof_finalized":
        raise PhysicsAuditorIntegrityError("Physics Auditor completion proof is absent")
    current = records[-1]
    prepared = _prepare_action(
        contract_path=contract_path,
        execution_config_path=execution_config_path,
        task_id=task_id,
        workspace=workspace,
        oracle_evidence_root=oracle_evidence_root,
        action_id=action_id or current.request.action_id,
        attempt_number=attempt_number,
    )
    if prepared.request != current.request:
        raise PhysicsAuditorIntegrityError("Physics Auditor action authority was substituted")
    verify_discovered_physics_auditor_evidence(
        discovered=prepared.discovered,
        contract=prepared.contract,
        task_id=task_id,
        workspace_identity=prepared.initial_identity,
    )
    _verify_accepted_control(output, prepared)
    observed_projection_integrity = _projection_integrity(output, prepared)
    result = _load_exact_model(output / RESULT_FILE, PhysicsAuditorActionResultV1, "action result")
    proof = _load_exact_model(output / PROOF_FILE, PhysicsAuditorActionProofV1, "action proof")
    if (
        current.result_sha256 != result.canonical_sha256()
        or current.action_proof_sha256 != proof.canonical_sha256()
        or result.action_proof_sha256 != proof.canonical_sha256()
        or result.request != prepared.request
    ):
        raise PhysicsAuditorIntegrityError("Physics Auditor completion hashes do not close")
    report: PhysicsAuditReportV1 | None = None
    decision: PhysicsRoutingDecisionV1 | None = None
    if result.report_validated:
        try:
            report = _load_persisted_report(output, prepared)
        except (PhysicsAuditError, PhysicsAuditorIntegrityError) as exc:
            raise PhysicsAuditorIntegrityError(
                "persisted Physics Auditor report is invalid"
            ) from exc
        if report.canonical_sha256() != result.parsed_report_sha256:
            raise PhysicsAuditorIntegrityError("persisted Physics Auditor report was replaced")
    if result.routing_decision is not None:
        decision = _load_exact_model(
            output / ROUTING_FILE, PhysicsRoutingDecisionV1, "routing decision"
        )
        expected_decision = derive_physics_audit_decision(
            prepared.contract,
            prepared.contract.audit_policy or DEFAULT_PHYSICS_AUDIT_POLICY_V1,
            report,
        )
        if decision != expected_decision or decision != result.routing_decision:
            raise PhysicsAuditorIntegrityError("deterministic Physics Auditor routing was replaced")
    provider = _load_provider_if_present(output)
    if provider is not None:
        _verify_persisted_provider(output, prepared, provider)
        detected = _detect_oracle_execution_in_events(
            Path(provider.adapter_result.artifact_directory) / "events.jsonl",
            prepared.discovered,
        )
        if detected != provider.oracle_execution_detected:
            raise PhysicsAuditorIntegrityError("Codex oracle-execution observation was substituted")
    model_output = _read_optional_bytes(output / MODEL_OUTPUT_FILE)
    if (
        hashlib.sha256(model_output).hexdigest() != result.model_output_sha256
        or len(model_output) != result.model_output_byte_length
    ):
        raise PhysicsAuditorIntegrityError("bounded Physics Auditor model output was replaced")
    expected_proof = _proof(
        prepared=prepared,
        status=result.status,
        provider=provider,
        model_output=model_output,
        report=report,
        decision=decision,
        post_identity=result.post_model_workspace_identity,
        final_identity=result.final_workspace_identity,
        projection_integrity=result.projected_workspace_integrity,
    )
    if proof != expected_proof:
        raise PhysicsAuditorIntegrityError("Physics Auditor action proof was replaced")
    if prepared.initial_identity != result.final_workspace_identity:
        raise PhysicsAuditorIntegrityError("workspace changed after Physics Auditor completion")
    if observed_projection_integrity != result.projected_workspace_integrity:
        raise PhysicsAuditorIntegrityError("projected workspace integrity was substituted")
    return cast(PhysicsAuditorActionResultV1, result)


def _prepare_action(
    *,
    contract_path: Path,
    execution_config_path: Path,
    task_id: str,
    workspace: Path,
    oracle_evidence_root: Path,
    action_id: str | None,
    attempt_number: int,
) -> _PreparedAction:
    config = load_physics_auditor_execution_config(execution_config_path)
    contract = load_physics_task_contract(contract_path)
    workspace_root = _canonical_workspace(workspace)
    try:
        evidence_root = oracle_evidence_root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise PhysicsAuditorInputError("oracle evidence root is unavailable") from exc
    initial = collect_physics_oracle_workspace_identity(workspace_root)
    changed = collect_changed_path_manifest(workspace_root, initial)
    selected_action_id = action_id or _default_action_id(task_id, attempt_number)
    discovered = discover_physics_auditor_evidence(
        contract=contract,
        task_id=task_id,
        workspace=workspace_root,
        workspace_identity=initial,
        changed_paths=changed,
        oracle_evidence_root=oracle_evidence_root,
    )
    if set(changed.paths) & set(discovered.oracle_program_paths):
        raise PhysicsAuditorInputError("candidate delta includes a sealed PA-2 oracle program")
    projection = build_physics_auditor_projection(
        contract=contract,
        evidence_index=discovered.index,
        changed_paths=changed,
        source_workspace=workspace_root,
        oracle_program_paths=discovered.oracle_program_paths,
    )
    prompt = build_physics_auditor_prompt(
        contract,
        discovered.index,
        changed,
        projection.manifest,
    )
    derivations = tuple(
        item.path
        for item in contract.evidence
        if item.kind == "derivation" and item.path is not None
    )
    documents = tuple(
        item.path for item in contract.evidence if item.kind == "document" and item.path is not None
    )
    try:
        request = PhysicsAuditorActionRequestV1(
            schema_version=1,
            action_id=selected_action_id,
            task_id=task_id,
            physics_contract_sha256=contract.canonical_sha256(),
            execution_config_sha256=config.canonical_sha256(),
            workspace_identity_sha256=initial.canonical_sha256(),
            changed_path_manifest_sha256=changed.canonical_sha256(),
            evidence_index_sha256=discovered.index.canonical_sha256(),
            projection_manifest_sha256=projection.manifest.canonical_sha256(),
            bubblewrap_policy_sha256=PHYSICS_AUDITOR_BUBBLEWRAP_POLICY_V1.canonical_sha256(),
            oracle_completion_proofs=discovered.bindings,
            declared_derivation_paths=derivations,
            declared_document_paths=documents,
            prompt_template_version=cast(Any, prompt.template_version),
            prompt_template_sha256=prompt.template_sha256,
            output_schema_sha256=prompt.output_schema_sha256,
            attempt_number=attempt_number,
            output_directory_identity="standalone_physics_auditor_action_v1",
        )
    except ValidationError as exc:
        raise PhysicsAuditorInputError("Physics Auditor action request is invalid") from exc
    return _PreparedAction(
        config,
        contract,
        workspace_root,
        evidence_root,
        initial,
        changed,
        discovered,
        prompt,
        projection,
        request,
    )


def _continue_action(
    *,
    output: Path,
    prepared: _PreparedAction,
    current: PhysicsAuditorActionRecordV1,
    environ: Mapping[str, str] | None,
    codex_invoker: PhysicsAuditorCodexInvoker,
    checkpoint: Checkpoint,
) -> PhysicsAuditorActionResultV1:
    if current.phase == "action_accepted":
        try:
            verify_discovered_physics_auditor_evidence(
                discovered=prepared.discovered,
                contract=prepared.contract,
                task_id=prepared.request.task_id,
                workspace_identity=prepared.initial_identity,
            )
        except PhysicsAuditorIntegrityError:
            identity = collect_physics_oracle_workspace_identity(prepared.workspace)
            return _finalize(
                output=output,
                prepared=prepared,
                current=current,
                status=(
                    "workspace_integrity_failure"
                    if identity != prepared.initial_identity
                    else "evidence_integrity_failure"
                ),
                failure_reason=(
                    "workspace_changed"
                    if identity != prepared.initial_identity
                    else "evidence_missing_or_invalid"
                ),
                provider=None,
                model_output=b"",
                report=None,
                decision=None,
                post_identity=identity,
                final_identity=identity,
                checkpoint=checkpoint,
            )
        _prepare_projection_layout(output)
        materialize_physics_auditor_projection(
            prepared.projection,
            _projection_root(output),
        )
        current = _append_record(
            output,
            request=prepared.request,
            phase="evidence_verified",
            previous=current,
            projected_workspace_integrity="unchanged",
        )
        checkpoint("evidence_verified")
    if current.phase == "evidence_verified":
        _persist_prompt_control(output, prepared)
        current = _append_record(
            output,
            request=prepared.request,
            phase="prompt_finalized",
            previous=current,
        )
        checkpoint("prompt_finalized")
    if current.phase == "prompt_finalized":
        launch_identity = collect_physics_oracle_workspace_identity(prepared.workspace)
        projection_integrity = _projection_integrity(output, prepared)
        if launch_identity != prepared.initial_identity or projection_integrity != "unchanged":
            return _finalize(
                output=output,
                prepared=prepared,
                current=current,
                status="workspace_integrity_failure",
                failure_reason=(
                    "workspace_changed"
                    if launch_identity != prepared.initial_identity
                    else "projection_changed"
                ),
                provider=None,
                model_output=b"",
                report=None,
                decision=None,
                post_identity=launch_identity,
                final_identity=launch_identity,
                checkpoint=checkpoint,
            )
        executable = _select_codex_executable(prepared.config)
        codex_prepared = _prepared_codex_request(output, prepared)
        current = _append_record(
            output,
            request=prepared.request,
            phase="model_launch_attempted",
            previous=current,
        )
        checkpoint("model_launch_attempted")

        def process_started(pid: int) -> None:
            nonlocal current
            ticks = _process_start_ticks(pid)
            if ticks is None:
                raise PhysicsAuditorStateError("Codex process identity could not be proven")
            current = _append_record(
                output,
                request=prepared.request,
                phase="model_running",
                previous=current,
                process_identity=PhysicsAuditorProcessIdentityV1(
                    pid=pid,
                    process_group_id=pid,
                    start_ticks=ticks,
                ),
            )
            checkpoint("model_running")

        try:
            codex_run = codex_invoker(
                prepared=codex_prepared,
                runs_dir=output / BACKEND_DIRECTORY,
                codex_executable=executable,
                config=prepared.config,
                output_schema=_isolated_output_schema(
                    output,
                    prepared.request.action_id,
                ),
                environ=environ,
                process_started=process_started,
                source_workspace=prepared.workspace,
                oracle_evidence_root=prepared.oracle_evidence_root,
                action_root=output,
            )
        except (CodexAdapterError, LiveShadowDependencyError, LiveShadowIntegrityError) as exc:
            raise PhysicsAuditorDependencyError("Codex Physics Auditor transport failed") from exc
        codex_run = replace(
            codex_run,
            oracle_execution_detected=_detect_oracle_execution(
                codex_run,
                prepared.discovered,
            ),
        )
        post_identity = collect_physics_oracle_workspace_identity(prepared.workspace)
        projection_integrity = _projection_integrity(output, prepared)
        _persist_provider_observation(output, codex_run)
        current = _append_record(
            output,
            request=prepared.request,
            phase="model_exit_observed",
            previous=current,
            process_identity=current.process_identity,
            provider_status=codex_run.adapter_result.status,
            provider_session_id=codex_run.provider_session_id,
            provider_thread_started_ids=codex_run.provider_thread_started_ids,
            backend_artifact_manifest_sha256=codex_run.backend_policy_evidence_sha256,
            post_model_workspace_identity=post_identity,
            projected_workspace_integrity=projection_integrity,
        )
        checkpoint("model_exit_observed")
    if current.phase == "model_exit_observed":
        provider_observation = _load_provider_observation(output)
        model_output = _read_exact_bytes(output / MODEL_OUTPUT_FILE)
        recorded_post_identity = current.post_model_workspace_identity
        if recorded_post_identity is None:
            raise PhysicsAuditorStateError("post-model workspace identity is absent")
        current = _append_record(
            output,
            request=prepared.request,
            phase="output_captured",
            previous=current,
            process_identity=current.process_identity,
            provider_status=provider_observation.adapter_result.status,
            provider_session_id=provider_observation.provider_session_id,
            provider_thread_started_ids=provider_observation.provider_thread_started_ids,
            backend_artifact_manifest_sha256=(provider_observation.backend_policy_evidence_sha256),
            model_output_sha256=hashlib.sha256(model_output).hexdigest(),
            model_output_byte_length=len(model_output),
            post_model_workspace_identity=recorded_post_identity,
            projected_workspace_integrity=current.projected_workspace_integrity,
        )
        checkpoint("output_captured")
    if current.phase == "output_captured":
        provider = _load_provider_observation(output)
        model_output = _read_exact_bytes(output / MODEL_OUTPUT_FILE)
        post_identity = cast(Any, current.post_model_workspace_identity)
        projection_integrity = _projection_integrity(output, prepared)
        if post_identity != prepared.initial_identity or projection_integrity != "unchanged":
            return _finalize(
                output=output,
                prepared=prepared,
                current=current,
                status="workspace_integrity_failure",
                failure_reason=(
                    "workspace_changed"
                    if post_identity != prepared.initial_identity
                    else "projection_changed"
                ),
                provider=provider,
                model_output=model_output,
                report=None,
                decision=None,
                post_identity=post_identity,
                final_identity=post_identity,
                checkpoint=checkpoint,
            )
        process_failure = _provider_failure(provider)
        if process_failure is not None:
            return _finalize(
                output=output,
                prepared=prepared,
                current=current,
                status="infrastructure_failure",
                failure_reason=process_failure,
                provider=provider,
                model_output=model_output,
                report=None,
                decision=None,
                post_identity=post_identity,
                final_identity=post_identity,
                checkpoint=checkpoint,
            )
        try:
            report = parse_physics_audit_report_json(model_output, prepared.contract)
            validate_report_evidence_index(report, prepared.discovered.index)
        except (PhysicsAuditError, PhysicsAuditorIntegrityError):
            return _finalize(
                output=output,
                prepared=prepared,
                current=current,
                status="report_invalid",
                failure_reason="invalid_structured_output",
                provider=provider,
                model_output=model_output,
                report=None,
                decision=None,
                post_identity=post_identity,
                final_identity=post_identity,
                checkpoint=checkpoint,
            )
        _write_once_or_verify(output / REPORT_FILE, report.to_canonical_json(), "validated report")
        current = _append_record(
            output,
            request=prepared.request,
            phase="report_validated",
            previous=current,
            process_identity=current.process_identity,
            provider_status=provider.adapter_result.status,
            provider_session_id=provider.provider_session_id,
            provider_thread_started_ids=provider.provider_thread_started_ids,
            backend_artifact_manifest_sha256=provider.backend_policy_evidence_sha256,
            model_output_sha256=hashlib.sha256(model_output).hexdigest(),
            model_output_byte_length=len(model_output),
            parsed_report_sha256=report.canonical_sha256(),
            post_model_workspace_identity=post_identity,
            projected_workspace_integrity=projection_integrity,
        )
        checkpoint("report_validated")
    if current.phase == "report_validated":
        provider = _load_provider_observation(output)
        model_output = _read_exact_bytes(output / MODEL_OUTPUT_FILE)
        report = _load_persisted_report(output, prepared)
        final_identity = collect_physics_oracle_workspace_identity(prepared.workspace)
        projection_integrity = _projection_integrity(output, prepared)
        current = _append_record(
            output,
            request=prepared.request,
            phase="workspace_rechecked",
            previous=current,
            process_identity=current.process_identity,
            provider_status=provider.adapter_result.status,
            provider_session_id=provider.provider_session_id,
            provider_thread_started_ids=provider.provider_thread_started_ids,
            backend_artifact_manifest_sha256=provider.backend_policy_evidence_sha256,
            model_output_sha256=hashlib.sha256(model_output).hexdigest(),
            model_output_byte_length=len(model_output),
            parsed_report_sha256=report.canonical_sha256(),
            post_model_workspace_identity=current.post_model_workspace_identity,
            final_workspace_identity=final_identity,
            projected_workspace_integrity=projection_integrity,
        )
        checkpoint("workspace_rechecked")
        if final_identity != prepared.initial_identity or projection_integrity != "unchanged":
            return _finalize(
                output=output,
                prepared=prepared,
                current=current,
                status="workspace_integrity_failure",
                failure_reason=(
                    "workspace_changed"
                    if final_identity != prepared.initial_identity
                    else "projection_changed"
                ),
                provider=provider,
                model_output=model_output,
                report=None,
                decision=None,
                post_identity=cast(Any, current.post_model_workspace_identity),
                final_identity=final_identity,
                checkpoint=checkpoint,
            )
    if current.phase == "workspace_rechecked":
        provider = _load_provider_observation(output)
        model_output = _read_exact_bytes(output / MODEL_OUTPUT_FILE)
        report = _load_persisted_report(output, prepared)
        decision = derive_physics_audit_decision(
            prepared.contract,
            prepared.contract.audit_policy or DEFAULT_PHYSICS_AUDIT_POLICY_V1,
            report,
        )
        _write_once_or_verify(
            output / ROUTING_FILE, decision.to_canonical_json(), "routing decision"
        )
        current = _append_record(
            output,
            request=prepared.request,
            phase="routing_completed",
            previous=current,
            process_identity=current.process_identity,
            provider_status=provider.adapter_result.status,
            provider_session_id=provider.provider_session_id,
            provider_thread_started_ids=provider.provider_thread_started_ids,
            backend_artifact_manifest_sha256=provider.backend_policy_evidence_sha256,
            model_output_sha256=hashlib.sha256(model_output).hexdigest(),
            model_output_byte_length=len(model_output),
            parsed_report_sha256=report.canonical_sha256(),
            post_model_workspace_identity=current.post_model_workspace_identity,
            final_workspace_identity=current.final_workspace_identity,
            projected_workspace_integrity=current.projected_workspace_integrity,
            routing_decision_sha256=decision.canonical_sha256(),
        )
        checkpoint("routing_completed")
    if current.phase == "routing_completed":
        provider = _load_provider_observation(output)
        model_output = _read_exact_bytes(output / MODEL_OUTPUT_FILE)
        report = _load_persisted_report(output, prepared)
        decision = _load_exact_model(
            output / ROUTING_FILE, PhysicsRoutingDecisionV1, "routing decision"
        )
        return _finalize(
            output=output,
            prepared=prepared,
            current=current,
            status="routing_completed",
            failure_reason="none",
            provider=provider,
            model_output=model_output,
            report=report,
            decision=decision,
            post_identity=cast(Any, current.post_model_workspace_identity),
            final_identity=cast(Any, current.final_workspace_identity),
            checkpoint=checkpoint,
        )
    raise PhysicsAuditorStateError("Physics Auditor action phase cannot be resumed")


def _finalize_ambiguous_recovery(
    *,
    output: Path,
    prepared: _PreparedAction,
    current: PhysicsAuditorActionRecordV1,
    checkpoint: Checkpoint,
) -> PhysicsAuditorActionResultV1:
    reason: PhysicsAuditorFailureReason = "recovery_ambiguous"
    identity = current.process_identity
    if identity is not None:
        observed_ticks = _process_start_ticks(identity.pid)
        if observed_ticks is None:
            reason = "stale_process_identity"
        elif observed_ticks != identity.start_ticks:
            reason = "reused_process_identity"
        else:
            _terminate_process_identity(identity)
    final_identity = collect_physics_oracle_workspace_identity(prepared.workspace)
    return _finalize(
        output=output,
        prepared=prepared,
        current=current,
        status=(
            "workspace_integrity_failure"
            if final_identity != prepared.initial_identity
            else "indeterminate_recovery"
        ),
        failure_reason=(
            "workspace_changed" if final_identity != prepared.initial_identity else reason
        ),
        provider=None,
        model_output=b"",
        report=None,
        decision=None,
        post_identity=final_identity,
        final_identity=final_identity,
        checkpoint=checkpoint,
    )


def _finalize(
    *,
    output: Path,
    prepared: _PreparedAction,
    current: PhysicsAuditorActionRecordV1,
    status: PhysicsAuditorStatus,
    failure_reason: PhysicsAuditorFailureReason,
    provider: PhysicsAuditorProviderObservationV1 | None,
    model_output: bytes,
    report: PhysicsAuditReportV1 | None,
    decision: PhysicsRoutingDecisionV1 | None,
    post_identity: PhysicsOracleWorkspaceIdentityV1,
    final_identity: PhysicsOracleWorkspaceIdentityV1,
    checkpoint: Checkpoint,
) -> PhysicsAuditorActionResultV1:
    final_identity = collect_physics_oracle_workspace_identity(prepared.workspace)
    projection_integrity = _projection_integrity(output, prepared)
    if final_identity != prepared.initial_identity:
        status = "workspace_integrity_failure"
        failure_reason = "workspace_changed"
        report = None
        decision = None
    elif projection_integrity == "changed":
        status = "workspace_integrity_failure"
        failure_reason = "projection_changed"
        report = None
        decision = None
    proof = _proof(
        prepared=prepared,
        status=status,
        provider=provider,
        model_output=model_output,
        report=report,
        decision=decision,
        post_identity=post_identity,
        final_identity=final_identity,
        projection_integrity=projection_integrity,
    )
    proof_hash = proof.canonical_sha256()
    result = PhysicsAuditorActionResultV1(
        schema_version=1,
        request=prepared.request,
        status=status,
        failure_reason=failure_reason,
        model_process_completed=(
            provider is not None and provider.adapter_result.status != "launch_failed"
        ),
        provider_status=provider.adapter_result.status if provider is not None else None,
        report_validated=report is not None,
        oracle_execution_detected=(
            provider.oracle_execution_detected if provider is not None else False
        ),
        routing_decision=decision,
        model_output_sha256=hashlib.sha256(model_output).hexdigest(),
        model_output_byte_length=len(model_output),
        parsed_report_sha256=report.canonical_sha256() if report is not None else None,
        initial_workspace_identity=prepared.initial_identity,
        post_model_workspace_identity=post_identity,
        final_workspace_identity=final_identity,
        projection_manifest_sha256=prepared.projection.manifest.canonical_sha256(),
        bubblewrap_policy_sha256=PHYSICS_AUDITOR_BUBBLEWRAP_POLICY_V1.canonical_sha256(),
        bubblewrap_backend_identity_sha256=(
            provider.bubblewrap_backend_identity_sha256 if provider is not None else zero_sha256()
        ),
        projected_workspace_integrity=projection_integrity,
        integrity_verdict=(
            "unchanged"
            if post_identity == prepared.initial_identity
            and final_identity == prepared.initial_identity
            else "changed"
        ),
        action_proof_sha256=proof_hash,
    )
    _write_once_or_verify(output / PROOF_FILE, proof.to_canonical_json(), "action proof")
    _write_once_or_verify(output / RESULT_FILE, result.to_canonical_json(), "action result")
    final = _append_record(
        output,
        request=prepared.request,
        phase="action_proof_finalized",
        previous=current,
        process_identity=current.process_identity,
        provider_status=provider.adapter_result.status
        if provider is not None
        else current.provider_status,
        provider_session_id=provider.provider_session_id
        if provider is not None
        else current.provider_session_id,
        provider_thread_started_ids=(
            provider.provider_thread_started_ids
            if provider is not None
            else current.provider_thread_started_ids
        ),
        backend_artifact_manifest_sha256=(
            provider.backend_policy_evidence_sha256
            if provider is not None
            else current.backend_artifact_manifest_sha256
        ),
        model_output_sha256=hashlib.sha256(model_output).hexdigest(),
        model_output_byte_length=len(model_output),
        parsed_report_sha256=report.canonical_sha256() if report is not None else None,
        post_model_workspace_identity=post_identity,
        final_workspace_identity=final_identity,
        projected_workspace_integrity=projection_integrity,
        routing_decision_sha256=decision.canonical_sha256() if decision is not None else None,
        result_sha256=result.canonical_sha256(),
        action_proof_sha256=proof_hash,
    )
    del final
    checkpoint("action_proof_finalized")
    return result


def _proof(
    *,
    prepared: _PreparedAction,
    status: PhysicsAuditorStatus,
    provider: PhysicsAuditorProviderObservationV1 | None,
    model_output: bytes,
    report: PhysicsAuditReportV1 | None,
    decision: PhysicsRoutingDecisionV1 | None,
    post_identity: PhysicsOracleWorkspaceIdentityV1,
    final_identity: PhysicsOracleWorkspaceIdentityV1,
    projection_integrity: ProjectionIntegrity,
) -> PhysicsAuditorActionProofV1:
    oracle_manifest = canonical_json(
        [item.model_dump(mode="json") for item in prepared.request.oracle_completion_proofs]
    )
    return PhysicsAuditorActionProofV1(
        schema_version=1,
        action_id=prepared.request.action_id,
        task_id=prepared.request.task_id,
        action_request_sha256=prepared.request.canonical_sha256(),
        physics_contract_sha256=prepared.request.physics_contract_sha256,
        execution_config_sha256=prepared.request.execution_config_sha256,
        backend="codex_cli",
        backend_executable_sha256=(provider.codex_executable_sha256 if provider else None),
        backend_version=provider.codex_cli_version if provider else None,
        model=prepared.config.model,
        reasoning_effort=prepared.config.reasoning_effort,
        role_policy_sha256=PHYSICS_AUDITOR_CODEX_ROLE_POLICY_V1.canonical_sha256(),
        prompt_template_version=prepared.request.prompt_template_version,
        prompt_template_sha256=prepared.request.prompt_template_sha256,
        canonical_prompt_sha256=prepared.prompt.rendered_sha256,
        output_schema_sha256=prepared.request.output_schema_sha256,
        initial_workspace_identity=prepared.initial_identity,
        projection_manifest_sha256=prepared.projection.manifest.canonical_sha256(),
        bubblewrap_policy_sha256=PHYSICS_AUDITOR_BUBBLEWRAP_POLICY_V1.canonical_sha256(),
        bubblewrap_backend_identity_sha256=(
            provider.bubblewrap_backend_identity_sha256 if provider is not None else zero_sha256()
        ),
        evidence_index_sha256=prepared.request.evidence_index_sha256,
        changed_path_manifest_sha256=prepared.request.changed_path_manifest_sha256,
        oracle_completion_proof_manifest_sha256=hashlib.sha256(oracle_manifest).hexdigest(),
        model_process_status=(
            provider.adapter_result.status
            if provider is not None
            else "recovery_ambiguous"
            if status == "indeterminate_recovery"
            else "not_launched"
        ),
        backend_artifact_manifest_sha256=(
            provider.backend_policy_evidence_sha256 if provider is not None else zero_sha256()
        ),
        model_output_sha256=hashlib.sha256(model_output).hexdigest(),
        model_output_byte_length=len(model_output),
        oracle_execution_detected=(
            provider.oracle_execution_detected if provider is not None else False
        ),
        parsed_report_sha256=report.canonical_sha256() if report is not None else None,
        routing_decision_sha256=decision.canonical_sha256() if decision is not None else None,
        post_model_workspace_identity=post_identity,
        final_workspace_identity=final_identity,
        projected_workspace_integrity=cast(Any, projection_integrity),
        integrity_verdict=(
            "unchanged"
            if post_identity == prepared.initial_identity
            and final_identity == prepared.initial_identity
            else "changed"
        ),
        action_status=status,
    )


def _prepared_codex_request(output: Path, prepared: _PreparedAction) -> PreparedCodexRequest:
    prompt_path = output / CONTROL_DIRECTORY / PROMPT_FILE
    request = CodexRunRequest(
        schema_version=1,
        run_id=prepared.request.action_id,
        role="auditor",
        workspace=str(_projection_root(output)),
        prompt_path=str(prompt_path),
        model=prepared.config.model,
        reasoning_effort=prepared.config.reasoning_effort,
        timeout_seconds=prepared.config.timeout_seconds,
    )
    return PreparedCodexRequest(
        request_path=output / CONTROL_DIRECTORY / REQUEST_FILE,
        request=request,
        workspace=_projection_root(output),
        prompt_path=prompt_path,
        prompt_bytes=prepared.prompt.content,
        prompt_sha256=prepared.prompt.rendered_sha256,
        policy=ROLE_POLICIES["auditor"],
    )


def _invoke_qualified_codex(
    *,
    prepared: PreparedCodexRequest,
    runs_dir: Path,
    codex_executable: Path,
    config: PhysicsAuditorExecutionConfigV1,
    output_schema: Path,
    environ: Mapping[str, str] | None,
    process_started: Callable[[int], None],
    source_workspace: Path,
    oracle_evidence_root: Path,
    action_root: Path,
) -> PhysicsAuditorCodexRun:
    parent_environment = os.environ if environ is None else environ
    _, _, sensitive_values = build_subprocess_environment(parent_environment)
    _assert_oracle_execution_surface_absent()
    capability = preflight_bubblewrap_isolation(
        bubblewrap_executable=None,
        codex_executable=str(codex_executable),
        authentication_file=None,
        environ=parent_environment,
        forbidden_roots=(source_workspace, oracle_evidence_root),
    )
    identity_path = action_root / CONTROL_DIRECTORY / BUBBLEWRAP_BACKEND_FILE
    if identity_path.exists():
        if load_backend_identity(identity_path) != capability.identity:
            raise LiveShadowIntegrityError("Bubblewrap backend identity changed")
    else:
        write_backend_identity(identity_path, capability.identity)
    runtime_home = action_root / RUNTIME_HOME_DIRECTORY
    auth_fragments = capability.authentication_confidentiality.text_fragments()

    def isolated_launch(
        command: Sequence[str],
        request: PreparedCodexRequest,
        environment: Mapping[str, str],
        final_message_path: Path,
        resolved_schema: Path | None,
    ) -> CodexProcessLaunch:
        indexes = [index for index, item in enumerate(command) if item == "--add-dir"]
        if len(indexes) != 1 or indexes[0] + 1 >= len(command):
            raise LiveShadowIntegrityError("Physics Auditor scratch authority is absent")
        scratch = Path(command[indexes[0] + 1])
        return build_bubblewrap_process_launch(
            command,
            request,
            environment,
            final_message_path,
            resolved_schema,
            capability=capability,
            stage4_run_root=action_root,
            runtime_home=runtime_home,
            forbidden_roots=(source_workspace, oracle_evidence_root),
            auditor_scratch=scratch,
        )

    try:
        result = run_prepared_codex(
            prepared,
            runs_dir=runs_dir,
            codex_executable=str(codex_executable),
            environ=_minimal_codex_environment(parent_environment),
            limits=AdapterLimits(
                stdout_bytes=config.max_stdout_bytes,
                stderr_bytes=config.max_stderr_bytes,
            ),
            output_schema=output_schema,
            resume_thread_id=None,
            skip_git_repo_check=True,
            confidential_fragments=(*sensitive_values, *auth_fragments),
            rejected_confidential_fragments=(*sensitive_values, *auth_fragments),
            durable_command_replacements={
                str(capability.authentication_file): RECORDED_AUTH_SOURCE,
            },
            process_launch_builder=isolated_launch,
            version_probe=lambda _executable, _environment, _workspace: None,
            process_started=process_started,
        )
        return _verify_codex_run(
            prepared=prepared,
            result=result,
            executable=codex_executable,
            config=config,
            output_schema=output_schema,
            bubblewrap_identity=capability.identity,
        )
    finally:
        reset_runtime_home_contents(runtime_home)


def _verify_codex_run(
    *,
    prepared: PreparedCodexRequest,
    result: CodexRunResult,
    executable: Path,
    config: PhysicsAuditorExecutionConfigV1,
    output_schema: Path,
    bubblewrap_identity: BubblewrapBackendIdentity,
) -> PhysicsAuditorCodexRun:
    directory = Path(result.artifact_directory)
    request = _load_pretty_model(
        directory / "request.normalized.json", NormalizedCodexRequest, "normalized Codex request"
    )
    metadata = _load_pretty_model(directory / "metadata.json", CodexMetadata, "Codex metadata")
    persisted_result = _load_pretty_model(directory / "result.json", CodexRunResult, "Codex result")
    completion = _load_pretty_model(
        directory / "stage2-completion.json", Stage2CompletionManifest, "Codex completion manifest"
    )
    if persisted_result != result:
        raise PhysicsAuditorIntegrityError("Codex result was substituted")
    verify_projected_auditor_bubblewrap_command(
        metadata,
        identity=bubblewrap_identity,
        projected_workspace=prepared.workspace,
        runtime_home=prepared.workspace.parent / "codex-home",
        output_schema=output_schema,
        codex_executable=executable,
        artifact_directory=directory,
    )
    if (
        request.role != "auditor"
        or request.policy.sandbox != "read-only"
        or request.policy.approval != "never"
        or not request.policy.ephemeral
        or metadata.role != "auditor"
        or metadata.sandbox != "read-only"
        or metadata.approval_policy != "never"
        or not metadata.ephemeral
        or metadata.resume_thread_id is not None
        or "--ephemeral" not in metadata.command
        or "resume" in metadata.command
        or "--yolo" in metadata.command
        or "danger-full-access" in metadata.command
        or metadata.output_schema_sha256 != hashlib.sha256(output_schema.read_bytes()).hexdigest()
        or metadata.confidentiality_violation_detected
        or metadata.sandbox_disposition != AUDITOR_SANDBOX_DISPOSITION
        or completion.result_status != result.status
    ):
        raise PhysicsAuditorIntegrityError("Codex action violated the Physics Auditor role policy")
    for path_text, digest in completion.artifact_hashes.items():
        path = Path(path_text)
        if not path.is_relative_to(directory) or _sha256_file(path) != digest:
            raise PhysicsAuditorIntegrityError("Codex completion artifact mapping is invalid")
    model_output, truncated = _read_bounded_file(directory / "final-message.md")
    executable_hash = _sha256_file(executable)
    if config.trusted_executable is not None and (
        config.trusted_executable.path != str(executable)
        or config.trusted_executable.sha256 != executable_hash
    ):
        raise PhysicsAuditorIntegrityError("Codex executable contradicted trusted configuration")
    semantic = {
        "schema_version": 1,
        "role": metadata.role,
        "model": metadata.model,
        "reasoning_effort": metadata.reasoning_effort,
        "timeout_seconds": metadata.timeout_seconds,
        "sandbox": metadata.sandbox,
        "approval_policy": metadata.approval_policy,
        "ephemeral": metadata.ephemeral,
        "resume_thread_id": metadata.resume_thread_id,
        "output_schema_sha256": metadata.output_schema_sha256,
        "stdout_byte_count": metadata.stdout_byte_count,
        "stderr_byte_count": metadata.stderr_byte_count,
        "stdout_limit_bytes": metadata.stdout_limit_bytes,
        "stderr_limit_bytes": metadata.stderr_limit_bytes,
        "process_status": result.status,
        "process_exit_code": metadata.process_exit_code,
        "termination_reason": metadata.termination_reason,
        "command_policy": "fresh_ephemeral_read_only_no_resume_v1",
        "network_policy": config.network_policy,
        "environment_profile": config.environment_allowlist_profile,
        "projection_policy_sha256": PHYSICS_AUDITOR_BUBBLEWRAP_POLICY_V1.canonical_sha256(),
        "bubblewrap_backend_identity_sha256": hashlib.sha256(
            canonical_json(bubblewrap_identity.to_dict())
        ).hexdigest(),
    }
    provider_session = metadata.thread_id or metadata.session_id
    return PhysicsAuditorCodexRun(
        adapter_result=result,
        model_output=model_output,
        model_output_truncated=truncated,
        provider_session_id=provider_session,
        provider_thread_started_ids=metadata.thread_started_ids,
        backend_policy_evidence_sha256=hashlib.sha256(canonical_json(semantic)).hexdigest(),
        bubblewrap_backend_identity_sha256=hashlib.sha256(
            canonical_json(bubblewrap_identity.to_dict())
        ).hexdigest(),
        codex_executable_sha256=executable_hash,
        codex_cli_version=metadata.codex_version,
    )


def _persist_accepted_control(output: Path, prepared: _PreparedAction) -> None:
    control = output / CONTROL_DIRECTORY
    control.mkdir(exist_ok=False)
    for name, model in (
        (CONFIG_FILE, prepared.config),
        (CONTRACT_FILE, prepared.contract),
        (CHANGED_PATHS_FILE, prepared.changed_paths),
        (EVIDENCE_INDEX_FILE, prepared.discovered.index),
        (PROJECTION_MANIFEST_FILE, prepared.projection.manifest),
        (REQUEST_FILE, prepared.request),
    ):
        _write_once_or_verify(control / name, model.to_canonical_json(), name)


def _persist_prompt_control(output: Path, prepared: _PreparedAction) -> None:
    control = output / CONTROL_DIRECTORY
    _write_once_or_verify(control / PROMPT_FILE, prepared.prompt.content, "Physics Auditor prompt")
    _write_once_or_verify(
        control / OUTPUT_SCHEMA_FILE,
        canonical_json(PHYSICS_AUDIT_REPORT_OUTPUT_SCHEMA),
        "PhysicsAuditReportV1 output schema",
    )
    _write_once_or_verify(
        control / ROLE_POLICY_FILE,
        PHYSICS_AUDITOR_CODEX_ROLE_POLICY_V1.to_canonical_json(),
        "Physics Auditor Codex role policy",
    )
    _write_once_or_verify(
        control / BUBBLEWRAP_POLICY_FILE,
        PHYSICS_AUDITOR_BUBBLEWRAP_POLICY_V1.to_canonical_json(),
        "Physics Auditor Bubblewrap policy",
    )
    isolated_schema = _isolated_output_schema(output, prepared.request.action_id)
    isolated_schema.parent.mkdir(parents=True, exist_ok=True)
    _write_once_or_verify(
        isolated_schema,
        canonical_json(PHYSICS_AUDIT_REPORT_OUTPUT_SCHEMA),
        "isolated PhysicsAuditReportV1 output schema",
    )


def _verify_accepted_control(output: Path, prepared: _PreparedAction) -> None:
    control = output / CONTROL_DIRECTORY
    expected = {
        CONFIG_FILE: prepared.config.to_canonical_json(),
        CONTRACT_FILE: prepared.contract.to_canonical_json(),
        CHANGED_PATHS_FILE: prepared.changed_paths.to_canonical_json(),
        EVIDENCE_INDEX_FILE: prepared.discovered.index.to_canonical_json(),
        PROJECTION_MANIFEST_FILE: prepared.projection.manifest.to_canonical_json(),
        REQUEST_FILE: prepared.request.to_canonical_json(),
    }
    if (control / PROMPT_FILE).exists():
        expected.update(
            {
                PROMPT_FILE: prepared.prompt.content,
                OUTPUT_SCHEMA_FILE: canonical_json(PHYSICS_AUDIT_REPORT_OUTPUT_SCHEMA),
                ROLE_POLICY_FILE: PHYSICS_AUDITOR_CODEX_ROLE_POLICY_V1.to_canonical_json(),
                BUBBLEWRAP_POLICY_FILE: PHYSICS_AUDITOR_BUBBLEWRAP_POLICY_V1.to_canonical_json(),
            }
        )
    for name, content in expected.items():
        if _read_exact_bytes(control / name) != content:
            raise PhysicsAuditorIntegrityError(f"Physics Auditor control file {name} was replaced")
    if (control / PROMPT_FILE).exists() and _read_exact_bytes(
        _isolated_output_schema(output, prepared.request.action_id)
    ) != canonical_json(PHYSICS_AUDIT_REPORT_OUTPUT_SCHEMA):
        raise PhysicsAuditorIntegrityError("Physics Auditor isolated output schema was replaced")


def _load_persisted_report(
    output: Path,
    prepared: _PreparedAction,
) -> PhysicsAuditReportV1:
    raw = _read_exact_bytes(output / REPORT_FILE)
    report = parse_physics_audit_report_json(raw, prepared.contract)
    validate_report_evidence_index(report, prepared.discovered.index)
    if raw != report.to_canonical_json():
        raise PhysicsAuditorIntegrityError("persisted Physics Auditor report is not canonical")
    return report


def _persist_provider_observation(output: Path, run: PhysicsAuditorCodexRun) -> None:
    observation = _provider_observation(run)
    _write_once_or_verify(
        output / PROVIDER_OBSERVATION_FILE,
        observation.to_canonical_json(),
        "Codex provider observation",
    )
    _write_once_or_verify(output / MODEL_OUTPUT_FILE, run.model_output, "bounded model output")


def _provider_observation(run: PhysicsAuditorCodexRun) -> PhysicsAuditorProviderObservationV1:
    return PhysicsAuditorProviderObservationV1(
        schema_version=1,
        adapter_result=run.adapter_result,
        codex_executable_sha256=run.codex_executable_sha256,
        codex_cli_version=run.codex_cli_version,
        provider_session_id=run.provider_session_id,
        provider_thread_started_ids=run.provider_thread_started_ids,
        backend_policy_evidence_sha256=run.backend_policy_evidence_sha256,
        bubblewrap_backend_identity_sha256=run.bubblewrap_backend_identity_sha256,
        model_output_sha256=hashlib.sha256(run.model_output).hexdigest(),
        model_output_byte_length=len(run.model_output),
        model_output_truncated=run.model_output_truncated,
        oracle_execution_detected=run.oracle_execution_detected,
    )


def _verify_persisted_provider(
    output: Path,
    prepared: _PreparedAction,
    provider: PhysicsAuditorProviderObservationV1,
) -> None:
    if provider.codex_cli_version == "scripted-test-v1":
        return
    expected_directory = (output / BACKEND_DIRECTORY / prepared.request.action_id).resolve()
    observed_directory = Path(provider.adapter_result.artifact_directory)
    try:
        resolved_directory = observed_directory.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise PhysicsAuditorIntegrityError("Codex artifact directory is unavailable") from exc
    if resolved_directory != expected_directory:
        raise PhysicsAuditorIntegrityError("Codex artifact directory escaped the action")
    metadata = _load_pretty_model(
        resolved_directory / "metadata.json",
        CodexMetadata,
        "Codex metadata",
    )
    completion = _load_pretty_model(
        resolved_directory / "stage2-completion.json",
        Stage2CompletionManifest,
        "Codex completion manifest",
    )
    expected_artifacts = {
        str(resolved_directory / name)
        for name in (
            "request.normalized.json",
            "prompt.sha256",
            "events.jsonl",
            "stderr.log",
            "final-message.md",
            "metadata.json",
            "result.json",
        )
    }
    if (
        set(completion.artifact_hashes) != expected_artifacts
        or completion.run_id != prepared.request.action_id
        or completion.role != "auditor"
        or completion.artifact_directory != str(resolved_directory)
        or completion.prompt_sha256 != prepared.prompt.rendered_sha256
        or completion.output_schema_path
        != str(_isolated_output_schema(output, prepared.request.action_id))
        or completion.output_schema_sha256 != prepared.prompt.output_schema_sha256
    ):
        raise PhysicsAuditorIntegrityError("Codex completion manifest is incomplete")
    identity = load_backend_identity(output / CONTROL_DIRECTORY / BUBBLEWRAP_BACKEND_FILE)
    identity_sha256 = hashlib.sha256(canonical_json(identity.to_dict())).hexdigest()
    if identity_sha256 != provider.bubblewrap_backend_identity_sha256:
        raise PhysicsAuditorIntegrityError("Bubblewrap backend identity was substituted")
    verified = _verify_codex_run(
        prepared=_prepared_codex_request(output, prepared),
        result=provider.adapter_result,
        executable=Path(metadata.codex_executable),
        config=prepared.config,
        output_schema=_isolated_output_schema(output, prepared.request.action_id),
        bubblewrap_identity=identity,
    )
    verified = replace(
        verified,
        oracle_execution_detected=_detect_oracle_execution(verified, prepared.discovered),
    )
    if _provider_observation(verified) != provider:
        raise PhysicsAuditorIntegrityError("Codex provider observation does not reverify")


def _load_provider_observation(output: Path) -> PhysicsAuditorProviderObservationV1:
    observation = _load_exact_model(
        output / PROVIDER_OBSERVATION_FILE,
        PhysicsAuditorProviderObservationV1,
        "Codex provider observation",
    )
    model_output = _read_exact_bytes(output / MODEL_OUTPUT_FILE)
    if (
        len(model_output) != observation.model_output_byte_length
        or hashlib.sha256(model_output).hexdigest() != observation.model_output_sha256
    ):
        raise PhysicsAuditorIntegrityError("bounded model output contradicts provider observation")
    return cast(PhysicsAuditorProviderObservationV1, observation)


def _load_provider_if_present(output: Path) -> PhysicsAuditorProviderObservationV1 | None:
    path = output / PROVIDER_OBSERVATION_FILE
    return _load_provider_observation(output) if path.exists() else None


def _detect_oracle_execution(
    run: PhysicsAuditorCodexRun,
    discovered: DiscoveredPhysicsAuditorEvidence,
) -> bool:
    return _detect_oracle_execution_in_events(
        Path(run.adapter_result.artifact_directory) / "events.jsonl",
        discovered,
    )


def _detect_oracle_execution_in_events(
    events_path: Path,
    discovered: DiscoveredPhysicsAuditorEvidence,
) -> bool:
    """Fail closed if a Codex command names a sealed PA-2 oracle program."""
    program_paths: set[str] = set()
    for _oracle_id, directory in discovered.oracle_directories:
        intent = _load_pretty_model(
            directory / ORACLE_CONTROL_DIRECTORY / SEALED_INTENT_FILE,
            PhysicsOracleIntentV1,
            "sealed PA-2 oracle intent",
        )
        program_paths.add(intent.program.path)
    if not program_paths or not events_path.exists():
        return False
    try:
        with events_path.open("rb") as events:
            for raw_line in events:
                if not raw_line.endswith(b"\n"):
                    raise PhysicsAuditorIntegrityError(
                        "Codex event stream is not canonically delimited"
                    )
                value = json.loads(raw_line.decode("utf-8"), parse_constant=_reject_constant)
                if not isinstance(value, Mapping):
                    raise PhysicsAuditorIntegrityError("Codex event stream contains a non-object")
                for command in _event_command_text(value):
                    if any(path in command for path in program_paths):
                        return True
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise PhysicsAuditorIntegrityError(
            "Codex event stream could not be checked for oracle execution"
        ) from exc
    return False


def _event_command_text(event: Mapping[str, Any]) -> tuple[str, ...]:
    command_types = {"command", "command.execution", "command_execution", "exec", "exec_command"}
    candidates: list[Mapping[str, Any]] = []
    event_type = event.get("type")
    if isinstance(event_type, str) and event_type.casefold() in command_types:
        candidates.append(event)
    item = event.get("item")
    if isinstance(item, Mapping):
        item_type = item.get("type")
        if isinstance(item_type, str) and item_type.casefold() in command_types:
            candidates.append(item)

    strings: list[str] = []

    def collect(value: Any) -> None:
        if isinstance(value, str):
            strings.append(value)
        elif isinstance(value, Mapping):
            for nested in value.values():
                collect(nested)
        elif isinstance(value, list):
            for nested in value:
                collect(nested)

    for candidate in candidates:
        for key in ("argv", "cmd", "command", "command_line", "input"):
            if key in candidate:
                collect(candidate[key])
    return tuple(strings)


def _provider_failure(
    provider: PhysicsAuditorProviderObservationV1,
) -> PhysicsAuditorFailureReason | None:
    if provider.oracle_execution_detected:
        return "oracle_execution_attempted"
    if provider.model_output_truncated:
        return "model_output_limit_exceeded"
    status = provider.adapter_result.status
    if status == "succeeded" and (
        provider.provider_session_id is None
        or len(provider.provider_thread_started_ids) != 1
        or provider.provider_thread_started_ids[0] != provider.provider_session_id
    ):
        return "model_process_failed"
    if status == "succeeded":
        return None
    if status == "launch_failed":
        return "model_launch_failed"
    if status == "timed_out":
        return "model_timed_out"
    if status == "output_limit_exceeded":
        return "model_output_limit_exceeded"
    return "model_process_failed"


def _select_codex_executable(config: PhysicsAuditorExecutionConfigV1) -> Path:
    if config.trusted_executable is not None:
        return validate_trusted_codex_executable(config.trusted_executable)
    selected = shutil.which("codex")
    if selected is None:
        raise PhysicsAuditorDependencyError("Codex executable is required for audit execution")
    try:
        resolved = Path(selected).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise PhysicsAuditorDependencyError("Codex executable could not be resolved") from exc
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise PhysicsAuditorDependencyError("Codex executable is unavailable")
    return resolved


def _minimal_codex_environment(source: Mapping[str, str]) -> dict[str, str]:
    allowed = {
        "COLORTERM",
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "NO_COLOR",
        "PATH",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "TERM",
        "XDG_DATA_DIRS",
        "XDG_RUNTIME_DIR",
    }
    environment = {name: value for name, value in source.items() if name in allowed}
    environment.setdefault("HOME", "/nonexistent")
    environment.setdefault("LANG", "C.UTF-8")
    environment.setdefault("LC_ALL", "C.UTF-8")
    environment.setdefault("PATH", "/usr/bin:/bin")
    return environment


def _assert_oracle_execution_surface_absent() -> None:
    """Reject a host runtime that would expose the packaged PA-2 CLI in /usr."""
    executable_candidates = (
        Path("/usr/bin/research-supervisor"),
        Path("/usr/sbin/research-supervisor"),
        Path("/bin/research-supervisor"),
        Path("/sbin/research-supervisor"),
    )
    module_candidates = tuple(Path("/usr/lib").glob("python*/dist-packages")) + tuple(
        Path("/usr/lib").glob("python*/site-packages")
    )
    try:
        exposed = any(path.exists() for path in executable_candidates) or any(
            (root / "research_automation_supervisor").exists() for root in module_candidates
        )
    except OSError as exc:
        raise PhysicsAuditorDependencyError(
            "system oracle-execution surface could not be excluded"
        ) from exc
    if exposed:
        raise PhysicsAuditorDependencyError(
            "system runtime would expose run-physics-oracle to the Physics Auditor"
        )


def _append_record(
    output: Path,
    *,
    request: PhysicsAuditorActionRequestV1,
    phase: Any,
    previous: PhysicsAuditorActionRecordV1 | None,
    **updates: object,
) -> PhysicsAuditorActionRecordV1:
    sequence = 1 if previous is None else previous.sequence + 1
    prior_hash = ZERO_HASH if previous is None else previous.canonical_sha256()
    try:
        model = cast(Any, PhysicsAuditorActionRecordV1)
        draft = model.model_construct(
            schema_version=1,
            record_sha256=ZERO_HASH,
            sequence=sequence,
            phase=phase,
            previous_record_sha256=prior_hash,
            request=request,
            **updates,
        )
        payload = draft.model_dump(mode="json", exclude={"record_sha256"})
        record = PhysicsAuditorActionRecordV1.model_validate(
            {
                **payload,
                "record_sha256": hashlib.sha256(canonical_json(payload)).hexdigest(),
            }
        )
    except ValidationError as exc:
        raise PhysicsAuditorStateError("Physics Auditor action boundary is contradictory") from exc
    directory = output / RECORDS_DIRECTORY
    directory.mkdir(exist_ok=True)
    _write_once_or_verify(
        directory / f"{sequence:03d}-{phase}.json",
        record.to_canonical_json(),
        "Physics Auditor action record",
    )
    return record


def _load_records(output: Path) -> tuple[PhysicsAuditorActionRecordV1, ...]:
    directory = output / RECORDS_DIRECTORY
    try:
        paths = sorted(directory.glob("*.json"))
    except OSError as exc:
        raise PhysicsAuditorStateError("Physics Auditor action records are unavailable") from exc
    records: list[PhysicsAuditorActionRecordV1] = []
    previous_hash = ZERO_HASH
    for sequence, path in enumerate(paths, start=1):
        record = _load_exact_model(
            path, PhysicsAuditorActionRecordV1, "Physics Auditor action record"
        )
        if (
            record.sequence != sequence
            or path.name != f"{sequence:03d}-{record.phase}.json"
            or record.previous_record_sha256 != previous_hash
            or (records and record.request != records[0].request)
        ):
            raise PhysicsAuditorIntegrityError("Physics Auditor action-record chain is invalid")
        records.append(record)
        previous_hash = record.canonical_sha256()
    _validate_phase_sequence(tuple(item.phase for item in records))
    return tuple(records)


def _validate_phase_sequence(phases: tuple[str, ...]) -> None:
    normal = (
        "action_accepted",
        "evidence_verified",
        "prompt_finalized",
        "model_launch_attempted",
        "model_running",
        "model_exit_observed",
        "output_captured",
        "report_validated",
        "workspace_rechecked",
        "routing_completed",
        "action_proof_finalized",
    )
    without_running = tuple(item for item in normal if item != "model_running")
    valid = phases == normal[: len(phases)] or phases == without_running[: len(phases)]
    early_final = (
        phases and phases[0] == "action_accepted" and phases[-1] == "action_proof_finalized"
    )
    if early_final:
        prefix = phases[:-1]
        valid = valid or prefix in {
            ("action_accepted",),
            ("action_accepted", "evidence_verified", "prompt_finalized"),
            ("action_accepted", "evidence_verified", "prompt_finalized", "model_launch_attempted"),
            (
                "action_accepted",
                "evidence_verified",
                "prompt_finalized",
                "model_launch_attempted",
                "model_running",
            ),
            (
                "action_accepted",
                "evidence_verified",
                "prompt_finalized",
                "model_launch_attempted",
                "model_exit_observed",
                "output_captured",
            ),
            (
                "action_accepted",
                "evidence_verified",
                "prompt_finalized",
                "model_launch_attempted",
                "model_running",
                "model_exit_observed",
                "output_captured",
            ),
            (
                "action_accepted",
                "evidence_verified",
                "prompt_finalized",
                "model_launch_attempted",
                "model_exit_observed",
                "output_captured",
                "report_validated",
                "workspace_rechecked",
            ),
            (
                "action_accepted",
                "evidence_verified",
                "prompt_finalized",
                "model_launch_attempted",
                "model_running",
                "model_exit_observed",
                "output_captured",
                "report_validated",
                "workspace_rechecked",
            ),
        }
    if not valid:
        raise PhysicsAuditorIntegrityError("Physics Auditor action phase sequence is invalid")


def _default_action_id(task_id: str, attempt_number: int) -> str:
    candidate = f"physics-audit-{task_id}-{attempt_number}"
    if len(candidate) <= 80:
        return candidate
    digest = hashlib.sha256(candidate.encode("utf-8")).hexdigest()[:16]
    return f"physics-audit-{digest}-{attempt_number}"


def _canonical_workspace(path: Path) -> Path:
    if ".." in path.parts:
        raise PhysicsAuditorInputError("workspace contains parent traversal")
    try:
        absolute = Path(os.path.abspath(path))
        resolved = path.resolve(strict=True)
        metadata = path.lstat()
    except (OSError, RuntimeError, ValueError) as exc:
        raise PhysicsAuditorInputError("workspace is unavailable") from exc
    if absolute != resolved or path.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
        raise PhysicsAuditorInputError("workspace must be a canonical non-symlink directory")
    return resolved


def _projection_root(output: Path) -> Path:
    return output / PROJECTION_DIRECTORY


def _runtime_home(output: Path) -> Path:
    return output / RUNTIME_HOME_DIRECTORY


def _isolated_output_schema(output: Path, action_id: str) -> Path:
    return output / "decisions" / action_id / "output-schema.json"


def _prepare_projection_layout(output: Path) -> None:
    quarantine = output / "quarantine"
    runtime = _runtime_home(output)
    try:
        quarantine.mkdir(mode=0o700, exist_ok=True)
        if quarantine.is_symlink() or not stat.S_ISDIR(quarantine.lstat().st_mode):
            raise PhysicsAuditorIntegrityError("projection quarantine is unsafe")
        runtime.mkdir(mode=0o700, exist_ok=True)
        if runtime.is_symlink() or not stat.S_ISDIR(runtime.lstat().st_mode):
            raise PhysicsAuditorIntegrityError("Codex runtime home is unsafe")
        if any(runtime.iterdir()):
            raise PhysicsAuditorIntegrityError(
                "Codex runtime home is not empty before the fresh session"
            )
    except PhysicsAuditorIntegrityError:
        raise
    except OSError as exc:
        raise PhysicsAuditorIntegrityError("projection layout could not be prepared") from exc


def _projection_integrity(
    output: Path,
    prepared: _PreparedAction,
) -> ProjectionIntegrity:
    root = _projection_root(output)
    if not root.exists():
        materialization_committed = any(
            (output / RECORDS_DIRECTORY).glob("*-evidence_verified.json")
        )
        return "changed" if materialization_committed else "not_materialized"
    try:
        verify_physics_auditor_projection(prepared.projection.manifest, root)
    except PhysicsAuditorIntegrityError:
        return "changed"
    return "unchanged"


def _new_output_directory(path: Path, workspace: Path, oracle_evidence_root: Path) -> Path:
    if ".." in path.parts or not path.name:
        raise PhysicsAuditorInputError("Physics Auditor output path is invalid")
    try:
        parent = path.parent.resolve(strict=True)
        output = parent / path.name
    except (OSError, RuntimeError, ValueError) as exc:
        raise PhysicsAuditorInputError("Physics Auditor output parent is unavailable") from exc
    if (
        output.exists()
        or _paths_overlap(output, workspace)
        or _paths_overlap(output, oracle_evidence_root)
    ):
        raise PhysicsAuditorInputError(
            "Physics Auditor output must be new and outside input authorities"
        )
    try:
        output.mkdir(mode=0o700)
    except OSError as exc:
        raise PhysicsAuditorInputError("Physics Auditor output could not be created") from exc
    return output


def _existing_output_directory(path: Path) -> Path:
    try:
        absolute = Path(os.path.abspath(path))
        resolved = path.resolve(strict=True)
        metadata = path.lstat()
    except (OSError, RuntimeError, ValueError) as exc:
        raise PhysicsAuditorInputError("Physics Auditor output is unavailable") from exc
    if absolute != resolved or path.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
        raise PhysicsAuditorInputError("Physics Auditor output must be a canonical directory")
    return resolved


@contextmanager
def _action_lock(output: Path) -> Any:
    descriptor = os.open(output / ".physics-auditor.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise PhysicsAuditorStateError("Physics Auditor action is already locked") from exc
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


def _terminate_process_identity(identity: PhysicsAuditorProcessIdentityV1) -> None:
    if not _process_identity_running(identity.pid, identity.start_ticks):
        return
    with suppress(ProcessLookupError):
        os.killpg(identity.process_group_id, signal.SIGTERM)
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if not _process_identity_running(identity.pid, identity.start_ticks):
            return
        time.sleep(0.01)
    if _process_identity_running(identity.pid, identity.start_ticks):
        with suppress(ProcessLookupError):
            os.killpg(identity.process_group_id, signal.SIGKILL)


def _process_identity_running(pid: int, expected_ticks: int) -> bool:
    try:
        value = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        close = value.rfind(")")
        fields = value[close + 2 :].split()
        return int(fields[19]) == expected_ticks and fields[0] != "Z"
    except (OSError, ValueError, IndexError):
        return False


def _read_bounded_file(path: Path) -> tuple[bytes, bool]:
    try:
        with path.open("rb") as handle:
            content = handle.read(MAX_PHYSICS_AUDITOR_OUTPUT_BYTES + 1)
    except OSError:
        return b"", False
    truncated = len(content) > MAX_PHYSICS_AUDITOR_OUTPUT_BYTES
    return content[:MAX_PHYSICS_AUDITOR_OUTPUT_BYTES], truncated


def _load_exact_model(path: Path, model: type[Any], label: str) -> Any:
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("ascii"), parse_constant=_reject_constant)
        parsed = model.model_validate(value)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValidationError, ValueError) as exc:
        raise PhysicsAuditorIntegrityError(f"{label} is malformed or unavailable") from exc
    if raw != parsed.to_canonical_json():
        raise PhysicsAuditorIntegrityError(f"{label} is not canonically encoded")
    return parsed


def _load_pretty_model(path: Path, model: type[Any], label: str) -> Any:
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
        parsed = model.model_validate(value)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValidationError, ValueError) as exc:
        raise PhysicsAuditorIntegrityError(f"{label} is malformed or unavailable") from exc
    if raw != render_json_bytes(value):
        raise PhysicsAuditorIntegrityError(f"{label} is not canonically encoded")
    return parsed


def _write_once_or_verify(path: Path, value: bytes, label: str) -> None:
    if path.exists():
        if _read_exact_bytes(path) != value:
            raise PhysicsAuditorIntegrityError(f"{label} already exists with different bytes")
        return
    atomic_write_bytes(
        path,
        value,
        error_factory=PhysicsAuditorStateError,
        error_message=f"{label} could not be persisted",
    )


def _read_exact_bytes(path: Path) -> bytes:
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
            raise OSError("not an exact regular file")
        return path.read_bytes()
    except OSError as exc:
        raise PhysicsAuditorIntegrityError("Physics Auditor artifact is unavailable") from exc


def _read_optional_bytes(path: Path) -> bytes:
    return _read_exact_bytes(path) if path.exists() else b""


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(_read_exact_bytes(path)).hexdigest()


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left.is_relative_to(right) or right.is_relative_to(left)


def _reject_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")
