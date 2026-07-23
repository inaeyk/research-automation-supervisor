"""Deterministic read-only Git and scope evidence for Stage 2 workflows."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from collections.abc import Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

from research_automation_supervisor.errors import WorkflowDependencyError, WorkflowInputError
from research_automation_supervisor.redaction import (
    is_sensitive_name,
    redact_text,
    would_redact_text,
)
from research_automation_supervisor.workflow_models import (
    _freeze_sequence,
    normalize_relative_path,
    path_matches_any,
)

GIT_TIMEOUT_SECONDS = 30
MAX_PATCH_BYTES = 25 * 1024 * 1024
MAX_UNTRACKED_CONTENT_BYTES = 1024 * 1024


class GitBaseline(BaseModel):
    """Frozen repository identity and clean starting point."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    workspace: str
    repository_root: str
    head: Annotated[str, Field(pattern=r"^[0-9a-fA-F]{40,64}$")]
    branch: str | None
    detached: bool
    clean: bool
    status_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]

    def to_dict(self) -> dict[str, object]:
        return self.model_dump(mode="json")


class ChangedPath(BaseModel):
    """One path identified from deterministic porcelain status."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    path: str
    status: str
    kind: Literal["tracked", "untracked", "deleted", "renamed", "type_changed"]
    old_path: str | None
    symlink: bool
    symlink_target: str | None
    symlink_escapes_workspace: bool


class ScopeFinding(BaseModel):
    """One deterministic allowed/protected/symlink scope violation."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    path: str
    reason: Literal["outside_allowed_paths", "protected_path", "symlink_escape"]


class GitEvidence(BaseModel):
    """Bounded current Git evidence relative to the recorded clean baseline."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    baseline_head: str
    current_head: str
    repository_root: str
    branch: str | None
    detached: bool
    status_sha256: str
    diff_sha256: str
    patch_sha256: str
    patch_byte_count: Annotated[int, Field(ge=0)]
    patch_stored_byte_count: Annotated[int, Field(ge=0)]
    patch_complete: bool
    patch_artifact: str
    changed_paths: Annotated[tuple[ChangedPath, ...], BeforeValidator(_freeze_sequence)]
    scope_compliant: bool
    scope_findings: Annotated[tuple[ScopeFinding, ...], BeforeValidator(_freeze_sequence)]
    index_tree_sha256_before: str
    index_tree_sha256_after: str

    def to_dict(self) -> dict[str, object]:
        return self.model_dump(mode="json")


def record_git_baseline(
    workspace: Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> GitBaseline:
    """Record exact repository identity and require a readable Git worktree."""
    environment = _git_environment(environ)
    root_text = _run_git(workspace, ("rev-parse", "--show-toplevel"), environment)
    try:
        repository_root = Path(root_text.strip()).resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise WorkflowInputError("Git repository root could not be resolved") from exc
    head = _run_git(workspace, ("rev-parse", "--verify", "HEAD"), environment).strip()
    if not head or any(character not in "0123456789abcdefABCDEF" for character in head):
        raise WorkflowInputError("Git HEAD is invalid")
    branch = _read_branch(workspace, environment)
    status = _run_git_bytes(
        workspace,
        ("status", "--porcelain=v1", "-z", "--untracked-files=all"),
        environment,
    )
    return GitBaseline(
        workspace=str(workspace),
        repository_root=str(repository_root),
        head=head,
        branch=branch,
        detached=branch is None,
        clean=not status,
        status_sha256=hashlib.sha256(status).hexdigest(),
    )


def collect_git_evidence(
    workspace: Path,
    baseline: GitBaseline,
    allowed_paths: Sequence[str],
    protected_paths: Sequence[str],
    artifact_directory: Path,
    *,
    sensitive_values: Sequence[str] = (),
    max_patch_bytes: int = MAX_PATCH_BYTES,
    environ: Mapping[str, str] | None = None,
) -> GitEvidence:
    """Collect bounded status/diff/untracked evidence without modifying the index."""
    environment = _git_environment(environ)
    current = record_git_baseline(workspace, environ=environment)
    if current.repository_root != baseline.repository_root:
        raise WorkflowInputError("workspace repository identity changed")
    if current.head != baseline.head:
        raise WorkflowInputError("workspace HEAD changed from the frozen baseline")

    index_before = _index_tree_hash(workspace, environment)
    status_bytes = _run_git_bytes(
        workspace,
        ("status", "--porcelain=v1", "-z", "--untracked-files=all"),
        environment,
    )
    entries = _parse_porcelain(status_bytes, workspace)
    changed_structural = [
        value
        for entry in entries
        for value in (entry.path, entry.old_path, entry.symlink_target)
        if value is not None
    ]
    if any(would_redact_text(value, sensitive_values) for value in changed_structural):
        raise WorkflowInputError("Git evidence contains a structural redaction collision")
    diff_bytes = _run_git_bytes(
        workspace,
        (
            "-c",
            "color.ui=false",
            "diff",
            "--binary",
            "--full-index",
            "--no-color",
            "--no-ext-diff",
            "--no-textconv",
            baseline.head,
            "--",
        ),
        environment,
    )
    patch = _build_patch(workspace, status_bytes, diff_bytes, entries, sensitive_values)
    patch_hash = hashlib.sha256(patch).hexdigest()
    patch_complete = len(patch) <= max_patch_bytes
    if patch_complete:
        stored_patch = patch
    else:
        marker = {
            "complete": False,
            "patch_byte_count": len(patch),
            "patch_sha256": patch_hash,
            "reason": "patch evidence exceeds the 25 MiB workflow limit",
        }
        stored_patch = (
            "PATCH EVIDENCE TRUNCATED; AUDIT MUST NOT RUN\n"
            + json.dumps(marker, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode("ascii")

    try:
        artifact_directory.mkdir(parents=True, exist_ok=True)
        patch_path = artifact_directory / "patch.txt"
        patch_path.write_bytes(stored_patch)
    except OSError as exc:
        raise WorkflowInputError("Git evidence artifact could not be written") from exc

    scope_findings = _scope_findings(entries, allowed_paths, protected_paths)
    index_after = _index_tree_hash(workspace, environment)
    if index_after != index_before:
        raise WorkflowInputError("Git evidence collection unexpectedly modified the index")

    evidence = GitEvidence(
        baseline_head=baseline.head,
        current_head=current.head,
        repository_root=current.repository_root,
        branch=current.branch,
        detached=current.detached,
        status_sha256=hashlib.sha256(status_bytes).hexdigest(),
        diff_sha256=hashlib.sha256(diff_bytes).hexdigest(),
        patch_sha256=patch_hash,
        patch_byte_count=len(patch),
        patch_stored_byte_count=len(stored_patch),
        patch_complete=patch_complete,
        patch_artifact=str(patch_path),
        changed_paths=entries,
        scope_compliant=not scope_findings,
        scope_findings=scope_findings,
        index_tree_sha256_before=index_before,
        index_tree_sha256_after=index_after,
    )
    _atomic_json(artifact_directory / "evidence.json", evidence.to_dict())
    return evidence


def validate_repository_identity(workspace: Path, baseline: GitBaseline) -> bool:
    """Return whether repository root and HEAD still match the frozen baseline."""
    current = record_git_baseline(workspace)
    return current.repository_root == baseline.repository_root and current.head == baseline.head


def _run_git(
    workspace: Path,
    arguments: Sequence[str],
    environment: Mapping[str, str],
) -> str:
    raw = _run_git_bytes(workspace, arguments, environment)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WorkflowInputError("Git output is not valid UTF-8") from exc


def _run_git_bytes(
    workspace: Path,
    arguments: Sequence[str],
    environment: Mapping[str, str],
    *,
    accepted_exit_codes: frozenset[int] = frozenset({0}),
) -> bytes:
    command = [
        "git",
        "--no-pager",
        "--no-optional-locks",
        "-C",
        str(workspace),
        *arguments,
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            check=False,
            env=dict(environment),
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as exc:
        raise WorkflowDependencyError("Git executable is required") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise WorkflowInputError("Git evidence command failed") from exc
    if completed.returncode not in accepted_exit_codes:
        raise WorkflowInputError("Git evidence command returned an unexpected status")
    return completed.stdout


def _read_branch(workspace: Path, environment: Mapping[str, str]) -> str | None:
    raw = _run_git_bytes(
        workspace,
        ("symbolic-ref", "--quiet", "--short", "HEAD"),
        environment,
        accepted_exit_codes=frozenset({0, 1}),
    )
    if not raw:
        return None
    try:
        return raw.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise WorkflowInputError("Git branch output is not valid UTF-8") from exc


def _git_environment(environ: Mapping[str, str] | None) -> dict[str, str]:
    source = os.environ if environ is None else environ
    environment = {
        name: value for name, value in source.items() if not is_sensitive_name(name)
    }
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    environment["LC_ALL"] = "C"
    return environment


def _parse_porcelain(status: bytes, workspace: Path) -> tuple[ChangedPath, ...]:
    if not status:
        return ()
    parts = status.split(b"\0")
    if parts[-1] != b"":
        raise WorkflowInputError("Git status did not produce complete NUL-delimited output")
    parts.pop()
    result: list[ChangedPath] = []
    offset = 0
    while offset < len(parts):
        item = parts[offset]
        offset += 1
        if len(item) < 4 or item[2:3] != b" ":
            raise WorkflowInputError("Git status output is malformed")
        try:
            code = item[:2].decode("ascii")
            path = normalize_relative_path(item[3:].decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise WorkflowInputError("Git status contains an invalid path") from exc
        old_path: str | None = None
        if "R" in code or "C" in code:
            if offset >= len(parts):
                raise WorkflowInputError("Git rename status is incomplete")
            try:
                old_path = normalize_relative_path(parts[offset].decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as exc:
                raise WorkflowInputError("Git rename contains an invalid path") from exc
            offset += 1
        full_path = workspace / path
        try:
            mode = full_path.lstat().st_mode
            is_symlink = stat.S_ISLNK(mode)
        except FileNotFoundError:
            is_symlink = False
        except OSError as exc:
            raise WorkflowInputError("changed path metadata could not be read") from exc
        target: str | None = None
        escapes = False
        if is_symlink:
            try:
                target = os.readlink(full_path)
                resolved_target = (full_path.parent / target).resolve(strict=False)
                resolved_target.relative_to(workspace)
            except ValueError:
                escapes = True
            except OSError as exc:
                raise WorkflowInputError("changed symlink target could not be inspected") from exc
        if code == "??":
            kind: Literal["tracked", "untracked", "deleted", "renamed", "type_changed"] = (
                "untracked"
            )
        elif "D" in code:
            kind = "deleted"
        elif "R" in code:
            kind = "renamed"
        elif "T" in code:
            kind = "type_changed"
        else:
            kind = "tracked"
        result.append(
            ChangedPath(
                path=path,
                status=code,
                kind=kind,
                old_path=old_path,
                symlink=is_symlink,
                symlink_target=target,
                symlink_escapes_workspace=escapes,
            )
        )
    return tuple(sorted(result, key=lambda entry: (entry.path, entry.old_path or "")))


def _scope_findings(
    entries: Sequence[ChangedPath],
    allowed_paths: Sequence[str],
    protected_paths: Sequence[str],
) -> tuple[ScopeFinding, ...]:
    findings: list[ScopeFinding] = []
    for entry in entries:
        paths = (entry.path,) if entry.old_path is None else (entry.path, entry.old_path)
        for path in paths:
            if path_matches_any(path, protected_paths):
                findings.append(ScopeFinding(path=path, reason="protected_path"))
            elif not path_matches_any(path, allowed_paths):
                findings.append(ScopeFinding(path=path, reason="outside_allowed_paths"))
        if entry.symlink_escapes_workspace:
            findings.append(ScopeFinding(path=entry.path, reason="symlink_escape"))
    unique = {(finding.path, finding.reason): finding for finding in findings}
    return tuple(unique[key] for key in sorted(unique))


def _build_patch(
    workspace: Path,
    status: bytes,
    diff: bytes,
    entries: Sequence[ChangedPath],
    sensitive_values: Sequence[str],
) -> bytes:
    status_display = _status_display(entries)
    diff_text = diff.decode("utf-8", errors="replace")
    pieces = [
        "# git status --porcelain=v1 -z (normalized)\n",
        status_display,
        "# git diff --binary --full-index --no-color --no-ext-diff --no-textconv\n",
        diff_text,
    ]
    del status
    for entry in entries:
        if entry.kind != "untracked" or entry.symlink:
            continue
        path = workspace / entry.path
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise WorkflowInputError("untracked file evidence could not be read") from exc
        digest = hashlib.sha256(content).hexdigest()
        pieces.append(f"\n# untracked {entry.path} sha256={digest} bytes={len(content)}\n")
        if len(content) > MAX_UNTRACKED_CONTENT_BYTES:
            pieces.append("[content omitted: exceeds safe per-file evidence limit]\n")
            continue
        try:
            pieces.append(content.decode("utf-8"))
        except UnicodeDecodeError:
            pieces.append("[binary content omitted; hash recorded]\n")
        if pieces[-1] and not pieces[-1].endswith("\n"):
            pieces.append("\n")
    return redact_text("".join(pieces), sensitive_values).encode("utf-8")


def _status_display(entries: Sequence[ChangedPath]) -> str:
    lines = []
    for entry in entries:
        suffix = f" <- {entry.old_path}" if entry.old_path is not None else ""
        lines.append(f"{entry.status} {entry.path}{suffix}\n")
    return "".join(lines)


def _index_tree_hash(workspace: Path, environment: Mapping[str, str]) -> str:
    try:
        git_path = _run_git(workspace, ("rev-parse", "--git-path", "index"), environment).strip()
        index_path = Path(git_path)
        if not index_path.is_absolute():
            index_path = workspace / index_path
        content = index_path.read_bytes() if index_path.exists() else b""
    except OSError as exc:
        raise WorkflowInputError("Git index could not be inspected") from exc
    return hashlib.sha256(content).hexdigest()


def _atomic_json(path: Path, value: object) -> None:
    import tempfile

    rendered = json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        with suppress(FileNotFoundError):
            temporary.unlink()


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)
