"""Privileged managed-Codex install logic with deterministic simulation seams."""

from __future__ import annotations

import hashlib
import json
import os
import pwd
import re
import stat
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from subprocess import SubprocessError
from typing import Literal

from research_automation_supervisor.custodian_errors import CustodianEnvironmentError
from research_automation_supervisor.doctor import subprocess_runner
from research_automation_supervisor.managed_codex import (
    MANAGED_DATA_ROOT_RELATIVE,
    MINIMUM_MANAGED_CODEX_VERSION,
    ManagedCodexContract,
    ManagedCodexHomeAuthority,
    ManagedCodexHomeAuthorityContract,
    ManagedCodexIdentity,
    ManagedCodexSecurityError,
    load_managed_codex_home_authority,
    render_managed_codex_home_authority,
    render_managed_codex_receipt,
    verify_managed_codex_installation,
)

PROTECTED_RELEASE_ROOT = Path("/opt/research-supervisor-release")
APPROVAL_RELATIVE_PATH = Path("managed-codex-approval-v1.json")
ARTIFACT_RELATIVE_PATH = Path("artifacts/codex")
PENDING_RECEIPT_NAME = "managed-codex-install-pending-v1.json"
_QUALIFICATION_STATE_NAME = ".managed-codex-main-qualification"

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_RELEASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


@dataclass(frozen=True)
class ManagedCodexApproval:
    """Identity authorized by one independently protected release contract."""

    release_id: str
    sha256: str
    version: str
    update_from_sha256: str | None


@dataclass(frozen=True)
class ManagedCodexInstallerLayout:
    """Fixed-path install layout; tests may substitute an isolated authority root."""

    release_root: Path
    release_trust_root: Path
    approval: Path
    artifact: Path
    installation: ManagedCodexContract
    pending_receipt: Path
    authority_uid: int
    authority_gid: int


@dataclass(frozen=True)
class ManagedCodexInstallResult:
    disposition: Literal["installed", "unchanged", "updated"]
    identity: ManagedCodexIdentity


VersionProbe = Callable[[Path], str]
FaultInjector = Callable[[str], None]


def production_installer_layout() -> ManagedCodexInstallerLayout:
    """Return the non-caller-controlled production layout."""
    installation = ManagedCodexContract()
    return ManagedCodexInstallerLayout(
        release_root=PROTECTED_RELEASE_ROOT,
        release_trust_root=Path("/"),
        approval=PROTECTED_RELEASE_ROOT / APPROVAL_RELATIVE_PATH,
        artifact=PROTECTED_RELEASE_ROOT / ARTIFACT_RELATIVE_PATH,
        installation=installation,
        pending_receipt=installation.pending_receipt,
        authority_uid=0,
        authority_gid=0,
    )


def _qualification_installer_layout(
    release_root: Path,
) -> ManagedCodexInstallerLayout:
    """Derive the fixed unprivileged main-boundary backend from its release."""
    if os.geteuid() == 0:
        raise ManagedCodexSecurityError(
            "qualification installer layout is unavailable under privilege"
        )
    resolved_release = release_root.resolve(strict=True)
    state_root = resolved_release.parent / _QUALIFICATION_STATE_NAME
    system_root = state_root / "system"
    executable = system_root / "usr/bin/codex"
    receipt_root = system_root / "etc/research-supervisor-core"
    pending_receipt = receipt_root / PENDING_RECEIPT_NAME
    installation = ManagedCodexContract(
        executable=executable,
        receipt=receipt_root / "managed-codex-install-v1.json",
        pending_receipt=pending_receipt,
        executable_trust_root=state_root,
        receipt_trust_root=state_root,
        authority_uid=os.geteuid(),
        authority_gid=os.getegid(),
    )
    return ManagedCodexInstallerLayout(
        release_root=resolved_release,
        release_trust_root=resolved_release.parent,
        approval=resolved_release / APPROVAL_RELATIVE_PATH,
        artifact=resolved_release / ARTIFACT_RELATIVE_PATH,
        installation=installation,
        pending_receipt=pending_receipt,
        authority_uid=os.geteuid(),
        authority_gid=os.getegid(),
    )


def verify_protected_release_tree(layout: ManagedCodexInstallerLayout) -> None:
    """Reject mutable, linked, or substituted release staging before loading it."""
    _validate_anchored_path(
        layout.release_root,
        layout.release_trust_root,
        owner_uid=layout.authority_uid,
        owner_gid=layout.authority_gid,
        require_directory=True,
    )
    for root, directories, files in os.walk(layout.release_root, followlinks=False):
        root_path = Path(root)
        for name in (*directories, *files):
            candidate = root_path / name
            status = candidate.lstat()
            if (
                stat.S_ISLNK(status.st_mode)
                or status.st_uid != layout.authority_uid
                or status.st_gid != layout.authority_gid
                or status.st_mode & 0o022
                or (stat.S_ISREG(status.st_mode) and status.st_nlink != 1)
                or not (stat.S_ISDIR(status.st_mode) or stat.S_ISREG(status.st_mode))
            ):
                raise ManagedCodexSecurityError(
                    "protected release tree contains mutable or linked content"
                )
    if layout.approval != layout.release_root / APPROVAL_RELATIVE_PATH:
        raise ManagedCodexSecurityError("approval path is not fixed by the release contract")
    if layout.artifact != layout.release_root / ARTIFACT_RELATIVE_PATH:
        raise ManagedCodexSecurityError("artifact path is not fixed by the release contract")


def load_managed_codex_approval(
    layout: ManagedCodexInstallerLayout,
) -> ManagedCodexApproval:
    """Load exact identity only from the protected release approval manifest."""
    verify_protected_release_tree(layout)
    content = _read_exact_file(
        layout.approval,
        owner_uid=layout.authority_uid,
        owner_gid=layout.authority_gid,
        required_mode=0o644,
        maximum_bytes=16 * 1024,
    )
    try:
        value = json.loads(content.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManagedCodexSecurityError("managed Codex approval is malformed") from exc
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "release_id",
        "artifact",
        "sha256",
        "version",
        "update_from_sha256",
    }:
        raise ManagedCodexSecurityError("managed Codex approval schema is invalid")
    release_id = value.get("release_id")
    digest = value.get("sha256")
    version = value.get("version")
    update_from = value.get("update_from_sha256")
    if value.get("schema_version") != 1 or value.get("artifact") != str(
        ARTIFACT_RELATIVE_PATH
    ):
        raise ManagedCodexSecurityError("managed Codex approval artifact is invalid")
    if not isinstance(release_id, str) or _RELEASE_ID.fullmatch(release_id) is None:
        raise ManagedCodexSecurityError("managed Codex approval release is invalid")
    if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
        raise ManagedCodexSecurityError("managed Codex approval digest is invalid")
    if not isinstance(version, str) or _VERSION.fullmatch(version) is None:
        raise ManagedCodexSecurityError("managed Codex approval version is invalid")
    if _version_tuple(version) < _version_tuple(MINIMUM_MANAGED_CODEX_VERSION):
        raise ManagedCodexSecurityError("managed Codex approval version is unsupported")
    if update_from is not None and (
        not isinstance(update_from, str) or _SHA256.fullmatch(update_from) is None
    ):
        raise ManagedCodexSecurityError("managed Codex update authority is invalid")
    if update_from == digest:
        raise ManagedCodexSecurityError("managed Codex update authority is self-referential")
    return ManagedCodexApproval(release_id, digest, version, update_from)


def install_managed_codex(
    layout: ManagedCodexInstallerLayout,
    *,
    version_probe: VersionProbe,
    fault_injector: FaultInjector | None = None,
) -> ManagedCodexInstallResult:
    """Install exact opened artifact bytes under explicit lifecycle rules."""
    approval = load_managed_codex_approval(layout)
    _validate_destination_directories(layout)
    if _lexists(layout.pending_receipt):
        raise ManagedCodexSecurityError(
            "managed Codex installation is incomplete; explicit administrator recovery is required"
        )
    target_exists = _lexists(layout.installation.executable)
    receipt_exists = _lexists(layout.installation.receipt)
    existing: ManagedCodexIdentity | None = None
    disposition: Literal["installed", "unchanged", "updated"]
    if target_exists != receipt_exists:
        raise ManagedCodexSecurityError(
            "managed Codex destination and receipt are an incomplete generation"
        )
    if target_exists:
        existing = verify_managed_codex_installation(layout.installation)
        if existing.sha256 == approval.sha256:
            if existing.version != approval.version:
                raise ManagedCodexSecurityError(
                    "approved digest has inconsistent version authority"
                )
            return ManagedCodexInstallResult("unchanged", existing)
        if approval.update_from_sha256 != existing.sha256:
            raise ManagedCodexSecurityError(
                "a different installed identity lacks explicit update authority"
            )
        disposition = "updated"
    else:
        if approval.update_from_sha256 is not None:
            raise ManagedCodexSecurityError(
                "update approval cannot initialize an absent managed installation"
            )
        disposition = "installed"

    staged = _stage_approved_artifact(layout, approval)
    pending_written = False
    try:
        observed_version = version_probe(staged)
        if observed_version != approval.version:
            raise ManagedCodexSecurityError(
                "staged Codex version does not match protected approval"
            )
        if fault_injector is not None:
            fault_injector("staged")
        pending = _render_json(
            {
                "schema_version": 1,
                "release_id": approval.release_id,
                "sha256": approval.sha256,
                "version": approval.version,
                "previous_sha256": None if existing is None else existing.sha256,
            }
        )
        _atomic_write_protected(
            layout.pending_receipt,
            pending,
            mode=0o600,
            owner_uid=layout.authority_uid,
            owner_gid=layout.authority_gid,
        )
        pending_written = True
        if fault_injector is not None:
            fault_injector("pending_recorded")
        os.replace(staged, layout.installation.executable)
        _fsync_directory(layout.installation.executable.parent)
        if fault_injector is not None:
            fault_injector("executable_replaced")
        provisional = ManagedCodexIdentity(
            executable=layout.installation.executable,
            sha256=approval.sha256,
            version=approval.version,
            release_id=approval.release_id,
            device=0,
            inode=0,
        )
        _atomic_write_protected(
            layout.installation.receipt,
            render_managed_codex_receipt(provisional),
            mode=0o644,
            owner_uid=layout.authority_uid,
            owner_gid=layout.authority_gid,
        )
        if fault_injector is not None:
            fault_injector("receipt_replaced")
        layout.pending_receipt.unlink()
        pending_written = False
        _fsync_directory(layout.pending_receipt.parent)
        identity = verify_managed_codex_installation(layout.installation)
        return ManagedCodexInstallResult(disposition, identity)
    finally:
        if _lexists(staged):
            staged.unlink()
        if pending_written:
            _fsync_directory(layout.pending_receipt.parent)


def probe_staged_codex_version(path: Path) -> str:
    """Execute only protected staged bytes to confirm the approved exact version."""
    try:
        completed = subprocess_runner([str(path), "--version"], timeout=30)
    except (OSError, SubprocessError) as exc:
        raise ManagedCodexSecurityError("staged Codex version probe failed") from exc
    if completed.returncode != 0:
        raise ManagedCodexSecurityError("staged Codex version probe failed")
    match = re.search(r"(?<![0-9])([0-9]+\.[0-9]+\.[0-9]+)(?![0-9])", completed.stdout)
    if match is None:
        raise ManagedCodexSecurityError("staged Codex version output is invalid")
    return match.group(1)


def bind_managed_codex_home_authority(
    operator_name: str,
    *,
    contract: ManagedCodexHomeAuthorityContract | None = None,
) -> ManagedCodexHomeAuthority:
    """Create-once protected operator/home authority from the passwd database."""
    try:
        account = pwd.getpwnam(operator_name)
    except KeyError as exc:
        raise ManagedCodexSecurityError("ordinary operator account is unavailable") from exc
    if account.pw_uid == 0:
        raise ManagedCodexSecurityError("managed Codex operator must not be root")
    data_root = Path(account.pw_dir) / MANAGED_DATA_ROOT_RELATIVE
    authority = ManagedCodexHomeAuthority(
        operator_uid=account.pw_uid,
        data_root=data_root,
        codex_home=data_root / "codex-home",
    )
    selected = contract or ManagedCodexHomeAuthorityContract(
        operator_uid=account.pw_uid,
        expected_data_root=data_root,
        data_trust_root=Path(account.pw_dir),
    )
    if _lexists(selected.receipt):
        existing = load_managed_codex_home_authority(selected)
        if existing != authority:
            raise ManagedCodexSecurityError(
                "managed Codex home authority is already bound to another identity"
            )
        return existing
    _validate_anchored_path(
        selected.receipt.parent,
        selected.receipt_trust_root,
        owner_uid=selected.authority_uid,
        owner_gid=selected.authority_gid,
        require_directory=True,
    )
    _atomic_write_protected(
        selected.receipt,
        render_managed_codex_home_authority(authority),
        mode=0o644,
        owner_uid=selected.authority_uid,
        owner_gid=selected.authority_gid,
    )
    return load_managed_codex_home_authority(selected)


def _stage_approved_artifact(
    layout: ManagedCodexInstallerLayout,
    approval: ManagedCodexApproval,
) -> Path:
    source_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    source = os.open(layout.artifact, source_flags)
    staged_descriptor = -1
    staged_path: Path | None = None
    try:
        before = os.fstat(source)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != layout.authority_uid
            or before.st_gid != layout.authority_gid
            or stat.S_IMODE(before.st_mode) != 0o755
            or before.st_nlink != 1
        ):
            raise ManagedCodexSecurityError("approved Codex artifact metadata is unsafe")
        staged_descriptor, name = tempfile.mkstemp(
            prefix=".research-supervisor-codex.",
            dir=layout.installation.executable.parent,
        )
        staged_path = Path(name)
        digest = hashlib.sha256()
        prefix = b""
        while True:
            block = os.read(source, 1024 * 1024)
            if not block:
                break
            if not prefix:
                prefix = block[:4]
            digest.update(block)
            _write_all(staged_descriptor, block)
        after = os.fstat(source)
        current = layout.artifact.lstat()
        if prefix != b"\x7fELF":
            raise ManagedCodexSecurityError("approved Codex artifact is not standalone ELF")
        if digest.hexdigest() != approval.sha256:
            raise ManagedCodexSecurityError(
                "staged Codex bytes do not match protected approval"
            )
        if _stable_stat(before) != _stable_stat(after) or (
            before.st_dev,
            before.st_ino,
        ) != (current.st_dev, current.st_ino):
            raise ManagedCodexSecurityError("approved artifact changed while being copied")
        os.fchmod(staged_descriptor, 0o755)
        os.fsync(staged_descriptor)
        staged_status = os.fstat(staged_descriptor)
        if (
            staged_status.st_uid != layout.authority_uid
            or staged_status.st_gid != layout.authority_gid
            or stat.S_IMODE(staged_status.st_mode) != 0o755
            or staged_status.st_nlink != 1
        ):
            raise ManagedCodexSecurityError("managed Codex staging metadata is unsafe")
        return staged_path
    finally:
        os.close(source)
        if staged_descriptor >= 0:
            os.close(staged_descriptor)
        if sys.exc_info()[0] is not None and staged_path is not None and _lexists(staged_path):
            staged_path.unlink()


def _validate_destination_directories(layout: ManagedCodexInstallerLayout) -> None:
    _validate_anchored_path(
        layout.installation.executable.parent,
        layout.installation.executable_trust_root,
        owner_uid=layout.authority_uid,
        owner_gid=layout.authority_gid,
        require_directory=True,
    )
    _validate_anchored_path(
        layout.installation.receipt.parent,
        layout.installation.receipt_trust_root,
        owner_uid=layout.authority_uid,
        owner_gid=layout.authority_gid,
        require_directory=True,
    )
    if layout.pending_receipt.parent != layout.installation.receipt.parent:
        raise ManagedCodexSecurityError("pending receipt location is not fixed")


def _validate_anchored_path(
    path: Path,
    trust_root: Path,
    *,
    owner_uid: int,
    owner_gid: int,
    require_directory: bool,
) -> None:
    if not path.is_absolute() or not trust_root.is_absolute():
        raise ManagedCodexSecurityError("protected path is not absolute")
    absolute = Path(os.path.abspath(path))
    root = Path(os.path.abspath(trust_root))
    try:
        absolute.relative_to(root)
    except ValueError as exc:
        raise ManagedCodexSecurityError("protected path escapes its trust root") from exc
    current = absolute
    while True:
        status = current.lstat()
        if (
            stat.S_ISLNK(status.st_mode)
            or status.st_uid != owner_uid
            or status.st_gid != owner_gid
            or status.st_mode & 0o022
            or (require_directory and not stat.S_ISDIR(status.st_mode))
        ):
            raise ManagedCodexSecurityError("protected path metadata is unsafe")
        if current == root:
            return
        current = current.parent


def _read_exact_file(
    path: Path,
    *,
    owner_uid: int,
    owner_gid: int,
    required_mode: int,
    maximum_bytes: int,
) -> bytes:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != owner_uid
            or before.st_gid != owner_gid
            or stat.S_IMODE(before.st_mode) != required_mode
            or before.st_nlink != 1
            or before.st_size > maximum_bytes
        ):
            raise ManagedCodexSecurityError("protected file metadata is unsafe")
        content = bytearray()
        while True:
            block = os.read(descriptor, min(64 * 1024, maximum_bytes + 1 - len(content)))
            if not block:
                break
            content.extend(block)
            if len(content) > maximum_bytes:
                raise ManagedCodexSecurityError("protected file is too large")
        after = os.fstat(descriptor)
        if _stable_stat(before) != _stable_stat(after):
            raise ManagedCodexSecurityError("protected file changed while being read")
        return bytes(content)
    finally:
        os.close(descriptor)


def _atomic_write_protected(
    path: Path,
    content: bytes,
    *,
    mode: int,
    owner_uid: int,
    owner_gid: int,
) -> None:
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        os.fchmod(descriptor, mode)
        if os.fstat(descriptor).st_uid != owner_uid or os.fstat(descriptor).st_gid != owner_gid:
            os.fchown(descriptor, owner_uid, owner_gid)
        _write_all(descriptor, content)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if _lexists(temporary):
            temporary.unlink()


def _write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short protected-file write")
        view = view[written:]


def _stable_stat(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _version_tuple(value: str) -> tuple[int, int, int]:
    major, minor, patch = value.split(".")
    return int(major), int(minor), int(patch)


def _render_json(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=True, sort_keys=True) + "\n").encode("ascii")


def _lexists(path: Path) -> bool:
    return os.path.lexists(path)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def main(
    argv: list[str] | None = None,
    *,
    _qualification_release_root: Path | None = None,
) -> int:
    """Fixed privileged CLI loaded only after protected-release verification."""
    arguments = sys.argv[1:] if argv is None else argv
    qualification = _qualification_release_root is not None
    if qualification and os.geteuid() == 0:
        print(
            "qualification installer backend is unavailable under privilege",
            file=sys.stderr,
        )
        return 2
    if not qualification and os.geteuid() != 0:
        print("Run this setup operation with administrator authorization.", file=sys.stderr)
        return 2
    if not arguments:
        print("one fixed managed-Codex setup operation is required", file=sys.stderr)
        return 2
    if qualification and arguments != ["install"]:
        print("qualification permits only the fixed install operation", file=sys.stderr)
        return 2
    try:
        layout = (
            production_installer_layout()
            if _qualification_release_root is None
            else _qualification_installer_layout(_qualification_release_root)
        )
        if arguments == ["install"]:
            result = install_managed_codex(
                layout, version_probe=probe_staged_codex_version
            )
            if qualification:
                receipt = json.loads(
                    render_managed_codex_receipt(result.identity).decode("ascii")
                )
                print(
                    json.dumps(
                        {
                            "disposition": result.disposition,
                            "identity": {
                                "executable": str(result.identity.executable),
                                "release_id": result.identity.release_id,
                                "sha256": result.identity.sha256,
                                "version": result.identity.version,
                            },
                            "operation": "install",
                            "protected_receipt": receipt,
                            "schema_version": 1,
                        },
                        sort_keys=True,
                    )
                )
            else:
                print(
                    f"Managed Codex {result.identity.version} {result.disposition} at "
                    f"{result.identity.executable}."
                )
            return 0
        if arguments == ["verify"]:
            identity = verify_managed_codex_installation()
            print(f"Managed Codex {identity.version} identity verified.")
            return 0
        if len(arguments) == 2 and arguments[0] == "bind-home":
            authority = bind_managed_codex_home_authority(arguments[1])
            print(f"Managed Codex home authority bound for UID {authority.operator_uid}.")
            return 0
        print("unknown managed-Codex setup operation", file=sys.stderr)
        return 2
    except (
        CustodianEnvironmentError,
        ManagedCodexSecurityError,
        OSError,
        SubprocessError,
    ) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
