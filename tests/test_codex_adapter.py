from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import time
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from research_automation_supervisor import __version__
from research_automation_supervisor.cli import _format_codex_result, app
from research_automation_supervisor.codex_adapter import (
    DEFAULT_LIMITS,
    STDERR_LIMIT_BYTES,
    STDOUT_LIMIT_BYTES,
    AdapterLimits,
    build_codex_command,
    execute_codex_request,
    run_prepared_codex,
    validate_locator_confidentiality,
)
from research_automation_supervisor.codex_models import (
    CodexRunResult,
    PreparedCodexRequest,
    RunStatus,
    load_codex_request,
)
from research_automation_supervisor.errors import (
    CodexConfidentialityError,
    CodexDependencyError,
    CodexRequestError,
)
from research_automation_supervisor.redaction import redact_text

FAKE_CODEX = (Path(__file__).parent / "fixtures" / "fake_codex.py").resolve()
ARTIFACT_NAMES = {
    "request.normalized.json",
    "prompt.sha256",
    "events.jsonl",
    "stderr.log",
    "final-message.md",
    "metadata.json",
    "result.json",
    "usage-receipt.json",
    "context-economy-receipt.json",
}


def request_data(role: str = "worker") -> dict[str, object]:
    return {
        "schema_version": 1,
        "run_id": f"{role}-run",
        "role": role,
        "workspace": "workspace",
        "prompt_path": "prompt.md",
        "model": "gpt-5.6-sol",
        "reasoning_effort": "xhigh",
        "timeout_seconds": 30,
    }


def prepared_request(
    tmp_path: Path,
    role: str = "worker",
    *,
    prompt_bytes: bytes = b"One exact human-written prompt.\n",
) -> PreparedCodexRequest:
    tmp_path.mkdir(parents=True, exist_ok=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (tmp_path / "prompt.md").write_bytes(prompt_bytes)
    request_path = tmp_path / "request.yaml"
    request_path.write_text(yaml.safe_dump(request_data(role)), encoding="utf-8")
    return load_codex_request(request_path, git_worktree_checker=lambda _: True)


def structural_request_path(tmp_path: Path, field: str, value: str) -> Path:
    """Create a valid request whose selected exact structure contains a value."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    data = request_data()
    workspace_name = f"workspace-{value}" if field == "workspace" else "workspace"
    prompt_name = f"prompt-{value}.md" if field == "prompt_path" else "prompt.md"
    request_name = f"request-{value}.yaml" if field == "request_path" else "request.yaml"
    if field in {"run_id", "model"}:
        data[field] = value
    data["workspace"] = workspace_name
    data["prompt_path"] = prompt_name

    (tmp_path / workspace_name).mkdir()
    (tmp_path / prompt_name).write_text(
        "One exact human-written prompt.\n",
        encoding="utf-8",
    )
    request_path = tmp_path / request_name
    request_path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return request_path


def configure(prepared: PreparedCodexRequest, **configuration: object) -> None:
    (prepared.workspace / ".fake-codex.json").write_text(
        json.dumps(configuration), encoding="utf-8"
    )


def fake_environment(**extra: str) -> dict[str, str]:
    environment = {
        "PATH": os.environ["PATH"],
        "HOME": "/tmp/fake-codex-home",
        "LANG": "C.UTF-8",
    }
    environment.update(extra)
    return environment


def run_fake(
    prepared: PreparedCodexRequest,
    *,
    limits: AdapterLimits | None = None,
    environ: dict[str, str] | None = None,
    monotonic=time.monotonic,
) -> CodexRunResult:
    return run_prepared_codex(
        prepared,
        runs_dir=prepared.request_path.parent / "runs",
        codex_executable=str(FAKE_CODEX),
        environ=environ or fake_environment(),
        limits=limits or AdapterLimits(),
        monotonic=monotonic,
    )


@pytest.mark.parametrize(
    ("role", "sandbox", "ephemeral"),
    [
        ("supervisor", "read-only", False),
        ("worker", "workspace-write", False),
        ("auditor", "read-only", True),
    ],
)
def test_exact_process_construction_and_prompt_stdin(
    tmp_path: Path, role: str, sandbox: str, ephemeral: bool
) -> None:
    prepared = prepared_request(tmp_path, role)
    configure(
        prepared,
        stdout_lines=['{"thread_id":"thread-123","type":"thread.started"}'],
        final="completed",
    )

    result = run_fake(prepared)
    observation = json.loads(
        (prepared.workspace / ".fake-codex-observation.json").read_text(encoding="utf-8")
    )
    argv = observation["argv"]

    assert result.status == "succeeded"
    assert result.exit_code == 0
    assert base64.b64decode(observation["prompt_base64"]) == prepared.prompt_bytes
    assert prepared.prompt_bytes.decode().strip() not in " ".join(argv)
    assert argv[:5] == [
        "--ask-for-approval",
        "never",
        "exec",
        "--json",
        "--output-last-message",
    ]
    assert argv[argv.index("--model") + 1] == "gpt-5.6-sol"
    assert "model_reasoning_effort=xhigh" in argv
    assert 'web_search="disabled"' in argv
    assert "sandbox_workspace_write.network_access=false" in argv
    assert "features.skill_mcp_dependency_install=false" in argv
    assert argv[argv.index("--sandbox") + 1] == sandbox
    exec_index = argv.index("exec")
    assert argv.index("--ask-for-approval") < exec_index
    assert "--ask-for-approval" not in argv[exec_index + 1 :]
    assert argv[argv.index("--cd") + 1] == str(prepared.workspace)
    assert observation["cwd"] == str(prepared.workspace)
    assert ("--ephemeral" in argv) is ephemeral
    assert argv[-1] == "-"
    assert {"--ignore-user-config", "--ignore-rules", "--strict-config"} <= set(argv)
    if role == "auditor":
        assert argv.count("--add-dir") == 1
        scratch = Path(argv[argv.index("--add-dir") + 1])
        assert scratch == Path(result.artifact_directory) / "scratch"
    else:
        assert "--add-dir" not in argv
    for forbidden in (
        "--skip-git-repo-check",
        "--search",
        "--full-auto",
        "--yolo",
        "danger-full-access",
    ):
        assert forbidden not in argv


def test_read_only_auditor_tempfile_reproducer_uses_only_action_scratch(
    tmp_path: Path,
) -> None:
    prepared = prepared_request(tmp_path, "auditor")
    protected = prepared.workspace / "protected.txt"
    protected.write_text("immutable\n", encoding="utf-8")
    configure(
        prepared,
        exercise_tempfile=True,
        stdout_lines=['{"thread_id":"audit-1","type":"thread.started"}'],
        final="completed",
    )

    result = run_fake(prepared)
    directory = Path(result.artifact_directory)
    metadata = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
    observation = json.loads(
        (prepared.workspace / ".fake-codex-observation.json").read_text(encoding="utf-8")
    )
    argv = observation["argv"]
    environment = observation["environment"]
    scratch = directory / "scratch"
    reproducer = observation["tempfile_reproducer"]

    assert result.status == "succeeded"
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    assert argv.count("--add-dir") == 1
    assert Path(argv[argv.index("--add-dir") + 1]) == scratch
    assert environment["TMPDIR"] == str(scratch)
    assert environment["TMP"] == str(scratch)
    assert environment["TEMP"] == str(scratch)
    assert Path(reproducer["temporary_directory"]).is_relative_to(scratch)
    assert reproducer["source_created"] is True
    assert reproducer["compiled_created"] is True
    assert protected.read_text(encoding="utf-8") == "immutable\n"
    assert metadata["auditor_scratch_path"] == str(scratch)
    assert metadata["sandbox_disposition"] == "workspace-read-only-action-scratch-write"


def test_auditor_scratch_is_unique_per_action_and_worker_is_unchanged(
    tmp_path: Path,
) -> None:
    first = prepared_request(tmp_path / "first", "auditor")
    second = prepared_request(tmp_path / "second", "auditor")
    worker = prepared_request(tmp_path / "worker", "worker")
    for prepared, thread in (
        (first, "audit-1"),
        (second, "audit-2"),
        (worker, "worker-1"),
    ):
        configure(
            prepared,
            stdout_lines=[json.dumps({"thread_id": thread, "type": "thread.started"})],
            final="completed",
        )

    first_result = run_fake(first)
    second_result = run_fake(second)
    worker_result = run_fake(worker)
    first_metadata = json.loads(
        (Path(first_result.artifact_directory) / "metadata.json").read_text()
    )
    second_metadata = json.loads(
        (Path(second_result.artifact_directory) / "metadata.json").read_text()
    )
    worker_metadata = json.loads(
        (Path(worker_result.artifact_directory) / "metadata.json").read_text()
    )

    assert first_metadata["auditor_scratch_path"] != second_metadata["auditor_scratch_path"]
    assert Path(first_metadata["auditor_scratch_path"]).is_dir()
    assert Path(second_metadata["auditor_scratch_path"]).is_dir()
    assert "auditor_scratch_path" not in worker_metadata
    assert "sandbox_disposition" not in worker_metadata
    assert "--add-dir" not in worker_metadata["command"]


def test_build_command_has_no_prompt_and_only_role_owned_policy(tmp_path: Path) -> None:
    prepared = prepared_request(tmp_path, "auditor")
    command = build_codex_command(prepared, "/tools/codex", Path("/tmp/final"))

    assert command[0:5] == [
        "/tools/codex",
        "--ask-for-approval",
        "never",
        "exec",
        "--json",
    ]
    assert "One exact human-written prompt" not in " ".join(command)
    assert command[-2:] == ["--ephemeral", "-"]


def test_resume_command_reapplies_execution_policy_before_resume(
    tmp_path: Path,
) -> None:
    prepared = prepared_request(tmp_path, "worker")
    schema = tmp_path / "output-schema.json"
    schema.write_text("{}\n", encoding="utf-8")

    command = build_codex_command(
        prepared,
        "/tools/codex",
        Path("/tmp/final"),
        output_schema=schema,
        resume_thread_id="thread-123",
    )

    resume_index = command.index("resume")
    assert command[resume_index + 1 :] == ["thread-123", "-"]
    for option in (
        "--model",
        "model_reasoning_effort=xhigh",
        "--sandbox",
        "--cd",
        "--output-schema",
    ):
        assert command.index(option) < resume_index
    for setting in (
        "model_auto_compact_token_limit=64000",
        "tool_output_token_limit=2048",
    ):
        assert command.index(setting) < resume_index


def test_repository_free_fake_requires_opt_in_skip_git_flag(tmp_path: Path) -> None:
    without_flag = prepared_request(tmp_path / "without", "supervisor")
    configure(
        without_flag,
        refuse_repository_free_without_skip=True,
        stdout_lines=['{"thread_id":"thread-123","type":"thread.started"}'],
        final="completed",
    )
    failed = run_prepared_codex(
        without_flag,
        runs_dir=tmp_path / "without-runs",
        codex_executable=str(FAKE_CODEX),
        environ=fake_environment(),
    )

    with_flag = prepared_request(tmp_path / "with", "supervisor")
    configure(
        with_flag,
        refuse_repository_free_without_skip=True,
        stdout_lines=['{"thread_id":"thread-123","type":"thread.started"}'],
        final="completed",
    )
    succeeded = run_prepared_codex(
        with_flag,
        runs_dir=tmp_path / "with-runs",
        codex_executable=str(FAKE_CODEX),
        environ=fake_environment(),
        skip_git_repo_check=True,
    )
    metadata = json.loads(
        (Path(succeeded.artifact_directory) / "metadata.json").read_text(encoding="utf-8")
    )
    normalized = json.loads(
        (Path(succeeded.artifact_directory) / "request.normalized.json").read_text(encoding="utf-8")
    )
    command = metadata["command"]
    exec_index = command.index("exec")

    assert failed.status == "process_failed"
    assert failed.event_count == 0
    assert failed.final_message_present is False
    assert failed.confidentiality_violation_detected is False
    assert succeeded.status == "succeeded"
    assert command.count("--skip-git-repo-check") == 1
    assert command.index("--skip-git-repo-check") == exec_index + 1
    assert normalized["skip_git_repo_check"] is True


def test_parser_aware_fake_rejects_post_exec_approval_option(tmp_path: Path) -> None:
    rejected = subprocess.run(
        [str(FAKE_CODEX), "exec", "--ask-for-approval", "never", "--help"],
        capture_output=True,
        check=False,
        cwd=tmp_path,
        encoding="utf-8",
        errors="replace",
        timeout=5,
    )
    accepted = subprocess.run(
        [str(FAKE_CODEX), "--ask-for-approval", "never", "exec", "--help"],
        capture_output=True,
        check=False,
        cwd=tmp_path,
        encoding="utf-8",
        errors="replace",
        timeout=5,
    )
    duplicate = subprocess.run(
        [
            str(FAKE_CODEX),
            "--ask-for-approval",
            "never",
            "exec",
            "--ask-for-approval",
            "never",
            "--help",
        ],
        capture_output=True,
        check=False,
        cwd=tmp_path,
        encoding="utf-8",
        errors="replace",
        timeout=5,
    )

    assert rejected.returncode == 2
    assert "before exec" in rejected.stderr
    assert accepted.returncode == 0
    assert duplicate.returncode == 2
    assert "not accepted after exec" in duplicate.stderr


def test_production_output_limit_defaults_are_exact() -> None:
    assert STDOUT_LIMIT_BYTES == 100 * 1024 * 1024
    assert STDERR_LIMIT_BYTES == 10 * 1024 * 1024
    assert DEFAULT_LIMITS.stdout_bytes == 100 * 1024 * 1024
    assert DEFAULT_LIMITS.stderr_bytes == 10 * 1024 * 1024


@pytest.mark.parametrize(
    "literal_schema",
    [
        {"const": 1},
        {"enum": ["a", "b"]},
    ],
)
def test_invalid_literal_schema_is_rejected_locally_before_codex_launch(
    tmp_path: Path,
    literal_schema: dict[str, object],
) -> None:
    prepared = prepared_request(tmp_path)
    schema_path = tmp_path / "output-schema.json"
    schema_path.write_text(
        json.dumps(
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["value"],
                "properties": {"value": literal_schema},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        CodexRequestError,
        match=r"output schema is not production-compatible: .*requires an explicit",
    ):
        run_prepared_codex(
            prepared,
            runs_dir=tmp_path / "runs",
            codex_executable=str(FAKE_CODEX),
            environ=fake_environment(),
            output_schema=schema_path,
        )

    assert not (tmp_path / "runs").exists()
    assert not (prepared.workspace / ".fake-codex-observation.json").exists()


def test_success_writes_complete_canonical_artifacts_and_metadata(tmp_path: Path) -> None:
    prepared = prepared_request(tmp_path)
    configure(
        prepared,
        stdout_lines=[
            '{"type": "thread.started", "thread_id": "thread-123", "z": 2, "a": 1}',
            '{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}',
            json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {
                        "input_tokens": 120,
                        "cached_input_tokens": 80,
                        "output_tokens": 30,
                        "reasoning_output_tokens": 12,
                    },
                }
            ),
        ],
        stderr="diagnostic\n",
        final="Final response.\n",
    )

    result = run_fake(prepared)
    directory = Path(result.artifact_directory)
    metadata = json.loads((directory / "metadata.json").read_text())

    assert result.status == "succeeded"
    assert {path.name for path in directory.iterdir()} >= ARTIFACT_NAMES
    assert (directory / "events.jsonl").read_text().splitlines()[0] == (
        '{"a":1,"thread_id":"thread-123","type":"thread.started","z":2}'
    )
    assert (directory / "stderr.log").read_text() == "diagnostic\n"
    assert (directory / "final-message.md").read_text() == "Final response.\n"
    assert (directory / "prompt.sha256").read_text().strip() == prepared.prompt_sha256
    assert json.loads((directory / "result.json").read_text()) == result.to_dict()
    assert metadata["package_version"] == __version__
    assert metadata["codex_version"] == "0.200.0"
    assert metadata["thread_id"] == "thread-123"
    assert metadata["artifact_directory"] == result.artifact_directory
    assert metadata["valid_event_count"] == 3
    assert metadata["malformed_event_count"] == 0
    usage = json.loads((directory / "usage-receipt.json").read_text())
    assert metadata["usage_complete"] is True
    assert metadata["usage_receipt_id"] == usage["receipt_id"]
    assert usage["input_tokens"] == 120
    assert usage["cached_input_tokens"] == 80
    assert usage["output_tokens"] == 30
    assert usage["reasoning_output_tokens"] == 12
    assert usage["combined_tokens"] == 150
    assert metadata["command"][:4] == [
        str(FAKE_CODEX),
        "--ask-for-approval",
        "never",
        "exec",
    ]
    assert metadata["command"][-1] == "<PROMPT_FROM_STDIN>"
    assert "<FINAL_MESSAGE_TEMP>" in metadata["command"]
    assert not list(directory.glob(".metadata.json.*"))
    assert not list(directory.glob(".result.json.*"))


@pytest.mark.parametrize(
    ("configuration", "status"),
    [
        (
            {"stdout_lines": ['{"type":"turn.completed"}'], "final": "done", "exit_code": 9},
            "process_failed",
        ),
        ({"stdout_lines": ["not-json"], "final": "done"}, "malformed_event_stream"),
        ({"stdout_lines": ["[]"], "final": "done"}, "malformed_event_stream"),
        (
            {"stdout_lines": ['{"type":"turn.completed"}'], "write_final": False},
            "missing_final_message",
        ),
        ({"stdout_lines": ['{"type":"turn.completed"}'], "final": " \n"}, "missing_final_message"),
    ],
)
def test_normalized_process_outcomes_leave_useful_artifacts(
    tmp_path: Path, configuration: dict[str, object], status: RunStatus
) -> None:
    prepared = prepared_request(tmp_path)
    configure(prepared, **configuration)

    result = run_fake(prepared)
    directory = Path(result.artifact_directory)

    assert result.status == status
    assert {path.name for path in directory.iterdir()} >= ARTIFACT_NAMES
    assert json.loads((directory / "result.json").read_text()) == result.to_dict()
    assert result.error is not None


def test_launch_failure_is_normalized_with_artifacts(tmp_path: Path) -> None:
    prepared = prepared_request(tmp_path)
    missing_executable = tmp_path / "missing-codex"
    secret = "LAUNCH_DIAGNOSTIC_SECRET_123"

    result = run_prepared_codex(
        prepared,
        runs_dir=tmp_path / "runs",
        codex_executable=str(missing_executable),
        environ=fake_environment(DEMO_TOKEN=secret),
    )
    raw_directory = (tmp_path / "runs" / prepared.request.run_id).resolve()
    persisted = json.loads((raw_directory / "result.json").read_text())

    assert result.status == "launch_failed"
    assert result.exit_code is None
    assert result.run_id == prepared.request.run_id
    assert Path(result.artifact_directory) == raw_directory
    assert raw_directory.exists()
    assert persisted == result.to_dict()
    assert secret not in json.dumps(result.to_dict())
    assert {path.name for path in raw_directory.iterdir()} >= ARTIFACT_NAMES


def test_timeout_terminates_process_group_and_child(tmp_path: Path) -> None:
    prepared = prepared_request(tmp_path)
    configure(
        prepared,
        sleep_seconds=10,
        spawn_child_sleep=10,
        child_ignore_term=True,
    )
    origin = time.monotonic()

    def accelerated_monotonic() -> float:
        return (time.monotonic() - origin) * 100.0

    result = run_fake(
        prepared,
        limits=AdapterLimits(termination_grace_seconds=10.0, io_poll_seconds=0.01),
        monotonic=accelerated_monotonic,
    )

    assert result.status == "timed_out"
    assert result.exit_code is not None and result.exit_code < 0
    metadata = json.loads((Path(result.artifact_directory) / "metadata.json").read_text())
    assert metadata["terminating_signal"] in {15, 9}
    child_pid_path = prepared.workspace / ".fake-codex-child.pid"
    child_ready_path = prepared.workspace / ".fake-codex-child.ready"
    assert child_pid_path.exists(), "the timeout test must prove the child was created"
    assert child_ready_path.exists(), "the child must install its signal policy before timeout"
    child_pid = int(child_pid_path.read_text())
    deadline = time.monotonic() + 2
    while _process_is_live(child_pid) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not _process_is_live(child_pid)


def test_normal_zero_leader_exit_cleans_up_sigterm_ignoring_child(tmp_path: Path) -> None:
    prepared = prepared_request(tmp_path)
    configure(
        prepared,
        stdout_lines=['{"type":"turn.completed"}'],
        final="done",
        spawn_child_sleep=30,
        child_ignore_term=True,
    )

    result = run_fake(
        prepared,
        limits=AdapterLimits(termination_grace_seconds=0.05, io_poll_seconds=0.005),
    )

    child_pid_path = prepared.workspace / ".fake-codex-child.pid"
    child_ready_path = prepared.workspace / ".fake-codex-child.ready"
    assert child_pid_path.exists(), "the normal-exit test must prove the child was created"
    assert child_ready_path.exists(), "the child must be ready before its leader exits"
    child_pid = int(child_pid_path.read_text())
    assert result.status == "succeeded"
    assert result.exit_code == 0
    deadline = time.monotonic() + 2
    while _process_is_live(child_pid) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not _process_is_live(child_pid)


@pytest.mark.parametrize(
    ("configuration", "stream"),
    [
        ({"stdout_repeat": 200, "final": "done"}, "stdout"),
        (
            {
                "stdout_lines": ['{"type":"turn.completed"}'],
                "stderr_repeat": 200,
                "final": "done",
            },
            "stderr",
        ),
    ],
)
def test_output_limits_terminate_without_retaining_excess(
    tmp_path: Path, configuration: dict[str, object], stream: str
) -> None:
    prepared = prepared_request(tmp_path)
    configure(prepared, **configuration)

    result = run_fake(
        prepared,
        limits=AdapterLimits(
            stdout_bytes=64,
            stderr_bytes=64,
            termination_grace_seconds=0.05,
            io_poll_seconds=0.01,
        ),
    )
    directory = Path(result.artifact_directory)
    metadata = json.loads((directory / "metadata.json").read_text())

    assert result.status == "output_limit_exceeded"
    assert metadata["output_limit_stream"] == stream
    limited_artifact = directory / ("events.jsonl" if stream == "stdout" else "stderr.log")
    assert limited_artifact.stat().st_size <= 64


def test_permission_blocked_from_stderr_and_structured_failure(tmp_path: Path) -> None:
    prepared = prepared_request(tmp_path)
    configure(
        prepared,
        stdout_lines=['{"type":"turn.failed","message":"ordinary failure"}'],
        stderr="sandbox violation: write rejected",
        final="could not complete",
        exit_code=1,
    )
    stderr_result = run_fake(prepared)
    assert stderr_result.status == "permission_blocked"
    assert stderr_result.permission_evidence

    second = prepared_request(tmp_path / "second")
    configure(
        second,
        stdout_lines=['{"type":"turn.failed","message":"network access disabled"}'],
        final="could not complete",
        exit_code=1,
    )
    event_result = run_fake(second)
    assert event_result.status == "permission_blocked"
    assert event_result.permission_evidence


def test_permission_like_assistant_prose_does_not_trigger_classification(tmp_path: Path) -> None:
    prepared = prepared_request(tmp_path)
    configure(
        prepared,
        stdout_lines=[
            '{"type":"item.completed","item":{"type":"agent_message","text":"permission denied"}}'
        ],
        final="failed",
        exit_code=1,
    )

    result = run_fake(prepared)

    assert result.status == "process_failed"
    assert not result.permission_evidence


@pytest.mark.parametrize(
    "event",
    [
        {
            "type": "assistant_not_error",
            "message": "permission denied",
            "item": {"type": "agent_message", "text": "permission denied"},
        },
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "status": "completed",
                "exit_code": 0,
                "aggregated_output": "permission denied was quoted in documentation",
            },
        },
    ],
)
def test_unknown_events_and_successful_commands_cannot_supply_permission_evidence(
    tmp_path: Path, event: dict[str, object]
) -> None:
    prepared = prepared_request(tmp_path)
    configure(
        prepared,
        stdout_lines=[json.dumps(event)],
        final="failed",
        exit_code=1,
    )

    result = run_fake(prepared)

    assert result.status == "process_failed"
    assert not result.permission_evidence


def test_failed_command_item_can_supply_explicit_permission_evidence(tmp_path: Path) -> None:
    prepared = prepared_request(tmp_path)
    configure(
        prepared,
        stdout_lines=[
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "command_execution",
                        "status": "failed",
                        "exit_code": 1,
                        "aggregated_output": "operation not permitted",
                    },
                }
            )
        ],
        final="failed",
        exit_code=1,
    )

    result = run_fake(prepared)

    assert result.status == "permission_blocked"
    assert result.permission_evidence


def test_invalid_jsonl_utf8_is_hashed_and_classified_malformed(tmp_path: Path) -> None:
    prepared = prepared_request(tmp_path)
    malformed_line = b'{"type":"note","text":"\xff"}'
    configure(
        prepared,
        stdout_hex=(malformed_line + b"\n").hex(),
        final_hex=b"answer \xff".hex(),
    )

    result = run_fake(prepared)
    directory = Path(result.artifact_directory)
    metadata = json.loads((directory / "metadata.json").read_text())

    assert result.status == "malformed_event_stream"
    assert result.exit_code == 0
    assert result.event_count == 0
    assert result.malformed_event_count == 1
    assert (directory / "events.jsonl").read_bytes() == b""
    assert metadata["malformed_event_sha256"] == [hashlib.sha256(malformed_line).hexdigest()]
    assert "\N{REPLACEMENT CHARACTER}" in (directory / "final-message.md").read_text()


def test_escaped_lone_surrogate_event_is_ascii_canonicalized(tmp_path: Path) -> None:
    prepared = prepared_request(tmp_path)
    configure(
        prepared,
        stdout_lines=[r'{"type":"note","text":"\ud800"}'],
        final="done",
    )

    result = run_fake(prepared)
    directory = Path(result.artifact_directory)
    metadata = json.loads((directory / "metadata.json").read_text())
    persisted = json.loads((directory / "result.json").read_text())

    assert result.status == "succeeded"
    assert result.exit_code == 0
    assert result.event_count == 1
    assert result.malformed_event_count == 0
    assert (directory / "events.jsonl").read_bytes() == (b'{"text":"\\ud800","type":"note"}\n')
    assert metadata["valid_event_count"] == 1
    assert metadata["malformed_event_count"] == 0
    assert persisted == result.to_dict()
    assert {path.name for path in directory.iterdir()} >= ARTIFACT_NAMES


def test_event_processing_exception_becomes_malformed_with_complete_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from research_automation_supervisor import codex_adapter

    prepared = prepared_request(tmp_path)
    line = b'{"force_processing_failure":true,"type":"note"}'
    configure(prepared, stdout_hex=(line + b"\n").hex(), final="done")
    original_redact_json = codex_adapter.redact_json

    def injected_redaction(value: object, sensitive_values: object = ()) -> object:
        if isinstance(value, dict) and value.get("force_processing_failure") is True:
            raise UnicodeEncodeError("utf-8", "\ud800", 0, 1, "injected event failure")
        return original_redact_json(value, sensitive_values)  # type: ignore[arg-type]

    monkeypatch.setattr(codex_adapter, "redact_json", injected_redaction)

    result = run_fake(prepared)
    directory = Path(result.artifact_directory)
    metadata = json.loads((directory / "metadata.json").read_text())
    persisted = json.loads((directory / "result.json").read_text())

    assert result.status == "malformed_event_stream"
    assert result.exit_code == 0
    assert result.event_count == 0
    assert result.malformed_event_count == 1
    assert (directory / "events.jsonl").read_bytes() == b""
    assert metadata["valid_event_count"] == 0
    assert metadata["malformed_event_count"] == 1
    assert metadata["malformed_event_sha256"] == [hashlib.sha256(line).hexdigest()]
    assert persisted == result.to_dict()
    assert {path.name for path in directory.iterdir()} >= ARTIFACT_NAMES

    monkeypatch.setattr(
        "research_automation_supervisor.cli.execute_codex_request",
        lambda path, *, runs_dir: result,
    )
    invocation = CliRunner().invoke(app, ["run-codex", "request.yaml", "--json"])
    assert invocation.exit_code == 7
    assert json.loads(invocation.stdout) == persisted
    assert "Traceback" not in invocation.stdout


@pytest.mark.parametrize("early_behavior", ["close_stdin_early", "skip_stdin"])
def test_incomplete_stdin_and_broken_pipe_are_normalized(
    tmp_path: Path, early_behavior: str
) -> None:
    prepared = prepared_request(tmp_path, prompt_bytes=b"x" * (1024 * 1024))
    configure(
        prepared,
        stdout_lines=['{"type":"turn.completed"}'],
        final="done",
        **{early_behavior: True},
    )

    result = run_fake(prepared)
    observation = json.loads((prepared.workspace / ".fake-codex-observation.json").read_text())

    assert base64.b64decode(observation["prompt_base64"]) == b""
    assert result.status == "process_failed"
    assert result.exit_code == 0
    assert (
        json.loads(
            (
                prepared.request_path.parent / "runs" / prepared.request.run_id / "result.json"
            ).read_text()
        )
        == result.to_dict()
    )


def test_redaction_and_malformed_hashes_prevent_raw_secret_retention(tmp_path: Path) -> None:
    secret = "SENSITIVE_REMOVED_VALUE_123"
    prompt = "One exact human-written prompt."
    prepared = prepared_request(tmp_path)
    malformed = f"malformed-{secret}"
    configure(
        prepared,
        stdout_lines=[
            json.dumps(
                {
                    "type": "turn.failed",
                    "payload": {"api_key": secret, "text": f"seen {secret}"},
                }
            ),
            malformed,
        ],
        stderr=f"{prompt}\ntoken={secret}\n",
        final=f"Final with {secret}",
        exit_code=1,
    )

    result = run_fake(prepared, environ=fake_environment(DEMO_TOKEN=secret))
    directory = Path(result.artifact_directory)
    metadata = json.loads((directory / "metadata.json").read_text())

    assert result.status == "malformed_event_stream"
    assert metadata["malformed_event_sha256"] == [hashlib.sha256(malformed.encode()).hexdigest()]
    assert "DEMO_TOKEN" in metadata["removed_environment_variable_names"]
    observation = json.loads((prepared.workspace / ".fake-codex-observation.json").read_text())
    assert "DEMO_TOKEN" not in observation["environment"]
    assert prompt not in (directory / "stderr.log").read_text()
    for artifact in directory.iterdir():
        assert secret not in artifact.read_text(encoding="utf-8")
        assert malformed not in artifact.read_text(encoding="utf-8")


def test_one_sanitized_result_is_persisted_returned_and_rendered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = prepared_request(tmp_path)
    secret = "prefix<REDACTED>suffix"
    configure(
        prepared,
        stdout_lines=[json.dumps({"type": "note", "message": secret})],
        stderr=f"diagnostic {secret}",
        final=f"final {secret}",
    )

    result = run_fake(prepared, environ=fake_environment(DEMO_TOKEN=secret))
    raw_directory = (prepared.request_path.parent / "runs" / prepared.request.run_id).resolve()
    persisted = json.loads((raw_directory / "result.json").read_text())

    assert result.run_id == prepared.request.run_id
    assert Path(result.artifact_directory) == raw_directory
    assert Path(result.artifact_directory).exists()
    assert (Path(result.artifact_directory) / "result.json").exists()
    assert persisted == result.to_dict()
    assert secret not in json.dumps(result.to_dict())
    for artifact in raw_directory.iterdir():
        assert secret not in artifact.read_text(encoding="utf-8")

    monkeypatch.setattr(
        "research_automation_supervisor.cli.execute_codex_request",
        lambda path, *, runs_dir: result,
    )
    human = CliRunner().invoke(app, ["run-codex", "request.yaml"])
    machine = CliRunner().invoke(app, ["run-codex", "request.yaml", "--json"])
    assert human.exit_code == 0
    assert machine.exit_code == 0
    assert secret not in human.stdout
    assert secret not in machine.stdout
    assert json.loads(machine.stdout) == persisted
    assert _format_codex_result(result) in human.stdout


def test_every_request_locator_collision_is_rejected_before_directory_creation(
    tmp_path: Path,
) -> None:
    prepared = prepared_request(tmp_path)
    runs_dir = tmp_path / "runs"
    artifact_directory = runs_dir.resolve() / prepared.request.run_id
    locator_values = (
        str(prepared.request_path),
        prepared.request.run_id,
        prepared.request.model,
        str(prepared.workspace),
        str(prepared.prompt_path),
        str(runs_dir.resolve()),
        str(artifact_directory),
    )

    for sensitive_value in locator_values:
        with pytest.raises(
            CodexConfidentialityError,
            match="structural redaction collision",
        ) as captured:
            run_prepared_codex(
                prepared,
                runs_dir=runs_dir,
                codex_executable=str(FAKE_CODEX),
                environ=fake_environment(DEMO_TOKEN=sensitive_value),
            )
        assert sensitive_value not in str(captured.value)
        assert not runs_dir.exists()


def test_run_id_sensitive_collision_is_rejected_by_run_cli_without_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = prepared_request(tmp_path)
    secret = prepared.request.run_id
    monkeypatch.setenv("DEMO_TOKEN", secret)

    def execute_with_collision(path: Path, *, runs_dir: Path) -> CodexRunResult:
        return run_prepared_codex(
            prepared,
            runs_dir=runs_dir,
            codex_executable=str(FAKE_CODEX),
            environ=fake_environment(DEMO_TOKEN=secret),
        )

    monkeypatch.setattr(
        "research_automation_supervisor.cli.execute_codex_request",
        execute_with_collision,
    )

    for arguments in ([], ["--json"]):
        runs_dir = tmp_path / f"cli-runs-{len(arguments)}"
        invocation = CliRunner().invoke(
            app,
            [
                "run-codex",
                str(prepared.request_path),
                "--runs-dir",
                str(runs_dir),
                *arguments,
            ],
        )

        rendered = invocation.stdout + invocation.stderr
        assert invocation.exit_code == 2
        assert secret not in rendered
        assert str(prepared.request_path) not in rendered
        assert "structural redaction collision" in rendered
        assert not runs_dir.exists()


def test_validate_cli_rejects_confidentiality_collision_in_human_and_json_modes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = prepared_request(tmp_path)
    secret = prepared.request.run_id
    monkeypatch.setenv("DEMO_TOKEN", secret)
    monkeypatch.setattr(
        "research_automation_supervisor.cli.load_codex_request",
        lambda path: prepared,
    )

    human = CliRunner().invoke(
        app,
        ["validate-codex-request", str(prepared.request_path)],
    )
    machine = CliRunner().invoke(
        app,
        ["validate-codex-request", str(prepared.request_path), "--json"],
    )

    assert human.exit_code == 2
    assert machine.exit_code == 2
    assert secret not in human.output
    assert secret not in machine.stdout
    assert str(prepared.request_path) not in human.output
    assert str(prepared.request_path) not in machine.stdout
    assert json.loads(machine.stdout) == {
        "error": "Codex request contains a structural redaction collision",
        "error_kind": "input",
        "ok": False,
        "path": "<REDACTED>",
    }


@pytest.mark.parametrize(
    ("field", "token"),
    [
        ("run_id", "sk-AUDIT_TOKEN_SHOULD_REDACT"),
        ("model", "ghp_AUDIT_TOKEN_SHOULD_REDACT"),
        ("request_path", "github_pat_AUDIT_TOKEN_SHOULD_REDACT"),
        ("workspace", "xoxb-AUDIT-TOKEN"),
        ("prompt_path", "xoxp-AUDIT-TOKEN"),
    ],
)
def test_validate_cli_rejects_builtin_token_structures_in_human_and_json_modes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    token: str,
) -> None:
    request_path = structural_request_path(tmp_path, field, token)
    monkeypatch.setattr(
        "research_automation_supervisor.codex_models._default_git_worktree_checker",
        lambda: lambda workspace: True,
    )

    human = CliRunner().invoke(app, ["validate-codex-request", str(request_path)])
    machine = CliRunner().invoke(
        app,
        ["validate-codex-request", str(request_path), "--json"],
    )

    for invocation in (human, machine):
        rendered = invocation.stdout + invocation.stderr
        assert invocation.exit_code == 2
        assert token not in rendered
        assert str(request_path) not in rendered
        assert "structural redaction collision" in rendered
        assert "Traceback" not in rendered
    assert json.loads(machine.stdout) == {
        "error": "Codex request contains a structural redaction collision",
        "error_kind": "input",
        "ok": False,
        "path": "<REDACTED>",
    }
    assert not (tmp_path / "runs").exists()


def test_service_rejects_token_shaped_request_path_before_loading_or_writing(
    tmp_path: Path,
) -> None:
    token = "github_pat_AUDIT_TOKEN_SHOULD_REDACT"
    request_path = tmp_path / f"missing-{token}.yaml"
    runs_dir = tmp_path / "service-runs"

    with pytest.raises(
        CodexConfidentialityError,
        match="structural redaction collision",
    ) as captured:
        execute_codex_request(
            request_path,
            runs_dir=runs_dir,
            codex_executable=str(FAKE_CODEX),
            environ=fake_environment(),
            git_worktree_checker=lambda workspace: True,
        )

    assert token not in str(captured.value)
    assert str(request_path) not in str(captured.value)
    assert not runs_dir.exists()


@pytest.mark.parametrize(
    "structural_value",
    [
        "Authorization: Bearer AUDIT_TOKEN_SHOULD_REDACT",
        "sk-AUDIT_TOKEN_SHOULD_REDACT",
        "ghp_AUDIT_TOKEN_SHOULD_REDACT",
        "github_pat_AUDIT_TOKEN_SHOULD_REDACT",
        "xoxb-AUDIT-TOKEN",
        "xoxp-AUDIT-TOKEN",
        "token=AUDIT_ASSIGNMENT_SHOULD_REDACT",
    ],
)
def test_structural_preflight_cannot_diverge_from_complete_redaction_policy(
    structural_value: str,
) -> None:
    assert redact_text(structural_value) != structural_value

    with pytest.raises(CodexConfidentialityError, match="structural redaction collision"):
        validate_locator_confidentiality((structural_value,), ())


@pytest.mark.parametrize(
    ("structural_field", "token"),
    [
        ("run_id", "sk-AUDIT_TOKEN_SHOULD_REDACT"),
        ("model", "ghp_AUDIT_TOKEN_SHOULD_REDACT"),
        ("request_path", "github_pat_AUDIT_TOKEN_SHOULD_REDACT"),
        ("workspace", "xoxb-AUDIT-TOKEN"),
        ("prompt_path", "xoxp-AUDIT-TOKEN"),
        ("runs_directory", "xoxb-AUDIT-TOKEN"),
        ("runs_directory", "Authorization: Bearer AUDIT_TOKEN_SHOULD_REDACT"),
        ("artifact_directory", "sk-AUDIT_ARTIFACT_SHOULD_REDACT"),
        ("assignment_runs_directory", "token=AUDIT_ASSIGNMENT_SHOULD_REDACT"),
    ],
)
def test_run_cli_rejects_complete_policy_structural_collisions_without_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    structural_field: str,
    token: str,
) -> None:
    request_field = "run_id" if structural_field == "artifact_directory" else structural_field
    if request_field in {"run_id", "model", "request_path", "workspace", "prompt_path"}:
        request_path = structural_request_path(tmp_path, request_field, token)
    else:
        request_path = structural_request_path(tmp_path, "ordinary", token)
    request_run_id = token if request_field == "run_id" else "worker-run"

    def execute_with_fake(path: Path, *, runs_dir: Path) -> CodexRunResult:
        return execute_codex_request(
            path,
            runs_dir=runs_dir,
            codex_executable=str(FAKE_CODEX),
            environ=fake_environment(),
            git_worktree_checker=lambda workspace: True,
        )

    monkeypatch.setattr(
        "research_automation_supervisor.cli.execute_codex_request",
        execute_with_fake,
    )

    for mode_arguments, mode_name in (([], "human"), (["--json"], "json")):
        if structural_field in {"runs_directory", "assignment_runs_directory"}:
            runs_dir = tmp_path / f"{token}-{mode_name}"
        else:
            runs_dir = tmp_path / f"runs-{mode_name}"
        artifact_directory = runs_dir.resolve() / request_run_id
        invocation = CliRunner().invoke(
            app,
            [
                "run-codex",
                str(request_path),
                "--runs-dir",
                str(runs_dir),
                *mode_arguments,
            ],
        )

        rendered = invocation.stdout + invocation.stderr
        assert invocation.exit_code == 2
        assert token not in rendered
        assert str(request_path) not in rendered
        assert str(runs_dir) not in rendered
        assert "structural redaction collision" in rendered
        assert "Traceback" not in rendered
        assert not runs_dir.exists()
        assert not artifact_directory.exists()
        if mode_arguments:
            assert json.loads(invocation.stdout) == {
                "error": "Codex request contains a structural redaction collision",
                "error_kind": "input",
                "ok": False,
                "path": "<REDACTED>",
            }


def test_existing_run_directory_collision_is_refused(tmp_path: Path) -> None:
    prepared = prepared_request(tmp_path)
    configure(prepared, stdout_lines=['{"type":"turn.completed"}'], final="done")
    first = run_fake(prepared)
    original = (Path(first.artifact_directory) / "result.json").read_bytes()

    with pytest.raises(CodexRequestError, match="already exists"):
        run_fake(prepared)

    assert (Path(first.artifact_directory) / "result.json").read_bytes() == original


def test_executable_discovery_and_request_loading_are_injected(tmp_path: Path) -> None:
    prepared = prepared_request(tmp_path)
    configure(prepared, stdout_lines=['{"type":"turn.completed"}'], final="done")

    result = execute_codex_request(
        prepared.request_path,
        runs_dir=tmp_path / "discovered-runs",
        which=lambda name: str(FAKE_CODEX) if name == "codex" else None,
        environ=fake_environment(),
        git_worktree_checker=lambda workspace: workspace == prepared.workspace,
    )

    assert result.status == "succeeded"

    with pytest.raises(CodexDependencyError, match="Codex executable is required"):
        execute_codex_request(
            prepared.request_path,
            runs_dir=tmp_path / "missing-runs",
            which=lambda _: None,
            git_worktree_checker=lambda _: True,
        )

    with pytest.raises(CodexDependencyError, match="0.144.0 or newer"):
        execute_codex_request(
            prepared.request_path,
            runs_dir=tmp_path / "old-version-runs",
            codex_executable=str(FAKE_CODEX),
            environ=fake_environment(),
            git_worktree_checker=lambda _: True,
            version_probe=lambda executable, environment, workspace: "0.143.9",
        )
    assert not (tmp_path / "old-version-runs").exists()


@pytest.mark.parametrize(
    ("status", "expected_exit"),
    [
        ("succeeded", 0),
        ("launch_failed", 4),
        ("timed_out", 5),
        ("output_limit_exceeded", 7),
        ("permission_blocked", 6),
        ("malformed_event_stream", 7),
        ("process_failed", 4),
        ("missing_final_message", 7),
    ],
)
def test_run_codex_cli_json_agrees_with_result_and_exit_code(
    monkeypatch: pytest.MonkeyPatch, status: RunStatus, expected_exit: int
) -> None:
    result = _result(status)
    monkeypatch.setattr(
        "research_automation_supervisor.cli.execute_codex_request",
        lambda path, *, runs_dir: result,
    )

    invocation = CliRunner().invoke(app, ["run-codex", "request.yaml", "--json"])

    assert invocation.exit_code == expected_exit
    assert json.loads(invocation.stdout) == result.to_dict()
    assert "Traceback" not in invocation.stdout


def test_run_codex_cli_human_output_and_expected_input_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    succeeded = _result("succeeded")
    monkeypatch.setattr(
        "research_automation_supervisor.cli.execute_codex_request",
        lambda path, *, runs_dir: succeeded,
    )
    invocation = CliRunner().invoke(app, ["run-codex", "request.yaml"])
    assert invocation.exit_code == 0
    assert "Status: succeeded" in invocation.stdout
    assert "Artifacts: /runs/run-1" in invocation.stdout

    def invalid(path: Path, *, runs_dir: Path) -> CodexRunResult:
        raise CodexRequestError("request rejected")

    monkeypatch.setattr("research_automation_supervisor.cli.execute_codex_request", invalid)
    invalid_invocation = CliRunner().invoke(app, ["run-codex", "request.yaml", "--json"])
    assert invalid_invocation.exit_code == 2
    assert json.loads(invalid_invocation.stdout)["error_kind"] == "input"
    assert "Traceback" not in invalid_invocation.stdout


def test_validate_codex_request_cli_is_read_only_and_stable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = prepared_request(tmp_path)
    before = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    monkeypatch.setattr(
        "research_automation_supervisor.cli.load_codex_request",
        lambda path: prepared,
    )

    human = CliRunner().invoke(app, ["validate-codex-request", str(prepared.request_path)])
    invocation = CliRunner().invoke(
        app, ["validate-codex-request", str(prepared.request_path), "--json"]
    )

    assert human.exit_code == 0
    assert (
        f"Valid Codex request {prepared.request.run_id} "
        f"({prepared.request.role}): {prepared.request_path}"
    ) in human.stdout
    assert invocation.exit_code == 0
    assert json.loads(invocation.stdout) == {
        "ok": True,
        "path": str(prepared.request_path),
        "request": prepared.normalized_dict(),
    }
    after = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    assert after == before


@pytest.mark.parametrize(
    ("raised", "expected_exit", "kind"),
    [
        (CodexRequestError("invalid input"), 2, "input"),
        (CodexDependencyError("missing Codex"), 3, "dependency"),
        (RuntimeError("internal detail that must not render"), 1, "internal"),
    ],
)
def test_stage1_cli_expected_and_internal_errors_have_no_traceback(
    monkeypatch: pytest.MonkeyPatch,
    raised: Exception,
    expected_exit: int,
    kind: str,
) -> None:
    def fail(path: Path, *, runs_dir: Path) -> CodexRunResult:
        raise raised

    monkeypatch.setattr("research_automation_supervisor.cli.execute_codex_request", fail)

    invocation = CliRunner().invoke(app, ["run-codex", "request.yaml", "--json"])

    assert invocation.exit_code == expected_exit
    payload = json.loads(invocation.stdout)
    assert payload["error_kind"] == kind
    assert "Traceback" not in invocation.stdout
    assert "internal detail" not in invocation.stdout


def test_stage1_cli_expected_errors_redact_sensitive_environment_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "CLI_ERROR_SECRET_VALUE_123"
    monkeypatch.setenv("DEMO_TOKEN", secret)

    def fail(path: Path, *, runs_dir: Path) -> CodexRunResult:
        raise CodexRequestError(f"request contained {secret}")

    monkeypatch.setattr("research_automation_supervisor.cli.execute_codex_request", fail)

    invocation = CliRunner().invoke(app, ["run-codex", "request.yaml", "--json"])

    assert invocation.exit_code == 2
    assert secret not in invocation.stdout
    assert "<REDACTED>" in invocation.stdout


def _result(status: RunStatus) -> CodexRunResult:
    return CodexRunResult(
        run_id="run-1",
        status=status,
        exit_code=0 if status == "succeeded" else 1,
        started_at="2026-01-01T00:00:00.000000Z",
        ended_at="2026-01-01T00:00:01.000000Z",
        duration_seconds=1.0,
        artifact_directory="/runs/run-1",
        event_count=1,
        malformed_event_count=0,
        final_message_present=status not in {"launch_failed", "missing_final_message"},
        permission_evidence=status == "permission_blocked",
        summary=f"status {status}",
        error=None if status == "succeeded" else f"status {status}",
    )


def _process_is_live(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    stat_path = Path(f"/proc/{pid}/stat")
    if stat_path.exists():
        state = stat_path.read_text(encoding="ascii").split()[2]
        return state != "Z"
    return True
