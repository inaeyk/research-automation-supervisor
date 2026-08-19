from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

WRAPPER = Path("/home/inaeyk/.codex/bin/codex-task")


def _fake_codex(path: Path) -> None:
    path.write_text(
        """#!/usr/bin/env python3
import json
import sys
from pathlib import Path

if sys.argv[1:] == ["--version"]:
    print("codex-cli 9.9.9-test")
    raise SystemExit(0)
arguments = sys.argv[1:]
assert arguments[0] == "exec"
assert "--json" in arguments
assert sys.stdin.buffer.read()
counter = Path.cwd() / ".fake-wrapper-count"
try:
    index = int(counter.read_text(encoding="ascii"))
except FileNotFoundError:
    index = 0
counter.write_text(str(index + 1), encoding="ascii")
thread = "11111111-1111-4111-8111-111111111111"
if "resume" in arguments:
    resume_index = arguments.index("resume")
    assert arguments[resume_index + 1] == thread
usage = (
    {"input_tokens": 100, "cached_input_tokens": 50, "output_tokens": 20,
     "reasoning_output_tokens": 7}
    if index == 0 else
    {"input_tokens": 30, "cached_input_tokens": 10, "output_tokens": 8,
     "reasoning_output_tokens": 3}
)
print(json.dumps({"type": "thread.started", "thread_id": thread}))
print(json.dumps({"type": "turn.completed", "usage": usage}))
output_index = arguments.index("--output-last-message")
Path(arguments[output_index + 1]).write_text(
    f"assistant-final-{index}\\n", encoding="utf-8"
)
""",
        encoding="utf-8",
    )
    path.chmod(0o755)


def test_global_wrapper_run_and_resume_aggregate_authoritative_events(
    tmp_path: Path,
) -> None:
    codex_home = tmp_path / "codex-home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    fake = tmp_path / "codex"
    _fake_codex(fake)
    first_prompt = tmp_path / "first.md"
    second_prompt = tmp_path / "second.md"
    first_prompt.write_text("first", encoding="utf-8")
    second_prompt.write_text("second", encoding="utf-8")
    environment = {
        **os.environ,
        "CODEX_HOME": str(codex_home),
        "CODEX_TASK_CODEX": str(fake),
    }

    first = subprocess.run(
        [
            str(WRAPPER),
            "run",
            "regression-task",
            str(workspace),
            str(first_prompt),
            "--model",
            "gpt-test",
        ],
        env=environment,
        capture_output=True,
        check=False,
        text=True,
    )
    resumed = subprocess.run(
        [str(WRAPPER), "resume", "regression-task", str(second_prompt)],
        env=environment,
        capture_output=True,
        check=False,
        text=True,
    )

    assert first.returncode == resumed.returncode == 0
    task_root = codex_home / "task-ledgers" / "regression-task"
    receipt = json.loads((task_root / "TaskUsageReceipt.json").read_text())
    state = json.loads((task_root / "task.json").read_text())
    sources = receipt["source_jsonl"]
    assert receipt == {
        "schema_version": 1,
        "receipt_type": "TaskUsageReceipt",
        "task_id": "regression-task",
        "complete": True,
        "incomplete_reasons": [],
        "codex_cli_version": "codex-cli 9.9.9-test",
        "model": "gpt-test",
        "thread_id": "11111111-1111-4111-8111-111111111111",
        "turn_count": 2,
        "input_tokens": 130,
        "cached_input_tokens": 60,
        "output_tokens": 28,
        "reasoning_output_tokens": 10,
        "combined_tokens": 158,
        "source_jsonl": sources,
    }
    assert state["thread_id"] == receipt["thread_id"]
    assert state["turn_count"] == 2
    assert len(sources) == 2
    for source in sources:
        event_path = Path(source["path"])
        assert source["sha256"] == hashlib.sha256(event_path.read_bytes()).hexdigest()
        assert source["completed_turn_count"] == 1
    assert (task_root / "turns" / "000001.final-message.md").read_text() == (
        "assistant-final-0\n"
    )
    assert (task_root / "turns" / "000002.final-message.md").read_text() == (
        "assistant-final-1\n"
    )
    latest = (task_root / "latest-output.md").read_text()
    assert latest.startswith("assistant-final-1\n")
    assert "MACHINE-GENERATED Token usage" in latest
    assert "combined_tokens: 158" in latest
    assert "combined_tokens: 158" in resumed.stdout
