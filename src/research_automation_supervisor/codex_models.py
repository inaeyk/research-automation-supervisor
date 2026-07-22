"""Strict models and input loading for deterministic Codex runs."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from research_automation_supervisor.contract import (
    _format_validation_error,
    _UniqueKeySafeLoader,
)
from research_automation_supervisor.errors import CodexDependencyError, CodexRequestError

MAX_PROMPT_BYTES = 1024 * 1024
MIN_CODEX_TIMEOUT_SECONDS = 30
MAX_CODEX_TIMEOUT_SECONDS = 14_400
GIT_CHECK_TIMEOUT_SECONDS = 10.0

RunId = Annotated[str, Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$")]
ModelName = Annotated[
    str,
    Field(
        min_length=1,
        max_length=80,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*)*$",
    ),
]
RequiredString = Annotated[str, Field(min_length=1)]
Role = Literal["supervisor", "worker", "auditor"]
ReasoningEffort = Literal["low", "medium", "high", "xhigh"]
RunStatus = Literal[
    "succeeded",
    "launch_failed",
    "timed_out",
    "output_limit_exceeded",
    "permission_blocked",
    "malformed_event_stream",
    "process_failed",
    "missing_final_message",
]


class CodexRunRequest(BaseModel):
    """The exact schema-version-1 human-authored Codex request."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True, strict=True)

    schema_version: Literal[1]
    run_id: RunId
    role: Role
    workspace: RequiredString
    prompt_path: RequiredString
    model: ModelName
    reasoning_effort: ReasoningEffort
    timeout_seconds: Annotated[
        int, Field(ge=MIN_CODEX_TIMEOUT_SECONDS, le=MAX_CODEX_TIMEOUT_SECONDS)
    ]


class RolePolicy(BaseModel):
    """Adapter-owned policy derived solely from a request role."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    sandbox: Literal["read-only", "workspace-write"]
    approval: Literal["never"] = "never"
    ephemeral: bool


ROLE_POLICIES: Mapping[Role, RolePolicy] = {
    "supervisor": RolePolicy(sandbox="read-only", ephemeral=False),
    "worker": RolePolicy(sandbox="workspace-write", ephemeral=False),
    "auditor": RolePolicy(sandbox="read-only", ephemeral=True),
}


class CodexRunResult(BaseModel):
    """Stable, immutable outcome returned by every launched adapter run."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    run_id: RunId
    status: RunStatus
    exit_code: int | None
    started_at: RequiredString
    ended_at: RequiredString
    duration_seconds: Annotated[float, Field(ge=0)]
    artifact_directory: RequiredString
    event_count: Annotated[int, Field(ge=0)]
    malformed_event_count: Annotated[int, Field(ge=0)]
    final_message_present: bool
    permission_evidence: bool
    summary: RequiredString
    error: str | None

    def to_dict(self) -> dict[str, object]:
        """Return a deterministic JSON-compatible representation."""
        return self.model_dump(mode="json")


@dataclass(frozen=True)
class PreparedCodexRequest:
    """Validated request plus the single pre-launch prompt read."""

    request_path: Path
    request: CodexRunRequest
    workspace: Path
    prompt_path: Path
    prompt_bytes: bytes
    prompt_sha256: str
    policy: RolePolicy

    def normalized_dict(self) -> dict[str, object]:
        """Return resolved paths and the fixed policy without prompt contents."""
        return {
            "schema_version": self.request.schema_version,
            "run_id": self.request.run_id,
            "role": self.request.role,
            "workspace": str(self.workspace),
            "prompt_path": str(self.prompt_path),
            "model": self.request.model,
            "reasoning_effort": self.request.reasoning_effort,
            "timeout_seconds": self.request.timeout_seconds,
            "policy": self.policy.model_dump(mode="json"),
        }


GitWorktreeChecker = Callable[[Path], bool]


def load_codex_request(
    path: Path,
    *,
    git_worktree_checker: GitWorktreeChecker | None = None,
) -> PreparedCodexRequest:
    """Load, resolve, and fully validate one Codex request without writing files."""
    request_path = _resolve_request_path(path)
    source = _read_request_source(request_path)
    data = _load_request_yaml(source)
    try:
        request = CodexRunRequest.model_validate(data)
    except ValidationError as exc:
        details = "; ".join(_format_validation_error(error) for error in exc.errors())
        raise CodexRequestError(f"Codex request validation failed: {details}") from exc

    parent = request_path.parent
    workspace = _resolve_referenced_path(parent, request.workspace, "workspace")
    if not workspace.is_dir():
        raise CodexRequestError("resolved workspace is not a directory")

    checker = git_worktree_checker or _default_git_worktree_checker()
    try:
        is_worktree = checker(workspace)
    except CodexDependencyError:
        raise
    except (OSError, subprocess.SubprocessError) as exc:
        raise CodexDependencyError("Git worktree validation could not be completed") from exc
    if not is_worktree:
        raise CodexRequestError("resolved workspace does not belong to a Git worktree")

    prompt_path = _resolve_referenced_path(parent, request.prompt_path, "prompt")
    if not prompt_path.is_file():
        raise CodexRequestError("resolved prompt is not a regular file")
    try:
        prompt_bytes = prompt_path.read_bytes()
    except OSError as exc:
        raise CodexRequestError("resolved prompt could not be read") from exc
    if len(prompt_bytes) > MAX_PROMPT_BYTES:
        raise CodexRequestError(f"prompt exceeds the {MAX_PROMPT_BYTES}-byte limit")
    try:
        prompt_text = prompt_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CodexRequestError(
            f"prompt is not valid UTF-8 at byte offset {exc.start}"
        ) from exc
    if not prompt_text.strip():
        raise CodexRequestError("prompt must not be empty after trimming")

    return PreparedCodexRequest(
        request_path=request_path,
        request=request,
        workspace=workspace,
        prompt_path=prompt_path,
        prompt_bytes=prompt_bytes,
        prompt_sha256=hashlib.sha256(prompt_bytes).hexdigest(),
        policy=ROLE_POLICIES[request.role],
    )


def _resolve_request_path(path: Path) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise CodexRequestError("Codex request path could not be resolved") from exc
    if not resolved.is_file():
        raise CodexRequestError("Codex request is not a regular file")
    return resolved


def _read_request_source(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise CodexRequestError(
            f"Codex request is not valid UTF-8 at byte offset {exc.start}"
        ) from exc
    except OSError as exc:
        raise CodexRequestError("Codex request could not be read") from exc


def _load_request_yaml(source: str) -> dict[str, Any]:
    try:
        data: Any = yaml.load(source, Loader=_UniqueKeySafeLoader)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        location = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        problem = getattr(exc, "problem", None) or "invalid YAML"
        raise CodexRequestError(f"malformed YAML{location}: {problem}") from exc
    if not isinstance(data, dict):
        raise CodexRequestError("Codex request root must be a YAML mapping")
    return data


def _resolve_referenced_path(parent: Path, value: str, label: str) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = parent / candidate
    try:
        return candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise CodexRequestError(f"{label} path could not be resolved") from exc


def _default_git_worktree_checker() -> GitWorktreeChecker:
    executable = shutil.which("git")
    if executable is None:
        raise CodexDependencyError("Git executable is required for worktree validation")

    def check(workspace: Path) -> bool:
        try:
            completed = subprocess.run(
                [executable, "-C", str(workspace), "rev-parse", "--is-inside-work-tree"],
                capture_output=True,
                check=False,
                encoding="utf-8",
                errors="replace",
                timeout=GIT_CHECK_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise CodexDependencyError("Git worktree validation could not be completed") from exc
        return completed.returncode == 0 and completed.stdout.strip() == "true"

    return check
