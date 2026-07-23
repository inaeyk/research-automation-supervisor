#!/usr/bin/env python3
"""Offline fake for exercising the Stage 1/2/3 Codex process boundary."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import UUID


def _canonical_non_nil_uuid(value: str) -> bool:
    try:
        parsed = UUID(value)
    except ValueError:
        return False
    return parsed.int != 0 and str(parsed) == value


def _validate_exec_arguments(
    arguments: list[str],
    *,
    uuid_resume_required: bool = False,
) -> str | None:
    """Reject the real parser defect this fake is intended to detect."""
    try:
        exec_index = arguments.index("exec")
    except ValueError:
        return "exec subcommand is required"
    if arguments[:2] != ["--ask-for-approval", "never"]:
        return "approval policy must be a global option before exec"
    if "--ask-for-approval" in arguments[exec_index + 1 :]:
        return "approval policy is not accepted after exec"
    if exec_index + 1 < len(arguments) and arguments[exec_index + 1] == "resume":
        if exec_index + 2 >= len(arguments):
            return "resume requires one explicit thread ID"
        thread_id = arguments[exec_index + 2]
        if not thread_id or thread_id in {"--last", "--all", "-"}:
            return "resume requires one explicit thread ID"
        if uuid_resume_required and not _canonical_non_nil_uuid(thread_id):
            return "Stage 3 resume requires one canonical UUID"
    if "--last" in arguments or "--all" in arguments:
        return "recency-based resume is forbidden"
    return None


def _select_configuration(configuration: dict[str, object]) -> tuple[dict[str, object], int]:
    selected = dict(configuration)
    responses = configuration.get("responses")
    if not isinstance(responses, list):
        return selected, 0
    counter_path_value = configuration.get("counter_path", ".fake-codex-counter")
    counter_path = Path(str(counter_path_value))
    try:
        call_index = int(counter_path.read_text(encoding="ascii"))
    except (FileNotFoundError, ValueError):
        call_index = 0
    counter_path.parent.mkdir(parents=True, exist_ok=True)
    counter_path.write_text(str(call_index + 1), encoding="ascii")
    if call_index >= len(responses) or not isinstance(responses[call_index], dict):
        selected.update(
            {
                "stderr": "fake Codex response queue exhausted",
                "exit_code": 71,
                "write_final": False,
                "stdout_lines": [],
            }
        )
    else:
        selected.update(responses[call_index])
    return selected, call_index


def _validate_stage2_policy(arguments: list[str], configuration: dict[str, object]) -> str | None:
    if not (
        configuration.get("require_stage2_policy")
        or configuration.get("require_stage3_policy")
    ):
        return None
    required_pairs = (
        ("--ask-for-approval", "never"),
        ("--sandbox", str(configuration.get("expected_sandbox", "workspace-write"))),
        ("-c", 'web_search="disabled"'),
        ("-c", "sandbox_workspace_write.network_access=false"),
        ("-c", "features.skill_mcp_dependency_install=false"),
    )
    for option, value in required_pairs:
        if not any(
            arguments[index : index + 2] == [option, value]
            for index in range(max(0, len(arguments) - 1))
        ):
            return f"missing required Stage 2 policy {option} {value}"
    for flag in ("--json", "--ignore-user-config", "--ignore-rules", "--strict-config"):
        if flag not in arguments:
            return f"missing required Stage 2 flag {flag}"
    if "--output-schema" not in arguments:
        return "Stage 2 output schema is required"
    schema_index = arguments.index("--output-schema")
    if schema_index + 1 >= len(arguments) or not Path(arguments[schema_index + 1]).is_file():
        return "Stage 2 output schema path is invalid"
    expected_ephemeral = bool(configuration.get("expected_ephemeral", False))
    if ("--ephemeral" in arguments) != expected_ephemeral:
        return "Stage 2 ephemeral policy mismatch"
    return None


def main() -> int:
    if sys.argv[1:] == ["--version"]:
        print("codex-cli 0.200.0")
        return 0

    workspace = Path.cwd()
    configuration_path = workspace / ".fake-codex.json"
    base_configuration = (
        json.loads(configuration_path.read_text(encoding="utf-8"))
        if configuration_path.exists()
        else {}
    )
    configuration, call_index = _select_configuration(base_configuration)
    parser_error = _validate_exec_arguments(
        sys.argv[1:],
        uuid_resume_required=bool(
            configuration.get("require_stage3_policy")
        ),
    )
    if parser_error is not None:
        print(f"fake codex parser error: {parser_error}", file=sys.stderr)
        return 2
    if "--help" in sys.argv[1:]:
        print("fake codex exec help")
        return 0
    policy_error = _validate_stage2_policy(sys.argv[1:], configuration)
    if policy_error is not None:
        print(f"fake codex policy error: {policy_error}", file=sys.stderr)
        return 2
    arguments = sys.argv[1:]
    if "resume" in arguments:
        resume_index = arguments.index("resume")
        resumed_thread = arguments[resume_index + 1]
        expected_thread = configuration.get("expected_resume_thread_id")
        if isinstance(expected_thread, str) and resumed_thread != expected_thread:
            print("fake codex thread unavailable", file=sys.stderr)
            return 72
    if configuration.get("close_stdin_early"):
        sys.stdin.close()
        prompt = b""
        time.sleep(0.05)
    elif configuration.get("skip_stdin"):
        prompt = b""
    else:
        prompt = sys.stdin.buffer.read()
    observation = {
        "argv": sys.argv[1:],
        "cwd": str(workspace),
        "prompt_base64": base64.b64encode(prompt).decode("ascii"),
        "environment": dict(os.environ),
        "call_index": call_index,
    }
    observation_path = Path(
        str(configuration.get("observation_path", workspace / ".fake-codex-observation.json"))
    )
    observation_path.parent.mkdir(parents=True, exist_ok=True)
    observation_path.write_text(
        json.dumps(observation, sort_keys=True),
        encoding="utf-8",
    )

    write_files = configuration.get("write_files", {})
    if isinstance(write_files, dict):
        for relative, content in write_files.items():
            destination = workspace / str(relative)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(str(content), encoding="utf-8")
    delete_files = configuration.get("delete_files", [])
    if isinstance(delete_files, list):
        for relative in delete_files:
            destination = workspace / str(relative)
            if destination.is_file() or destination.is_symlink():
                destination.unlink()

    child_sleep = configuration.get("spawn_child_sleep")
    if isinstance(child_sleep, (int, float)):
        ready_path = workspace / ".fake-codex-child.ready"
        child_source = (
            "import pathlib, time; "
            f"pathlib.Path({str(ready_path)!r}).write_text('ready', encoding='ascii'); "
            f"time.sleep({float(child_sleep)!r})"
        )
        if configuration.get("child_ignore_term"):
            child_source = (
                "import pathlib, signal, time; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                f"pathlib.Path({str(ready_path)!r}).write_text('ready', encoding='ascii'); "
                f"time.sleep({float(child_sleep)!r})"
            )
        child = subprocess.Popen(
            [sys.executable, "-c", child_source],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        (workspace / ".fake-codex-child.pid").write_text(str(child.pid), encoding="ascii")
        ready_deadline = time.monotonic() + 2.0
        while (
            not ready_path.exists()
            and child.poll() is None
            and time.monotonic() < ready_deadline
        ):
            time.sleep(0.005)
        if not ready_path.exists():
            print("fake child did not become ready", file=sys.stderr)
            return 70

    sleep_seconds = configuration.get("sleep_seconds", 0)
    if isinstance(sleep_seconds, (int, float)) and sleep_seconds:
        time.sleep(float(sleep_seconds))

    stdout_hex = configuration.get("stdout_hex")
    if isinstance(stdout_hex, str):
        sys.stdout.buffer.write(bytes.fromhex(stdout_hex))
    else:
        for line in configuration.get("stdout_lines", []):
            sys.stdout.buffer.write(str(line).encode("utf-8") + b"\n")
    stdout_repeat = configuration.get("stdout_repeat")
    if isinstance(stdout_repeat, int) and stdout_repeat > 0:
        sys.stdout.buffer.write(b"x" * stdout_repeat)
    sys.stdout.buffer.flush()

    stderr_hex = configuration.get("stderr_hex")
    if isinstance(stderr_hex, str):
        sys.stderr.buffer.write(bytes.fromhex(stderr_hex))
    else:
        sys.stderr.write(str(configuration.get("stderr", "")))
    stderr_repeat = configuration.get("stderr_repeat")
    if isinstance(stderr_repeat, int) and stderr_repeat > 0:
        sys.stderr.buffer.write(b"y" * stderr_repeat)
    sys.stderr.buffer.flush()

    if "--output-last-message" in sys.argv and configuration.get("write_final", True):
        index = sys.argv.index("--output-last-message")
        final_path = Path(sys.argv[index + 1])
        final_hex = configuration.get("final_hex")
        if isinstance(final_hex, str):
            final_path.write_bytes(bytes.fromhex(final_hex))
        else:
            final_path.write_text(str(configuration.get("final", "done")), encoding="utf-8")
    return int(configuration.get("exit_code", 0))


if __name__ == "__main__":
    raise SystemExit(main())
