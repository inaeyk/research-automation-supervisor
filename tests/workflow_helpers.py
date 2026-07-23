from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml


def git(project: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(project), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def initialize_repository(project: Path) -> None:
    git(project, "init", "-q")
    git(project, "config", "user.email", "tests@example.invalid")
    git(project, "config", "user.name", "Stage 2 Tests")
    git(project, "add", ".")
    git(project, "commit", "-q", "-m", "baseline")


def worker_result(status: str = "completed") -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "status": status,
            "summary": f"worker {status}",
            "changed_files": [],
            "assumptions": [],
            "questions": [],
        },
        sort_keys=True,
    )


def auditor_result(verdict: str = "pass") -> str:
    findings = []
    scope_compliant = True
    contract_satisfied = True
    if verdict == "fail_repairable":
        findings = [
            {
                "id": "finding-1",
                "severity": "medium",
                "category": "correctness",
                "file": "src/output.txt",
                "line": 1,
                "evidence": "repair required",
                "required_fix": "repair it",
            }
        ]
        contract_satisfied = False
    return json.dumps(
        {
            "schema_version": 1,
            "verdict": verdict,
            "summary": f"auditor {verdict}",
            "scope_compliant": scope_compliant,
            "contract_satisfied": contract_satisfied,
            "findings": findings,
            "human_questions": [],
        },
        sort_keys=True,
    )


def codex_response(
    role: str,
    thread_id: str,
    final: str,
    **extra: object,
) -> dict[str, object]:
    response: dict[str, object] = {
        "require_stage2_policy": True,
        "expected_sandbox": "workspace-write" if role == "worker" else "read-only",
        "expected_ephemeral": role == "auditor",
        "stdout_lines": [json.dumps({"type": "thread.started", "thread_id": thread_id})],
        "final": final,
    }
    response.update(extra)
    return response


def create_workflow_tree(
    tmp_path: Path,
    *,
    responses: list[dict[str, object]] | None = None,
    checkpoint_after: bool = False,
    max_repair_rounds: int = 2,
    test_requires_marker: bool = False,
) -> tuple[Path, Path, Path]:
    project = tmp_path / "project"
    control = project / "control"
    tools = project / "tools"
    source = project / "src"
    config = tmp_path / "config"
    for directory in (control, tools, source, config):
        directory.mkdir(parents=True, exist_ok=True)
    (control / "contract.md").write_text("Frozen contract sentence.\n", encoding="utf-8")
    (control / "worker-initial.md").write_text("Implement the substage.\n", encoding="utf-8")
    (control / "worker-repair.md").write_text("Repair validated failures.\n", encoding="utf-8")
    (control / "auditor.md").write_text("Audit the current workspace.\n", encoding="utf-8")
    test_source = (
        "from pathlib import Path\n"
        "raise SystemExit(0 if Path('src/ready.txt').is_file() else 9)\n"
        if test_requires_marker
        else "raise SystemExit(0)\n"
    )
    (tools / "acceptance.py").write_text(test_source, encoding="utf-8")
    fake_codex = Path(__file__).parent / "fixtures" / "fake_codex.py"
    selected_responses = responses or [
        codex_response("worker", "worker-thread-1", worker_result()),
        codex_response("auditor", "audit-thread-1", auditor_result()),
    ]
    fake_configuration = {
        "counter_path": str(tmp_path / "fake-counter"),
        "observation_path": str(tmp_path / "fake-observation.json"),
        "responses": selected_responses,
    }
    (project / ".fake-codex.json").write_text(
        json.dumps(fake_configuration, sort_keys=True),
        encoding="utf-8",
    )
    initialize_repository(project)
    specification: dict[str, Any] = {
        "schema_version": 1,
        "substage_id": "minimal-substage",
        "title": "Minimal deterministic substage",
        "workspace": "../project",
        "contract_path": "../project/control/contract.md",
        "worker_initial_prompt_path": "../project/control/worker-initial.md",
        "worker_repair_prompt_path": "../project/control/worker-repair.md",
        "auditor_prompt_path": "../project/control/auditor.md",
        "worker_model": "gpt-5.6-sol",
        "worker_reasoning_effort": "high",
        "worker_timeout_seconds": 60,
        "auditor_model": "gpt-5.6-sol",
        "auditor_reasoning_effort": "high",
        "auditor_timeout_seconds": 60,
        "acceptance_tests": [
            {
                "id": "fixed-test",
                "argv": [sys.executable, "tools/acceptance.py"],
                "cwd": "../project",
                "timeout_seconds": 5,
                "max_stdout_bytes": 4096,
                "max_stderr_bytes": 4096,
            }
        ],
        "allowed_paths": ["src/**"],
        "protected_paths": ["control/**", "tools/**", ".fake-codex.json"],
        "max_repair_rounds": max_repair_rounds,
        "checkpoint_after": checkpoint_after,
    }
    spec_path = config / "substage.yaml"
    spec_path.write_text(yaml.safe_dump(specification, sort_keys=False), encoding="utf-8")
    return spec_path, project, fake_codex
