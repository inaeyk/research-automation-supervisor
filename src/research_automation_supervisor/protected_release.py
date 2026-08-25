#!/usr/bin/python3 -I
"""Protected release installation contract for the externally trusted bootstrap.

The callable functions are packaged into a distribution-owned helper at the fixed
``PROTECTED_RELEASE_INSTALLER`` path.  Running this module from a source checkout is
not a privileged installation path and cannot establish bootstrap provenance.

The file is deliberately standard-library-only.  Unprivileged release preparation
copies its exact bytes to both fixed authority executables; direct execution dispatches
by the installed basename only after an administrator has installed and approved it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal

PROTECTED_RELEASE_INSTALLER = Path(
    "/usr/libexec/research-supervisor/install-protected-release"
)
PROTECTED_RELEASE_VERIFIER = Path(
    "/usr/libexec/research-supervisor/verify-protected-release"
)
PROTECTED_RELEASE_APPROVAL = Path(
    "/usr/share/research-supervisor-release-authority/approved-release-v1.json"
)
PROTECTED_RELEASE_UPDATE_APPROVAL = Path(
    "/usr/share/research-supervisor-release-authority/approved-release-update-v1.json"
)
PROTECTED_RELEASE_CANDIDATE = Path(
    "/var/tmp/research-supervisor-release-candidate"
)
PROTECTED_RELEASE_ROOT = Path("/opt/research-supervisor-release")
PROTECTED_RELEASE_RECEIPT = Path(
    "/var/lib/research-supervisor-release-authority/installed-release-v1.json"
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RELEASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_MAXIMUM_MANIFEST_BYTES = 4 * 1024 * 1024
_MAXIMUM_RELEASE_FILES = 4096
_MANDATORY_RELEASE_FILES = frozenset(
    {
        PurePosixPath("artifacts/codex"),
        PurePosixPath(
            "artifacts/research_automation_supervisor-0.2.0-py3-none-any.whl"
        ),
        PurePosixPath("managed-codex-approval-v1.json"),
        PurePosixPath("scripts/install-research-supervisor.sh"),
        PurePosixPath("scripts/install-managed-codex.sh"),
        PurePosixPath("scripts/install-core-authority-service.sh"),
        PurePosixPath("scripts/run-protected-python.sh"),
        PurePosixPath("scripts/protected-managed-codex-entry.py"),
        PurePosixPath("scripts/research-supervisor-core-authority.service"),
        PurePosixPath("src/research_automation_supervisor/__init__.py"),
        PurePosixPath("src/research_automation_supervisor/custodian_errors.py"),
        PurePosixPath("src/research_automation_supervisor/doctor.py"),
        PurePosixPath("src/research_automation_supervisor/errors.py"),
        PurePosixPath("src/research_automation_supervisor/managed_codex.py"),
        PurePosixPath(
            "src/research_automation_supervisor/managed_codex_installer.py"
        ),
    }
)
_CODE_MODE_HOST_RELEASE_FILES = frozenset(
    {PurePosixPath("artifacts/codex-code-mode-host")}
)


class ProtectedReleaseSecurityError(ValueError):
    """The externally approved protected-release contract failed closed."""


@dataclass(frozen=True)
class ApprovedReleaseFile:
    """One exact data object approved by the external release authority."""

    path: PurePosixPath
    sha256: str
    mode: int


@dataclass(frozen=True)
class ApprovedRelease:
    """Strict protected approval manifest."""

    release_id: str
    files: tuple[ApprovedReleaseFile, ...]
    manifest_sha256: str
    update_from_manifest_sha256: str | None


@dataclass(frozen=True)
class ProtectedReleaseLayout:
    """Fixed production layout with explicit overrides for unprivileged simulation."""

    authority_executable: Path = PROTECTED_RELEASE_INSTALLER
    verifier_executable: Path = PROTECTED_RELEASE_VERIFIER
    approval: Path = PROTECTED_RELEASE_APPROVAL
    update_approval: Path = PROTECTED_RELEASE_UPDATE_APPROVAL
    candidate_root: Path = PROTECTED_RELEASE_CANDIDATE
    release_root: Path = PROTECTED_RELEASE_ROOT
    receipt: Path = PROTECTED_RELEASE_RECEIPT
    authority_trust_root: Path = Path("/")
    approval_trust_root: Path = Path("/")
    release_trust_root: Path = Path("/")
    receipt_trust_root: Path = Path("/")
    authority_uid: int = 0
    authority_gid: int = 0


@dataclass(frozen=True)
class ProtectedReleaseInstallResult:
    """Deterministic result from the simulated or real protected boundary."""

    disposition: Literal["installed", "unchanged", "updated"]
    release_id: str
    manifest_sha256: str
    release_root: Path


PRODUCTION_PROTECTED_RELEASE_LAYOUT = ProtectedReleaseLayout()


def load_approved_release(
    layout: ProtectedReleaseLayout,
    *,
    approval_path: Path | None = None,
) -> ApprovedRelease:
    """Load release identity only from separately protected approval metadata."""
    _validate_production_authority_selection(layout)
    selected_approval = layout.approval if approval_path is None else approval_path
    if selected_approval not in {layout.approval, layout.update_approval}:
        raise ProtectedReleaseSecurityError("protected release approval selection is not fixed")
    raw = _read_protected_file(
        selected_approval,
        trust_root=layout.approval_trust_root,
        owner_uid=layout.authority_uid,
        owner_gid=layout.authority_gid,
        mode=0o644,
        maximum_bytes=_MAXIMUM_MANIFEST_BYTES,
    )
    try:
        value: Any = json.loads(raw.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtectedReleaseSecurityError("protected release approval is malformed") from exc
    schema_version = value.get("schema_version") if isinstance(value, dict) else None
    versioned_keys = {
        1: {"schema_version", "release_id", "files"},
        2: {
            "schema_version",
            "release_id",
            "files",
            "update_from_manifest_sha256",
        },
    }
    expected_keys = (
        versioned_keys.get(schema_version) if isinstance(schema_version, int) else None
    )
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise ProtectedReleaseSecurityError("protected release approval schema is invalid")
    release_id = value.get("release_id")
    raw_files = value.get("files")
    update_from = value.get("update_from_manifest_sha256")
    if not isinstance(release_id, str):
        raise ProtectedReleaseSecurityError("protected release approval identity is invalid")
    if _RELEASE_ID.fullmatch(release_id) is None:
        raise ProtectedReleaseSecurityError("protected release approval identity is invalid")
    if update_from is not None and (
        not isinstance(update_from, str) or _SHA256.fullmatch(update_from) is None
    ):
        raise ProtectedReleaseSecurityError(
            "protected release update authority is invalid"
        )
    if (
        not isinstance(raw_files, list)
        or not raw_files
        or len(raw_files) > _MAXIMUM_RELEASE_FILES
    ):
        raise ProtectedReleaseSecurityError("protected release file approval is invalid")
    files: list[ApprovedReleaseFile] = []
    seen: set[PurePosixPath] = set()
    for item in raw_files:
        if not isinstance(item, dict) or set(item) != {"path", "sha256", "mode"}:
            raise ProtectedReleaseSecurityError("protected release file schema is invalid")
        path_text = item.get("path")
        digest = item.get("sha256")
        mode_text = item.get("mode")
        if not isinstance(path_text, str) or not isinstance(digest, str):
            raise ProtectedReleaseSecurityError("protected release file identity is invalid")
        relative = PurePosixPath(path_text)
        if (
            relative.is_absolute()
            or relative.as_posix() != path_text
            or not relative.parts
            or ".." in relative.parts
            or "." in relative.parts
            or relative in seen
            or _SHA256.fullmatch(digest) is None
            or mode_text not in {"0644", "0755"}
        ):
            raise ProtectedReleaseSecurityError("protected release file identity is invalid")
        seen.add(relative)
        files.append(
            ApprovedReleaseFile(
                path=relative,
                sha256=digest,
                mode=int(mode_text, 8),
            )
        )
    if not _MANDATORY_RELEASE_FILES.issubset(seen):
        raise ProtectedReleaseSecurityError("protected release entrypoint approval is incomplete")
    if schema_version == 2 and not _CODE_MODE_HOST_RELEASE_FILES.issubset(seen):
        raise ProtectedReleaseSecurityError(
            "protected release code-mode host approval is incomplete"
        )
    if not any(
        path.parts[0] == "wheelhouse" and path.suffix == ".whl" for path in seen
    ):
        raise ProtectedReleaseSecurityError("protected release wheelhouse approval is incomplete")
    return ApprovedRelease(
        release_id=release_id,
        files=tuple(sorted(files, key=lambda item: item.path.as_posix())),
        manifest_sha256=hashlib.sha256(raw).hexdigest(),
        update_from_manifest_sha256=update_from,
    )


def install_approved_release(
    layout: ProtectedReleaseLayout = PRODUCTION_PROTECTED_RELEASE_LAYOUT,
) -> ProtectedReleaseInstallResult:
    """Copy exact approved candidate bytes into the fixed protected destination."""
    approval = load_approved_release(layout)
    _verify_candidate_tree(layout.candidate_root, approval)
    _validate_protected_directory(
        layout.release_root.parent,
        trust_root=layout.release_trust_root,
        owner_uid=layout.authority_uid,
        owner_gid=layout.authority_gid,
    )
    _validate_protected_directory(
        layout.receipt.parent,
        trust_root=layout.receipt_trust_root,
        owner_uid=layout.authority_uid,
        owner_gid=layout.authority_gid,
    )
    release_exists = os.path.lexists(layout.release_root)
    receipt_exists = os.path.lexists(layout.receipt)
    if release_exists or receipt_exists:
        if not (release_exists and receipt_exists):
            raise ProtectedReleaseSecurityError(
                "protected release installation is an incomplete generation"
            )
        verified = verify_installed_release(layout)
        if (
            verified.release_id != approval.release_id
            or verified.manifest_sha256 != approval.manifest_sha256
        ):
            raise ProtectedReleaseSecurityError(
                "protected release update requires external distribution recovery"
            )
        return ProtectedReleaseInstallResult(
            "unchanged",
            approval.release_id,
            approval.manifest_sha256,
            layout.release_root,
        )
    if approval.update_from_manifest_sha256 is not None:
        raise ProtectedReleaseSecurityError(
            "protected release update approval cannot initialize an absent installation"
        )

    staging = _stage_approved_candidate(layout, approval)
    installed = False
    try:
        if os.path.lexists(layout.release_root):
            raise ProtectedReleaseSecurityError("protected release destination changed")
        os.rename(staging, layout.release_root)
        installed = True
        _fsync_directory(layout.release_root.parent)
        _write_protected_receipt(layout, approval)
        verified = verify_installed_release(layout)
        return ProtectedReleaseInstallResult(
            "installed",
            verified.release_id,
            verified.manifest_sha256,
            layout.release_root,
        )
    finally:
        if not installed and os.path.lexists(staging):
            _remove_staging_tree(staging)


def update_approved_release(
    layout: ProtectedReleaseLayout = PRODUCTION_PROTECTED_RELEASE_LAYOUT,
) -> ProtectedReleaseInstallResult:
    """Replace one exact installed release through separately protected update data."""
    current = verify_installed_release(layout)
    approval = load_approved_release(
        layout,
        approval_path=layout.update_approval,
    )
    if (
        approval.release_id != current.release_id
        or approval.update_from_manifest_sha256 != current.manifest_sha256
        or approval.manifest_sha256 == current.manifest_sha256
    ):
        raise ProtectedReleaseSecurityError(
            "protected release update authority does not match the installed identity"
        )
    _verify_candidate_tree(layout.candidate_root, approval)
    staging = _stage_approved_candidate(layout, approval)
    previous = Path(
        tempfile.mkdtemp(
            prefix=".research-supervisor-release.previous.",
            dir=layout.release_root.parent,
        )
    )
    previous.rmdir()
    replaced = False
    try:
        os.rename(layout.release_root, previous)
        os.rename(staging, layout.release_root)
        replaced = True
        _fsync_directory(layout.release_root.parent)
        _write_protected_receipt(layout, approval)
        _promote_update_approval(layout)
        verified = verify_installed_release(layout)
        _remove_staging_tree(previous)
        layout.update_approval.unlink()
        _fsync_directory(layout.update_approval.parent)
        return ProtectedReleaseInstallResult(
            "updated",
            verified.release_id,
            verified.manifest_sha256,
            verified.release_root,
        )
    finally:
        if not replaced and os.path.lexists(staging):
            _remove_staging_tree(staging)


def verify_installed_release(
    layout: ProtectedReleaseLayout = PRODUCTION_PROTECTED_RELEASE_LAYOUT,
) -> ProtectedReleaseInstallResult:
    """Verify protected approval, exact installed bytes, and protected receipt."""
    approval = load_approved_release(layout)
    receipt = _read_protected_file(
        layout.receipt,
        trust_root=layout.receipt_trust_root,
        owner_uid=layout.authority_uid,
        owner_gid=layout.authority_gid,
        mode=0o644,
        maximum_bytes=16 * 1024,
    )
    try:
        value: Any = json.loads(receipt.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtectedReleaseSecurityError("protected release receipt is malformed") from exc
    expected = {
        "schema_version": 1,
        "release_id": approval.release_id,
        "manifest_sha256": approval.manifest_sha256,
        "release_root": str(layout.release_root),
    }
    if value != expected:
        raise ProtectedReleaseSecurityError("protected release receipt was substituted")
    _verify_release_tree(
        layout.release_root,
        approval,
        layout.authority_uid,
        layout.authority_gid,
    )
    return ProtectedReleaseInstallResult(
        "unchanged",
        approval.release_id,
        approval.manifest_sha256,
        layout.release_root,
    )


def _stage_approved_candidate(
    layout: ProtectedReleaseLayout,
    approval: ApprovedRelease,
) -> Path:
    candidate_descriptor = _open_candidate_root(layout.candidate_root)
    staging = Path(
        tempfile.mkdtemp(
            prefix=".research-supervisor-release.",
            dir=layout.release_root.parent,
        )
    )
    try:
        os.chmod(staging, 0o755)
        os.chown(staging, layout.authority_uid, layout.authority_gid)
        for approved in approval.files:
            _copy_approved_candidate_file(
                candidate_descriptor,
                staging,
                approved,
                owner_uid=layout.authority_uid,
                owner_gid=layout.authority_gid,
            )
        _verify_release_tree(
            staging,
            approval,
            layout.authority_uid,
            layout.authority_gid,
        )
        return staging
    except BaseException:
        if os.path.lexists(staging):
            _remove_staging_tree(staging)
        raise
    finally:
        os.close(candidate_descriptor)


def _promote_update_approval(layout: ProtectedReleaseLayout) -> None:
    raw = _read_protected_file(
        layout.update_approval,
        trust_root=layout.approval_trust_root,
        owner_uid=layout.authority_uid,
        owner_gid=layout.authority_gid,
        mode=0o644,
        maximum_bytes=_MAXIMUM_MANIFEST_BYTES,
    )
    _atomic_write_protected_bytes(
        layout.approval,
        raw,
        owner_uid=layout.authority_uid,
        owner_gid=layout.authority_gid,
        mode=0o644,
    )


def _validate_production_authority_selection(layout: ProtectedReleaseLayout) -> None:
    if (
        layout == PRODUCTION_PROTECTED_RELEASE_LAYOUT
        and layout.authority_executable != PROTECTED_RELEASE_INSTALLER
    ):
        raise ProtectedReleaseSecurityError("privileged release authority is not fixed")
    _read_protected_file(
        layout.authority_executable,
        trust_root=layout.authority_trust_root,
        owner_uid=layout.authority_uid,
        owner_gid=layout.authority_gid,
        mode=0o755,
        maximum_bytes=None,
    )
    _read_protected_file(
        layout.verifier_executable,
        trust_root=layout.authority_trust_root,
        owner_uid=layout.authority_uid,
        owner_gid=layout.authority_gid,
        mode=0o755,
        maximum_bytes=None,
    )


def _verify_candidate_tree(root: Path, approval: ApprovedRelease) -> None:
    """Reject any missing, extra, linked, mistyped, or mode-mismatched input data."""
    expected = {item.path.as_posix(): item for item in approval.files}
    observed: set[str] = set()
    try:
        root_status = root.lstat()
        if not stat.S_ISDIR(root_status.st_mode) or stat.S_ISLNK(root_status.st_mode):
            raise ProtectedReleaseSecurityError("release candidate data root is unsafe")
        for directory, directories, files in os.walk(root, followlinks=False):
            parent = Path(directory)
            for name in directories:
                child = parent / name
                status = child.lstat()
                if (
                    not stat.S_ISDIR(status.st_mode)
                    or stat.S_ISLNK(status.st_mode)
                    or stat.S_IMODE(status.st_mode) != 0o755
                ):
                    raise ProtectedReleaseSecurityError(
                        "release candidate directory metadata is unsafe"
                    )
            for name in files:
                child = parent / name
                relative = child.relative_to(root).as_posix()
                approved = expected.get(relative)
                if approved is None:
                    raise ProtectedReleaseSecurityError(
                        "release candidate contains unapproved data"
                    )
                status = child.lstat()
                if (
                    not stat.S_ISREG(status.st_mode)
                    or stat.S_ISLNK(status.st_mode)
                    or status.st_nlink != 1
                    or stat.S_IMODE(status.st_mode) != approved.mode
                ):
                    raise ProtectedReleaseSecurityError(
                        "release candidate file metadata is unsafe"
                    )
                observed.add(relative)
    except OSError as exc:
        raise ProtectedReleaseSecurityError("release candidate data is unavailable") from exc
    if observed != set(expected):
        raise ProtectedReleaseSecurityError("release candidate is incomplete")


def _open_candidate_root(path: Path) -> int:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        status = os.fstat(descriptor)
    except OSError as exc:
        raise ProtectedReleaseSecurityError("release candidate data is unavailable") from exc
    if not stat.S_ISDIR(status.st_mode):
        os.close(descriptor)
        raise ProtectedReleaseSecurityError("release candidate data root is unsafe")
    return descriptor


def _open_relative_file(root_descriptor: int, relative: PurePosixPath) -> int:
    current = os.dup(root_descriptor)
    try:
        for component in relative.parts[:-1]:
            next_descriptor = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=current,
            )
            os.close(current)
            current = next_descriptor
        return os.open(relative.parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=current)
    except OSError as exc:
        raise ProtectedReleaseSecurityError("approved candidate file is unavailable") from exc
    finally:
        os.close(current)


def _copy_approved_candidate_file(
    root_descriptor: int,
    staging: Path,
    approved: ApprovedReleaseFile,
    *,
    owner_uid: int,
    owner_gid: int,
) -> None:
    source = _open_relative_file(root_descriptor, approved.path)
    target = staging.joinpath(*approved.path.parts)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    _fix_directory_chain(target.parent, staging, owner_uid, owner_gid)
    target_descriptor = -1
    try:
        before = os.fstat(source)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ProtectedReleaseSecurityError("approved candidate object is not plain data")
        target_descriptor = os.open(
            target,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            approved.mode,
        )
        digest = hashlib.sha256()
        while True:
            chunk = os.read(source, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            _write_all(target_descriptor, chunk)
        after = os.fstat(source)
        if _stable_identity(before) != _stable_identity(after):
            raise ProtectedReleaseSecurityError("approved candidate changed while copying")
        if digest.hexdigest() != approved.sha256:
            raise ProtectedReleaseSecurityError("candidate bytes are not externally approved")
        os.fchmod(target_descriptor, approved.mode)
        os.fchown(target_descriptor, owner_uid, owner_gid)
        os.fsync(target_descriptor)
    except OSError as exc:
        raise ProtectedReleaseSecurityError("approved candidate could not be staged") from exc
    finally:
        os.close(source)
        if target_descriptor >= 0:
            os.close(target_descriptor)


def _verify_release_tree(
    root: Path,
    approval: ApprovedRelease,
    owner_uid: int,
    owner_gid: int,
) -> None:
    try:
        root_status = root.lstat()
    except OSError as exc:
        raise ProtectedReleaseSecurityError("protected release is unavailable") from exc
    if (
        not stat.S_ISDIR(root_status.st_mode)
        or stat.S_IMODE(root_status.st_mode) != 0o755
        or root_status.st_uid != owner_uid
        or root_status.st_gid != owner_gid
    ):
        raise ProtectedReleaseSecurityError("protected release root metadata is unsafe")
    expected = {item.path.as_posix(): item for item in approval.files}
    observed: set[str] = set()
    for directory, directories, files in os.walk(root, followlinks=False):
        parent = Path(directory)
        for name in directories:
            child = parent / name
            status = child.lstat()
            if (
                not stat.S_ISDIR(status.st_mode)
                or stat.S_IMODE(status.st_mode) != 0o755
                or status.st_uid != owner_uid
                or status.st_gid != owner_gid
            ):
                raise ProtectedReleaseSecurityError("protected release directory is unsafe")
        for name in files:
            child = parent / name
            relative = child.relative_to(root).as_posix()
            approved = expected.get(relative)
            if approved is None:
                raise ProtectedReleaseSecurityError("protected release contains unapproved data")
            raw = _read_exact_installed_file(child, approved, owner_uid, owner_gid)
            if hashlib.sha256(raw).hexdigest() != approved.sha256:
                raise ProtectedReleaseSecurityError("protected release bytes were substituted")
            observed.add(relative)
    if observed != set(expected):
        raise ProtectedReleaseSecurityError("protected release is incomplete")


def _read_exact_installed_file(
    path: Path,
    approved: ApprovedReleaseFile,
    owner_uid: int,
    owner_gid: int,
) -> bytes:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        before = os.fstat(descriptor)
        raw = _read_all(descriptor, None)
        after = os.fstat(descriptor)
    except OSError as exc:
        raise ProtectedReleaseSecurityError("protected release file is unavailable") from exc
    finally:
        if "descriptor" in locals():
            os.close(descriptor)
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or before.st_uid != owner_uid
        or before.st_gid != owner_gid
        or stat.S_IMODE(before.st_mode) != approved.mode
        or _stable_identity(before) != _stable_identity(after)
    ):
        raise ProtectedReleaseSecurityError("protected release file metadata is unsafe")
    return raw


def _write_protected_receipt(
    layout: ProtectedReleaseLayout,
    approval: ApprovedRelease,
) -> None:
    raw = _render_json(
        {
            "schema_version": 1,
            "release_id": approval.release_id,
            "manifest_sha256": approval.manifest_sha256,
            "release_root": str(layout.release_root),
        }
    )
    _atomic_write_protected_bytes(
        layout.receipt,
        raw,
        owner_uid=layout.authority_uid,
        owner_gid=layout.authority_gid,
        mode=0o644,
    )


def _atomic_write_protected_bytes(
    path: Path,
    raw: bytes,
    *,
    owner_uid: int,
    owner_gid: int,
    mode: int,
) -> None:
    descriptor, temporary_text = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
    )
    temporary = Path(temporary_text)
    try:
        os.fchmod(descriptor, mode)
        os.fchown(descriptor, owner_uid, owner_gid)
        _write_all(descriptor, raw)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if os.path.lexists(temporary):
            temporary.unlink()


def _read_protected_file(
    path: Path,
    *,
    trust_root: Path,
    owner_uid: int,
    owner_gid: int,
    mode: int,
    maximum_bytes: int | None,
) -> bytes:
    _validate_protected_directory(
        path.parent,
        trust_root=trust_root,
        owner_uid=owner_uid,
        owner_gid=owner_gid,
    )
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        before = os.fstat(descriptor)
        raw = _read_all(descriptor, maximum_bytes)
        after = os.fstat(descriptor)
    except OSError as exc:
        raise ProtectedReleaseSecurityError("protected release authority is unavailable") from exc
    finally:
        if "descriptor" in locals():
            os.close(descriptor)
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or before.st_uid != owner_uid
        or before.st_gid != owner_gid
        or stat.S_IMODE(before.st_mode) != mode
        or _stable_identity(before) != _stable_identity(after)
    ):
        raise ProtectedReleaseSecurityError("protected release authority metadata is unsafe")
    return raw


def _validate_protected_directory(
    path: Path,
    *,
    trust_root: Path,
    owner_uid: int,
    owner_gid: int,
) -> None:
    try:
        selected = Path(os.path.abspath(path))
        root = Path(os.path.abspath(trust_root))
        selected.relative_to(root)
        current = selected
        while True:
            status = current.lstat()
            if (
                not stat.S_ISDIR(status.st_mode)
                or status.st_uid != owner_uid
                or status.st_gid != owner_gid
                or status.st_mode & 0o022
            ):
                raise ProtectedReleaseSecurityError(
                    "protected release authority ancestry is unsafe"
                )
            if current == root:
                return
            current = current.parent
    except (OSError, RuntimeError, ValueError) as exc:
        if isinstance(exc, ProtectedReleaseSecurityError):
            raise
        raise ProtectedReleaseSecurityError(
            "protected release authority ancestry is unavailable"
        ) from exc


def _fix_directory_chain(path: Path, root: Path, owner_uid: int, owner_gid: int) -> None:
    current = path
    while current != root:
        os.chmod(current, 0o755)
        os.chown(current, owner_uid, owner_gid)
        current = current.parent


def _remove_staging_tree(root: Path) -> None:
    for directory, directories, files in os.walk(root, topdown=False, followlinks=False):
        parent = Path(directory)
        for name in files:
            (parent / name).unlink()
        for name in directories:
            (parent / name).rmdir()
    root.rmdir()


def _stable_identity(status: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        status.st_dev,
        status.st_ino,
        status.st_size,
        status.st_mtime_ns,
        status.st_ctime_ns,
    )


def _read_all(descriptor: int, maximum_bytes: int | None) -> bytes:
    chunks: list[bytes] = []
    observed = 0
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            return b"".join(chunks)
        observed += len(chunk)
        if maximum_bytes is not None and observed > maximum_bytes:
            raise ProtectedReleaseSecurityError("protected release authority is oversized")
        chunks.append(chunk)


def _write_all(descriptor: int, content: bytes) -> None:
    offset = 0
    while offset < len(content):
        written = os.write(descriptor, content[offset:])
        if written <= 0:
            raise OSError("short protected release write")
        offset += written


def _render_json(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _installed_authority_main(argv: list[str] | None = None) -> int:
    """Dispatch only fixed installed authority basenames under real root privilege."""
    arguments = sys.argv[1:] if argv is None else argv
    executable = Path(sys.argv[0])
    try:
        selected = executable.resolve(strict=True)
    except OSError:
        print("ERROR: protected release authority is unavailable", file=sys.stderr)
        return 2
    if os.geteuid() != 0:
        print(
            "ERROR: protected release authority requires administrator authorization",
            file=sys.stderr,
        )
        return 2
    try:
        if selected == PROTECTED_RELEASE_VERIFIER:
            if executable != PROTECTED_RELEASE_VERIFIER or executable.is_symlink():
                raise ProtectedReleaseSecurityError(
                    "protected release verifier selection is not fixed"
                )
            if arguments:
                raise ProtectedReleaseSecurityError(
                    "protected release verifier accepts no caller arguments"
                )
            verified = verify_installed_release()
            print(
                json.dumps(
                    {
                        "disposition": verified.disposition,
                        "manifest_sha256": verified.manifest_sha256,
                        "release_id": verified.release_id,
                        "release_root": str(verified.release_root),
                    },
                    sort_keys=True,
                )
            )
            return 0
        if selected != PROTECTED_RELEASE_INSTALLER:
            raise ProtectedReleaseSecurityError(
                "privileged release authority is outside the fixed distribution path"
            )
        if executable != PROTECTED_RELEASE_INSTALLER or executable.is_symlink():
            raise ProtectedReleaseSecurityError(
                "protected release installer selection is not fixed"
            )
        updating = len(arguments) == 2 and arguments[0] == "--update"
        if (
            (not updating and len(arguments) != 1)
            or not arguments
            or not arguments[-1]
        ):
            raise ProtectedReleaseSecurityError(
                "one fixed install/update operation and ordinary operator are required"
            )
        operator_name = arguments[-1]
        if updating:
            update_approved_release()
        else:
            install_approved_release()
        os.execv(
            "/bin/sh",
            [
                "/bin/sh",
                str(PROTECTED_RELEASE_ROOT / "scripts/install-research-supervisor.sh"),
                operator_name,
            ],
        )
    except (OSError, ProtectedReleaseSecurityError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(_installed_authority_main())
