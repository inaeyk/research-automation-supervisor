#!/usr/bin/env python3
"""Offline fake for exercising the Stage 1 Codex process boundary."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import time
from pathlib import Path


def main() -> int:
    if sys.argv[1:] == ["--version"]:
        print("codex-cli 0.200.0")
        return 0

    workspace = Path.cwd()
    configuration_path = workspace / ".fake-codex.json"
    configuration = (
        json.loads(configuration_path.read_text(encoding="utf-8"))
        if configuration_path.exists()
        else {}
    )
    prompt = sys.stdin.buffer.read()
    observation = {
        "argv": sys.argv[1:],
        "cwd": str(workspace),
        "prompt_base64": base64.b64encode(prompt).decode("ascii"),
        "environment": dict(os.environ),
    }
    (workspace / ".fake-codex-observation.json").write_text(
        json.dumps(observation, sort_keys=True),
        encoding="utf-8",
    )

    child_sleep = configuration.get("spawn_child_sleep")
    if isinstance(child_sleep, (int, float)):
        child_source = f"import time; time.sleep({float(child_sleep)!r})"
        if configuration.get("child_ignore_term"):
            child_source = (
                "import signal, time; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                f"time.sleep({float(child_sleep)!r})"
            )
        child = subprocess.Popen(
            [sys.executable, "-c", child_source],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        (workspace / ".fake-codex-child.pid").write_text(str(child.pid), encoding="ascii")

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
