from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from research_automation_supervisor.codex_models import (
    MAX_CODEX_TIMEOUT_SECONDS,
    MAX_PROMPT_BYTES,
    MIN_CODEX_TIMEOUT_SECONDS,
    CodexRunResult,
    load_codex_request,
)
from research_automation_supervisor.errors import CodexRequestError


def request_data() -> dict[str, object]:
    return {
        "schema_version": 1,
        "run_id": "worker-001",
        "role": "worker",
        "workspace": "workspace",
        "prompt_path": "prompt.md",
        "model": "gpt-5.6-sol",
        "reasoning_effort": "high",
        "timeout_seconds": 300,
    }


def request_tree(tmp_path: Path, data: dict[str, object] | None = None) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    (tmp_path / "prompt.md").write_text("Exact human prompt.\n", encoding="utf-8")
    path = tmp_path / "request.yaml"
    path.write_text(yaml.safe_dump(data or request_data(), sort_keys=False), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("role", "sandbox", "ephemeral"),
    [
        ("supervisor", "read-only", False),
        ("worker", "workspace-write", False),
        ("auditor", "read-only", True),
    ],
)
def test_valid_request_for_each_role(
    tmp_path: Path, role: str, sandbox: str, ephemeral: bool
) -> None:
    data = request_data()
    data["role"] = role
    prepared = load_codex_request(request_tree(tmp_path, data), git_worktree_checker=lambda _: True)

    assert prepared.workspace == (tmp_path / "workspace").resolve()
    assert prepared.prompt_path == (tmp_path / "prompt.md").resolve()
    assert prepared.prompt_bytes == b"Exact human prompt.\n"
    assert prepared.policy.sandbox == sandbox
    assert prepared.policy.approval == "never"
    assert prepared.policy.ephemeral is ephemeral
    assert "Exact human prompt" not in str(prepared.normalized_dict())


def test_unknown_and_duplicate_request_fields_are_rejected(tmp_path: Path) -> None:
    data = request_data()
    data["executable"] = "/unsafe/codex"
    with pytest.raises(CodexRequestError, match="executable: Extra inputs"):
        load_codex_request(request_tree(tmp_path, data), git_worktree_checker=lambda _: True)

    source = yaml.safe_dump(request_data(), sort_keys=False)
    path = request_tree(tmp_path)
    path.write_text(source.replace("run_id: worker-001\n", "run_id: one\nrun_id: two\n"))
    with pytest.raises(CodexRequestError, match="duplicate mapping key"):
        load_codex_request(path, git_worktree_checker=lambda _: True)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", 2),
        ("run_id", "bad id"),
        ("run_id", "x" * 81),
        ("model", "--danger"),
        ("model", "bad model"),
        ("model", "vendor//model"),
        ("reasoning_effort", "maximum"),
        ("timeout_seconds", MIN_CODEX_TIMEOUT_SECONDS - 1),
        ("timeout_seconds", MAX_CODEX_TIMEOUT_SECONDS + 1),
    ],
)
def test_invalid_request_values_are_rejected(
    tmp_path: Path, field: str, value: object
) -> None:
    data = request_data()
    data[field] = value
    with pytest.raises(CodexRequestError, match=field):
        load_codex_request(request_tree(tmp_path, data), git_worktree_checker=lambda _: True)


@pytest.mark.parametrize(
    "timeout", [MIN_CODEX_TIMEOUT_SECONDS, MAX_CODEX_TIMEOUT_SECONDS]
)
def test_timeout_boundaries_are_accepted(tmp_path: Path, timeout: int) -> None:
    data = request_data()
    data["timeout_seconds"] = timeout
    prepared = load_codex_request(request_tree(tmp_path, data), git_worktree_checker=lambda _: True)
    assert prepared.request.timeout_seconds == timeout


def test_relative_paths_resolve_from_request_parent(tmp_path: Path) -> None:
    request_directory = tmp_path / "requests"
    request_directory.mkdir()
    workspace = tmp_path / "project"
    workspace.mkdir()
    prompt = tmp_path / "prompts" / "task.md"
    prompt.parent.mkdir()
    prompt.write_text("Do the exact task.", encoding="utf-8")
    data = request_data()
    data["workspace"] = "../project"
    data["prompt_path"] = "../prompts/task.md"
    path = request_directory / "run.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")

    prepared = load_codex_request(path, git_worktree_checker=lambda _: True)

    assert prepared.workspace == workspace.resolve()
    assert prepared.prompt_path == prompt.resolve()


def test_workspace_must_exist_be_directory_and_belong_to_worktree(tmp_path: Path) -> None:
    data = request_data()
    data["workspace"] = "missing"
    with pytest.raises(CodexRequestError, match="workspace path could not be resolved"):
        load_codex_request(request_tree(tmp_path, data), git_worktree_checker=lambda _: True)

    file_workspace = tmp_path / "workspace-file"
    file_workspace.write_text("not a directory", encoding="utf-8")
    data["workspace"] = "workspace-file"
    with pytest.raises(CodexRequestError, match="workspace is not a directory"):
        load_codex_request(request_tree(tmp_path, data), git_worktree_checker=lambda _: True)

    data["workspace"] = "workspace"
    with pytest.raises(CodexRequestError, match="does not belong to a Git worktree"):
        load_codex_request(request_tree(tmp_path, data), git_worktree_checker=lambda _: False)


@pytest.mark.parametrize(
    ("prompt_bytes", "message"),
    [
        (b"", "must not be empty"),
        (b" \n\t", "must not be empty"),
        (b"\xff", "not valid UTF-8"),
        (b"x" * (MAX_PROMPT_BYTES + 1), "exceeds"),
    ],
)
def test_prompt_content_validation(
    tmp_path: Path, prompt_bytes: bytes, message: str
) -> None:
    path = request_tree(tmp_path)
    (tmp_path / "prompt.md").write_bytes(prompt_bytes)
    with pytest.raises(CodexRequestError, match=message):
        load_codex_request(path, git_worktree_checker=lambda _: True)


def test_prompt_must_exist_and_be_regular(tmp_path: Path) -> None:
    data = request_data()
    data["prompt_path"] = "missing.md"
    with pytest.raises(CodexRequestError, match="prompt path could not be resolved"):
        load_codex_request(request_tree(tmp_path, data), git_worktree_checker=lambda _: True)

    prompt_directory = tmp_path / "prompt-directory"
    prompt_directory.mkdir()
    data["prompt_path"] = "prompt-directory"
    with pytest.raises(CodexRequestError, match="prompt is not a regular file"):
        load_codex_request(request_tree(tmp_path, data), git_worktree_checker=lambda _: True)


def test_prompt_size_boundary_and_symlink_loop(tmp_path: Path) -> None:
    path = request_tree(tmp_path)
    (tmp_path / "prompt.md").write_bytes(b"x" * MAX_PROMPT_BYTES)
    prepared = load_codex_request(path, git_worktree_checker=lambda _: True)
    assert len(prepared.prompt_bytes) == MAX_PROMPT_BYTES

    loop = tmp_path / "loop.md"
    loop.symlink_to(loop)
    data = request_data()
    data["prompt_path"] = "loop.md"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(CodexRequestError, match="prompt path could not be resolved"):
        load_codex_request(path, git_worktree_checker=lambda _: True)


def test_request_path_and_yaml_failures_are_sanitized(tmp_path: Path) -> None:
    with pytest.raises(CodexRequestError, match="path could not be resolved"):
        load_codex_request(tmp_path / "missing.yaml", git_worktree_checker=lambda _: True)

    path = tmp_path / "invalid.yaml"
    path.write_bytes(b"\xffsecret-like-input")
    with pytest.raises(CodexRequestError, match="byte offset 0") as captured:
        load_codex_request(path, git_worktree_checker=lambda _: True)
    assert "secret-like-input" not in str(captured.value)

    path.write_text("run_id: [unterminated", encoding="utf-8")
    with pytest.raises(CodexRequestError, match=r"malformed YAML at line \d+, column \d+"):
        load_codex_request(path, git_worktree_checker=lambda _: True)


def test_result_is_strict_and_immutable() -> None:
    result = CodexRunResult(
        run_id="run-1",
        status="succeeded",
        exit_code=0,
        started_at="2026-01-01T00:00:00Z",
        ended_at="2026-01-01T00:00:01Z",
        duration_seconds=1.0,
        artifact_directory="/runs/run-1",
        event_count=1,
        malformed_event_count=0,
        final_message_present=True,
        permission_evidence=False,
        summary="Codex run succeeded.",
        error=None,
    )

    with pytest.raises(ValidationError):
        CodexRunResult(**{**result.to_dict(), "status": "unknown"})
    with pytest.raises(ValidationError):
        CodexRunResult(**{**result.to_dict(), "surprise": True})
    with pytest.raises(ValidationError):
        result.status = "process_failed"
