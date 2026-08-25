"""Deterministic process adapter for one exact human-authored Codex prompt."""

from __future__ import annotations

import hashlib
import json
import os
import selectors
import shutil
import signal
import stat
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from packaging.version import Version

from research_automation_supervisor import __version__
from research_automation_supervisor.codex_models import (
    CodexRunResult,
    GitWorktreeChecker,
    PreparedCodexRequest,
    RunStatus,
    load_codex_request,
)
from research_automation_supervisor.context_economy import (
    codex_context_configuration,
    context_economy_receipt_from_events,
    durable_context_config_item,
)
from research_automation_supervisor.doctor import MINIMUM_CODEX, _parse_codex_version
from research_automation_supervisor.errors import (
    CodexConfidentialityError,
    CodexDependencyError,
    CodexRequestError,
)
from research_automation_supervisor.execution_budget_enforcement import (
    LiveExecutionBudgetControllerV1,
)
from research_automation_supervisor.native_rollout_budget import (
    NativeRolloutBudgetObserverV1,
)
from research_automation_supervisor.process_enforcement import (
    CONTAINMENT_BACKEND,
    PROCESS_TERMINATION_EVIDENCE_FILENAME,
    ContainmentBackend,
    ContainmentControlError,
    OwnedProcessIdentityV1,
    ProcessEnforcementPolicyV1,
    ProcessGroupSignalResultV1,
    ProcessTerminationEvidenceV1,
    ProcessTerminationReasonV1,
    SystemdStopResultV1,
    SystemdUnitInspectionV1,
    SystemdUserCgroupV2Backend,
    file_sha256,
    inspect_owned_process_group,
    load_process_termination_evidence,
    new_action_unit_name,
    process_start_ticks,
    write_process_termination_evidence,
)
from research_automation_supervisor.redaction import (
    is_sensitive_name,
    redact_json,
    redact_text,
    would_redact_text,
)
from research_automation_supervisor.structured_outputs import (
    ProductionSchemaError,
    validate_production_schema,
)
from research_automation_supervisor.systemd_launch_helper import (
    encode_environment_frame,
)
from research_automation_supervisor.token_accounting import (
    CodexTurnUsageV1,
    CodexUsageBindingV1,
    CodexUsageReceiptV1,
    aggregate_task_receipts,
    cumulative_usage_from_jsonl,
    load_verified_receipt,
    receipt_from_jsonl,
    write_ledger,
    write_receipt,
)

STDOUT_LIMIT_BYTES = 100 * 1024 * 1024
STDERR_LIMIT_BYTES = 10 * 1024 * 1024
TERMINATION_GRACE_SECONDS = 2.0
IO_POLL_SECONDS = 0.1
VERSION_PROBE_TIMEOUT_SECONDS = 10.0
AUDITOR_SCRATCH_DIRECTORY_NAME = "scratch"
AUDITOR_SANDBOX_DISPOSITION = "workspace-read-only-action-scratch-write"

_PERMISSION_PHRASES = (
    "permission denied",
    "operation not permitted",
    "sandbox denied",
    "sandbox violation",
    "approval required",
    "network access disabled",
    "network is disabled",
    "read-only file system",
)
_FAILURE_EVENT_TYPES = frozenset(
    {
        "error",
        "failure",
        "denial",
        "turn.error",
        "turn.failed",
        "turn.denied",
        "item.error",
        "item.failed",
        "item.denied",
        "command.error",
        "command.failed",
        "command.denied",
    }
)
_EXPLICIT_PERMISSION_EVENT_TYPES = frozenset(
    {"approval.required", "network.denied", "permission.denied", "sandbox.denied"}
)
_COMMAND_ITEM_TYPES = frozenset(
    {"command", "command.execution", "command_execution", "exec", "exec_command"}
)
_FAILED_COMMAND_STATUSES = frozenset({"blocked", "denied", "error", "failed", "failure"})
_FAILURE_BEARING_FIELDS = (
    "aggregated_output",
    "detail",
    "error",
    "failure",
    "message",
    "output",
    "reason",
    "stderr",
)


@dataclass(frozen=True)
class AdapterLimits:
    """Fixed production limits, replaceable with smaller values in tests."""

    stdout_bytes: int = STDOUT_LIMIT_BYTES
    stderr_bytes: int = STDERR_LIMIT_BYTES
    termination_grace_seconds: float = TERMINATION_GRACE_SECONDS
    io_poll_seconds: float = IO_POLL_SECONDS


DEFAULT_LIMITS = AdapterLimits()


def _default_usage_binding(
    prepared: PreparedCodexRequest,
    *,
    resume_thread_id: str | None,
) -> CodexUsageBindingV1:
    """Bind standalone adapter calls without guessing any external campaign identity."""
    workspace_identity = hashlib.sha256(str(prepared.workspace).encode("utf-8")).hexdigest()[:24]
    role = {
        "worker": "worker",
        "auditor": "coding_auditor",
        "supervisor": "supervisor",
    }[prepared.request.role]
    return CodexUsageBindingV1(
        campaign_id=f"standalone-{workspace_identity}",
        task_id=prepared.request.run_id,
        action_id=prepared.request.run_id,
        role=cast(Any, role),
        repair_or_retry=resume_thread_id is not None,
    )


def _refresh_usage_ledger(
    prepared: PreparedCodexRequest,
    runs_dir: Path,
    binding: CodexUsageBindingV1,
) -> None:
    """Rebuild a deduplicated ledger from verified receipts, including recovery reuse."""
    if prepared.usage_ledger_root is None and prepared.usage_ledger_path is None:
        return
    root = prepared.usage_ledger_root or runs_dir
    destination = prepared.usage_ledger_path or (runs_dir / "task-token-ledger.json")
    receipts = []
    for current, directories, files in os.walk(root, topdown=True, followlinks=False):
        directories[:] = sorted(
            name for name in directories if not (Path(current) / name).is_symlink()
        )
        if "usage-receipt.json" not in files:
            continue
        receipt_path = Path(current) / "usage-receipt.json"
        event_log = Path(current) / "events.jsonl"
        receipt = load_verified_receipt(receipt_path, event_log=event_log)
        if _sealed_prelaunch_receipt(receipt_path, event_log, receipt):
            continue
        if receipt.incomplete_reasons == (
            "non_monotonic_or_changed_usage_counters",
        ):
            recovered = receipt_from_jsonl(
                event_log,
                binding=CodexUsageBindingV1(
                    campaign_id=receipt.campaign_id,
                    task_id=receipt.task_id,
                    action_id=receipt.action_id,
                    role=receipt.role,
                    repair_or_retry=receipt.repair_or_retry,
                ),
                model=receipt.model,
                codex_cli_version=receipt.codex_cli_version,
                per_turn_usage=True,
            )
            if recovered.complete:
                write_receipt(
                    destination.parent
                    / "recovered-usage-receipts"
                    / f"{receipt.receipt_id}.json",
                    recovered,
                )
                receipt = recovered
        receipts.append(receipt)
    ledger = aggregate_task_receipts(
        receipts,
        campaign_id=binding.campaign_id,
        task_id=binding.task_id,
    )
    write_ledger(destination, ledger)
    campaign_ledger = aggregate_task_receipts(
        receipts,
        campaign_id=binding.campaign_id,
        task_id="*",
    )
    write_ledger(destination.parent / "campaign-token-ledger.json", campaign_ledger)


def _sealed_prelaunch_receipt(
    receipt_path: Path,
    event_log: Path,
    receipt: CodexUsageReceiptV1,
) -> bool:
    """Prove an incomplete receipt belongs to an action that launched no model."""
    if receipt.incomplete_reasons != (
        "missing_or_ambiguous_thread_id",
        "missing_turn_completed_event",
    ):
        return False
    if receipt.event_count != 0 or receipt.completed_turn_count != 0:
        return False

    directory = receipt_path.parent
    paths = {
        "events": event_log,
        "metadata": directory / "metadata.json",
        "process": directory / PROCESS_TERMINATION_EVIDENCE_FILENAME,
        "receipt": receipt_path,
        "result": directory / "result.json",
    }
    completion_path = directory / "stage2-completion.json"
    try:
        if any(path.is_symlink() or not path.is_file() for path in paths.values()):
            return False
        if completion_path.is_symlink() or not completion_path.is_file():
            return False
        completion = json.loads(completion_path.read_bytes())
        artifact_hashes = completion["artifact_hashes"]
        if any(
            artifact_hashes.get(str(path)) != file_sha256(path)
            for path in paths.values()
        ):
            return False
        metadata = json.loads(paths["metadata"].read_bytes())
        result = CodexRunResult.model_validate_json(paths["result"].read_bytes())
        termination = load_process_termination_evidence(paths["process"])
    except (KeyError, OSError, TypeError, ValueError):
        return False

    return bool(
        completion.get("run_id") == receipt.action_id
        and completion.get("role")
        == {"coding_auditor": "auditor"}.get(receipt.role, receipt.role)
        and completion.get("result_status") == "launch_failed"
        and result.run_id == receipt.action_id
        and result.status == "launch_failed"
        and result.event_count == 0
        and metadata.get("run_id") == receipt.action_id
        and metadata.get("process_enforcement_enabled") is True
        and metadata.get("process_launched") is False
        and metadata.get("launch_error_present") is True
        and metadata.get("valid_event_count") == 0
        and metadata.get("usage_receipt_id") == receipt.receipt_id
        and metadata.get("usage_complete") is False
        and termination.action_id == receipt.action_id
        and termination.phase == "termination_failed"
        and termination.invocation_id is None
        and termination.control_group is None
        and termination.process_identity is None
    )


def _prior_cumulative_usage_for_thread(
    root: Path,
    *,
    current_event_log: Path,
    thread_id: str,
) -> CodexTurnUsageV1 | None:
    """Find the unique componentwise-latest retained snapshot for one thread."""
    snapshots: list[CodexTurnUsageV1] = []
    current_resolved = current_event_log.resolve()
    for current, directories, files in os.walk(root, topdown=True, followlinks=False):
        directories[:] = sorted(
            name for name in directories if not (Path(current) / name).is_symlink()
        )
        if "events.jsonl" not in files:
            continue
        event_log = Path(current) / "events.jsonl"
        if event_log.resolve() == current_resolved:
            continue
        try:
            candidate_thread, snapshot = cumulative_usage_from_jsonl(event_log)
        except (OSError, ValueError):
            continue
        if candidate_thread == thread_id:
            snapshots.append(snapshot)
    if not snapshots:
        return None
    latest = snapshots[0]
    for candidate in snapshots[1:]:
        latest_values = latest.model_dump(exclude_none=True)
        candidate_values = candidate.model_dump(exclude_none=True)
        if latest_values.keys() != candidate_values.keys():
            return None
        if all(candidate_values[key] >= latest_values[key] for key in latest_values):
            latest = candidate
        elif not all(latest_values[key] >= candidate_values[key] for key in latest_values):
            return None
    return latest


@dataclass
class _ProcessObservation:
    launched: bool = False
    exit_code: int | None = None
    termination_reason: str | None = None
    output_limit_stream: str | None = None
    stdin_error: bool = False
    stdout_bytes: int = 0
    stderr_bytes: int = 0
    stderr: bytearray = field(default_factory=bytearray)
    launch_error: str | None = None
    confidentiality_violation_detected: bool = False
    execution_budget_decision: str | None = None
    termination_evidence: ProcessTerminationEvidenceV1 | None = None


@dataclass
class _EventProcessor:
    destination: Any
    sensitive_values: tuple[str, ...]
    rejected_values: tuple[bytes, ...] = ()
    pending: bytearray = field(default_factory=bytearray)
    event_count: int = 0
    malformed_hashes: list[str] = field(default_factory=list)
    permission_evidence: bool = False
    identifier_kind: str | None = None
    identifier_value: str | None = None
    started_thread_ids: list[str] = field(default_factory=list)
    confidentiality_violation_detected: bool = False
    native_rollout_budget_observer: NativeRolloutBudgetObserverV1 | None = None

    def feed(self, chunk: bytes) -> None:
        """Accept a bounded stdout chunk and emit all complete redacted events."""
        self.pending.extend(chunk)
        while True:
            newline = self.pending.find(b"\n")
            if newline < 0:
                return
            line = bytes(self.pending[:newline])
            del self.pending[: newline + 1]
            self._process_line(line.removesuffix(b"\r"))

    def finish(self) -> None:
        """Process a final unterminated JSONL line, if present."""
        if self.pending:
            line = bytes(self.pending)
            self.pending.clear()
            self._process_line(line.removesuffix(b"\r"))

    def _process_line(self, line: bytes) -> None:
        if not line.strip():
            return
        rejected = _contains_confidential_fragment(line, self.rejected_values)
        if rejected:
            self.confidentiality_violation_detected = True
        try:
            value = json.loads(line.decode("utf-8"), parse_constant=_reject_json_constant)
            if not isinstance(value, dict):
                raise ValueError("JSONL event is not an object")
            redacted = redact_json(value, self.sensitive_values)
            if not isinstance(redacted, dict):
                raise ValueError("redacted JSONL event is not an object")
            rendered = json.dumps(
                redacted,
                ensure_ascii=True,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            permission_evidence = _event_has_permission_evidence(value)
            kind, identifier = _extract_identifier(value)
            started_thread_id = _extract_started_thread_id(value)
        except Exception:
            malformed_evidence = b"confidentiality-violation" if rejected else line
            self.malformed_hashes.append(hashlib.sha256(malformed_evidence).hexdigest())
            return

        if started_thread_id is not None and self.native_rollout_budget_observer is not None:
            self.native_rollout_budget_observer.bind_thread(started_thread_id)

        self.event_count += 1
        if permission_evidence:
            self.permission_evidence = True
        if self.identifier_value is None and identifier is not None:
            self.identifier_kind = kind
            self.identifier_value = identifier
        if started_thread_id is not None and started_thread_id not in self.started_thread_ids:
            self.started_thread_ids.append(started_thread_id)
        self.destination.write(rendered.encode("ascii") + b"\n")
        self.destination.flush()


VersionProbe = Callable[[str, Mapping[str, str], Path], str | None]
UtcNow = Callable[[], datetime]
Monotonic = Callable[[], float]
ProcessStarted = Callable[[int], None]
ProcessFinished = Callable[[int], None]


@dataclass(frozen=True)
class CodexProcessLaunch:
    """Exact subprocess boundary selected after the semantic Codex argv is built."""

    command: tuple[str, ...]
    cwd: Path
    environment: Mapping[str, str]


ProcessLaunchBuilder = Callable[
    [
        Sequence[str],
        PreparedCodexRequest,
        Mapping[str, str],
        Path,
        Path | None,
    ],
    CodexProcessLaunch,
]
ProcessLaunchVerifier = Callable[[CodexProcessLaunch], None]


def execute_codex_request(
    request_path: Path,
    *,
    runs_dir: Path,
    codex_executable: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
    environ: Mapping[str, str] | None = None,
    git_worktree_checker: GitWorktreeChecker | None = None,
    limits: AdapterLimits = DEFAULT_LIMITS,
    monotonic: Monotonic = time.monotonic,
    utc_now: UtcNow = lambda: datetime.now(UTC),
    version_probe: VersionProbe | None = None,
    process_enforcement_policy: ProcessEnforcementPolicyV1 | None = None,
    containment_backend: ContainmentBackend | None = None,
) -> CodexRunResult:
    """Validate one request and execute exactly one deterministic Codex process."""
    environment, _, sensitive_values = build_subprocess_environment(environ)
    validate_locator_confidentiality((request_path,), sensitive_values)
    prepared = load_codex_request(
        request_path,
        git_worktree_checker=git_worktree_checker,
    )
    resolved_runs_dir = validate_request_confidentiality(
        prepared,
        sensitive_values,
        request_locator=request_path,
        runs_dir=runs_dir,
    )
    assert resolved_runs_dir is not None
    executable = codex_executable or which("codex")
    if executable is None:
        raise CodexDependencyError("Codex executable is required")
    executable_path = str(Path(executable).resolve())
    probe = version_probe or probe_codex_version
    codex_version = probe(executable_path, environment, prepared.workspace)
    if codex_version is not None and Version(codex_version) < MINIMUM_CODEX:
        raise CodexDependencyError(f"Codex {MINIMUM_CODEX} or newer is required")

    def use_probed_version(
        ignored_executable: str,
        ignored_environment: Mapping[str, str],
        ignored_workspace: Path,
    ) -> str | None:
        del ignored_executable, ignored_environment, ignored_workspace
        return codex_version

    return run_prepared_codex(
        prepared,
        runs_dir=resolved_runs_dir,
        codex_executable=executable_path,
        environ=environ,
        limits=limits,
        monotonic=monotonic,
        utc_now=utc_now,
        version_probe=use_probed_version,
        process_enforcement_policy=process_enforcement_policy,
        containment_backend=containment_backend,
    )


def run_prepared_codex(
    prepared: PreparedCodexRequest,
    *,
    runs_dir: Path,
    codex_executable: str,
    environ: Mapping[str, str] | None = None,
    limits: AdapterLimits = DEFAULT_LIMITS,
    monotonic: Monotonic = time.monotonic,
    utc_now: UtcNow = lambda: datetime.now(UTC),
    version_probe: VersionProbe | None = None,
    output_schema: Path | None = None,
    resume_thread_id: str | None = None,
    skip_git_repo_check: bool = False,
    confidential_fragments: Sequence[str] = (),
    rejected_confidential_fragments: Sequence[str] = (),
    durable_command_replacements: Mapping[str, str] | None = None,
    process_launch_builder: ProcessLaunchBuilder | None = None,
    process_launch_verifier: ProcessLaunchVerifier | None = None,
    process_started: ProcessStarted | None = None,
    process_finished: ProcessFinished | None = None,
    execution_budget_controller: LiveExecutionBudgetControllerV1 | None = None,
    process_enforcement_policy: ProcessEnforcementPolicyV1 | None = None,
    containment_backend: ContainmentBackend | None = None,
) -> CodexRunResult:
    """Run an already validated request and durably finalize its artifacts."""
    environment, removed_names, sensitive_values = build_subprocess_environment(environ)
    resolved_runs_dir = validate_request_confidentiality(
        prepared,
        sensitive_values,
        runs_dir=runs_dir,
    )
    assert resolved_runs_dir is not None
    resolved_output_schema = _validate_output_schema(output_schema, sensitive_values)
    if resume_thread_id is not None:
        if prepared.request.role not in {"worker", "supervisor"}:
            raise CodexRequestError("only a persistent worker or supervisor may resume a thread")
        if not resume_thread_id.strip() or resume_thread_id in {"--last", "--all"}:
            raise CodexRequestError("an explicit persistent thread ID is required for resume")
        validate_locator_confidentiality((resume_thread_id,), sensitive_values)
    artifact_directory = _create_artifact_directory(
        resolved_runs_dir,
        prepared.request.run_id,
    )
    auditor_scratch = (
        prepare_auditor_scratch_directory(artifact_directory)
        if prepared.request.role == "auditor"
        else None
    )
    if auditor_scratch is not None:
        scratch_value = str(auditor_scratch)
        environment.update(
            {
                "TMPDIR": scratch_value,
                "TMP": scratch_value,
                "TEMP": scratch_value,
            }
        )
    redaction_values = tuple(
        sorted(
            {value for value in (*sensitive_values, *confidential_fragments) if value},
            key=lambda item: (-len(item), item),
        )
    )
    rejected_values = tuple(
        sorted(
            {value.encode("utf-8") for value in rejected_confidential_fragments if value},
            key=lambda item: (-len(item), item),
        )
    )
    _initialize_artifacts(
        artifact_directory,
        prepared,
        redaction_values,
        skip_git_repo_check=skip_git_repo_check,
    )

    executable_path = str(Path(codex_executable).resolve())
    probe = version_probe or probe_codex_version
    codex_version = probe(executable_path, environment, prepared.workspace)

    started_at = utc_now()
    started_monotonic = monotonic()
    observation = _ProcessObservation()
    event_path = artifact_directory / "events.jsonl"
    raw_final_bytes: bytes | None = None
    process_enforcement_enabled = (
        execution_budget_controller is not None
        or process_enforcement_policy is not None
    )
    effective_process_enforcement_policy = (
        process_enforcement_policy or ProcessEnforcementPolicyV1()
        if process_enforcement_enabled
        else None
    )
    effective_containment_backend = (
        containment_backend
        or SystemdUserCgroupV2Backend(
            environment,
            control_plane_timeout_seconds=(
                effective_process_enforcement_policy.control_plane_timeout_seconds
                if effective_process_enforcement_policy is not None
                else 5.0
            ),
        )
        if process_enforcement_enabled
        else None
    )
    process_termination_evidence_path = (
        artifact_directory / PROCESS_TERMINATION_EVIDENCE_FILENAME
        if process_enforcement_enabled
        else None
    )
    if process_termination_evidence_path is not None:
        initial_thread_id = (
            execution_budget_controller.checkpoint.codex_thread_id
            if execution_budget_controller is not None
            else resume_thread_id
        )
        observation.termination_evidence = ProcessTerminationEvidenceV1(
            task_id=(
                execution_budget_controller.checkpoint.task_id
                if execution_budget_controller is not None
                else prepared.request.run_id
            ),
            action_id=prepared.request.run_id,
            codex_thread_id=initial_thread_id,
            containment_backend=CONTAINMENT_BACKEND,
            unit_name=new_action_unit_name(),
        )
        write_process_termination_evidence(
            process_termination_evidence_path,
            observation.termination_evidence,
        )
    native_rollout_budget_observer = (
        NativeRolloutBudgetObserverV1.create(
            controller=execution_budget_controller,
            sessions_root=_codex_sessions_root(environment),
            source_cursor_directory=_native_rollout_cursor_directory(environment),
            require_existing_source_cursor=resume_thread_id is not None,
        )
        if execution_budget_controller is not None
        else None
    )

    with tempfile.TemporaryDirectory(prefix="research-supervisor-codex-") as temporary:
        temporary_final = Path(temporary) / "last-message.md"
        command = build_codex_command(
            prepared,
            executable_path,
            temporary_final,
            output_schema=resolved_output_schema,
            resume_thread_id=resume_thread_id,
            skip_git_repo_check=skip_git_repo_check,
            writable_scratch=auditor_scratch,
        )
        launch = (
            process_launch_builder(
                command,
                prepared,
                environment,
                temporary_final,
                resolved_output_schema,
            )
            if process_launch_builder is not None
            else CodexProcessLaunch(
                command=tuple(command),
                cwd=prepared.workspace,
                environment=environment,
            )
        )
        with event_path.open("wb") as event_file:
            event_processor = _EventProcessor(
                event_file,
                redaction_values,
                rejected_values,
                native_rollout_budget_observer=native_rollout_budget_observer,
            )
            _run_process(
                launch.command,
                prepared,
                launch.environment,
                launch.cwd,
                event_processor,
                observation,
                limits,
                started_monotonic,
                monotonic,
                rejected_values,
                (
                    (lambda: process_launch_verifier(launch))
                    if process_launch_verifier is not None
                    else None
                ),
                process_started,
                process_finished,
                native_rollout_budget_observer,
                effective_process_enforcement_policy,
                process_termination_evidence_path,
                effective_containment_backend,
            )
            event_processor.finish()

        try:
            if temporary_final.is_file():
                raw_final_bytes = temporary_final.read_bytes()
        except OSError:
            raw_final_bytes = None
        if _scan_temporary_action_directory(
            Path(temporary),
            rejected_values,
        ):
            observation.confidentiality_violation_detected = True

    ended_monotonic = monotonic()
    ended_at = utc_now()
    duration = max(0.0, ended_monotonic - started_monotonic)
    if _contains_confidential_fragment(
        bytes(observation.stderr),
        rejected_values,
    ):
        observation.confidentiality_violation_detected = True
    stderr_text = bytes(observation.stderr).decode("utf-8", errors="replace")
    prompt_text = prepared.prompt_bytes.decode("utf-8")
    redacted_stderr = redact_text(
        stderr_text,
        (*redaction_values, prompt_text),
    )
    _write_text(artifact_directory / "stderr.log", redacted_stderr)

    final_message = ""
    if raw_final_bytes is not None:
        if _contains_confidential_fragment(raw_final_bytes, rejected_values):
            observation.confidentiality_violation_detected = True
        final_message = redact_text(
            raw_final_bytes.decode("utf-8", errors="replace"),
            redaction_values,
        )
    final_message_present = bool(final_message.strip())
    _write_text(artifact_directory / "final-message.md", final_message)

    stderr_permission = _contains_permission_phrase(stderr_text)
    otherwise_unsuccessful = (
        not observation.launched
        or observation.termination_reason is not None
        or observation.exit_code != 0
        or observation.stdin_error
        or bool(event_processor.malformed_hashes)
        or not final_message_present
    )
    permission_evidence = otherwise_unsuccessful and (
        stderr_permission or event_processor.permission_evidence
    )
    status = _classify_status(
        observation,
        event_processor,
        final_message_present,
        permission_evidence,
        (
            observation.confidentiality_violation_detected
            or event_processor.confidentiality_violation_detected
        ),
    )
    confidentiality_violation_detected = (
        observation.confidentiality_violation_detected
        or event_processor.confidentiality_violation_detected
    )
    summary, error = _status_messages(
        status,
        observation,
        confidentiality_violation_detected,
    )
    result = _sanitize_result(
        CodexRunResult(
            run_id=prepared.request.run_id,
            status=status,
            exit_code=observation.exit_code,
            started_at=_utc_string(started_at),
            ended_at=_utc_string(ended_at),
            duration_seconds=round(duration, 6),
            artifact_directory=str(artifact_directory),
            event_count=event_processor.event_count,
            malformed_event_count=len(event_processor.malformed_hashes),
            final_message_present=final_message_present,
            permission_evidence=permission_evidence,
            confidentiality_violation_detected=(confidentiality_violation_detected),
            summary=summary,
            error=error,
        ),
        redaction_values,
    )

    usage_binding = prepared.usage_binding or _default_usage_binding(
        prepared,
        resume_thread_id=resume_thread_id,
    )
    usage_receipt = receipt_from_jsonl(
        event_path,
        binding=usage_binding,
        model=prepared.request.model,
        codex_cli_version=codex_version,
        known_malformed_event_count=len(event_processor.malformed_hashes),
        per_turn_usage=True,
    )
    usage_receipt_path = artifact_directory / "usage-receipt.json"
    write_receipt(usage_receipt_path, usage_receipt)
    context_receipt = context_economy_receipt_from_events(
        event_path,
        prompt_bytes=len(prepared.prompt_bytes),
        profile=prepared.request.brevity_profile,
        usage_receipt=usage_receipt,
        overrides=(
            (prepared.request.context_economy_override,)
            if prepared.request.context_economy_override is not None
            else ()
        ),
    )
    context_receipt_path = artifact_directory / "context-economy-receipt.json"
    _atomic_write_json(context_receipt_path, context_receipt.model_dump(mode="json"))
    _refresh_usage_ledger(
        prepared,
        resolved_runs_dir,
        usage_binding,
    )

    metadata = _build_metadata(
        prepared=prepared,
        artifact_directory=artifact_directory,
        command=launch.command,
        executable_path=executable_path,
        codex_version=codex_version,
        removed_names=removed_names,
        started_at=started_at,
        ended_at=ended_at,
        duration=duration,
        observation=observation,
        events=event_processor,
        final_message_present=final_message_present,
        permission_evidence=permission_evidence,
        output_schema=resolved_output_schema,
        resume_thread_id=resume_thread_id,
        limits=limits,
        confidentiality_violation_detected=(confidentiality_violation_detected),
        durable_command_replacements=durable_command_replacements or {},
        auditor_scratch=auditor_scratch,
        usage_receipt_path=usage_receipt_path,
        usage_receipt_id=usage_receipt.receipt_id,
        usage_complete=usage_receipt.complete,
        context_receipt_path=context_receipt_path,
    )
    _atomic_write_json(
        artifact_directory / "metadata.json",
        redact_json(metadata, redaction_values),
    )
    _atomic_write_json(
        artifact_directory / "result.json",
        result.to_dict(),
    )
    if resolved_output_schema is not None:
        _write_stage2_completion_manifest(
            artifact_directory,
            prepared,
            result,
            resolved_output_schema,
        )
    return result


def build_subprocess_environment(
    environ: Mapping[str, str] | None = None,
) -> tuple[dict[str, str], tuple[str, ...], tuple[str, ...]]:
    """Copy an environment and remove credential-shaped names case-insensitively."""
    source = os.environ if environ is None else environ
    copied: dict[str, str] = {}
    removed_names: list[str] = []
    sensitive_values: list[str] = []
    for name, value in source.items():
        if is_sensitive_name(name):
            removed_names.append(name)
            if value:
                sensitive_values.append(value)
        else:
            copied[name] = value
    return (
        copied,
        tuple(sorted(removed_names, key=lambda item: (item.casefold(), item))),
        tuple(sorted(set(sensitive_values), key=lambda item: (-len(item), item))),
    )


def validate_request_confidentiality(
    prepared: PreparedCodexRequest,
    sensitive_values: Sequence[str],
    *,
    request_locator: Path | None = None,
    runs_dir: Path | None = None,
) -> Path | None:
    """Reject exact request structures that the complete redactor would modify."""
    locators = [
        str(prepared.request_path),
        prepared.request.run_id,
        prepared.request.role,
        prepared.request.model,
        prepared.request.reasoning_effort,
        str(prepared.workspace),
        str(prepared.prompt_path),
        prepared.prompt_sha256,
        prepared.policy.sandbox,
        prepared.policy.approval,
    ]
    if request_locator is not None:
        locators.append(str(request_locator))

    resolved_runs_dir: Path | None = None
    if runs_dir is not None:
        resolved_runs_dir = _resolve_runs_directory(runs_dir)
        artifact_directory = resolved_runs_dir / prepared.request.run_id
        locators.extend((str(runs_dir), str(resolved_runs_dir), str(artifact_directory)))

    validate_locator_confidentiality(locators, sensitive_values)
    return resolved_runs_dir


def validate_locator_confidentiality(
    locators: Sequence[str | Path],
    sensitive_values: Sequence[str],
) -> None:
    """Reject exact structural strings that cannot be rendered unchanged."""
    rendered_locators = tuple(str(locator) for locator in locators)
    if any(would_redact_text(locator, sensitive_values) for locator in rendered_locators):
        raise CodexConfidentialityError("Codex request contains a structural redaction collision")


def _validate_output_schema(
    output_schema: Path | None,
    sensitive_values: Sequence[str],
) -> Path | None:
    if output_schema is None:
        return None
    try:
        resolved = output_schema.resolve(strict=True)
        if not resolved.is_file():
            raise CodexRequestError("output schema is not a regular file")
        content = resolved.read_bytes()
    except CodexRequestError:
        raise
    except (OSError, RuntimeError) as exc:
        raise CodexRequestError("output schema path could not be resolved") from exc
    if len(content) > 2 * 1024 * 1024:
        raise CodexRequestError("output schema exceeds the adapter-owned size limit")
    try:
        parsed = json.loads(content.decode("utf-8"), parse_constant=_reject_json_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise CodexRequestError("output schema is not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise CodexRequestError("output schema root must be a JSON object")
    try:
        validate_production_schema(parsed)
    except ProductionSchemaError as exc:
        raise CodexRequestError(f"output schema is not production-compatible: {exc}") from exc
    validate_locator_confidentiality(
        (resolved, hashlib.sha256(content).hexdigest()), sensitive_values
    )
    return resolved


def build_codex_command(
    prepared: PreparedCodexRequest,
    executable: str,
    final_message_path: Path,
    *,
    output_schema: Path | None = None,
    resume_thread_id: str | None = None,
    skip_git_repo_check: bool = False,
    writable_scratch: Path | None = None,
) -> list[str]:
    """Construct the fixed shell-free Codex argument vector."""
    request = prepared.request
    if writable_scratch is not None and request.role != "auditor":
        raise CodexRequestError("only an auditor may receive action-owned scratch")
    if writable_scratch is not None and (
        not writable_scratch.is_absolute()
        or writable_scratch.name != AUDITOR_SCRATCH_DIRECTORY_NAME
    ):
        raise CodexRequestError("auditor scratch locator is invalid")
    if resume_thread_id is not None and (
        request.role not in {"worker", "supervisor"}
        or not resume_thread_id.strip()
        or resume_thread_id in {"--last", "--all"}
    ):
        raise CodexRequestError(
            "resume requires one exact persistent worker or supervisor thread ID"
        )
    context_config = _context_economy_config(prepared)
    if resume_thread_id is None:
        command = [
            executable,
            "--ask-for-approval",
            prepared.policy.approval,
            "exec",
            *(["--skip-git-repo-check"] if skip_git_repo_check else []),
            "--json",
            "--output-last-message",
            str(final_message_path),
            "--model",
            request.model,
            "-c",
            f"model_reasoning_effort={request.reasoning_effort}",
            "-c",
            'web_search="disabled"',
            "-c",
            "sandbox_workspace_write.network_access=false",
            "-c",
            "features.skill_mcp_dependency_install=false",
            "-c",
            "features.code_mode_host=true",
            *context_config,
            "--sandbox",
            prepared.policy.sandbox,
            "--ignore-user-config",
            "--ignore-rules",
            "--strict-config",
            "--cd",
            str(prepared.workspace),
            *(["--output-schema", str(output_schema)] if output_schema is not None else []),
        ]
        if writable_scratch is not None:
            command.extend(("--add-dir", str(writable_scratch)))
        if prepared.policy.ephemeral:
            command.append("--ephemeral")
    else:
        command = [
            executable,
            "--ask-for-approval",
            prepared.policy.approval,
            "exec",
            *(["--skip-git-repo-check"] if skip_git_repo_check else []),
            "--json",
            "--output-last-message",
            str(final_message_path),
            "--model",
            request.model,
            "-c",
            f"model_reasoning_effort={request.reasoning_effort}",
            "-c",
            'web_search="disabled"',
            "-c",
            "sandbox_workspace_write.network_access=false",
            "-c",
            "features.skill_mcp_dependency_install=false",
            "-c",
            "features.code_mode_host=true",
            *context_config,
            "--sandbox",
            prepared.policy.sandbox,
            "--ignore-user-config",
            "--ignore-rules",
            "--strict-config",
            "--cd",
            str(prepared.workspace),
            *(["--output-schema", str(output_schema)] if output_schema is not None else []),
            "resume",
            resume_thread_id,
        ]
    command.append("-")
    return command


def _context_economy_config(prepared: PreparedCodexRequest) -> list[str]:
    """Render only documented Codex configuration; never fake a context window."""
    request = prepared.request
    return list(
        codex_context_configuration(
            request.brevity_profile,
            request.context_economy_override,
        )
    )


def probe_codex_version(
    executable: str,
    environment: Mapping[str, str],
    workspace: Path,
) -> str | None:
    """Return only a strictly parsed Codex version, when the local probe succeeds."""
    try:
        completed = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            check=False,
            cwd=workspace,
            encoding="utf-8",
            errors="replace",
            env=dict(environment),
            timeout=VERSION_PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return _parse_codex_version(completed.stdout)


def _codex_sessions_root(environment: Mapping[str, str]) -> Path:
    codex_home = environment.get("CODEX_HOME")
    if codex_home:
        return Path(codex_home).resolve() / "sessions"
    home = environment.get("HOME")
    if not home:
        raise CodexRequestError(
            "execution-budget integration requires CODEX_HOME or HOME"
        )
    return Path(home).resolve() / ".codex" / "sessions"


def _native_rollout_cursor_directory(environment: Mapping[str, str]) -> Path:
    return _codex_sessions_root(environment).parent / "execution-budget-rollout-cursors"


def _run_systemd_contained_process(
    command: Sequence[str],
    prepared: PreparedCodexRequest,
    environment: Mapping[str, str],
    cwd: Path,
    events: _EventProcessor,
    observation: _ProcessObservation,
    limits: AdapterLimits,
    started_monotonic: float,
    monotonic: Monotonic,
    rejected_values: Sequence[bytes],
    prelaunch_verifier: Callable[[], None] | None,
    process_started: ProcessStarted | None,
    process_finished: ProcessFinished | None,
    observer: NativeRolloutBudgetObserverV1 | None,
    policy: ProcessEnforcementPolicyV1,
    evidence_path: Path,
    backend: ContainmentBackend,
) -> None:
    """Run one action with systemd/cgroup-v2 as its only closure authority."""
    evidence = observation.termination_evidence
    if evidence is None:
        raise RuntimeError("process termination evidence was not initialized")
    unit_name = evidence.unit_name
    if prelaunch_verifier is not None:
        prelaunch_verifier()
    try:
        backend.preflight(unit_name)
    except ContainmentControlError as exc:
        observation.launch_error = "Codex containment is unavailable"
        _persist_containment_failure(observation, evidence_path, str(exc), None)
        return

    _update_process_termination_evidence(
        observation,
        evidence_path,
        phase="launch_intent_persisted",
    )
    launch_command = backend.build_launch_command(
        unit_name,
        command,
        cwd,
        limits.termination_grace_seconds,
        min(prepared.request.timeout_seconds, policy.max_wall_clock_seconds),
    )
    try:
        process = subprocess.Popen(
            list(launch_command),
            cwd=cwd,
            env=dict(
                getattr(backend, "control_environment", environment)
            ),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            close_fds=True,
        )
    except (OSError, subprocess.SubprocessError):
        observation.launch_error = "Codex containment launch failed"
        return

    observation.launched = True
    process_identity = _diagnostic_process_identity(process.pid)
    try:
        identity = backend.bind_identity(unit_name, process.pid)
    except (ContainmentControlError, OSError) as exc:
        identity = SystemdUnitInspectionV1(
            state="ambiguous",
            unit_name=unit_name,
            error=str(exc),
        )
    if (
        identity.state != "proven_live"
        or identity.invocation_id is None
        or identity.control_group is None
    ):
        observation.launch_error = "Codex containment identity could not be proven"
        _reconcile_post_launch_identity_uncertainty(
            process,
            observation,
            evidence_path,
            backend,
            limits,
            identity,
            process_identity,
        )
        return
    _update_process_termination_evidence(
        observation,
        evidence_path,
        phase="running",
        process_identity=process_identity,
        invocation_id=identity.invocation_id,
        control_group=identity.control_group,
        unit_active_state=identity.active_state,
        unit_sub_state=identity.sub_state,
        unit_result=identity.unit_result,
        cgroup_empty=False,
    )

    try:
        if process_started is not None:
            process_started(process.pid)
    except BaseException:
        _close_systemd_after_adapter_failure(
            process,
            observation,
            limits,
            evidence_path,
            observer,
            process_finished,
            backend,
        )
        raise
    if process.stdin is None or process.stdout is None or process.stderr is None:
        observation.launch_error = "Codex containment pipes were unavailable"
        _close_systemd_after_adapter_failure(
            process,
            observation,
            limits,
            evidence_path,
            observer,
            process_finished,
            backend,
        )
        return

    input_bytes = encode_environment_frame(dict(environment)) + prepared.prompt_bytes
    input_offset = 0
    selector = selectors.DefaultSelector()
    streams = {
        "stdin": process.stdin,
        "stdout": process.stdout,
        "stderr": process.stderr,
    }
    try:
        for name, stream in streams.items():
            os.set_blocking(stream.fileno(), False)
            selector.register(
                stream,
                selectors.EVENT_WRITE if name == "stdin" else selectors.EVENT_READ,
                name,
            )
    except (KeyError, OSError, ValueError):
        for stream in streams.values():
            if _is_registered(selector, stream):
                _unregister_and_close(selector, stream)
        selector.close()
        observation.launch_error = "Codex containment pipes could not be configured"
        _close_systemd_after_adapter_failure(
            process,
            observation,
            limits,
            evidence_path,
            observer,
            process_finished,
            backend,
        )
        return

    request_deadline = started_monotonic + prepared.request.timeout_seconds
    wall_clock_deadline = started_monotonic + policy.max_wall_clock_seconds
    stdout_open = True
    stderr_open = True
    stop_attempted = False
    termination_failed = False
    closure: SystemdUnitInspectionV1 | None = None
    closure_drain_deadline: float | None = None

    try:
        while True:
            if observer is not None and observation.termination_reason is None:
                observation.execution_budget_decision = observer.poll().decision
            now = monotonic()

            budget_reason = _budget_termination_reason(
                observation.execution_budget_decision
            )
            if budget_reason is not None and observation.termination_reason is None:
                observation.termination_reason = budget_reason
                _persist_termination_intent(
                    observation,
                    evidence_path,
                    budget_reason,
                    now - started_monotonic,
                    observer,
                )
            if (
                observation.termination_reason is None
                and observation.exit_code is None
                and now >= wall_clock_deadline
            ):
                observation.termination_reason = "wall_clock_limit_exceeded"
                _persist_termination_intent(
                    observation,
                    evidence_path,
                    "wall_clock_limit_exceeded",
                    now - started_monotonic,
                    observer,
                )
            if (
                observation.termination_reason is None
                and observation.exit_code is None
                and now >= request_deadline
            ):
                observation.termination_reason = "timeout"
                _persist_generic_containment_stop_intent(
                    observation,
                    evidence_path,
                    "adapter_timeout",
                )

            if observation.termination_reason is not None and not stop_attempted:
                stop_attempted = True
                stopped = _request_systemd_containment_stop(
                    observation,
                    evidence_path,
                    backend,
                    limits.termination_grace_seconds,
                )
                if stopped.status == "failed":
                    termination_failed = True
                else:
                    closure = stopped.inspection
                    closure_drain_deadline = (
                        time.monotonic() + backend.control_plane_timeout_seconds
                    )

            if (
                not stdout_open
                and not stderr_open
                and closure is None
                and not stop_attempted
            ):
                if _is_registered(selector, process.stdin):
                    observation.stdin_error = input_offset < len(input_bytes)
                    _unregister_and_close(selector, process.stdin)
                inspected = backend.inspect(
                    unit_name,
                    identity.invocation_id,
                    identity.control_group,
                )
                if inspected.state == "proven_live":
                    _persist_generic_containment_stop_intent(
                        observation,
                        evidence_path,
                        "surviving_processes_after_wrapper_exit",
                    )
                    stop_attempted = True
                    stopped = _request_systemd_containment_stop(
                        observation,
                        evidence_path,
                        backend,
                        limits.termination_grace_seconds,
                    )
                    if stopped.status == "failed":
                        termination_failed = True
                    else:
                        closure = stopped.inspection
                        closure_drain_deadline = (
                            time.monotonic() + backend.control_plane_timeout_seconds
                        )
                else:
                    closure = _normalize_closed_inspection(inspected, identity)
                    if closure is None:
                        _persist_containment_failure(
                            observation,
                            evidence_path,
                            inspected.error
                            or "exact containment closure was not proven",
                            inspected,
                        )
                        termination_failed = True

            if termination_failed:
                break
            if closure is not None:
                if not stdout_open and not stderr_open:
                    break
                if (
                    closure_drain_deadline is not None
                    and time.monotonic() >= closure_drain_deadline
                ):
                    break

            timeout = limits.io_poll_seconds
            if observation.termination_reason is None and observation.exit_code is None:
                timeout = min(
                    timeout,
                    max(0.0, request_deadline - now),
                    max(0.0, wall_clock_deadline - now),
                )
            try:
                ready = selector.select(timeout)
            except InterruptedError:
                continue
            for key, _ in ready:
                stream_name = str(key.data)
                ready_stream: Any = key.fileobj
                if stream_name == "stdin":
                    input_offset = _write_prompt_chunk(
                        selector,
                        ready_stream,
                        input_bytes,
                        input_offset,
                        observation,
                    )
                    continue
                try:
                    chunk = os.read(ready_stream.fileno(), 64 * 1024)
                except (BlockingIOError, InterruptedError):
                    continue
                except OSError:
                    chunk = b""
                if not chunk:
                    _unregister_and_close(selector, ready_stream)
                    if stream_name == "stdout":
                        stdout_open = False
                    else:
                        stderr_open = False
                    continue
                if _contains_confidential_fragment(chunk, rejected_values):
                    observation.confidentiality_violation_detected = True
                if stream_name == "stdout":
                    accepted = _accepted_prefix(
                        chunk,
                        observation.stdout_bytes,
                        limits.stdout_bytes,
                    )
                    observation.stdout_bytes += len(chunk)
                    if accepted:
                        events.feed(accepted)
                    if observation.stdout_bytes > limits.stdout_bytes:
                        observation.output_limit_stream = "stdout"
                        observation.termination_reason = "output_limit"
                        _persist_generic_containment_stop_intent(
                            observation,
                            evidence_path,
                            "stdout_limit_exceeded",
                        )
                else:
                    accepted = _accepted_prefix(
                        chunk,
                        observation.stderr_bytes,
                        limits.stderr_bytes,
                    )
                    observation.stderr_bytes += len(chunk)
                    observation.stderr.extend(accepted)
                    if observation.stderr_bytes > limits.stderr_bytes:
                        observation.output_limit_stream = "stderr"
                        observation.termination_reason = "output_limit"
                        _persist_generic_containment_stop_intent(
                            observation,
                            evidence_path,
                            "stderr_limit_exceeded",
                        )
    except BaseException:
        _close_systemd_after_adapter_failure(
            process,
            observation,
            limits,
            evidence_path,
            observer,
            process_finished,
            backend,
        )
        raise
    finally:
        for stream in streams.values():
            if _is_registered(selector, stream):
                _unregister_and_close(selector, stream)
        selector.close()

    if termination_failed:
        return
    if closure is None:
        _persist_containment_failure(
            observation,
            evidence_path,
            "exact containment closure was not observed",
            None,
        )
        return
    if not _bounded_reap_wrapper(process, backend.control_plane_timeout_seconds, limits):
        _persist_containment_failure(
            observation,
            evidence_path,
            "local systemd-run wrapper could not be reaped boundedly",
            closure,
        )
        return
    observation.exit_code = process.returncode
    _persist_reaped_process_evidence(
        observation,
        evidence_path,
        observer,
        closure,
    )
    if process_finished is not None:
        process_finished(process.pid)


def _diagnostic_process_identity(pid: int) -> OwnedProcessIdentityV1 | None:
    ticks = process_start_ticks(pid)
    if ticks is None:
        return None
    try:
        return OwnedProcessIdentityV1(
            pid=pid,
            process_group_id=os.getpgid(pid),
            session_id=os.getsid(pid),
            start_ticks=ticks,
        )
    except (OSError, ProcessLookupError):
        return None


def _reconcile_post_launch_identity_uncertainty(
    process: subprocess.Popen[bytes],
    observation: _ProcessObservation,
    evidence_path: Path,
    backend: ContainmentBackend,
    limits: AdapterLimits,
    initial: SystemdUnitInspectionV1,
    process_identity: OwnedProcessIdentityV1 | None,
) -> None:
    """Never abandon a crossed launch boundary without exact-unit reconciliation."""
    evidence = observation.termination_evidence
    if evidence is None:
        raise RuntimeError("process termination evidence was not initialized")
    try:
        reconciled = backend.inspect(evidence.unit_name, None, None)
    except (ContainmentControlError, OSError) as exc:
        _persist_containment_failure(
            observation,
            evidence_path,
            str(exc),
            initial,
            process_identity=process_identity,
        )
        return
    if (
        reconciled.state == "proven_live"
        and reconciled.invocation_id is not None
        and reconciled.control_group is not None
    ):
        _update_process_termination_evidence(
            observation,
            evidence_path,
            phase="termination_intent_persisted",
            process_identity=process_identity,
            invocation_id=reconciled.invocation_id,
            control_group=reconciled.control_group,
            containment_stop_reason="post_launch_identity_uncertainty",
            unit_active_state=reconciled.active_state,
            unit_sub_state=reconciled.sub_state,
            unit_result=reconciled.unit_result,
            containment_closed=False,
            cgroup_empty=False,
        )
        stopped = _request_systemd_containment_stop(
            observation,
            evidence_path,
            backend,
            limits.termination_grace_seconds,
        )
        if stopped.status != "closed":
            return
        if _bounded_reap_wrapper(
            process,
            backend.control_plane_timeout_seconds,
            limits,
        ):
            observation.exit_code = process.returncode
        _persist_containment_failure(
            observation,
            evidence_path,
            "post-launch identity binding failed after exact-unit closure",
            stopped.inspection,
            process_identity=process_identity,
        )
        return
    if (
        reconciled.state == "proven_closed"
        and reconciled.invocation_id is not None
        and reconciled.control_group is not None
        and reconciled.cgroup_empty is True
    ):
        if _bounded_reap_wrapper(
            process,
            backend.control_plane_timeout_seconds,
            limits,
        ):
            observation.exit_code = process.returncode
        _persist_containment_failure(
            observation,
            evidence_path,
            "post-launch identity binding failed after containment closed",
            reconciled,
            process_identity=process_identity,
        )
        return
    _persist_containment_failure(
        observation,
        evidence_path,
        reconciled.error
        or initial.error
        or "post-launch containment identity remains unresolved",
        reconciled,
        process_identity=process_identity,
    )


def _normalize_closed_inspection(
    inspected: SystemdUnitInspectionV1,
    bound: SystemdUnitInspectionV1,
) -> SystemdUnitInspectionV1 | None:
    if inspected.state == "proven_closed" and inspected.cgroup_empty is True:
        return inspected
    if inspected.state == "absent" and inspected.cgroup_empty is True:
        return bound.model_copy(
            update={
                "state": "proven_closed",
                "active_state": "inactive",
                "sub_state": "dead",
                "cgroup_empty": True,
            }
        )
    return None


def _persist_generic_containment_stop_intent(
    observation: _ProcessObservation,
    destination: Path,
    reason: str,
) -> None:
    current = observation.termination_evidence
    if current is None or current.phase not in {
        "termination_intent_persisted",
        "graceful_termination_sent",
        "hard_kill_sent",
    }:
        _update_process_termination_evidence(
            observation,
            destination,
            phase="termination_intent_persisted",
            containment_stop_reason=reason,
        )


def _request_systemd_containment_stop(
    observation: _ProcessObservation,
    destination: Path,
    backend: ContainmentBackend,
    stop_grace_seconds: float,
) -> SystemdStopResultV1:
    evidence = observation.termination_evidence
    if evidence is None:
        raise RuntimeError("process termination evidence was not initialized")
    _update_process_termination_evidence(
        observation,
        destination,
        systemd_stop_requested=True,
    )
    stopped = backend.stop(
        evidence.unit_name,
        evidence.invocation_id,
        evidence.control_group,
        stop_grace_seconds,
    )
    if stopped.status == "failed":
        _persist_containment_failure(
            observation,
            destination,
            stopped.error or "bounded systemd containment stop failed",
            stopped.inspection,
        )
        return stopped
    inspection = stopped.inspection
    _update_process_termination_evidence(
        observation,
        destination,
        phase="hard_kill_sent" if stopped.final_kill_observed else "graceful_termination_sent",
        graceful_termination_sent=stopped.stop_requested,
        hard_kill_sent=stopped.final_kill_observed,
        unit_active_state=inspection.active_state,
        unit_sub_state=inspection.sub_state,
        unit_result=inspection.unit_result,
        cgroup_empty=inspection.cgroup_empty,
    )
    return stopped


def _persist_containment_failure(
    observation: _ProcessObservation,
    destination: Path,
    error: str,
    inspection: SystemdUnitInspectionV1 | None,
    *,
    process_identity: OwnedProcessIdentityV1 | None = None,
) -> None:
    updates: dict[str, Any] = {
        "phase": "termination_failed",
        "signal_error": error,
        "final_return_code": observation.exit_code,
        "process_reaped": observation.exit_code is not None,
    }
    if process_identity is not None:
        updates["process_identity"] = process_identity
    current = observation.termination_evidence
    if inspection is not None:
        if (
            current is not None
            and current.invocation_id is None
            and inspection.state in {"proven_live", "proven_closed"}
            and inspection.invocation_id is not None
            and inspection.control_group is not None
        ):
            updates["invocation_id"] = inspection.invocation_id
            updates["control_group"] = inspection.control_group
        updates.update(
            unit_active_state=inspection.active_state,
            unit_sub_state=inspection.sub_state,
            unit_result=inspection.unit_result,
            cgroup_empty=inspection.cgroup_empty,
            containment_closed=(
                True
                if inspection.state == "proven_closed"
                and inspection.cgroup_empty is True
                else False
                if inspection.state == "proven_live"
                else None
            ),
        )
    _update_process_termination_evidence(observation, destination, **updates)


def _close_systemd_after_adapter_failure(
    process: subprocess.Popen[bytes],
    observation: _ProcessObservation,
    limits: AdapterLimits,
    evidence_path: Path,
    observer: NativeRolloutBudgetObserverV1 | None,
    process_finished: ProcessFinished | None,
    backend: ContainmentBackend,
) -> None:
    evidence = observation.termination_evidence
    if evidence is None or evidence.invocation_id is None or evidence.control_group is None:
        _persist_containment_failure(
            observation,
            evidence_path,
            "containment identity unavailable during adapter cleanup",
            None,
        )
        return
    _persist_generic_containment_stop_intent(
        observation,
        evidence_path,
        "adapter_failure",
    )
    stopped = _request_systemd_containment_stop(
        observation,
        evidence_path,
        backend,
        limits.termination_grace_seconds,
    )
    if stopped.status != "closed":
        return
    if not _bounded_reap_wrapper(process, backend.control_plane_timeout_seconds, limits):
        _persist_containment_failure(
            observation,
            evidence_path,
            "local systemd-run wrapper could not be reaped boundedly",
            stopped.inspection,
        )
        return
    observation.exit_code = process.returncode
    _persist_reaped_process_evidence(
        observation,
        evidence_path,
        observer,
        stopped.inspection,
    )
    if process_finished is not None:
        process_finished(process.pid)


def _bounded_reap_wrapper(
    process: subprocess.Popen[bytes],
    timeout_seconds: float,
    limits: AdapterLimits,
) -> bool:
    try:
        process.wait(timeout=timeout_seconds)
        return True
    except subprocess.TimeoutExpired:
        _emergency_process_group_cleanup(process, limits)
        try:
            process.wait(timeout=limits.termination_grace_seconds)
        except subprocess.TimeoutExpired:
            return False
        return True


def _run_process(
    command: Sequence[str],
    prepared: PreparedCodexRequest,
    environment: Mapping[str, str],
    cwd: Path,
    events: _EventProcessor,
    observation: _ProcessObservation,
    limits: AdapterLimits,
    started_monotonic: float,
    monotonic: Monotonic,
    rejected_values: Sequence[bytes],
    prelaunch_verifier: Callable[[], None] | None,
    process_started: ProcessStarted | None,
    process_finished: ProcessFinished | None,
    native_rollout_budget_observer: NativeRolloutBudgetObserverV1 | None,
    process_enforcement_policy: ProcessEnforcementPolicyV1 | None,
    process_termination_evidence_path: Path | None,
    containment_backend: ContainmentBackend | None,
) -> None:
    if process_enforcement_policy is not None:
        assert process_termination_evidence_path is not None
        assert containment_backend is not None
        _run_systemd_contained_process(
            command,
            prepared,
            environment,
            cwd,
            events,
            observation,
            limits,
            started_monotonic,
            monotonic,
            rejected_values,
            prelaunch_verifier,
            process_started,
            process_finished,
            native_rollout_budget_observer,
            process_enforcement_policy,
            process_termination_evidence_path,
            containment_backend,
        )
        return
    if prelaunch_verifier is not None:
        prelaunch_verifier()
    if process_termination_evidence_path is not None:
        _update_process_termination_evidence(
            observation,
            process_termination_evidence_path,
            phase="launch_intent_persisted",
        )
    try:
        process = subprocess.Popen(
            list(command),
            cwd=cwd,
            env=dict(environment),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            close_fds=True,
        )
    except (OSError, subprocess.SubprocessError):
        observation.launch_error = "Codex process launch failed"
        return

    observation.launched = True
    process_identity: OwnedProcessIdentityV1 | None = None
    if process_termination_evidence_path is not None:
        start_ticks = process_start_ticks(process.pid)
        if start_ticks is None:
            observation.launch_error = "Codex process identity could not be proven"
            _emergency_process_group_cleanup(process, limits)
            observation.exit_code = process.wait()
            _update_process_termination_evidence(
                observation,
                process_termination_evidence_path,
                phase="termination_failed",
                signal_error=observation.launch_error,
                final_return_code=observation.exit_code,
                process_reaped=True,
                owned_process_group_empty=None,
            )
            return
        process_identity = OwnedProcessIdentityV1(
            pid=process.pid,
            process_group_id=process.pid,
            session_id=process.pid,
            start_ticks=start_ticks,
        )
        _update_process_termination_evidence(
            observation,
            process_termination_evidence_path,
            phase="running",
            process_identity=process_identity,
        )
    try:
        if process_started is not None:
            process_started(process.pid)
    except BaseException:
        _close_process_after_adapter_failure(
            process,
            observation,
            limits,
            process_identity,
            process_termination_evidence_path,
            native_rollout_budget_observer,
            process_finished,
        )
        raise
    if process.stdin is None or process.stdout is None or process.stderr is None:
        observation.launch_error = "Codex process pipes were unavailable"
        _close_process_after_adapter_failure(
            process,
            observation,
            limits,
            process_identity,
            process_termination_evidence_path,
            native_rollout_budget_observer,
            process_finished,
        )
        return

    selector = selectors.DefaultSelector()
    streams = {
        "stdin": process.stdin,
        "stdout": process.stdout,
        "stderr": process.stderr,
    }
    try:
        for name, stream in streams.items():
            os.set_blocking(stream.fileno(), False)
            selector.register(
                stream,
                selectors.EVENT_WRITE if name == "stdin" else selectors.EVENT_READ,
                name,
            )
    except (KeyError, OSError, ValueError):
        observation.launch_error = "Codex process pipes could not be configured"
        for stream in streams.values():
            if _is_registered(selector, stream):
                _unregister_and_close(selector, stream)
        selector.close()
        _close_process_after_adapter_failure(
            process,
            observation,
            limits,
            process_identity,
            process_termination_evidence_path,
            native_rollout_budget_observer,
            process_finished,
        )
        return

    prompt_offset = 0
    deadline = started_monotonic + prepared.request.timeout_seconds
    wall_clock_deadline = (
        started_monotonic + process_enforcement_policy.max_wall_clock_seconds
        if process_enforcement_policy is not None
        else None
    )
    termination_started: float | None = None
    killed = False
    hard_kill_started: float | None = None
    termination_failed = False
    normal_cleanup_started: float | None = None
    normal_cleanup_forced = False
    stdout_open = True
    stderr_open = True

    try:
        while True:
            if (
                native_rollout_budget_observer is not None
                and observation.termination_reason is None
            ):
                observation.execution_budget_decision = (
                    native_rollout_budget_observer.poll().decision
                )
            now = monotonic()
            polled = process.poll()
            if polled is not None:
                observation.exit_code = polled
                if _is_registered(selector, process.stdin):
                    observation.stdin_error = prompt_offset < len(prepared.prompt_bytes)
                    _unregister_and_close(selector, process.stdin)

                if (
                    observation.termination_reason is None
                    and normal_cleanup_started is None
                    and (
                        inspect_owned_process_group(process_identity).state
                        == "verified_owned_group_present"
                        if process_identity is not None
                        else _process_group_exists(process.pid)
                    )
                ):
                    normal_cleanup_started = now
                    if process_identity is not None:
                        _verified_process_group_signal(process_identity, signal.SIGTERM)
                    else:
                        _signal_process_group(process, signal.SIGTERM)

            budget_termination_reason = _budget_termination_reason(
                observation.execution_budget_decision
            )
            if (
                budget_termination_reason is not None
                and observation.termination_reason is None
            ):
                observation.termination_reason = budget_termination_reason
                termination_started = now
                if process_termination_evidence_path is not None:
                    _persist_termination_intent(
                        observation,
                        process_termination_evidence_path,
                        budget_termination_reason,
                        now - started_monotonic,
                        native_rollout_budget_observer,
                    )
                _send_graceful_enforcement_signal(
                    observation,
                    process_termination_evidence_path,
                    process_identity,
                )

            if (
                wall_clock_deadline is not None
                and observation.termination_reason is None
                and observation.exit_code is None
                and now >= wall_clock_deadline
            ):
                observation.termination_reason = "wall_clock_limit_exceeded"
                termination_started = now
                assert process_termination_evidence_path is not None
                _persist_termination_intent(
                    observation,
                    process_termination_evidence_path,
                    "wall_clock_limit_exceeded",
                    now - started_monotonic,
                    native_rollout_budget_observer,
                )
                _send_graceful_enforcement_signal(
                    observation,
                    process_termination_evidence_path,
                    process_identity,
                )

            if (
                observation.termination_reason is None
                and observation.exit_code is None
                and now >= deadline
            ):
                observation.termination_reason = "timeout"
                termination_started = now
                _signal_process_group(process, signal.SIGTERM)

            if (
                termination_started is not None
                and not killed
                and now - termination_started >= limits.termination_grace_seconds
            ):
                if (
                    process_termination_evidence_path is not None
                    and _is_process_enforcement_reason(
                        observation.termination_reason
                    )
                ):
                    hard_result = _send_hard_enforcement_signal(
                        observation,
                        process_termination_evidence_path,
                        process_identity,
                    )
                    if hard_result.status in {"sent", "group_already_empty"}:
                        killed = True
                        hard_kill_started = now
                    else:
                        termination_failed = True
                        _persist_termination_failure(
                            observation,
                            process_termination_evidence_path,
                            hard_result.error or "hard process-group signal failed",
                            process_identity,
                        )
                else:
                    _signal_process_group(process, signal.SIGKILL)
                    killed = True
                    hard_kill_started = now

            if (
                normal_cleanup_started is not None
                and not normal_cleanup_forced
                and (
                    inspect_owned_process_group(process_identity).state
                    == "verified_owned_group_present"
                    if process_identity is not None
                    else _process_group_exists(process.pid)
                )
                and now - normal_cleanup_started >= limits.termination_grace_seconds
            ):
                if process_identity is not None:
                    hard_result = _verified_process_group_signal(
                        process_identity, signal.SIGKILL
                    )
                    if hard_result.status in {"sent", "group_already_empty"}:
                        normal_cleanup_forced = True
                        hard_kill_started = now
                    else:
                        termination_failed = True
                        assert process_termination_evidence_path is not None
                        _persist_termination_failure(
                            observation,
                            process_termination_evidence_path,
                            hard_result.error or "hard process-group signal failed",
                            process_identity,
                        )
                else:
                    _signal_process_group(process, signal.SIGKILL)
                    normal_cleanup_forced = True
                    hard_kill_started = now

            group_empty = (
                inspect_owned_process_group(process_identity).state
                == "owned_group_empty"
                if process_identity is not None
                else not _process_group_exists(process.pid)
            )
            if (
                process_identity is not None
                and hard_kill_started is not None
                and not group_empty
                and now - hard_kill_started >= limits.termination_grace_seconds
            ):
                termination_failed = True
                assert process_termination_evidence_path is not None
                _persist_termination_failure(
                    observation,
                    process_termination_evidence_path,
                    "owned process group remained live after hard-kill grace",
                    process_identity,
                )

            if termination_failed:
                break

            if observation.exit_code is not None and not stdout_open and not stderr_open:
                if process_identity is not None:
                    if group_empty:
                        break
                elif (
                    termination_started is None and normal_cleanup_started is None
                ) or killed or normal_cleanup_forced or not _process_group_exists(
                    process.pid
                ):
                    break

            timeout = limits.io_poll_seconds
            if observation.termination_reason is None and observation.exit_code is None:
                timeout = min(timeout, max(0.0, deadline - now))
                if wall_clock_deadline is not None:
                    timeout = min(timeout, max(0.0, wall_clock_deadline - now))
            elif termination_started is not None and not killed:
                timeout = min(
                    timeout,
                    max(0.0, termination_started + limits.termination_grace_seconds - now),
                )
            if normal_cleanup_started is not None and not normal_cleanup_forced:
                timeout = min(
                    timeout,
                    max(
                        0.0,
                        normal_cleanup_started + limits.termination_grace_seconds - now,
                    ),
                )
            if hard_kill_started is not None and not group_empty:
                timeout = min(
                    timeout,
                    max(
                        0.0,
                        hard_kill_started + limits.termination_grace_seconds - now,
                    ),
                )
            try:
                ready = selector.select(timeout)
            except InterruptedError:
                continue

            for key, _ in ready:
                stream_name = str(key.data)
                ready_stream: Any = key.fileobj
                if stream_name == "stdin":
                    prompt_offset = _write_prompt_chunk(
                        selector,
                        ready_stream,
                        prepared.prompt_bytes,
                        prompt_offset,
                        observation,
                    )
                    continue
                try:
                    chunk = os.read(ready_stream.fileno(), 64 * 1024)
                except (BlockingIOError, InterruptedError):
                    continue
                except OSError:
                    chunk = b""
                if not chunk:
                    _unregister_and_close(selector, ready_stream)
                    if stream_name == "stdout":
                        stdout_open = False
                    else:
                        stderr_open = False
                    continue
                if _contains_confidential_fragment(chunk, rejected_values):
                    observation.confidentiality_violation_detected = True
                if stream_name == "stdout":
                    accepted = _accepted_prefix(
                        chunk,
                        observation.stdout_bytes,
                        limits.stdout_bytes,
                    )
                    observation.stdout_bytes += len(chunk)
                    if accepted:
                        events.feed(accepted)
                    if observation.stdout_bytes > limits.stdout_bytes:
                        _start_output_limit_termination(
                            process,
                            observation,
                            "stdout",
                            now,
                        )
                        if termination_started is None:
                            termination_started = now
                else:
                    accepted = _accepted_prefix(
                        chunk,
                        observation.stderr_bytes,
                        limits.stderr_bytes,
                    )
                    observation.stderr_bytes += len(chunk)
                    observation.stderr.extend(accepted)
                    if observation.stderr_bytes > limits.stderr_bytes:
                        _start_output_limit_termination(
                            process,
                            observation,
                            "stderr",
                            now,
                        )
                        if termination_started is None:
                            termination_started = now
    except BaseException:
        _close_process_after_adapter_failure(
            process,
            observation,
            limits,
            process_identity,
            process_termination_evidence_path,
            native_rollout_budget_observer,
            process_finished,
        )
        raise
    finally:
        for stream in streams.values():
            if _is_registered(selector, stream):
                _unregister_and_close(selector, stream)
        selector.close()

    if termination_failed:
        return
    if observation.exit_code is None:
        observation.exit_code = process.wait()
    if process_termination_evidence_path is not None:
        assert process_identity is not None
        if inspect_owned_process_group(process_identity).state != "owned_group_empty":
            _persist_termination_failure(
                observation,
                process_termination_evidence_path,
                "owned process-group closure was not proven",
                process_identity,
            )
            return
        _persist_reaped_process_evidence(
            observation,
            process_termination_evidence_path,
            native_rollout_budget_observer,
        )
    if process_finished is not None:
        process_finished(process.pid)


def _update_process_termination_evidence(
    observation: _ProcessObservation,
    destination: Path,
    **updates: Any,
) -> ProcessTerminationEvidenceV1:
    current = observation.termination_evidence
    if current is None:
        raise RuntimeError("process termination evidence was not initialized")
    payload = current.model_dump(mode="python")
    payload.update(updates)
    candidate = ProcessTerminationEvidenceV1.model_validate(payload)
    write_process_termination_evidence(destination, candidate)
    observation.termination_evidence = candidate
    return candidate


def _budget_termination_reason(
    decision: str | None,
) -> ProcessTerminationReasonV1 | None:
    if decision == "bounded_continuation_required":
        return "execution_budget_exhausted"
    if decision == "accounting_integrity_failure":
        return "execution_budget_accounting_integrity_failure"
    return None


def _is_process_enforcement_reason(reason: str | None) -> bool:
    return reason in {
        "execution_budget_exhausted",
        "execution_budget_accounting_integrity_failure",
        "wall_clock_limit_exceeded",
    }


def _persist_termination_intent(
    observation: _ProcessObservation,
    destination: Path,
    reason: ProcessTerminationReasonV1,
    elapsed_seconds: float,
    observer: NativeRolloutBudgetObserverV1 | None,
) -> None:
    updates: dict[str, Any] = {
        "phase": "termination_intent_persisted",
        "termination_reason": reason,
        "decision_elapsed_seconds": max(0.0, elapsed_seconds),
    }
    if observer is not None:
        checkpoint = observer.controller.checkpoint
        checkpoint_path = observer.controller.checkpoint_path
        cursor = observer.cursor
        updates.update(
            {
                "codex_thread_id": checkpoint.codex_thread_id,
                "reached_hard_limits": checkpoint.state.reached_hard_limits,
                "budget_checkpoint_path": str(checkpoint_path),
                "budget_checkpoint_sha256": file_sha256(checkpoint_path),
                "native_source_cursor_path": (
                    str(observer.source_cursor_path)
                    if observer.source_cursor_path is not None
                    else None
                ),
                "native_source_cursor_offset_at_stop": (
                    cursor.consumed_byte_offset if cursor is not None else None
                ),
                "rollout_relative_path": (
                    cursor.rollout_relative_path if cursor is not None else None
                ),
            }
        )
    _update_process_termination_evidence(observation, destination, **updates)


def _verified_process_group_signal(
    identity: OwnedProcessIdentityV1 | None,
    requested_signal: int,
) -> ProcessGroupSignalResultV1:
    if identity is None:
        return ProcessGroupSignalResultV1(
            status="failed", error="owned process identity is unavailable"
        )
    inspection = inspect_owned_process_group(identity)
    if inspection.state == "owned_group_empty":
        return ProcessGroupSignalResultV1(status="group_already_empty")
    if inspection.state == "ambiguous":
        return ProcessGroupSignalResultV1(
            status="failed", error="owned process-group identity is ambiguous"
        )
    try:
        os.killpg(identity.process_group_id, requested_signal)
    except ProcessLookupError:
        if inspect_owned_process_group(identity).state == "owned_group_empty":
            return ProcessGroupSignalResultV1(status="group_already_empty")
        return ProcessGroupSignalResultV1(
            status="failed", error="verified owned process group disappeared ambiguously"
        )
    except (OSError, PermissionError) as exc:
        return ProcessGroupSignalResultV1(
            status="failed",
            error=f"{type(exc).__name__}: process-group signal failed",
        )
    return ProcessGroupSignalResultV1(status="sent")


def _send_graceful_enforcement_signal(
    observation: _ProcessObservation,
    destination: Path | None,
    identity: OwnedProcessIdentityV1 | None,
) -> ProcessGroupSignalResultV1:
    if destination is None:
        return ProcessGroupSignalResultV1(
            status="failed", error="termination evidence destination is unavailable"
        )
    result = _verified_process_group_signal(identity, signal.SIGTERM)
    updates: dict[str, Any] = {}
    if result.status == "sent":
        updates.update(
            phase="graceful_termination_sent",
            graceful_termination_sent=True,
        )
    if result.error is not None:
        updates["signal_error"] = result.error
    if updates:
        _update_process_termination_evidence(observation, destination, **updates)
    return result


def _send_hard_enforcement_signal(
    observation: _ProcessObservation,
    destination: Path,
    identity: OwnedProcessIdentityV1 | None,
) -> ProcessGroupSignalResultV1:
    result = _verified_process_group_signal(identity, signal.SIGKILL)
    updates: dict[str, Any] = {}
    if result.status == "sent":
        updates.update(phase="hard_kill_sent", hard_kill_sent=True)
    if result.error is not None:
        updates["signal_error"] = result.error
    if updates:
        _update_process_termination_evidence(observation, destination, **updates)
    return result


def _persist_reaped_process_evidence(
    observation: _ProcessObservation,
    destination: Path,
    observer: NativeRolloutBudgetObserverV1 | None,
    containment: SystemdUnitInspectionV1 | None = None,
) -> None:
    current = observation.termination_evidence
    if current is None:
        raise RuntimeError("process termination evidence was not initialized")
    updates: dict[str, Any] = {
        "phase": "reaped",
        "final_return_code": observation.exit_code,
        "process_reaped": True,
    }
    if containment is not None:
        if (
            containment.state != "proven_closed"
            or containment.cgroup_empty is not True
            or containment.unit_name != current.unit_name
            or containment.invocation_id != current.invocation_id
            or containment.control_group != current.control_group
        ):
            raise RuntimeError("exact systemd containment closure was not proven")
        updates.update(
            containment_closed=True,
            cgroup_empty=True,
            unit_active_state=containment.active_state,
            unit_sub_state=containment.sub_state,
            unit_result=containment.unit_result,
        )
        if current.process_identity is not None:
            updates["owned_process_group_empty"] = (
                inspect_owned_process_group(current.process_identity).state
                == "owned_group_empty"
            )
    cursor = observer.cursor if observer is not None else None
    if observer is not None:
        checkpoint_path = observer.controller.checkpoint_path
        updates.update(
            codex_thread_id=observer.controller.checkpoint.codex_thread_id,
            reached_hard_limits=(
                observer.controller.checkpoint.state.reached_hard_limits
            ),
            budget_checkpoint_path=str(checkpoint_path),
            budget_checkpoint_sha256=file_sha256(checkpoint_path),
            native_source_cursor_path=(
                str(observer.source_cursor_path)
                if observer.source_cursor_path is not None
                else None
            ),
        )
        if current.native_source_cursor_offset_at_stop is None and cursor is not None:
            updates["native_source_cursor_offset_at_stop"] = (
                cursor.consumed_byte_offset
            )
            updates["rollout_relative_path"] = cursor.rollout_relative_path
    rollout_path = observer.rollout_path if observer is not None else None
    if rollout_path is not None:
        try:
            final_size = rollout_path.stat().st_size
        except OSError:
            final_size = None
        stop_offset = updates.get(
            "native_source_cursor_offset_at_stop",
            current.native_source_cursor_offset_at_stop,
        )
        if final_size is not None and stop_offset is not None and final_size >= stop_offset:
            tail_bytes = final_size - stop_offset
            updates.update(
                final_rollout_size_bytes=final_size,
                unconsumed_tail_bytes=tail_bytes,
                unconsumed_tail_present=tail_bytes > 0,
            )
    _update_process_termination_evidence(observation, destination, **updates)


def _persist_termination_failure(
    observation: _ProcessObservation,
    destination: Path,
    error: str,
    identity: OwnedProcessIdentityV1 | None,
) -> None:
    """Persist bounded fail-closed evidence without claiming safe closure."""
    group_empty: bool | None = None
    if identity is not None:
        inspection = inspect_owned_process_group(identity)
        if inspection.state == "verified_owned_group_present":
            group_empty = False
    _update_process_termination_evidence(
        observation,
        destination,
        phase="termination_failed",
        signal_error=error,
        final_return_code=observation.exit_code,
        process_reaped=observation.exit_code is not None,
        owned_process_group_empty=group_empty,
    )


def _close_process_after_adapter_failure(
    process: subprocess.Popen[bytes],
    observation: _ProcessObservation,
    limits: AdapterLimits,
    identity: OwnedProcessIdentityV1 | None,
    evidence_path: Path | None,
    observer: NativeRolloutBudgetObserverV1 | None,
    process_finished: ProcessFinished | None,
) -> None:
    """Boundedly contain an adapter failure before reporting safe closure."""
    if identity is None or evidence_path is None:
        _emergency_process_group_cleanup(process, limits)
        observation.exit_code = process.wait()
        if evidence_path is not None:
            _persist_termination_failure(
                observation,
                evidence_path,
                "owned process identity was unavailable during adapter cleanup",
                identity,
            )
        elif process_finished is not None:
            process_finished(process.pid)
        return

    graceful = _verified_process_group_signal(identity, signal.SIGTERM)
    deadline = time.monotonic() + limits.termination_grace_seconds
    inspection = inspect_owned_process_group(identity)
    while (
        inspection.state == "verified_owned_group_present"
        and time.monotonic() < deadline
    ):
        time.sleep(min(limits.io_poll_seconds, max(0.0, deadline - time.monotonic())))
        inspection = inspect_owned_process_group(identity)

    hard: ProcessGroupSignalResultV1 | None = None
    if inspection.state == "verified_owned_group_present":
        hard = _verified_process_group_signal(identity, signal.SIGKILL)
        if hard.status in {"sent", "group_already_empty"}:
            deadline = time.monotonic() + limits.termination_grace_seconds
            inspection = inspect_owned_process_group(identity)
            while (
                inspection.state == "verified_owned_group_present"
                and time.monotonic() < deadline
            ):
                time.sleep(
                    min(
                        limits.io_poll_seconds,
                        max(0.0, deadline - time.monotonic()),
                    )
                )
                inspection = inspect_owned_process_group(identity)

    if inspection.state != "owned_group_empty":
        observation.exit_code = process.poll()
        error = (
            hard.error
            if hard is not None and hard.error is not None
            else graceful.error
            if graceful.error is not None
            else "owned process-group closure was not proven after adapter failure"
        )
        _persist_termination_failure(observation, evidence_path, error, identity)
        return

    observation.exit_code = process.wait()
    _persist_reaped_process_evidence(observation, evidence_path, observer)
    if process_finished is not None:
        process_finished(process.pid)


def _write_prompt_chunk(
    selector: selectors.BaseSelector,
    stream: Any,
    prompt: bytes,
    offset: int,
    observation: _ProcessObservation,
) -> int:
    try:
        written = os.write(stream.fileno(), prompt[offset : offset + 64 * 1024])
    except (BlockingIOError, InterruptedError):
        return offset
    except (BrokenPipeError, OSError):
        observation.stdin_error = offset < len(prompt)
        _unregister_and_close(selector, stream)
        return offset
    offset += written
    if offset >= len(prompt):
        _unregister_and_close(selector, stream)
    return offset


def _accepted_prefix(chunk: bytes, count_before: int, limit: int) -> bytes:
    remaining = max(0, limit - count_before)
    return chunk[:remaining]


def _contains_confidential_fragment(
    value: bytes,
    fragments: Sequence[bytes],
) -> bool:
    return any(fragment in value for fragment in fragments)


def _scan_temporary_action_directory(
    directory: Path,
    rejected_values: Sequence[bytes],
) -> bool:
    """Fail closed on protected bytes or an unscannable writable action entry."""
    if not rejected_values:
        return False
    file_count = 0
    total_bytes = 0
    try:
        for current, directories, files in os.walk(
            directory,
            topdown=True,
            followlinks=False,
        ):
            current_path = Path(current)
            depth = len(current_path.relative_to(directory).parts)
            if depth > 8:
                return True
            for name in (*directories, *files):
                path = current_path / name
                status = path.lstat()
                file_count += 1
                if file_count > 256 or path.is_symlink():
                    return True
                if stat.S_ISDIR(status.st_mode):
                    continue
                if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
                    return True
                if status.st_size > 4 * 1024 * 1024:
                    return True
                total_bytes += status.st_size
                if total_bytes > 8 * 1024 * 1024:
                    return True
                flags = os.O_RDONLY
                if hasattr(os, "O_CLOEXEC"):
                    flags |= os.O_CLOEXEC
                if hasattr(os, "O_NOFOLLOW"):
                    flags |= os.O_NOFOLLOW
                descriptor = os.open(path, flags)
                try:
                    before = os.fstat(descriptor)
                    content = bytearray()
                    while len(content) <= 4 * 1024 * 1024:
                        chunk = os.read(descriptor, 64 * 1024)
                        if not chunk:
                            break
                        content.extend(chunk)
                    after = os.fstat(descriptor)
                finally:
                    os.close(descriptor)
                if len(content) > 4 * 1024 * 1024 or (
                    before.st_dev,
                    before.st_ino,
                    before.st_size,
                    before.st_mtime_ns,
                ) != (
                    after.st_dev,
                    after.st_ino,
                    after.st_size,
                    after.st_mtime_ns,
                ):
                    return True
                if _contains_confidential_fragment(
                    bytes(content),
                    rejected_values,
                ):
                    return True
    except (OSError, RuntimeError, ValueError):
        return True
    return False


def _start_output_limit_termination(
    process: subprocess.Popen[bytes],
    observation: _ProcessObservation,
    stream_name: str,
    now: float,
) -> None:
    del now
    if observation.termination_reason is None:
        observation.termination_reason = "output_limit"
        observation.output_limit_stream = stream_name
        _signal_process_group(process, signal.SIGTERM)


def _signal_process_group(process: subprocess.Popen[bytes], requested_signal: int) -> None:
    try:
        os.killpg(process.pid, requested_signal)
    except (ProcessLookupError, PermissionError):
        return


def _process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _emergency_process_group_cleanup(
    process: subprocess.Popen[bytes], limits: AdapterLimits
) -> None:
    """Contain descendants when an unexpected adapter-side failure interrupts I/O."""
    if not _process_group_exists(process.pid):
        return
    _signal_process_group(process, signal.SIGTERM)
    deadline = time.monotonic() + limits.termination_grace_seconds
    while _process_group_exists(process.pid) and time.monotonic() < deadline:
        time.sleep(min(limits.io_poll_seconds, max(0.0, deadline - time.monotonic())))
    if _process_group_exists(process.pid):
        _signal_process_group(process, signal.SIGKILL)


def _is_registered(selector: selectors.BaseSelector, stream: Any) -> bool:
    try:
        selector.get_key(stream)
    except (KeyError, ValueError):
        return False
    return True


def _unregister_and_close(selector: selectors.BaseSelector, stream: Any) -> None:
    with suppress(KeyError):
        selector.unregister(stream)
    with suppress(OSError):
        stream.close()


def _resolve_runs_directory(runs_dir: Path) -> Path:
    try:
        return runs_dir.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise CodexRequestError("runs directory could not be resolved") from exc


def _create_artifact_directory(runs_dir: Path, run_id: str) -> Path:
    try:
        runs_dir.mkdir(parents=True, exist_ok=True)
        if not runs_dir.is_dir():
            raise CodexRequestError("runs directory is not a directory")
        artifact_directory = runs_dir / run_id
        artifact_directory.mkdir(exist_ok=False)
        return artifact_directory
    except FileExistsError as exc:
        raise CodexRequestError(f"run directory already exists for run_id '{run_id}'") from exc
    except CodexRequestError:
        raise
    except OSError as exc:
        raise CodexRequestError("run directory could not be created") from exc


def prepare_auditor_scratch_directory(artifact_directory: Path) -> Path:
    """Create or verify one private scratch directory below an exact action directory."""
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        inspected = artifact_directory.lstat()
        parent_descriptor = os.open(artifact_directory, flags)
        try:
            opened = os.fstat(parent_descriptor)
            if (
                not stat.S_ISDIR(inspected.st_mode)
                or stat.S_ISLNK(inspected.st_mode)
                or (inspected.st_dev, inspected.st_ino) != (opened.st_dev, opened.st_ino)
            ):
                raise OSError("auditor action directory is not exact")
            with suppress(FileExistsError):
                os.mkdir(
                    AUDITOR_SCRATCH_DIRECTORY_NAME,
                    mode=0o700,
                    dir_fd=parent_descriptor,
                )
            scratch_status = os.stat(
                AUDITOR_SCRATCH_DIRECTORY_NAME,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISDIR(scratch_status.st_mode)
                or stat.S_ISLNK(scratch_status.st_mode)
                or scratch_status.st_uid != os.getuid()
                or scratch_status.st_nlink < 2
            ):
                raise OSError("auditor scratch directory is not exact")
            scratch_descriptor = os.open(
                AUDITOR_SCRATCH_DIRECTORY_NAME,
                flags,
                dir_fd=parent_descriptor,
            )
            try:
                opened_scratch = os.fstat(scratch_descriptor)
                if (
                    opened_scratch.st_dev,
                    opened_scratch.st_ino,
                ) != (
                    scratch_status.st_dev,
                    scratch_status.st_ino,
                ):
                    raise OSError("auditor scratch directory changed during open")
                os.fchmod(scratch_descriptor, 0o700)
                os.fsync(scratch_descriptor)
            finally:
                os.close(scratch_descriptor)
            os.fsync(parent_descriptor)
        finally:
            os.close(parent_descriptor)
    except OSError as exc:
        raise CodexRequestError(
            "auditor action-owned scratch could not be prepared safely"
        ) from exc
    return artifact_directory / AUDITOR_SCRATCH_DIRECTORY_NAME


def _initialize_artifacts(
    artifact_directory: Path,
    prepared: PreparedCodexRequest,
    sensitive_values: Sequence[str],
    *,
    skip_git_repo_check: bool,
) -> None:
    normalized_request = prepared.normalized_dict()
    if skip_git_repo_check:
        normalized_request["skip_git_repo_check"] = True
    _atomic_write_json(
        artifact_directory / "request.normalized.json",
        redact_json(normalized_request, sensitive_values),
    )
    _write_text(artifact_directory / "prompt.sha256", f"{prepared.prompt_sha256}\n")
    _write_text(artifact_directory / "events.jsonl", "")
    _write_text(artifact_directory / "stderr.log", "")
    _write_text(artifact_directory / "final-message.md", "")


def _build_metadata(
    *,
    prepared: PreparedCodexRequest,
    artifact_directory: Path,
    command: Sequence[str],
    executable_path: str,
    codex_version: str | None,
    removed_names: Sequence[str],
    started_at: datetime,
    ended_at: datetime,
    duration: float,
    observation: _ProcessObservation,
    events: _EventProcessor,
    final_message_present: bool,
    permission_evidence: bool,
    output_schema: Path | None,
    resume_thread_id: str | None,
    limits: AdapterLimits,
    confidentiality_violation_detected: bool,
    durable_command_replacements: Mapping[str, str],
    auditor_scratch: Path | None,
    usage_receipt_path: Path,
    usage_receipt_id: str,
    usage_complete: bool,
    context_receipt_path: Path,
) -> dict[str, object]:
    terminating_signal = (
        -observation.exit_code
        if observation.exit_code is not None and observation.exit_code < 0
        else None
    )
    recorded_command = list(command)
    recorded_command[recorded_command.index("--output-last-message") + 1] = "<FINAL_MESSAGE_TEMP>"
    recorded_command[-1] = "<PROMPT_FROM_STDIN>"
    recorded_command = [durable_command_replacements.get(item, item) for item in recorded_command]
    recorded_command = [durable_context_config_item(item) for item in recorded_command]
    metadata: dict[str, object] = {
        "schema_version": 1,
        "package_version": __version__,
        "run_id": prepared.request.run_id,
        "role": prepared.request.role,
        "workspace": str(prepared.workspace),
        "prompt_path": str(prepared.prompt_path),
        "prompt_sha256": prepared.prompt_sha256,
        "prompt_byte_count": len(prepared.prompt_bytes),
        "model": prepared.request.model,
        "reasoning_effort": prepared.request.reasoning_effort,
        "timeout_seconds": prepared.request.timeout_seconds,
        "sandbox": prepared.policy.sandbox,
        "approval_policy": prepared.policy.approval,
        "ephemeral": prepared.policy.ephemeral,
        "command": recorded_command,
        "removed_environment_variable_names": list(removed_names),
        "started_at": _utc_string(started_at),
        "ended_at": _utc_string(ended_at),
        "duration_seconds": round(max(0.0, duration), 6),
        "artifact_directory": str(artifact_directory),
        "codex_executable": executable_path,
        "codex_version": codex_version,
        "process_launched": observation.launched,
        "process_enforcement_enabled": observation.termination_evidence is not None,
        "containment_termination_failed": (
            observation.termination_evidence is not None
            and observation.termination_evidence.phase == "termination_failed"
        ),
        "launch_error_present": observation.launch_error is not None,
        "stdin_error": observation.stdin_error,
        "process_exit_code": (
            observation.exit_code
            if observation.exit_code is not None and observation.exit_code >= 0
            else None
        ),
        "terminating_signal": terminating_signal,
        "termination_reason": observation.termination_reason,
        "valid_event_count": events.event_count,
        "malformed_event_count": len(events.malformed_hashes),
        "malformed_event_sha256": events.malformed_hashes,
        "stdout_byte_count": observation.stdout_bytes,
        "stderr_byte_count": observation.stderr_bytes,
        "stdout_limit_bytes": limits.stdout_bytes,
        "stderr_limit_bytes": limits.stderr_bytes,
        "final_message_present": final_message_present,
        "permission_evidence": permission_evidence,
        "output_limit_stream": observation.output_limit_stream,
        "thread_id": events.identifier_value if events.identifier_kind == "thread_id" else None,
        "session_id": events.identifier_value if events.identifier_kind == "session_id" else None,
        "thread_started_ids": list(events.started_thread_ids),
        "resume_thread_id": resume_thread_id,
        "output_schema_path": str(output_schema) if output_schema is not None else None,
        "output_schema_sha256": (
            hashlib.sha256(output_schema.read_bytes()).hexdigest()
            if output_schema is not None
            else None
        ),
        "events_sha256": hashlib.sha256(
            (artifact_directory / "events.jsonl").read_bytes()
        ).hexdigest(),
        "stderr_sha256": hashlib.sha256(
            (artifact_directory / "stderr.log").read_bytes()
        ).hexdigest(),
        "final_message_sha256": hashlib.sha256(
            (artifact_directory / "final-message.md").read_bytes()
        ).hexdigest(),
        "usage_receipt_path": str(usage_receipt_path),
        "usage_receipt_sha256": hashlib.sha256(usage_receipt_path.read_bytes()).hexdigest(),
        "usage_receipt_id": usage_receipt_id,
        "usage_complete": usage_complete,
        "context_economy_receipt_path": str(context_receipt_path),
        "context_economy_receipt_sha256": hashlib.sha256(
            context_receipt_path.read_bytes()
        ).hexdigest(),
    }
    if confidentiality_violation_detected:
        metadata["confidentiality_violation_detected"] = True
    if auditor_scratch is not None:
        metadata["auditor_scratch_path"] = str(auditor_scratch)
        metadata["sandbox_disposition"] = AUDITOR_SANDBOX_DISPOSITION
    return metadata


def _write_stage2_completion_manifest(
    artifact_directory: Path,
    prepared: PreparedCodexRequest,
    result: CodexRunResult,
    output_schema: Path,
) -> None:
    """Seal the complete Stage 2 Stage 1 artifact set after all final writes."""
    artifact_names = [
        "request.normalized.json",
        "prompt.sha256",
        "events.jsonl",
        "stderr.log",
        "final-message.md",
        "metadata.json",
        "result.json",
        "usage-receipt.json",
        "context-economy-receipt.json",
    ]
    if (artifact_directory / PROCESS_TERMINATION_EVIDENCE_FILENAME).is_file():
        artifact_names.append(PROCESS_TERMINATION_EVIDENCE_FILENAME)
    artifact_hashes = {
        str(artifact_directory / name): hashlib.sha256(
            (artifact_directory / name).read_bytes()
        ).hexdigest()
        for name in artifact_names
    }
    _atomic_write_json(
        artifact_directory / "stage2-completion.json",
        {
            "schema_version": 1,
            "run_id": prepared.request.run_id,
            "role": prepared.request.role,
            "artifact_directory": str(artifact_directory),
            "prompt_sha256": prepared.prompt_sha256,
            "output_schema_path": str(output_schema),
            "output_schema_sha256": hashlib.sha256(output_schema.read_bytes()).hexdigest(),
            "result_status": result.status,
            "completed_at": result.ended_at,
            "artifact_hashes": artifact_hashes,
        },
    )


def _classify_status(
    observation: _ProcessObservation,
    events: _EventProcessor,
    final_message_present: bool,
    permission_evidence: bool,
    confidentiality_violation_detected: bool,
) -> RunStatus:
    if not observation.launched or observation.launch_error is not None:
        return "launch_failed"
    if observation.termination_reason == "timeout":
        return "timed_out"
    if observation.termination_reason == "execution_budget_exhausted":
        return "bounded_continuation_required"
    if (
        observation.termination_reason
        == "execution_budget_accounting_integrity_failure"
    ):
        return "accounting_integrity_failure"
    if observation.termination_reason == "wall_clock_limit_exceeded":
        return "wall_clock_limit_exceeded"
    if observation.termination_reason == "output_limit":
        return "output_limit_exceeded"
    if (
        observation.termination_evidence is not None
        and observation.termination_evidence.phase == "termination_failed"
    ):
        return "process_failed"
    if confidentiality_violation_detected:
        return "process_failed"
    if permission_evidence:
        return "permission_blocked"
    if events.malformed_hashes:
        return "malformed_event_stream"
    if observation.exit_code != 0 or observation.stdin_error:
        return "process_failed"
    if not final_message_present:
        return "missing_final_message"
    return "succeeded"


def _status_messages(
    status: RunStatus,
    observation: _ProcessObservation,
    confidentiality_violation_detected: bool = False,
) -> tuple[str, str | None]:
    if confidentiality_violation_detected:
        message = "Codex emitted protected confidential content."
        return message, message
    messages: Mapping[RunStatus, str] = {
        "succeeded": "Codex run succeeded.",
        "launch_failed": "Codex process could not be launched.",
        "timed_out": "Codex process exceeded its hard timeout.",
        "bounded_continuation_required": (
            "Codex reached a hard execution budget; bounded continuation requires "
            "outer authorization."
        ),
        "accounting_integrity_failure": (
            "Codex execution stopped because exact budget accounting integrity failed."
        ),
        "wall_clock_limit_exceeded": (
            "Codex process exceeded its hard monotonic wall-clock limit."
        ),
        "output_limit_exceeded": (
            f"Codex {observation.output_limit_stream or 'output'} exceeded its byte limit."
        ),
        "permission_blocked": "Codex run was blocked by a permission or sandbox policy.",
        "malformed_event_stream": "Codex produced a malformed JSONL event stream.",
        "process_failed": "Codex process exited unsuccessfully.",
        "missing_final_message": "Codex did not produce a nonempty final message.",
    }
    message = messages[status]
    return message, None if status == "succeeded" else message


def _sanitize_result(
    result: CodexRunResult,
    sensitive_values: Sequence[str],
) -> CodexRunResult:
    """Return the one type-safe sanitized result used by storage and all callers."""
    return CodexRunResult(
        schema_version=result.schema_version,
        run_id=result.run_id,
        status=result.status,
        exit_code=result.exit_code,
        started_at=result.started_at,
        ended_at=result.ended_at,
        duration_seconds=result.duration_seconds,
        artifact_directory=result.artifact_directory,
        event_count=result.event_count,
        malformed_event_count=result.malformed_event_count,
        final_message_present=result.final_message_present,
        permission_evidence=result.permission_evidence,
        confidentiality_violation_detected=(result.confidentiality_violation_detected),
        summary=redact_text(result.summary, sensitive_values),
        error=redact_text(result.error, sensitive_values) if result.error is not None else None,
    )


def _extract_identifier(event: Mapping[str, Any]) -> tuple[str | None, str | None]:
    for key in ("thread_id", "session_id"):
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return key, value.strip()
    return None, None


def _extract_started_thread_id(event: Mapping[str, Any]) -> str | None:
    """Return only an explicit ID from a structured thread.started event."""
    event_type = event.get("type")
    if not isinstance(event_type, str) or event_type.casefold() != "thread.started":
        return None
    thread_id = event.get("thread_id")
    if isinstance(thread_id, str) and thread_id.strip():
        return thread_id.strip()
    thread = event.get("thread")
    if isinstance(thread, Mapping):
        nested_id = thread.get("id")
        if isinstance(nested_id, str) and nested_id.strip():
            return nested_id.strip()
    return None


def _event_has_permission_evidence(event: Mapping[str, Any]) -> bool:
    event_type = event.get("type")
    normalized_event_type = event_type.casefold() if isinstance(event_type, str) else None
    if normalized_event_type in _EXPLICIT_PERMISSION_EVENT_TYPES:
        return True
    if normalized_event_type in _FAILURE_EVENT_TYPES:
        return _contains_permission_phrase(_failure_field_text(event))

    item = event.get("item")
    if not isinstance(item, Mapping):
        return False
    item_type = item.get("type")
    if not isinstance(item_type, str) or item_type.casefold() not in _COMMAND_ITEM_TYPES:
        return False
    status = item.get("status")
    exit_code = item.get("exit_code")
    explicitly_failed = (
        isinstance(status, str) and status.casefold() in _FAILED_COMMAND_STATUSES
    ) or (isinstance(exit_code, int) and not isinstance(exit_code, bool) and exit_code != 0)
    return explicitly_failed and _contains_permission_phrase(_failure_field_text(item))


def _failure_field_text(value: Mapping[str, Any]) -> str:
    strings: list[str] = []

    def collect(item: Any) -> None:
        if isinstance(item, str):
            strings.append(item)
        elif isinstance(item, Mapping):
            for nested in item.values():
                collect(nested)
        elif isinstance(item, list):
            for nested in item:
                collect(nested)

    for field_name in _FAILURE_BEARING_FIELDS:
        if field_name in value:
            collect(value[field_name])
    return "\n".join(strings)


def _contains_permission_phrase(value: str) -> bool:
    normalized = " ".join(value.casefold().split())
    return any(phrase in normalized for phrase in _PERMISSION_PHRASES)


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")


def _utc_string(value: datetime) -> str:
    normalized = value.astimezone(UTC)
    return normalized.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _write_text(path: Path, value: str) -> None:
    path.write_text(value, encoding="utf-8", newline="\n")


def _atomic_write_json(path: Path, value: object) -> None:
    rendered = (
        json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        with suppress(FileNotFoundError):
            temporary_path.unlink()
