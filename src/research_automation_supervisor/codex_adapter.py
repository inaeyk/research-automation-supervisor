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
from typing import Any

from packaging.version import Version

from research_automation_supervisor import __version__
from research_automation_supervisor.codex_models import (
    CodexRunResult,
    GitWorktreeChecker,
    PreparedCodexRequest,
    RunStatus,
    load_codex_request,
)
from research_automation_supervisor.doctor import MINIMUM_CODEX, _parse_codex_version
from research_automation_supervisor.errors import (
    CodexConfidentialityError,
    CodexDependencyError,
    CodexRequestError,
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

STDOUT_LIMIT_BYTES = 100 * 1024 * 1024
STDERR_LIMIT_BYTES = 10 * 1024 * 1024
TERMINATION_GRACE_SECONDS = 2.0
IO_POLL_SECONDS = 0.1
VERSION_PROBE_TIMEOUT_SECONDS = 10.0

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
            malformed_evidence = (
                b"confidentiality-violation"
                if rejected
                else line
            )
            self.malformed_hashes.append(
                hashlib.sha256(malformed_evidence).hexdigest()
            )
            return

        self.event_count += 1
        if permission_evidence:
            self.permission_evidence = True
        if self.identifier_value is None and identifier is not None:
            self.identifier_kind = kind
            self.identifier_value = identifier
        if (
            started_thread_id is not None
            and started_thread_id not in self.started_thread_ids
        ):
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
    confidential_fragments: Sequence[str] = (),
    rejected_confidential_fragments: Sequence[str] = (),
    durable_command_replacements: Mapping[str, str] | None = None,
    process_launch_builder: ProcessLaunchBuilder | None = None,
    process_started: ProcessStarted | None = None,
    process_finished: ProcessFinished | None = None,
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
            raise CodexRequestError(
                "only a persistent worker or supervisor may resume a thread"
            )
        if not resume_thread_id.strip() or resume_thread_id in {"--last", "--all"}:
            raise CodexRequestError(
                "an explicit persistent thread ID is required for resume"
            )
        validate_locator_confidentiality((resume_thread_id,), sensitive_values)
    artifact_directory = _create_artifact_directory(
        resolved_runs_dir,
        prepared.request.run_id,
    )
    redaction_values = tuple(
        sorted(
            {value for value in (*sensitive_values, *confidential_fragments) if value},
            key=lambda item: (-len(item), item),
        )
    )
    rejected_values = tuple(
        sorted(
            {
                value.encode("utf-8")
                for value in rejected_confidential_fragments
                if value
            },
            key=lambda item: (-len(item), item),
        )
    )
    _initialize_artifacts(artifact_directory, prepared, redaction_values)

    executable_path = str(Path(codex_executable).resolve())
    probe = version_probe or probe_codex_version
    codex_version = probe(executable_path, environment, prepared.workspace)

    started_at = utc_now()
    started_monotonic = monotonic()
    observation = _ProcessObservation()
    event_path = artifact_directory / "events.jsonl"
    raw_final_bytes: bytes | None = None

    with tempfile.TemporaryDirectory(prefix="research-supervisor-codex-") as temporary:
        temporary_final = Path(temporary) / "last-message.md"
        command = build_codex_command(
            prepared,
            executable_path,
            temporary_final,
            output_schema=resolved_output_schema,
            resume_thread_id=resume_thread_id,
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
                process_started,
                process_finished,
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
            confidentiality_violation_detected=(
                confidentiality_violation_detected
            ),
            summary=summary,
            error=error,
        ),
        redaction_values,
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
        confidentiality_violation_detected=(
            confidentiality_violation_detected
        ),
        durable_command_replacements=durable_command_replacements or {},
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
        locators.extend(
            (str(runs_dir), str(resolved_runs_dir), str(artifact_directory))
        )

    validate_locator_confidentiality(locators, sensitive_values)
    return resolved_runs_dir


def validate_locator_confidentiality(
    locators: Sequence[str | Path],
    sensitive_values: Sequence[str],
) -> None:
    """Reject exact structural strings that cannot be rendered unchanged."""
    rendered_locators = tuple(str(locator) for locator in locators)
    if any(would_redact_text(locator, sensitive_values) for locator in rendered_locators):
        raise CodexConfidentialityError(
            "Codex request contains a structural redaction collision"
        )


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
        raise CodexRequestError(
            f"output schema is not production-compatible: {exc}"
        ) from exc
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
) -> list[str]:
    """Construct the fixed shell-free Codex argument vector."""
    request = prepared.request
    if resume_thread_id is not None and (
        request.role not in {"worker", "supervisor"}
        or not resume_thread_id.strip()
        or resume_thread_id in {"--last", "--all"}
    ):
        raise CodexRequestError(
            "resume requires one exact persistent worker or supervisor thread ID"
        )
    if resume_thread_id is None:
        command = [
            executable,
            "--ask-for-approval",
            prepared.policy.approval,
            "exec",
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
            "--sandbox",
            prepared.policy.sandbox,
            "--ignore-user-config",
            "--ignore-rules",
            "--strict-config",
            "--cd",
            str(prepared.workspace),
        ]
        if prepared.policy.ephemeral:
            command.append("--ephemeral")
    else:
        command = [
            executable,
            "--ask-for-approval",
            prepared.policy.approval,
            "--sandbox",
            prepared.policy.sandbox,
            "--cd",
            str(prepared.workspace),
            "exec",
            "resume",
            resume_thread_id,
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
            "--ignore-user-config",
            "--ignore-rules",
            "--strict-config",
        ]
    if output_schema is not None:
        command.extend(("--output-schema", str(output_schema)))
    command.append("-")
    return command


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
    process_started: ProcessStarted | None,
    process_finished: ProcessFinished | None,
) -> None:
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
    try:
        if process_started is not None:
            process_started(process.pid)
    except BaseException:
        _emergency_process_group_cleanup(process, limits)
        process.wait()
        if process_finished is not None:
            process_finished(process.pid)
        raise
    if process.stdin is None or process.stdout is None or process.stderr is None:
        observation.launch_error = "Codex process pipes were unavailable"
        _emergency_process_group_cleanup(process, limits)
        observation.exit_code = process.wait()
        if process_finished is not None:
            process_finished(process.pid)
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
        _emergency_process_group_cleanup(process, limits)
        observation.exit_code = process.wait()
        if process_finished is not None:
            process_finished(process.pid)
        return

    prompt_offset = 0
    deadline = started_monotonic + prepared.request.timeout_seconds
    termination_started: float | None = None
    killed = False
    normal_cleanup_started: float | None = None
    normal_cleanup_forced = False
    stdout_open = True
    stderr_open = True

    try:
        while True:
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
                    and _process_group_exists(process.pid)
                ):
                    normal_cleanup_started = now
                    _signal_process_group(process, signal.SIGTERM)

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
                _signal_process_group(process, signal.SIGKILL)
                killed = True

            if (
                normal_cleanup_started is not None
                and not normal_cleanup_forced
                and _process_group_exists(process.pid)
                and now - normal_cleanup_started >= limits.termination_grace_seconds
            ):
                _signal_process_group(process, signal.SIGKILL)
                normal_cleanup_forced = True

            if observation.exit_code is not None and not stdout_open and not stderr_open:
                if termination_started is None and normal_cleanup_started is None:
                    break
                if (
                    killed
                    or normal_cleanup_forced
                    or not _process_group_exists(process.pid)
                ):
                    break

            timeout = limits.io_poll_seconds
            if observation.termination_reason is None and observation.exit_code is None:
                timeout = min(timeout, max(0.0, deadline - now))
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
        _emergency_process_group_cleanup(process, limits)
        if observation.exit_code is None:
            observation.exit_code = process.wait()
        if process_finished is not None:
            process_finished(process.pid)
        raise
    finally:
        for stream in streams.values():
            if _is_registered(selector, stream):
                _unregister_and_close(selector, stream)
        selector.close()

    if observation.exit_code is None:
        observation.exit_code = process.wait()
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
                if (
                    len(content) > 4 * 1024 * 1024
                    or (
                        before.st_dev,
                        before.st_ino,
                        before.st_size,
                        before.st_mtime_ns,
                    )
                    != (
                        after.st_dev,
                        after.st_ino,
                        after.st_size,
                        after.st_mtime_ns,
                    )
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


def _initialize_artifacts(
    artifact_directory: Path,
    prepared: PreparedCodexRequest,
    sensitive_values: Sequence[str],
) -> None:
    _atomic_write_json(
        artifact_directory / "request.normalized.json",
        redact_json(prepared.normalized_dict(), sensitive_values),
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
) -> dict[str, object]:
    terminating_signal = (
        -observation.exit_code
        if observation.exit_code is not None and observation.exit_code < 0
        else None
    )
    recorded_command = list(command)
    recorded_command[recorded_command.index("--output-last-message") + 1] = "<FINAL_MESSAGE_TEMP>"
    recorded_command[-1] = "<PROMPT_FROM_STDIN>"
    recorded_command = [
        durable_command_replacements.get(item, item)
        for item in recorded_command
    ]
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
    }
    if confidentiality_violation_detected:
        metadata["confidentiality_violation_detected"] = True
    return metadata


def _write_stage2_completion_manifest(
    artifact_directory: Path,
    prepared: PreparedCodexRequest,
    result: CodexRunResult,
    output_schema: Path,
) -> None:
    """Seal the complete Stage 2 Stage 1 artifact set after all final writes."""
    artifact_names = (
        "request.normalized.json",
        "prompt.sha256",
        "events.jsonl",
        "stderr.log",
        "final-message.md",
        "metadata.json",
        "result.json",
    )
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
    if observation.termination_reason == "output_limit":
        return "output_limit_exceeded"
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
        confidentiality_violation_detected=(
            result.confidentiality_violation_detected
        ),
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
        isinstance(status, str)
        and status.casefold() in _FAILED_COMMAND_STATUSES
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
    rendered = json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        indent=2,
        sort_keys=True,
    ) + "\n"
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
