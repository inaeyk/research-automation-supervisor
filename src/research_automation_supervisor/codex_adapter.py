"""Deterministic process adapter for one exact human-authored Codex prompt."""

from __future__ import annotations

import hashlib
import json
import os
import selectors
import shutil
import signal
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
from research_automation_supervisor.errors import CodexDependencyError, CodexRequestError
from research_automation_supervisor.redaction import is_sensitive_name, redact_json, redact_text

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
_FAILURE_TYPE_MARKERS = ("error", "fail", "denial", "denied")
_COMMAND_TYPE_MARKERS = ("command", "exec")
_FAILED_STATUSES = ("error", "failed", "failure", "denied", "blocked")


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


@dataclass
class _EventProcessor:
    destination: Any
    sensitive_values: tuple[str, ...]
    pending: bytearray = field(default_factory=bytearray)
    event_count: int = 0
    malformed_hashes: list[str] = field(default_factory=list)
    permission_evidence: bool = False
    identifier_kind: str | None = None
    identifier_value: str | None = None

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
        try:
            value = json.loads(
                line.decode("utf-8", errors="replace"),
                parse_constant=_reject_json_constant,
            )
        except (json.JSONDecodeError, ValueError):
            self.malformed_hashes.append(hashlib.sha256(line).hexdigest())
            return
        if not isinstance(value, dict):
            self.malformed_hashes.append(hashlib.sha256(line).hexdigest())
            return

        self.event_count += 1
        if _event_has_permission_evidence(value):
            self.permission_evidence = True
        if self.identifier_value is None:
            kind, identifier = _extract_identifier(value)
            if identifier is not None:
                self.identifier_kind = kind
                self.identifier_value = identifier
        redacted = redact_json(value, self.sensitive_values)
        rendered = json.dumps(
            redacted,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        self.destination.write(rendered.encode("utf-8") + b"\n")
        self.destination.flush()


VersionProbe = Callable[[str, Mapping[str, str], Path], str | None]
UtcNow = Callable[[], datetime]
Monotonic = Callable[[], float]


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
    prepared = load_codex_request(
        request_path,
        git_worktree_checker=git_worktree_checker,
    )
    executable = codex_executable or which("codex")
    if executable is None:
        raise CodexDependencyError("Codex executable is required")
    executable_path = str(Path(executable).resolve())
    environment, _, _ = build_subprocess_environment(environ)
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
        runs_dir=runs_dir,
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
) -> CodexRunResult:
    """Run an already validated request and durably finalize its artifacts."""
    environment, removed_names, sensitive_values = build_subprocess_environment(environ)
    artifact_directory = _create_artifact_directory(runs_dir, prepared.request.run_id)
    redaction_values = tuple(sensitive_values)
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
        command = build_codex_command(prepared, executable_path, temporary_final)
        with event_path.open("wb") as event_file:
            event_processor = _EventProcessor(event_file, redaction_values)
            _run_process(
                command,
                prepared,
                environment,
                event_processor,
                observation,
                limits,
                started_monotonic,
                monotonic,
            )
            event_processor.finish()

        try:
            if temporary_final.is_file():
                raw_final_bytes = temporary_final.read_bytes()
        except OSError:
            raw_final_bytes = None

    ended_monotonic = monotonic()
    ended_at = utc_now()
    duration = max(0.0, ended_monotonic - started_monotonic)
    stderr_text = bytes(observation.stderr).decode("utf-8", errors="replace")
    prompt_text = prepared.prompt_bytes.decode("utf-8")
    redacted_stderr = redact_text(
        stderr_text,
        (*redaction_values, prompt_text),
    )
    _write_text(artifact_directory / "stderr.log", redacted_stderr)

    final_message = ""
    if raw_final_bytes is not None:
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
    )
    summary, error = _status_messages(status, observation)
    result = CodexRunResult(
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
        summary=summary,
        error=error,
    )

    metadata = _build_metadata(
        prepared=prepared,
        artifact_directory=artifact_directory,
        command=command,
        executable_path=executable_path,
        codex_version=codex_version,
        removed_names=removed_names,
        started_at=started_at,
        ended_at=ended_at,
        duration=duration,
        observation=observation,
        events=event_processor,
        final_message_present=final_message_present,
    )
    _atomic_write_json(
        artifact_directory / "metadata.json",
        redact_json(metadata, redaction_values),
    )
    _atomic_write_json(
        artifact_directory / "result.json",
        redact_json(result.to_dict(), redaction_values),
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


def build_codex_command(
    prepared: PreparedCodexRequest,
    executable: str,
    final_message_path: Path,
) -> list[str]:
    """Construct the fixed shell-free Codex argument vector."""
    request = prepared.request
    command = [
        executable,
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
        "--ask-for-approval",
        prepared.policy.approval,
        "--ignore-user-config",
        "--ignore-rules",
        "--strict-config",
        "--cd",
        str(prepared.workspace),
    ]
    if prepared.policy.ephemeral:
        command.append("--ephemeral")
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
    events: _EventProcessor,
    observation: _ProcessObservation,
    limits: AdapterLimits,
    started_monotonic: float,
    monotonic: Monotonic,
) -> None:
    try:
        process = subprocess.Popen(
            list(command),
            cwd=prepared.workspace,
            env=dict(environment),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except (OSError, subprocess.SubprocessError):
        observation.launch_error = "Codex process launch failed"
        return

    observation.launched = True
    if process.stdin is None or process.stdout is None or process.stderr is None:
        observation.launch_error = "Codex process pipes were unavailable"
        _signal_process_group(process, signal.SIGKILL)
        observation.exit_code = process.wait()
        return

    selector = selectors.DefaultSelector()
    streams = {
        "stdin": process.stdin,
        "stdout": process.stdout,
        "stderr": process.stderr,
    }
    for name, stream in streams.items():
        os.set_blocking(stream.fileno(), False)
        selector.register(
            stream,
            selectors.EVENT_WRITE if name == "stdin" else selectors.EVENT_READ,
            name,
        )

    prompt_offset = 0
    deadline = started_monotonic + prepared.request.timeout_seconds
    termination_started: float | None = None
    killed = False
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

            if observation.termination_reason is None and now >= deadline:
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

            if observation.exit_code is not None and not stdout_open and not stderr_open:
                if termination_started is None:
                    break
                if killed or not _process_group_exists(process.pid):
                    break

            timeout = limits.io_poll_seconds
            if observation.termination_reason is None:
                timeout = min(timeout, max(0.0, deadline - now))
            elif termination_started is not None and not killed:
                timeout = min(
                    timeout,
                    max(0.0, termination_started + limits.termination_grace_seconds - now),
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
    finally:
        for stream in streams.values():
            if _is_registered(selector, stream):
                _unregister_and_close(selector, stream)
        selector.close()

    if observation.exit_code is None:
        observation.exit_code = process.wait()


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


def _create_artifact_directory(runs_dir: Path, run_id: str) -> Path:
    try:
        runs_dir.mkdir(parents=True, exist_ok=True)
        if not runs_dir.is_dir():
            raise CodexRequestError("runs directory is not a directory")
        artifact_directory = runs_dir / run_id
        artifact_directory.mkdir(exist_ok=False)
        return artifact_directory.resolve()
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
) -> dict[str, object]:
    terminating_signal = (
        -observation.exit_code
        if observation.exit_code is not None and observation.exit_code < 0
        else None
    )
    recorded_command = list(command)
    recorded_command[recorded_command.index("--output-last-message") + 1] = "<FINAL_MESSAGE_TEMP>"
    recorded_command[-1] = "<PROMPT_FROM_STDIN>"
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
        "process_exit_code": (
            observation.exit_code
            if observation.exit_code is not None and observation.exit_code >= 0
            else None
        ),
        "terminating_signal": terminating_signal,
        "valid_event_count": events.event_count,
        "malformed_event_count": len(events.malformed_hashes),
        "malformed_event_sha256": events.malformed_hashes,
        "stdout_byte_count": observation.stdout_bytes,
        "stderr_byte_count": observation.stderr_bytes,
        "final_message_present": final_message_present,
        "output_limit_stream": observation.output_limit_stream,
        "thread_id": events.identifier_value if events.identifier_kind == "thread_id" else None,
        "session_id": events.identifier_value if events.identifier_kind == "session_id" else None,
    }
    return metadata


def _classify_status(
    observation: _ProcessObservation,
    events: _EventProcessor,
    final_message_present: bool,
    permission_evidence: bool,
) -> RunStatus:
    if not observation.launched or observation.launch_error is not None:
        return "launch_failed"
    if observation.termination_reason == "timeout":
        return "timed_out"
    if observation.termination_reason == "output_limit":
        return "output_limit_exceeded"
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
) -> tuple[str, str | None]:
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


def _extract_identifier(event: Mapping[str, Any]) -> tuple[str | None, str | None]:
    for key in ("thread_id", "session_id"):
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return key, value.strip()
    return None, None


def _event_has_permission_evidence(event: Mapping[str, Any]) -> bool:
    event_type = event.get("type")
    if isinstance(event_type, str) and any(
        marker in event_type.casefold() for marker in _FAILURE_TYPE_MARKERS
    ):
        return _contains_permission_phrase(_all_strings(event))

    item = event.get("item")
    if not isinstance(item, Mapping):
        return False
    item_type = item.get("type")
    if not isinstance(item_type, str) or not any(
        marker in item_type.casefold() for marker in _COMMAND_TYPE_MARKERS
    ):
        return False
    status = item.get("status")
    exit_code = item.get("exit_code")
    explicitly_failed = (
        isinstance(status, str)
        and any(marker in status.casefold() for marker in _FAILED_STATUSES)
    ) or (isinstance(exit_code, int) and not isinstance(exit_code, bool) and exit_code != 0)
    return explicitly_failed and _contains_permission_phrase(_all_strings(item))


def _all_strings(value: Any) -> str:
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

    collect(value)
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
        ensure_ascii=False,
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
