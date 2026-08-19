from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

INSTALLED_WRAPPER = Path(
    os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))
) / "bin" / "codex-task"


@pytest.mark.skipif(not INSTALLED_WRAPPER.is_file(), reason="global wrapper not installed")
def test_global_wrapper_run_resume_and_authoritative_aggregate(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_codex = fake_bin / "codex"
    fake_codex.write_text(
        """#!/usr/bin/env python3
import json
import pathlib
import sys

if sys.argv[1:] == ["--version"]:
    print("codex-cli 9.9.9-test")
    raise SystemExit(0)
args = sys.argv[1:]
resumed = len(args) > 2 and args[0:2] == ["exec", "resume"]
output_index = args.index("--output-last-message") + 1
pathlib.Path(args[output_index]).write_text(
    "resumed final\\n" if resumed else "initial final\\n", encoding="utf-8"
)
usage = {
    "input_tokens": 20 if resumed else 10,
    "cached_input_tokens": 5 if resumed else 3,
    "output_tokens": 6 if resumed else 4,
    "reasoning_output_tokens": 3 if resumed else 2,
}
print(json.dumps({"type": "thread.started", "thread_id": "thread-test"}))
print(json.dumps({"type": "turn.completed", "usage": usage}))
""",
        encoding="utf-8",
    )
    fake_codex.chmod(fake_codex.stat().st_mode | stat.S_IXUSR)
    codex_home = tmp_path / "codex-home"
    working_directory = tmp_path / "work"
    working_directory.mkdir()
    first_prompt = tmp_path / "first.txt"
    first_prompt.write_text("first", encoding="utf-8")
    second_prompt = tmp_path / "second.txt"
    second_prompt.write_text("second", encoding="utf-8")
    environment = {
        **os.environ,
        "CODEX_HOME": str(codex_home),
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
    }

    first = subprocess.run(
        [
            str(INSTALLED_WRAPPER),
            "run",
            "task-1",
            str(working_directory),
            str(first_prompt),
            "--model",
            "fake-model",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    resumed = subprocess.run(
        [str(INSTALLED_WRAPPER), "resume", "task-1", str(second_prompt)],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )

    task_dir = codex_home / "task-ledgers" / "task-1"
    receipt = json.loads((task_dir / "TaskUsageReceipt.json").read_bytes())
    raw_logs = sorted((task_dir / "events").glob("*.jsonl"))
    assert first.stdout.endswith("TaskUsageReceipt.json\n")
    assert resumed.stdout.endswith("TaskUsageReceipt.json\n")
    assert receipt["complete"] is True
    assert receipt["thread_id"] == "thread-test"
    assert receipt["model"] == "fake-model"
    assert receipt["codex_cli_version"] == "codex-cli 9.9.9-test"
    assert receipt["turn_count"] == receipt["usage_event_count"] == 2
    assert receipt["input_tokens"] == 30
    assert receipt["cached_input_tokens"] == 8
    assert receipt["output_tokens"] == 10
    assert receipt["reasoning_output_tokens"] == 5
    assert receipt["combined_tokens"] == 40
    assert len(raw_logs) == 2
    assert [item["sha256"] for item in receipt["source_event_logs"]] == [
        hashlib.sha256(path.read_bytes()).hexdigest() for path in raw_logs
    ]
    assert (task_dir / "final-assistant-message.md").read_text(encoding="utf-8") == (
        "resumed final\n"
    )
