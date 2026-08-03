"""Deterministic exact workspace projection for standalone Physics Auditor runs."""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from research_automation_supervisor.durable_state import canonical_json
from research_automation_supervisor.errors import (
    PhysicsAuditorInputError,
    PhysicsAuditorIntegrityError,
)
from research_automation_supervisor.physics_auditor_models import (
    MAX_PHYSICS_AUDITOR_FILES,
    MAX_PHYSICS_AUDITOR_PROJECTED_FILE_BYTES,
    MAX_PHYSICS_AUDITOR_PROJECTION_BYTES,
    PhysicsAuditorChangedPathManifestV1,
    PhysicsAuditorEvidenceIndexV1,
    PhysicsAuditorProjectionManifestV1,
    PhysicsAuditorProjectionObjectV1,
)
from research_automation_supervisor.physics_models import (
    PHYSICS_AUDIT_REPORT_OUTPUT_SCHEMA,
    PhysicsTaskContractV1,
)

AUTHORITY_DIRECTORY = "__physics_auditor_authority__"
PROJECTION_DIRECTORY = "quarantine/workspace"
RUNTIME_HOME_DIRECTORY = "quarantine/codex-home"
PROJECTION_MANIFEST_FILE = "projection-manifest.json"
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
_GIT = "/usr/bin/git"
_GIT_ENVIRONMENT = {
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
_FORBIDDEN_COMPONENTS = frozenset(
    {
        ".git",
        "candidate-evaluation",
        "candidate_evaluation",
        "gold",
        "hidden",
        "historical",
        "historical_gold",
        "private_evaluation",
        "protected",
    }
)


@dataclass(frozen=True)
class PhysicsAuditorProjectionPlan:
    """Manifest plus trusted bytes used to materialize one exact projection."""

    manifest: PhysicsAuditorProjectionManifestV1
    regular_files: tuple[tuple[str, bytes], ...]


def build_physics_auditor_projection(
    *,
    contract: PhysicsTaskContractV1,
    evidence_index: PhysicsAuditorEvidenceIndexV1,
    changed_paths: PhysicsAuditorChangedPathManifestV1,
    source_workspace: Path,
    oracle_program_paths: tuple[str, ...],
) -> PhysicsAuditorProjectionPlan:
    """Build a bounded exact allowlist without creating any filesystem object."""
    workspace = _canonical_workspace(source_workspace)
    sealed_programs = frozenset(oracle_program_paths)
    ignored = _ignored_paths(
        workspace,
        tuple(item.path for item in evidence_index.workspace_files if item.kind != "missing"),
    )
    if ignored:
        raise PhysicsAuditorInputError("declared auditor input is ignored by Git")

    entries: dict[str, tuple[PhysicsAuditorProjectionObjectV1, bytes | None]] = {}
    for declared in evidence_index.workspace_files:
        if declared.kind == "missing":
            continue
        relative = _safe_relative_path(declared.path)
        if relative in sealed_programs:
            raise PhysicsAuditorInputError(
                "a sealed PA-2 oracle program cannot be projected to the auditor"
            )
        if relative == AUTHORITY_DIRECTORY or relative.startswith(f"{AUTHORITY_DIRECTORY}/"):
            raise PhysicsAuditorInputError("workspace input collides with auditor control material")
        _reject_nested_repository(workspace, relative)
        source = workspace / relative
        try:
            metadata = source.lstat()
        except OSError as exc:
            raise PhysicsAuditorInputError("projected workspace input is unavailable") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise PhysicsAuditorInputError("symlinks are forbidden in the auditor projection")
        if stat.S_IMODE(metadata.st_mode) & 0o7000:
            raise PhysicsAuditorInputError(
                "special permission bits are forbidden in the auditor projection"
            )
        authority: Literal["declared_workspace", "candidate_delta", "engine_control"] = (
            "declared_workspace" if declared.declared_evidence_ids else "candidate_delta"
        )
        if stat.S_ISREG(metadata.st_mode):
            content = _bounded_read(source)
            digest = hashlib.sha256(content).hexdigest()
            if (
                declared.kind != "regular"
                or declared.byte_length != len(content)
                or declared.sha256 != digest
                or declared.mode != stat.S_IMODE(metadata.st_mode)
            ):
                raise PhysicsAuditorIntegrityError(
                    "projected source file contradicts the safe evidence index"
                )
            item = PhysicsAuditorProjectionObjectV1(
                path=relative,
                kind="regular",
                mode=stat.S_IMODE(metadata.st_mode),
                byte_length=len(content),
                sha256=digest,
                authority=authority,
            )
            entries[relative] = (item, content)
        elif stat.S_ISDIR(metadata.st_mode):
            try:
                if any(source.iterdir()):
                    raise PhysicsAuditorInputError(
                        "non-empty declared directories are ambiguous projection inputs"
                    )
            except OSError as exc:
                raise PhysicsAuditorInputError(
                    "projected workspace directory could not be enumerated"
                ) from exc
            if declared.kind != "directory" or declared.mode != stat.S_IMODE(metadata.st_mode):
                raise PhysicsAuditorIntegrityError(
                    "projected source directory contradicts the safe evidence index"
                )
            item = PhysicsAuditorProjectionObjectV1(
                path=relative,
                kind="directory",
                mode=stat.S_IMODE(metadata.st_mode),
                byte_length=0,
                sha256=_EMPTY_SHA256,
                authority=authority,
            )
            entries[relative] = (item, None)
        else:
            raise PhysicsAuditorInputError(
                "auditor projection inputs must be regular files or safe directories"
            )

    authority_files = _authority_files(contract, evidence_index, changed_paths)
    for relative, content in authority_files:
        item = PhysicsAuditorProjectionObjectV1(
            path=relative,
            kind="regular",
            mode=0o444,
            byte_length=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            authority="engine_control",
        )
        entries[relative] = (item, content)

    _add_parent_directories(entries)
    objects = tuple(value[0] for _, value in sorted(entries.items()))
    total = sum(item.byte_length for item in objects if item.kind == "regular")
    if len(objects) > MAX_PHYSICS_AUDITOR_FILES:
        raise PhysicsAuditorInputError("auditor projection exceeds its object bound")
    if total > MAX_PHYSICS_AUDITOR_PROJECTION_BYTES:
        raise PhysicsAuditorInputError("auditor projection exceeds its total byte bound")
    manifest = PhysicsAuditorProjectionManifestV1(
        schema_version=1,
        policy="exact_read_only_projection_v1",
        source_workspace_identity_sha256=evidence_index.workspace_identity_sha256,
        objects=objects,
        total_regular_file_bytes=total,
    )
    files = tuple(
        (path, content)
        for path, (item, content) in sorted(entries.items())
        if item.kind == "regular" and content is not None
    )
    return PhysicsAuditorProjectionPlan(manifest=manifest, regular_files=files)


def materialize_physics_auditor_projection(
    plan: PhysicsAuditorProjectionPlan,
    projection_root: Path,
) -> None:
    """Create once, or independently verify an already durable projection."""
    if projection_root.exists():
        verify_physics_auditor_projection(plan.manifest, projection_root)
        return
    try:
        projection_root.mkdir(mode=0o700)
        objects = {item.path: item for item in plan.manifest.objects}
        for item in plan.manifest.objects:
            if item.kind == "directory":
                (projection_root / item.path).mkdir(mode=0o700)
        for relative, content in plan.regular_files:
            destination = projection_root / relative
            with destination.open("xb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            destination.chmod(objects[relative].mode)
        directories = sorted(
            (item for item in plan.manifest.objects if item.kind == "directory"),
            key=lambda item: (-len(PurePosixPath(item.path).parts), item.path),
        )
        for item in directories:
            (projection_root / item.path).chmod(item.mode)
        projection_root.chmod(0o555)
        _fsync_directory(projection_root)
        _fsync_directory(projection_root.parent)
    except FileExistsError as exc:
        raise PhysicsAuditorIntegrityError("projection materialization raced") from exc
    except OSError as exc:
        raise PhysicsAuditorIntegrityError("projection could not be materialized") from exc
    verify_physics_auditor_projection(plan.manifest, projection_root)


def verify_physics_auditor_projection(
    manifest: PhysicsAuditorProjectionManifestV1,
    projection_root: Path,
) -> None:
    """Verify the complete tree without following links or accepting extra objects."""
    try:
        root = projection_root.resolve(strict=True)
        root_status = projection_root.lstat()
    except (OSError, RuntimeError) as exc:
        raise PhysicsAuditorIntegrityError("projected workspace is unavailable") from exc
    if root != projection_root or not stat.S_ISDIR(root_status.st_mode):
        raise PhysicsAuditorIntegrityError("projected workspace root is unsafe")
    expected = {item.path: item for item in manifest.objects}
    observed: set[str] = set()

    def inspect(directory: Path, prefix: PurePosixPath | None = None) -> None:
        try:
            children = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError as exc:
            raise PhysicsAuditorIntegrityError(
                "projected workspace could not be enumerated"
            ) from exc
        for child in children:
            relative_path = PurePosixPath(child.name) if prefix is None else prefix / child.name
            relative = relative_path.as_posix()
            if relative in observed or len(observed) >= MAX_PHYSICS_AUDITOR_FILES:
                raise PhysicsAuditorIntegrityError("projected workspace object set is invalid")
            observed.add(relative)
            item = expected.get(relative)
            try:
                status = child.stat(follow_symlinks=False)
            except OSError as exc:
                raise PhysicsAuditorIntegrityError(
                    "projected workspace object is unavailable"
                ) from exc
            if item is None or stat.S_IMODE(status.st_mode) != item.mode:
                raise PhysicsAuditorIntegrityError("projected workspace manifest changed")
            if item.kind == "directory":
                if not stat.S_ISDIR(status.st_mode):
                    raise PhysicsAuditorIntegrityError("projected directory type changed")
                inspect(Path(child.path), relative_path)
            elif item.kind == "regular":
                if not stat.S_ISREG(status.st_mode) or status.st_size != item.byte_length:
                    raise PhysicsAuditorIntegrityError("projected file metadata changed")
                try:
                    content = _bounded_read(Path(child.path))
                except (PhysicsAuditorInputError, PhysicsAuditorIntegrityError) as exc:
                    raise PhysicsAuditorIntegrityError(
                        "projected file could not be reverified"
                    ) from exc
                if hashlib.sha256(content).hexdigest() != item.sha256:
                    raise PhysicsAuditorIntegrityError("projected file content changed")
            else:  # pragma: no cover - strict model excludes this branch
                raise PhysicsAuditorIntegrityError("projected workspace type is unsupported")

    inspect(root)
    if observed != set(expected):
        raise PhysicsAuditorIntegrityError("projected workspace is incomplete")


def _authority_files(
    contract: PhysicsTaskContractV1,
    evidence_index: PhysicsAuditorEvidenceIndexV1,
    changed_paths: PhysicsAuditorChangedPathManifestV1,
) -> tuple[tuple[str, bytes], ...]:
    prefix = AUTHORITY_DIRECTORY
    return (
        (f"{prefix}/changed-path-manifest.json", changed_paths.to_canonical_json()),
        (f"{prefix}/evidence-index.json", evidence_index.to_canonical_json()),
        (
            f"{prefix}/oracle-proof-identities.json",
            canonical_json(
                [
                    {
                        "completion_proof_id": item.completion_proof_id,
                        "completion_proof_sha256": item.completion_proof_sha256,
                        "oracle_id": item.oracle_id,
                    }
                    for item in evidence_index.oracle_evidence
                    if item.availability == "verified"
                ]
            ),
        ),
        (
            f"{prefix}/oracle-result-summaries.json",
            canonical_json(
                [item.model_dump(mode="json") for item in evidence_index.oracle_evidence]
            ),
        ),
        (f"{prefix}/output-schema.json", canonical_json(PHYSICS_AUDIT_REPORT_OUTPUT_SCHEMA)),
        (f"{prefix}/physics-contract.json", contract.to_canonical_json()),
    )


def _add_parent_directories(
    entries: dict[str, tuple[PhysicsAuditorProjectionObjectV1, bytes | None]],
) -> None:
    parents: set[str] = set()
    for relative in tuple(entries):
        path = PurePosixPath(relative)
        for parent in path.parents:
            if parent == PurePosixPath("."):
                break
            parents.add(parent.as_posix())
    for relative in sorted(parents):
        existing = entries.get(relative)
        if existing is not None:
            if existing[0].kind != "directory":
                raise PhysicsAuditorInputError("projection destinations overlap ambiguously")
            continue
        entries[relative] = (
            PhysicsAuditorProjectionObjectV1(
                path=relative,
                kind="directory",
                mode=0o555,
                byte_length=0,
                sha256=_EMPTY_SHA256,
                authority="engine_control",
            ),
            None,
        )


def _safe_relative_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or value != value.strip()
        or "\\" in value
        or "\x00" in value
        or path.is_absolute()
        or path.as_posix() != value
        or any(part in {"", ".", ".."} for part in path.parts)
        or any(part.casefold() in _FORBIDDEN_COMPONENTS for part in path.parts)
    ):
        raise PhysicsAuditorInputError("projection path is protected or unsafe")
    return value


def _canonical_workspace(path: Path) -> Path:
    try:
        absolute = Path(os.path.abspath(path))
        resolved = path.resolve(strict=True)
        status = path.lstat()
    except (OSError, RuntimeError, ValueError) as exc:
        raise PhysicsAuditorInputError("projection source workspace is unavailable") from exc
    if absolute != resolved or path.is_symlink() or not stat.S_ISDIR(status.st_mode):
        raise PhysicsAuditorInputError("projection source workspace is unsafe")
    return resolved


def _bounded_read(path: Path) -> bytes:
    descriptor = -1
    try:
        status = path.lstat()
        if not stat.S_ISREG(status.st_mode):
            raise PhysicsAuditorInputError("projected input changed type")
        if status.st_size > MAX_PHYSICS_AUDITOR_PROJECTED_FILE_BYTES:
            raise PhysicsAuditorInputError("projected file exceeds its byte bound")
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        before = os.fstat(descriptor)
        chunks: list[bytes] = []
        observed = 0
        while observed <= MAX_PHYSICS_AUDITOR_PROJECTED_FILE_BYTES:
            chunk = os.read(
                descriptor,
                min(
                    1024 * 1024,
                    MAX_PHYSICS_AUDITOR_PROJECTED_FILE_BYTES + 1 - observed,
                ),
            )
            if not chunk:
                break
            chunks.append(chunk)
            observed += len(chunk)
        after = os.fstat(descriptor)
        current = path.lstat()
    except PhysicsAuditorInputError:
        raise
    except OSError as exc:
        raise PhysicsAuditorInputError("projected file could not be read") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if (
        not stat.S_ISREG(before.st_mode)
        or observed > MAX_PHYSICS_AUDITOR_PROJECTED_FILE_BYTES
        or len(b"".join(chunks)) != before.st_size
        or (before.st_dev, before.st_ino, before.st_mode, before.st_size, before.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_mode, after.st_size, after.st_mtime_ns)
        or (after.st_dev, after.st_ino, after.st_mode, after.st_size, after.st_mtime_ns)
        != (current.st_dev, current.st_ino, current.st_mode, current.st_size, current.st_mtime_ns)
    ):
        raise PhysicsAuditorIntegrityError("projected source changed while being read")
    return b"".join(chunks)


def _ignored_paths(workspace: Path, paths: tuple[str, ...]) -> frozenset[str]:
    if not paths:
        return frozenset()
    payload = b"\x00".join(os.fsencode(path) for path in paths) + b"\x00"
    try:
        completed = subprocess.run(
            (_GIT, "-C", workspace, "check-ignore", "--stdin", "-z"),
            input=payload,
            capture_output=True,
            check=False,
            timeout=30,
            env=_GIT_ENVIRONMENT,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PhysicsAuditorInputError("ignored-file projection check failed") from exc
    if completed.returncode not in {0, 1} or len(completed.stdout) > 4 * 1024 * 1024:
        raise PhysicsAuditorInputError("ignored-file projection check failed")
    raw = completed.stdout.split(b"\x00")
    if raw and raw[-1] == b"":
        raw.pop()
    try:
        return frozenset(item.decode("utf-8") for item in raw)
    except UnicodeDecodeError as exc:
        raise PhysicsAuditorInputError("ignored-file path is not UTF-8") from exc


def _reject_nested_repository(workspace: Path, relative: str) -> None:
    path = PurePosixPath(relative)
    for parent in tuple(path.parents)[:-1]:
        directory = workspace / parent.as_posix()
        marker = directory / ".git"
        try:
            parent_status = directory.lstat()
            if stat.S_ISLNK(parent_status.st_mode) or not stat.S_ISDIR(parent_status.st_mode):
                raise PhysicsAuditorInputError(
                    "projection ancestors must be real workspace directories"
                )
            if marker.exists() or marker.is_symlink():
                raise PhysicsAuditorInputError(
                    "nested repositories are forbidden in the auditor projection"
                )
        except OSError as exc:
            raise PhysicsAuditorInputError("nested repository check failed") from exc


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
