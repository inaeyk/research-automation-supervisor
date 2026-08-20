from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

WRAPPER = Path(__file__).resolve().parents[1] / "scripts" / "codex-task"


def _fake_codex(path: Path) -> None:
    path.write_text(
        """#!/usr/bin/env python3
import json
import sys
from pathlib import Path

if sys.argv[1:] == ["--version"]:
    print("codex-cli 0.147.0-test")
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
(Path.cwd() / f"argv-{index}.json").write_text(json.dumps(arguments), encoding="utf-8")
thread = "11111111-1111-4111-8111-111111111111"
if "resume" in arguments:
    resume_index = arguments.index("resume")
    assert arguments[resume_index + 1] == thread
    for option in ("--sandbox", "--ask-for-approval", "--add-dir", "--model"):
        assert arguments.index(option) < resume_index
    for setting in (
        "model_auto_compact_token_limit=64000",
        "tool_output_token_limit=2048",
        "model_reasoning_effort=high",
    ):
        assert arguments.index(setting) < resume_index
usage = (
    {"input_tokens": 100, "cached_input_tokens": 80,
     "cache_write_input_tokens": 2, "output_tokens": 10,
     "reasoning_output_tokens": 3}
    if index == 0 else
    {"input_tokens": 250, "cached_input_tokens": 210,
     "cache_write_input_tokens": 5, "output_tokens": 20,
     "reasoning_output_tokens": 7}
    if index == 1 else
    {"input_tokens": 400, "cached_input_tokens": 350,
     "cache_write_input_tokens": 9, "output_tokens": 30,
     "reasoning_output_tokens": 11}
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


def test_global_wrapper_run_resume_resume_preserves_policy_and_uses_deltas(
    tmp_path: Path,
) -> None:
    codex_home = tmp_path / "codex-home"
    workspace = tmp_path / "workspace"
    additional = tmp_path / "additional"
    workspace.mkdir()
    additional.mkdir()
    fake = tmp_path / "codex"
    _fake_codex(fake)
    prompts = [tmp_path / f"prompt-{index}.md" for index in range(3)]
    for index, prompt in enumerate(prompts, start=1):
        prompt.write_text(f"reply OK{index}", encoding="utf-8")
    environment = {
        **os.environ,
        "CODEX_HOME": str(codex_home),
        "CODEX_TASK_CODEX": str(fake),
    }
    frozen_options = [
        "--sandbox",
        "workspace-write",
        "--ask-for-approval",
        "never",
        "-c",
        "model_auto_compact_token_limit=64000",
        "-c",
        "tool_output_token_limit=2048",
        "--add-dir",
        str(additional),
        "--model",
        "gpt-test",
        "-c",
        "model_reasoning_effort=high",
    ]

    results = [
        subprocess.run(
            [
                str(WRAPPER),
                "run",
                "regression-task",
                str(workspace),
                str(prompts[0]),
                *frozen_options,
            ],
            env=environment,
            capture_output=True,
            check=False,
            text=True,
        )
    ]
    results.extend(
        subprocess.run(
            [str(WRAPPER), "resume", "regression-task", str(prompt)],
            env=environment,
            capture_output=True,
            check=False,
            text=True,
        )
        for prompt in prompts[1:]
    )

    assert [result.returncode for result in results] == [0, 0, 0]
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
        "codex_cli_version": "codex-cli 0.147.0-test",
        "model": "gpt-test",
        "thread_id": "11111111-1111-4111-8111-111111111111",
        "turn_count": 3,
        "input_tokens": 400,
        "cached_input_tokens": 350,
        "cache_write_input_tokens": 9,
        "output_tokens": 30,
        "reasoning_output_tokens": 11,
        "combined_tokens": 430,
        "source_jsonl": sources,
    }
    assert state["schema_version"] == 2
    assert state["thread_id"] == receipt["thread_id"]
    assert state["turn_count"] == 3
    policy = state["frozen_execution_policy"]
    assert policy["codex_exec_options"] == frozen_options
    assert policy["model"] == "gpt-test"
    policy_core = {key: policy[key] for key in ("schema_version", "codex_exec_options", "model")}
    policy_hash = hashlib.sha256(
        json.dumps(policy_core, sort_keys=True, separators=(",", ":")).encode("ascii")
    ).hexdigest()
    assert policy["sha256"] == policy_hash

    assert [source["usage_delta"]["input_tokens"] for source in sources] == [
        100,
        150,
        150,
    ]
    assert [source["cumulative_usage"]["input_tokens"] for source in sources] == [
        100,
        250,
        400,
    ]
    assert len(sources) == 3
    for source in sources:
        event_path = Path(source["path"])
        assert source["sha256"] == hashlib.sha256(event_path.read_bytes()).hexdigest()
        assert source["completed_turn_count"] == 1

    for index in range(3):
        argv = json.loads((workspace / f"argv-{index}.json").read_text())
        for option in frozen_options:
            assert option in argv
        if index:
            assert argv.index("resume") > max(argv.index(option) for option in frozen_options)
    assert "combined_tokens: 430" in results[-1].stdout

    rejected_override = subprocess.run(
        [
            str(WRAPPER),
            "resume",
            "regression-task",
            str(prompts[-1]),
            "--model",
            "gpt-other",
        ],
        env=environment,
        capture_output=True,
        check=False,
        text=True,
    )
    assert rejected_override.returncode == 64
    assert not (workspace / "argv-3.json").exists()
