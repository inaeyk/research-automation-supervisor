"""Bounded shell-free fixed-test execution for Stage 2 workflows."""

from __future__ import annotations

import hashlib
import json
import os
import selectors
import signal
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Annotated, Literal, cast

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

from research_automation_supervisor.codex_adapter import build_subprocess_environment
from research_automation_supervisor.errors import WorkflowInputError
from research_automation_supervisor.redaction import is_sensitive_name, redact_text
from research_automation_supervisor.workflow_models import PreparedWorkflowTest, _freeze_sequence

TEST_TERMINATION_GRACE_SECONDS = 2.0
TEST_IO_POLL_SECONDS = 0.05
TestStatus = Literal[
    "passed",
    "failed",
    "timed_out",
    "output_limit_exceeded",
    "launch_failed",
    "skipped",
]


class TestAttemptResult(BaseModel):
    """Durable normalized result for one fixed test attempt."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    action_id: str
    test_id: str
    status: TestStatus
    argv: Annotated[tuple[str, ...], BeforeValidator(_freeze_sequence)]
    cwd: str
    timeout_seconds: Annotated[int, Field(ge=1)]
    max_stdout_bytes: Annotated[int, Field(ge=1)]
    max_stderr_bytes: Annotated[int, Field(ge=1)]
    started_at: str | None
    ended_at: str | None
    duration_seconds: Annotated[float, Field(ge=0)]
    exit_code: int | None
    terminating_signal: int | None
    timed_out: bool
    output_limit_stream: Literal["stdout", "stderr"] | None
    stdout_byte_count: Annotated[int, Field(ge=0)]
    stderr_byte_count: Annotated[int, Field(ge=0)]
    stdout_stored_byte_count: Annotated[int, Field(ge=0)]
    stderr_stored_byte_count: Annotated[int, Field(ge=0)]
    stdout_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    stderr_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    stdout_artifact: str | None
    stderr_artifact: str | None
    removed_environment_variable_names: Annotated[
        tuple[str, ...], BeforeValidator(_freeze_sequence)
    ]
    redaction_policy: Literal["stage1-redaction-v1"]
    logs_redacted: bool
    passed: bool
    skip_reason: str | None

    @model_validator(mode="after")
    def validate_outcome(self) -> TestAttemptResult:
        if self.passed != (self.status == "passed"):
            raise ValueError("fixed-test status and passed fields contradict")
        if self.exit_code is not None and self.terminating_signal is not None:
            raise ValueError("fixed-test exit code and signal are mutually exclusive")
        if not self.logs_redacted:
            raise ValueError("durable fixed-test logs must use the redaction policy")
        if tuple(
            sorted(
                self.removed_environment_variable_names,
                key=lambda item: (item.casefold(), item),
            )
        ) != self.removed_environment_variable_names or len(
            set(self.removed_environment_variable_names)
        ) != len(self.removed_environment_variable_names) or any(
            not is_sensitive_name(name)
            for name in self.removed_environment_variable_names
        ):
            raise ValueError("removed environment-variable names must be sorted and unique")
        if (
            self.stdout_stored_byte_count > self.max_stdout_bytes
            or self.stderr_stored_byte_count > self.max_stderr_bytes
        ):
            raise ValueError("stored fixed-test logs exceed their configured limits")
        if self.status == "skipped":
            empty_hash = hashlib.sha256(b"").hexdigest()
            if (
                self.started_at is not None
                or self.ended_at is not None
                or self.duration_seconds != 0
                or self.exit_code is not None
                or self.terminating_signal is not None
                or self.timed_out
                or self.output_limit_stream is not None
                or self.stdout_byte_count
                or self.stderr_byte_count
                or self.stdout_stored_byte_count
                or self.stderr_stored_byte_count
                or self.stdout_sha256 != empty_hash
                or self.stderr_sha256 != empty_hash
                or self.stdout_artifact is None
                or self.stderr_artifact is None
                or self.removed_environment_variable_names
                or self.skip_reason is None
            ):
                raise ValueError("skipped fixed-test evidence is contradictory")
            return self
        if (
            self.started_at is None
            or self.ended_at is None
            or self.stdout_artifact is None
            or self.stderr_artifact is None
            or self.skip_reason is not None
        ):
            raise ValueError("launched fixed-test evidence is incomplete")
        if self.status == "passed" and (
            self.exit_code != 0
            or self.terminating_signal is not None
            or self.timed_out
            or self.output_limit_stream is not None
        ):
            raise ValueError("passing fixed-test evidence is contradictory")
        if self.status == "failed" and (
            (self.exit_code is None and self.terminating_signal is None)
            or self.exit_code == 0
            or self.timed_out
            or self.output_limit_stream is not None
        ):
            raise ValueError("failed fixed-test evidence is contradictory")
        if self.status == "timed_out" and (
            not self.timed_out or self.output_limit_stream is not None
        ):
            raise ValueError("timed-out fixed-test evidence is contradictory")
        if self.status == "output_limit_exceeded" and (
            self.timed_out or self.output_limit_stream is None
        ):
            raise ValueError("output-limit fixed-test evidence is contradictory")
        if self.status == "output_limit_exceeded":
            selected_count = (
                self.stdout_byte_count
                if self.output_limit_stream == "stdout"
                else self.stderr_byte_count
            )
            selected_limit = (
                self.max_stdout_bytes
                if self.output_limit_stream == "stdout"
                else self.max_stderr_bytes
            )
            if selected_count <= selected_limit:
                raise ValueError("output-limit status has no matching byte-count breach")
        elif (
            self.stdout_byte_count > self.max_stdout_bytes
            or self.stderr_byte_count > self.max_stderr_bytes
        ):
            raise ValueError("fixed-test byte counts contain an unreported output-limit breach")
        if self.status == "launch_failed" and (
            self.exit_code is not None
            or self.terminating_signal is not None
            or self.timed_out
            or self.output_limit_stream is not None
            or self.stdout_byte_count
            or self.stderr_byte_count
        ):
            raise ValueError("launch-failure fixed-test evidence is contradictory")
        return self

    def to_dict(self) -> dict[str, object]:
        return self.model_dump(mode="json")


class TestSuiteResult(BaseModel):
    """Ordered fixed-test results with first-failure short circuiting."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    passed: bool
    results: Annotated[tuple[TestAttemptResult, ...], BeforeValidator(_freeze_sequence)]

    def to_dict(self) -> dict[str, object]:
        return self.model_dump(mode="json")


def run_test_attempt(
    prepared_test: PreparedWorkflowTest,
    artifact_directory: Path,
    action_id: str,
    *,
    environ: Mapping[str, str] | None = None,
    utc_now: Callable[[], datetime] = lambda: datetime.now(UTC),
    monotonic: Callable[[], float] = time.monotonic,
    termination_grace_seconds: float = TEST_TERMINATION_GRACE_SECONDS,
) -> TestAttemptResult:
    """Execute one exact argv in a new process session with bounded output."""
    test = prepared_test.specification
    environment, removed_names, sensitive_values = build_subprocess_environment(environ)
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        artifact_directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise WorkflowInputError("test artifact directory could not be created") from exc

    started = utc_now()
    started_tick = monotonic()
    process: subprocess.Popen[bytes] | None = None
    stdout = bytearray()
    stderr = bytearray()
    stdout_count = 0
    stderr_count = 0
    output_limit: Literal["stdout", "stderr"] | None = None
    timed_out = False
    launch_failed = False
    exit_code: int | None = None

    try:
        process = subprocess.Popen(
            list(test.argv),
            cwd=prepared_test.cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            start_new_session=True,
        )
    except (OSError, subprocess.SubprocessError):
        launch_failed = True

    if process is not None:
        try:
            (
                exit_code,
                timed_out,
                output_limit,
                stdout_count,
                stderr_count,
            ) = _observe_process(
                process,
                stdout,
                stderr,
                stdout_limit=test.max_stdout_bytes,
                stderr_limit=test.max_stderr_bytes,
                deadline=started_tick + test.timeout_seconds,
                monotonic=monotonic,
                termination_grace_seconds=termination_grace_seconds,
            )
        except BaseException:
            _cleanup_process_group(process, termination_grace_seconds)
            raise

    ended_tick = monotonic()
    ended = utc_now()
    stdout_text = redact_text(stdout.decode("utf-8", errors="replace"), sensitive_values)
    stderr_text = redact_text(stderr.decode("utf-8", errors="replace"), sensitive_values)
    stdout_bytes = _bounded_utf8(stdout_text, test.max_stdout_bytes)
    stderr_bytes = _bounded_utf8(stderr_text, test.max_stderr_bytes)
    stdout_path = artifact_directory / "stdout.log"
    stderr_path = artifact_directory / "stderr.log"
    try:
        stdout_path.write_bytes(stdout_bytes)
        stderr_path.write_bytes(stderr_bytes)
    except OSError as exc:
        raise WorkflowInputError("test logs could not be written") from exc

    if launch_failed:
        status: TestStatus = "launch_failed"
    elif timed_out:
        status = "timed_out"
    elif output_limit is not None:
        status = "output_limit_exceeded"
    elif exit_code == 0:
        status = "passed"
    else:
        status = "failed"
    signal_number = -exit_code if exit_code is not None and exit_code < 0 else None
    result = TestAttemptResult(
        action_id=action_id,
        test_id=test.id,
        status=status,
        argv=test.argv,
        cwd=str(prepared_test.cwd),
        timeout_seconds=test.timeout_seconds,
        max_stdout_bytes=test.max_stdout_bytes,
        max_stderr_bytes=test.max_stderr_bytes,
        started_at=_utc_string(started),
        ended_at=_utc_string(ended),
        duration_seconds=round(max(0.0, ended_tick - started_tick), 6),
        exit_code=exit_code if exit_code is None or exit_code >= 0 else None,
        terminating_signal=signal_number,
        timed_out=timed_out,
        output_limit_stream=output_limit,
        stdout_byte_count=stdout_count,
        stderr_byte_count=stderr_count,
        stdout_stored_byte_count=len(stdout_bytes),
        stderr_stored_byte_count=len(stderr_bytes),
        stdout_sha256=hashlib.sha256(stdout_bytes).hexdigest(),
        stderr_sha256=hashlib.sha256(stderr_bytes).hexdigest(),
        stdout_artifact=str(stdout_path),
        stderr_artifact=str(stderr_path),
        removed_environment_variable_names=removed_names,
        redaction_policy="stage1-redaction-v1",
        logs_redacted=True,
        passed=status == "passed",
        skip_reason=None,
    )
    _atomic_json(artifact_directory / "result.json", result.to_dict())
    return result


def skipped_test_result(
    prepared_test: PreparedWorkflowTest,
    artifact_directory: Path,
    action_id: str,
    *,
    reason: str = "skipped after the first failed acceptance test",
) -> TestAttemptResult:
    """Record a deterministic skipped result without launching a process."""
    try:
        artifact_directory.mkdir(parents=True, exist_ok=True)
        stdout_path = artifact_directory / "stdout.log"
        stderr_path = artifact_directory / "stderr.log"
        stdout_path.write_bytes(b"")
        stderr_path.write_bytes(b"")
    except OSError as exc:
        raise WorkflowInputError("test artifact directory could not be created") from exc
    result = TestAttemptResult(
        action_id=action_id,
        test_id=prepared_test.specification.id,
        status="skipped",
        argv=prepared_test.specification.argv,
        cwd=str(prepared_test.cwd),
        timeout_seconds=prepared_test.specification.timeout_seconds,
        max_stdout_bytes=prepared_test.specification.max_stdout_bytes,
        max_stderr_bytes=prepared_test.specification.max_stderr_bytes,
        started_at=None,
        ended_at=None,
        duration_seconds=0.0,
        exit_code=None,
        terminating_signal=None,
        timed_out=False,
        output_limit_stream=None,
        stdout_byte_count=0,
        stderr_byte_count=0,
        stdout_stored_byte_count=0,
        stderr_stored_byte_count=0,
        stdout_sha256=hashlib.sha256(b"").hexdigest(),
        stderr_sha256=hashlib.sha256(b"").hexdigest(),
        stdout_artifact=str(stdout_path),
        stderr_artifact=str(stderr_path),
        removed_environment_variable_names=(),
        redaction_policy="stage1-redaction-v1",
        logs_redacted=True,
        passed=False,
        skip_reason=reason,
    )
    _atomic_json(artifact_directory / "result.json", result.to_dict())
    return result


def run_test_suite(
    tests: Sequence[PreparedWorkflowTest],
    artifact_directory: Path,
    *,
    action_prefix: str = "test",
    environ: Mapping[str, str] | None = None,
) -> TestSuiteResult:
    """Run in specification order and record later tests as skipped after failure."""
    results: list[TestAttemptResult] = []
    failed = False
    for index, prepared_test in enumerate(tests):
        action_id = f"{action_prefix}-{index:03d}-{prepared_test.specification.id}"
        destination = artifact_directory / action_id
        if failed:
            result = skipped_test_result(prepared_test, destination, action_id)
        else:
            result = run_test_attempt(
                prepared_test,
                destination,
                action_id,
                environ=environ,
            )
            failed = not result.passed
        results.append(result)
    suite = TestSuiteResult(passed=not failed, results=tuple(results))
    artifact_directory.mkdir(parents=True, exist_ok=True)
    _atomic_json(artifact_directory / "suite.json", suite.to_dict())
    return suite


def _observe_process(
    process: subprocess.Popen[bytes],
    stdout: bytearray,
    stderr: bytearray,
    *,
    stdout_limit: int,
    stderr_limit: int,
    deadline: float,
    monotonic: Callable[[], float],
    termination_grace_seconds: float,
) -> tuple[int, bool, Literal["stdout", "stderr"] | None, int, int]:
    if process.stdout is None or process.stderr is None:
        _cleanup_process_group(process, termination_grace_seconds)
        return process.wait(), False, None, 0, 0
    selector = selectors.DefaultSelector()
    streams = {"stdout": process.stdout, "stderr": process.stderr}
    for name, stream in streams.items():
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ, name)
    counts = {"stdout": 0, "stderr": 0}
    limits = {"stdout": stdout_limit, "stderr": stderr_limit}
    destinations = {"stdout": stdout, "stderr": stderr}
    open_streams = set(streams)
    timed_out = False
    output_limit: Literal["stdout", "stderr"] | None = None
    termination_started: float | None = None
    force_killed = False
    normal_cleanup_started: float | None = None
    normal_cleanup_forced = False
    observed_exit: int | None = None
    try:
        while True:
            now = monotonic()
            polled = process.poll()
            if polled is not None:
                observed_exit = polled
                if normal_cleanup_started is None and _process_group_exists(process.pid):
                    normal_cleanup_started = now
                    _signal_group(process.pid, signal.SIGTERM)
            if observed_exit is None and termination_started is None and now >= deadline:
                timed_out = True
                termination_started = now
                _signal_group(process.pid, signal.SIGTERM)
            if (
                termination_started is not None
                and not force_killed
                and now - termination_started >= termination_grace_seconds
            ):
                _signal_group(process.pid, signal.SIGKILL)
                force_killed = True
            if (
                normal_cleanup_started is not None
                and not normal_cleanup_forced
                and _process_group_exists(process.pid)
                and now - normal_cleanup_started >= termination_grace_seconds
            ):
                _signal_group(process.pid, signal.SIGKILL)
                normal_cleanup_forced = True
            if observed_exit is not None and not open_streams and (
                (termination_started is None and normal_cleanup_started is None)
                or force_killed
                or normal_cleanup_forced
                or not _process_group_exists(process.pid)
            ):
                break
            timeout = TEST_IO_POLL_SECONDS
            if observed_exit is None and termination_started is None:
                timeout = min(timeout, max(0.0, deadline - now))
            try:
                ready = selector.select(timeout)
            except InterruptedError:
                continue
            for key, _ in ready:
                name = str(key.data)
                stream = cast(IO[bytes], key.fileobj)
                try:
                    chunk = os.read(stream.fileno(), 64 * 1024)
                except (BlockingIOError, InterruptedError):
                    continue
                except OSError:
                    chunk = b""
                if not chunk:
                    with suppress(KeyError):
                        selector.unregister(stream)
                    with suppress(OSError):
                        stream.close()
                    open_streams.discard(name)
                    continue
                before = counts[name]
                counts[name] += len(chunk)
                remaining = max(0, limits[name] - before)
                destinations[name].extend(chunk[:remaining])
                if (
                    counts[name] > limits[name]
                    and output_limit is None
                    and not timed_out
                ):
                    output_limit = "stdout" if name == "stdout" else "stderr"
                    termination_started = now
                    _signal_group(process.pid, signal.SIGTERM)
    finally:
        for stream in streams.values():
            with suppress(KeyError, ValueError):
                selector.unregister(stream)
            with suppress(OSError):
                stream.close()
        selector.close()
    if observed_exit is None:
        observed_exit = process.wait()
    return observed_exit, timed_out, output_limit, counts["stdout"], counts["stderr"]


def _cleanup_process_group(process: subprocess.Popen[bytes], grace: float) -> None:
    if not _process_group_exists(process.pid):
        return
    _signal_group(process.pid, signal.SIGTERM)
    deadline = time.monotonic() + grace
    while _process_group_exists(process.pid) and time.monotonic() < deadline:
        time.sleep(min(TEST_IO_POLL_SECONDS, max(0.0, deadline - time.monotonic())))
    if _process_group_exists(process.pid):
        _signal_group(process.pid, signal.SIGKILL)


def _signal_group(process_group_id: int, requested_signal: int) -> None:
    with suppress(ProcessLookupError, PermissionError):
        os.killpg(process_group_id, requested_signal)


def _process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _utc_string(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _bounded_utf8(value: str, limit: int) -> bytes:
    rendered = value.encode("utf-8")
    if len(rendered) <= limit:
        return rendered
    return rendered[:limit].decode("utf-8", errors="ignore").encode("utf-8")


def _atomic_json(path: Path, value: object) -> None:
    import tempfile

    rendered = json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        with suppress(FileNotFoundError):
            temporary.unlink()
