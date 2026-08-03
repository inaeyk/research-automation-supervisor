"""Deterministic path-independent Git identity for Physics Oracle execution."""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
from pathlib import Path
from typing import Literal, cast

from research_automation_supervisor.durable_state import canonical_json
from research_automation_supervisor.errors import (
    PhysicsOracleDependencyError,
    PhysicsOracleInputError,
    PhysicsOracleIntegrityError,
)
from research_automation_supervisor.physics_oracle_models import (
    PhysicsOracleWorkspaceIdentityV1,
)

GIT_TIMEOUT_SECONDS = 30
MAX_GIT_IDENTITY_OUTPUT_BYTES = 256 * 1024 * 1024
MAX_WORKSPACE_PATHS = 200_000
GIT_EXECUTABLE = Path("/usr/bin/git")

_GIT_ENVIRONMENT = {
    "GIT_ASKPASS": "/nonexistent",
    "GIT_ATTR_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_DISCOVERY_ACROSS_FILESYSTEM": "0",
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_PROTOCOL_FROM_USER": "0",
    "GIT_SSH_COMMAND": "/nonexistent",
    "GIT_TERMINAL_PROMPT": "0",
    "HOME": "/nonexistent",
    "LC_ALL": "C",
    "PATH": "/usr/bin:/bin",
    "SSH_ASKPASS": "/nonexistent",
    "XDG_CONFIG_HOME": "/nonexistent",
}


def collect_physics_oracle_workspace_identity(
    workspace: Path,
) -> PhysicsOracleWorkspaceIdentityV1:
    """Collect a stable identity without changing the worktree or existing hashes."""
    root = _canonical_workspace(workspace)
    git = _trusted_git()
    repository = _git_text(git, root, ("rev-parse", "--show-toplevel")).strip()
    try:
        repository_root = Path(repository).resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise PhysicsOracleInputError("Git repository root could not be resolved") from exc
    if repository_root != root:
        raise PhysicsOracleInputError("oracle workspace must be the Git worktree root")

    before = _anchors(git, root)
    index_manifest = _git_bytes(git, root, ("ls-files", "--stage", "-z"))
    tracked_paths = _nul_paths(_git_bytes(git, root, ("ls-files", "-z")))
    untracked_paths = _nul_paths(
        _git_bytes(
            git,
            root,
            ("ls-files", "--others", "--exclude-standard", "-z"),
        )
    )
    tracked_manifest_hash = _filesystem_manifest_hash(root, tracked_paths)
    untracked_manifest_hash = _filesystem_manifest_hash(root, untracked_paths)
    tracked_diff = _git_bytes(
        git,
        root,
        (
            "-c",
            "color.ui=false",
            "diff",
            "--binary",
            "--full-index",
            "--no-color",
            "--no-ext-diff",
            "--no-textconv",
            "HEAD",
            "--",
        ),
    )
    submodules = _git_bytes(
        git,
        root,
        ("submodule", "status", "--recursive"),
        accepted_exit_codes=frozenset({0}),
    )
    after = _anchors(git, root)
    if before != after:
        raise PhysicsOracleIntegrityError(
            "workspace changed while its oracle identity was being collected"
        )
    return PhysicsOracleWorkspaceIdentityV1(
        schema_version=1,
        head_commit=before[0],
        branch=before[1],
        detached=before[1] is None,
        object_format=cast(Literal["sha1", "sha256"], before[2]),
        index_manifest_sha256=hashlib.sha256(index_manifest).hexdigest(),
        index_file_sha256=before[3],
        tracked_diff_sha256=hashlib.sha256(tracked_diff).hexdigest(),
        tracked_worktree_manifest_sha256=tracked_manifest_hash,
        tracked_path_count=len(tracked_paths),
        untracked_manifest_sha256=untracked_manifest_hash,
        untracked_path_count=len(untracked_paths),
        status_sha256=before[4],
        submodule_status_sha256=hashlib.sha256(submodules).hexdigest(),
    )


def _canonical_workspace(workspace: Path) -> Path:
    if ".." in workspace.parts:
        raise PhysicsOracleInputError("workspace contains parent traversal")
    try:
        absolute = Path(os.path.abspath(workspace))
        resolved = workspace.resolve(strict=True)
        metadata = resolved.lstat()
    except (OSError, RuntimeError, ValueError) as exc:
        raise PhysicsOracleInputError("workspace could not be resolved") from exc
    if absolute != resolved or workspace.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
        raise PhysicsOracleInputError("workspace must be a canonical non-symlink directory")
    return resolved


def _trusted_git() -> Path:
    try:
        resolved = GIT_EXECUTABLE.resolve(strict=True)
        metadata = resolved.lstat()
    except (OSError, RuntimeError, ValueError) as exc:
        raise PhysicsOracleDependencyError("Git executable is required") from exc
    if not stat.S_ISREG(metadata.st_mode) or not os.access(resolved, os.X_OK):
        raise PhysicsOracleDependencyError("Git executable is unavailable")
    return resolved


def _anchors(git: Path, workspace: Path) -> tuple[str, str | None, str, str, str]:
    head = _git_text(git, workspace, ("rev-parse", "--verify", "HEAD")).strip()
    if not re_full_hex(head):
        raise PhysicsOracleInputError("workspace Git HEAD is invalid")
    branch_raw = _git_bytes(
        git,
        workspace,
        ("symbolic-ref", "--quiet", "--short", "HEAD"),
        accepted_exit_codes=frozenset({0, 1}),
    )
    try:
        branch = branch_raw.decode("utf-8").strip() or None
    except UnicodeDecodeError as exc:
        raise PhysicsOracleInputError("workspace branch is invalid") from exc
    object_format = _git_text(git, workspace, ("rev-parse", "--show-object-format")).strip()
    if object_format not in {"sha1", "sha256"}:
        raise PhysicsOracleInputError("workspace Git object format is unsupported")
    index_path_text = _git_text(git, workspace, ("rev-parse", "--git-path", "index")).strip()
    index_path = Path(index_path_text)
    if not index_path.is_absolute():
        index_path = workspace / index_path
    try:
        index_hash = hashlib.sha256(
            index_path.read_bytes() if index_path.exists() else b""
        ).hexdigest()
    except OSError as exc:
        raise PhysicsOracleInputError("Git index could not be inspected") from exc
    status_bytes = _git_bytes(
        git,
        workspace,
        ("status", "--porcelain=v1", "-z", "--untracked-files=all"),
    )
    return head, branch, object_format, index_hash, hashlib.sha256(status_bytes).hexdigest()


def _filesystem_manifest_hash(root: Path, paths: tuple[str, ...]) -> str:
    digest = hashlib.sha256()
    for relative in paths:
        path = root / relative
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise PhysicsOracleIntegrityError(
                "workspace path changed during identity collection"
            ) from exc
        mode = stat.S_IMODE(metadata.st_mode)
        if stat.S_ISREG(metadata.st_mode):
            kind = "regular"
            content_sha = _hash_regular_file(path)
            size = metadata.st_size
            target = None
        elif stat.S_ISLNK(metadata.st_mode):
            kind = "symlink"
            try:
                raw_target = os.fsencode(os.readlink(path))
            except OSError as exc:
                raise PhysicsOracleIntegrityError(
                    "workspace symlink changed during identity collection"
                ) from exc
            content_sha = hashlib.sha256(raw_target).hexdigest()
            size = len(raw_target)
            target = content_sha
        elif stat.S_ISDIR(metadata.st_mode):
            kind = "directory"
            content_sha = hashlib.sha256(b"").hexdigest()
            size = 0
            target = None
        else:
            raise PhysicsOracleInputError(
                "workspace identity does not support special filesystem objects"
            )
        digest.update(
            canonical_json(
                {
                    "content_sha256": content_sha,
                    "kind": kind,
                    "mode": mode,
                    "path": relative,
                    "size": size,
                    "symlink_target_sha256": target,
                }
            )
        )
    return digest.hexdigest()


def _hash_regular_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    except OSError as exc:
        raise PhysicsOracleIntegrityError(
            "workspace file changed during identity collection"
        ) from exc
    return digest.hexdigest()


def _nul_paths(raw: bytes) -> tuple[str, ...]:
    if not raw:
        return ()
    pieces = raw.split(b"\0")
    if pieces[-1] != b"":
        raise PhysicsOracleInputError("Git path output is incomplete")
    pieces.pop()
    if len(pieces) > MAX_WORKSPACE_PATHS:
        raise PhysicsOracleInputError("workspace contains too many paths")
    result: list[str] = []
    for raw_path in pieces:
        try:
            value = raw_path.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PhysicsOracleInputError("workspace contains a non-UTF-8 path") from exc
        if (
            not value
            or "\x00" in value
            or "\\" in value
            or value.startswith("/")
            or any(part in {"", ".", ".."} for part in value.split("/"))
        ):
            raise PhysicsOracleInputError("workspace contains an unsafe path")
        result.append(value)
    if len(result) != len(set(result)):
        raise PhysicsOracleInputError("Git returned duplicate workspace paths")
    return tuple(sorted(result))


def _git_text(git: Path, workspace: Path, arguments: tuple[str, ...]) -> str:
    raw = _git_bytes(git, workspace, arguments)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PhysicsOracleInputError("Git returned non-UTF-8 identity output") from exc


def _git_bytes(
    git: Path,
    workspace: Path,
    arguments: tuple[str, ...],
    *,
    accepted_exit_codes: frozenset[int] = frozenset({0}),
) -> bytes:
    command = (
        str(git),
        "--no-pager",
        "--no-optional-locks",
        "-C",
        str(workspace),
        *arguments,
    )
    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            close_fds=True,
            env=_GIT_ENVIRONMENT,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as exc:
        raise PhysicsOracleDependencyError("Git executable is required") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise PhysicsOracleInputError("Git identity command failed") from exc
    if completed.returncode not in accepted_exit_codes:
        raise PhysicsOracleInputError("Git identity command returned an unexpected status")
    if len(completed.stdout) > MAX_GIT_IDENTITY_OUTPUT_BYTES:
        raise PhysicsOracleInputError("Git identity output exceeds its bound")
    return completed.stdout


def re_full_hex(value: str) -> bool:
    return len(value) in {40, 64} and all(character in "0123456789abcdef" for character in value)
