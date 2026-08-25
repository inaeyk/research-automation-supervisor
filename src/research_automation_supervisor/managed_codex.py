"""Canonical managed-Codex executable and credential-home security contract."""

from __future__ import annotations

import hashlib
import json
import os
import pwd
import re
import stat
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from research_automation_supervisor.custodian_errors import CustodianEnvironmentError

MANAGED_CODEX_EXECUTABLE = Path("/usr/bin/codex")
MANAGED_CODEX_CODE_MODE_HOST = Path("/usr/bin/codex-code-mode-host")
MANAGED_CODEX_RECEIPT = Path(
    "/etc/research-supervisor-core/managed-codex-install-v1.json"
)
MANAGED_CODEX_CODE_MODE_HOST_RECEIPT = Path(
    "/etc/research-supervisor-core/managed-codex-code-mode-host-install-v1.json"
)
MANAGED_CODEX_HOME_AUTHORITY = Path(
    "/etc/research-supervisor-core/managed-codex-home-v1.json"
)
MANAGED_CODEX_HOME_NAME = "codex-home"
MANAGED_CODEX_HOME_BINDING = Path("runtime/managed-codex-home-v1")
MANAGED_DATA_ROOT_RELATIVE = Path(".local/share/research-automation-supervisor")
MINIMUM_MANAGED_CODEX_VERSION = "0.144.0"

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_RELEASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class ManagedCodexSecurityError(ValueError):
    """A protected managed-Codex contract failed closed."""


@dataclass(frozen=True)
class ManagedCodexContract:
    """Paths and ownership authority for one installation contract."""

    executable: Path = MANAGED_CODEX_EXECUTABLE
    code_mode_host: Path = MANAGED_CODEX_CODE_MODE_HOST
    receipt: Path = MANAGED_CODEX_RECEIPT
    code_mode_host_receipt: Path = MANAGED_CODEX_CODE_MODE_HOST_RECEIPT
    pending_receipt: Path = Path(
        "/etc/research-supervisor-core/managed-codex-install-pending-v1.json"
    )
    executable_trust_root: Path = Path("/")
    receipt_trust_root: Path = Path("/")
    authority_uid: int = 0
    authority_gid: int = 0


@dataclass(frozen=True)
class ManagedCodexIdentity:
    """Exact approved installed-byte identity returned by the common verifier."""

    executable: Path
    sha256: str
    version: str
    release_id: str
    device: int
    inode: int


@dataclass(frozen=True)
class ManagedCodexCodeModeHostIdentity:
    """Exact companion identity bound to one unchanged managed-Codex identity."""

    executable: Path
    sha256: str
    managed_codex_executable: Path
    managed_codex_sha256: str
    release_id: str
    device: int
    inode: int


@dataclass(frozen=True)
class ManagedCodexHomeAuthorityContract:
    """Protected authority for one operator's canonical credential home."""

    receipt: Path = MANAGED_CODEX_HOME_AUTHORITY
    receipt_trust_root: Path = Path("/")
    authority_uid: int = 0
    authority_gid: int = 0
    operator_uid: int | None = None
    expected_data_root: Path | None = None
    data_trust_root: Path | None = None


@dataclass(frozen=True)
class ManagedCodexHomeAuthority:
    """Verified protected binding between an operator and one product data root."""

    operator_uid: int
    data_root: Path
    codex_home: Path


PRODUCTION_MANAGED_CODEX_CONTRACT = ManagedCodexContract()


def trusted_system_executable(path: Path) -> Path | None:
    """Accept a fixed root-owned system utility; never follow a symlink."""
    if not path.is_absolute():
        return None
    try:
        absolute = Path(os.path.abspath(path))
        if absolute != path:
            return None
        status = path.lstat()
        if (
            stat.S_ISLNK(status.st_mode)
            or not stat.S_ISREG(status.st_mode)
            or status.st_uid != 0
            or status.st_mode & 0o022
            or not status.st_mode & 0o111
        ):
            return None
        _validate_directory_chain(path.parent, Path("/"), owner_uid=0)
        return path
    except (OSError, RuntimeError, ValueError):
        return None


def verify_managed_codex_installation(
    contract: ManagedCodexContract | None = None,
    *,
    require_code_mode_host: bool = False,
) -> ManagedCodexIdentity:
    """Verify managed Codex and, when required, its exact native companion."""
    contract = contract or PRODUCTION_MANAGED_CODEX_CONTRACT
    if os.path.lexists(contract.pending_receipt):
        raise ManagedCodexSecurityError("managed Codex installation is incomplete")
    receipt_bytes, _ = _read_protected_regular(
        contract.receipt,
        trust_root=contract.receipt_trust_root,
        owner_uid=contract.authority_uid,
        owner_gid=contract.authority_gid,
        required_mode=0o644,
        maximum_bytes=16 * 1024,
    )
    try:
        value = json.loads(receipt_bytes.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManagedCodexSecurityError("managed Codex receipt is malformed") from exc
    expected_keys = {
        "schema_version",
        "executable",
        "sha256",
        "version",
        "release_id",
    }
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise ManagedCodexSecurityError("managed Codex receipt schema is invalid")
    executable = value.get("executable")
    digest = value.get("sha256")
    version = value.get("version")
    release_id = value.get("release_id")
    if value.get("schema_version") != 1 or executable != str(contract.executable):
        raise ManagedCodexSecurityError("managed Codex receipt names another executable")
    if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
        raise ManagedCodexSecurityError("managed Codex receipt digest is invalid")
    if not isinstance(version, str) or _VERSION.fullmatch(version) is None:
        raise ManagedCodexSecurityError("managed Codex receipt version is invalid")
    if _version_tuple(version) < _version_tuple(MINIMUM_MANAGED_CODEX_VERSION):
        raise ManagedCodexSecurityError("managed Codex receipt version is unsupported")
    if not isinstance(release_id, str) or _RELEASE_ID.fullmatch(release_id) is None:
        raise ManagedCodexSecurityError("managed Codex receipt release identity is invalid")

    executable_bytes, executable_status = _read_protected_regular(
        contract.executable,
        trust_root=contract.executable_trust_root,
        owner_uid=contract.authority_uid,
        owner_gid=contract.authority_gid,
        required_mode=0o755,
        maximum_bytes=None,
    )
    actual_digest = hashlib.sha256(executable_bytes).hexdigest()
    if actual_digest != digest:
        raise ManagedCodexSecurityError(
            "managed Codex executable does not match its approved receipt"
        )
    identity = ManagedCodexIdentity(
        executable=contract.executable,
        sha256=digest,
        version=version,
        release_id=release_id,
        device=executable_status.st_dev,
        inode=executable_status.st_ino,
    )
    if require_code_mode_host:
        verify_managed_codex_code_mode_host(identity, contract)
    return identity


def verify_managed_codex_code_mode_host(
    identity: ManagedCodexIdentity,
    contract: ManagedCodexContract | None = None,
) -> ManagedCodexCodeModeHostIdentity:
    """Verify the fixed companion and its binding to the managed Codex release."""
    contract = contract or PRODUCTION_MANAGED_CODEX_CONTRACT
    receipt_bytes, _ = _read_protected_regular(
        contract.code_mode_host_receipt,
        trust_root=contract.receipt_trust_root,
        owner_uid=contract.authority_uid,
        owner_gid=contract.authority_gid,
        required_mode=0o644,
        maximum_bytes=16 * 1024,
    )
    try:
        value = json.loads(receipt_bytes.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManagedCodexSecurityError(
            "managed Codex code-mode host receipt is malformed"
        ) from exc
    expected = {
        "schema_version": 1,
        "executable": str(contract.code_mode_host),
        "sha256": value.get("sha256") if isinstance(value, dict) else None,
        "managed_codex_executable": str(identity.executable),
        "managed_codex_sha256": identity.sha256,
        "release_id": identity.release_id,
    }
    digest = value.get("sha256") if isinstance(value, dict) else None
    if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
        raise ManagedCodexSecurityError(
            "managed Codex code-mode host receipt digest is invalid"
        )
    expected["sha256"] = digest
    if value != expected:
        raise ManagedCodexSecurityError(
            "managed Codex code-mode host receipt binding is invalid"
        )
    executable_bytes, executable_status = _read_protected_regular(
        contract.code_mode_host,
        trust_root=contract.executable_trust_root,
        owner_uid=contract.authority_uid,
        owner_gid=contract.authority_gid,
        required_mode=0o755,
        maximum_bytes=None,
    )
    if hashlib.sha256(executable_bytes).hexdigest() != digest:
        raise ManagedCodexSecurityError(
            "managed Codex code-mode host does not match its approved receipt"
        )
    return ManagedCodexCodeModeHostIdentity(
        executable=contract.code_mode_host,
        sha256=digest,
        managed_codex_executable=identity.executable,
        managed_codex_sha256=identity.sha256,
        release_id=identity.release_id,
        device=executable_status.st_dev,
        inode=executable_status.st_ino,
    )


def trusted_managed_codex_executable() -> Path | None:
    """Return the managed executable only when its complete identity verifies."""
    try:
        return verify_managed_codex_installation().executable
    except ManagedCodexSecurityError:
        return None


def render_managed_codex_receipt(identity: ManagedCodexIdentity) -> bytes:
    """Render the only accepted installation receipt schema."""
    return _render_json(
        {
            "schema_version": 1,
            "executable": str(identity.executable),
            "sha256": identity.sha256,
            "version": identity.version,
            "release_id": identity.release_id,
        }
    )


def render_managed_codex_code_mode_host_receipt(
    identity: ManagedCodexCodeModeHostIdentity,
) -> bytes:
    """Render the exact companion-to-managed-Codex release binding."""
    return _render_json(
        {
            "schema_version": 1,
            "executable": str(identity.executable),
            "sha256": identity.sha256,
            "managed_codex_executable": str(identity.managed_codex_executable),
            "managed_codex_sha256": identity.managed_codex_sha256,
            "release_id": identity.release_id,
        }
    )


def production_managed_codex_home_contract() -> ManagedCodexHomeAuthorityContract:
    """Derive the product location from passwd state, never caller environment."""
    operator_uid = os.getuid()
    try:
        passwd_home = Path(pwd.getpwuid(operator_uid).pw_dir)
        _validate_canonical_passwd_home(passwd_home, operator_uid)
    except KeyError as exc:
        raise CustodianEnvironmentError(
            "Managed Codex credential storage authority is unavailable."
        ) from exc
    data_root = passwd_home / MANAGED_DATA_ROOT_RELATIVE
    return ManagedCodexHomeAuthorityContract(
        operator_uid=operator_uid,
        expected_data_root=data_root,
        data_trust_root=passwd_home,
    )


def load_managed_codex_home_authority(
    contract: ManagedCodexHomeAuthorityContract,
) -> ManagedCodexHomeAuthority:
    """Read the root-protected canonical operator/home binding."""
    operator_uid = os.getuid() if contract.operator_uid is None else contract.operator_uid
    try:
        content, _ = _read_protected_regular(
            contract.receipt,
            trust_root=contract.receipt_trust_root,
            owner_uid=contract.authority_uid,
            owner_gid=contract.authority_gid,
            required_mode=0o644,
            maximum_bytes=16 * 1024,
        )
        value = json.loads(content.decode("ascii"))
        if not isinstance(value, dict) or set(value) != {
            "schema_version",
            "operator_uid",
            "data_root",
            "codex_home",
        }:
            raise ManagedCodexSecurityError("managed home authority schema is invalid")
        data_root = Path(_required_string(value, "data_root"))
        codex_home = Path(_required_string(value, "codex_home"))
        if value.get("schema_version") != 1 or value.get("operator_uid") != operator_uid:
            raise ManagedCodexSecurityError("managed home authority names another operator")
        if not data_root.is_absolute() or codex_home != data_root / MANAGED_CODEX_HOME_NAME:
            raise ManagedCodexSecurityError("managed home authority path is invalid")
        if contract.expected_data_root is not None and data_root != contract.expected_data_root:
            raise ManagedCodexSecurityError("managed home authority is not canonical")
        return ManagedCodexHomeAuthority(operator_uid, data_root, codex_home)
    except (OSError, RuntimeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CustodianEnvironmentError(
            "Managed Codex credential storage authority is unavailable."
        ) from exc


def render_managed_codex_home_authority(authority: ManagedCodexHomeAuthority) -> bytes:
    """Render the protected setup-time operator/home authority."""
    return _render_json(
        {
            "schema_version": 1,
            "operator_uid": authority.operator_uid,
            "data_root": str(authority.data_root),
            "codex_home": str(authority.codex_home),
        }
    )


def initialize_managed_codex_home(
    contract: ManagedCodexHomeAuthorityContract | None = None,
) -> Path:
    """Explicitly create first-use storage; never repair partial prior state."""
    selected = contract or production_managed_codex_home_contract()
    authority = load_managed_codex_home_authority(selected)
    trust_root = selected.data_trust_root or authority.data_root.parent
    try:
        _ensure_anchored_data_root(
            authority.data_root,
            trust_root=trust_root,
            owner_uid=authority.operator_uid,
        )
        runtime = authority.data_root / "runtime"
        binding = authority.data_root / MANAGED_CODEX_HOME_BINDING
        home_exists = authority.codex_home.exists() or authority.codex_home.is_symlink()
        binding_exists = binding.exists() or binding.is_symlink()
        if home_exists or binding_exists:
            if not (home_exists and binding_exists):
                raise OSError("managed home initialization is incomplete")
            return validate_managed_codex_home(
                authority.codex_home,
                expected_data_root=authority.data_root,
                data_trust_root=trust_root,
                expected_uid=authority.operator_uid,
            )
        if any(authority.data_root.iterdir()):
            raise OSError("existing application state lacks managed home binding")
        _create_private_directory(runtime, authority.operator_uid)
        _create_private_directory(authority.codex_home, authority.operator_uid)
        expected = f"{authority.codex_home}\n".encode()
        descriptor = os.open(
            binding,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            os.write(descriptor, expected)
            os.fchmod(descriptor, 0o600)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        _fsync_directory(runtime)
        return validate_managed_codex_home(
            authority.codex_home,
            expected_data_root=authority.data_root,
            data_trust_root=trust_root,
            expected_uid=authority.operator_uid,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise CustodianEnvironmentError(
            "Managed Codex credential storage initialization failed closed."
        ) from exc


def verified_managed_codex_home(
    contract: ManagedCodexHomeAuthorityContract | None = None,
) -> Path:
    """Verify initialized storage from protected authority without creating anything."""
    selected = contract or production_managed_codex_home_contract()
    authority = load_managed_codex_home_authority(selected)
    return validate_managed_codex_home(
        authority.codex_home,
        expected_data_root=authority.data_root,
        data_trust_root=selected.data_trust_root or authority.data_root.parent,
        expected_uid=authority.operator_uid,
    )


def managed_codex_home_for_data_root(data_root: Path) -> Path:
    """Return the fixed child used by the protected setup authority."""
    return data_root / MANAGED_CODEX_HOME_NAME


def prepare_managed_codex_home(data_root: Path) -> Path:
    """Compatibility helper for isolated tests, never used by product launch."""
    contract = _test_home_contract(data_root)
    if not contract.receipt.exists():
        authority = ManagedCodexHomeAuthority(
            operator_uid=os.getuid(),
            data_root=Path(os.path.abspath(data_root)),
            codex_home=managed_codex_home_for_data_root(Path(os.path.abspath(data_root))),
        )
        _write_test_authority(contract, render_managed_codex_home_authority(authority))
    return initialize_managed_codex_home(contract)


def validate_managed_codex_home(
    path: Path,
    *,
    expected_data_root: Path | None = None,
    data_trust_root: Path | None = None,
    expected_uid: int | None = None,
) -> Path:
    """Validate an exact bound home without following or repairing prior state."""
    owner_uid = os.getuid() if expected_uid is None else expected_uid
    if not path.is_absolute() or path.name != MANAGED_CODEX_HOME_NAME:
        raise CustodianEnvironmentError("Managed Codex credential storage is unavailable.")
    try:
        absolute = Path(os.path.abspath(path))
        if absolute != path:
            raise OSError("credential home path is not canonical")
        root = path.parent
        if expected_data_root is not None and root != expected_data_root:
            raise OSError("credential home belongs to another application data root")
        trust_root = data_trust_root or root
        _validate_directory_chain(root, trust_root, owner_uid=owner_uid)
        _validate_private_directory(root, owner_uid)
        _validate_private_directory(root / "runtime", owner_uid)
        _validate_private_directory(path, owner_uid)
        _validate_binding(
            root / MANAGED_CODEX_HOME_BINDING,
            f"{path}\n".encode(),
            owner_uid,
        )
        return path
    except (OSError, RuntimeError, ValueError) as exc:
        raise CustodianEnvironmentError(
            "Managed Codex credential storage is unavailable."
        ) from exc


def managed_codex_home_from_environment() -> Path:
    """Return protected canonical storage; ambient CODEX_HOME is intentionally ignored."""
    return verified_managed_codex_home()


def _read_protected_regular(
    path: Path,
    *,
    trust_root: Path,
    owner_uid: int,
    owner_gid: int,
    required_mode: int,
    maximum_bytes: int | None,
) -> tuple[bytes, os.stat_result]:
    if not path.is_absolute() or Path(os.path.abspath(path)) != path:
        raise ManagedCodexSecurityError("protected path is not absolute and canonical")
    try:
        _validate_directory_chain(path.parent, trust_root, owner_uid=owner_uid)
    except OSError as exc:
        raise ManagedCodexSecurityError("protected file ancestry is unsafe") from exc
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ManagedCodexSecurityError("protected file is unavailable") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != owner_uid
            or before.st_gid != owner_gid
            or stat.S_IMODE(before.st_mode) != required_mode
            or before.st_nlink != 1
        ):
            raise ManagedCodexSecurityError("protected file metadata is unsafe")
        blocks: list[bytes] = []
        total = 0
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            total += len(block)
            if maximum_bytes is not None and total > maximum_bytes:
                raise ManagedCodexSecurityError("protected file is too large")
            blocks.append(block)
        after = os.fstat(descriptor)
        current = path.lstat()
        stable = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        if stable != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ) or (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino):
            raise ManagedCodexSecurityError("protected file changed while being verified")
        return b"".join(blocks), before
    finally:
        os.close(descriptor)


def _validate_directory_chain(path: Path, trust_root: Path, *, owner_uid: int) -> None:
    if not path.is_absolute() or not trust_root.is_absolute():
        raise OSError("protected directory chain is not absolute")
    absolute_path = Path(os.path.abspath(path))
    absolute_root = Path(os.path.abspath(trust_root))
    try:
        absolute_path.relative_to(absolute_root)
    except ValueError as exc:
        raise OSError("protected path escapes its trust root") from exc
    current = absolute_path
    while True:
        status = current.lstat()
        if (
            stat.S_ISLNK(status.st_mode)
            or not stat.S_ISDIR(status.st_mode)
            or status.st_uid != owner_uid
            or status.st_mode & 0o022
        ):
            raise OSError("protected directory chain is unsafe")
        if current == absolute_root:
            return
        parent = current.parent
        if parent == current:
            raise OSError("protected directory chain missed its trust root")
        current = parent


def _ensure_anchored_data_root(path: Path, *, trust_root: Path, owner_uid: int) -> None:
    if not path.is_absolute() or not trust_root.is_absolute():
        raise OSError("managed data root is not absolute")
    absolute_path = Path(os.path.abspath(path))
    absolute_root = Path(os.path.abspath(trust_root))
    try:
        relative = absolute_path.relative_to(absolute_root)
    except ValueError as exc:
        raise OSError("managed data root escapes its protected home") from exc
    _validate_directory_chain(absolute_root, absolute_root, owner_uid=owner_uid)
    current = absolute_root
    for part in relative.parts:
        current = current / part
        with suppress(FileExistsError):
            current.mkdir(mode=0o700 if current == absolute_path else 0o755)
        status = current.lstat()
        if (
            stat.S_ISLNK(status.st_mode)
            or not stat.S_ISDIR(status.st_mode)
            or status.st_uid != owner_uid
            or status.st_mode & 0o022
        ):
            raise OSError("managed data ancestor is unsafe")
    _validate_private_directory(absolute_path, owner_uid)


def _validate_canonical_passwd_home(path: Path, owner_uid: int) -> None:
    if not path.is_absolute() or Path(os.path.abspath(path)) != path:
        raise CustodianEnvironmentError(
            "Managed Codex passwd home authority is unsafe."
        )
    try:
        status = path.lstat()
        if (
            stat.S_ISLNK(status.st_mode)
            or not stat.S_ISDIR(status.st_mode)
            or status.st_uid != owner_uid
            or status.st_mode & 0o022
        ):
            raise OSError("operator home is unsafe")
        for parent in path.parents:
            parent_status = parent.lstat()
            if (
                stat.S_ISLNK(parent_status.st_mode)
                or not stat.S_ISDIR(parent_status.st_mode)
                or parent_status.st_uid != 0
                or parent_status.st_mode & 0o022
            ):
                raise OSError("operator home ancestry is unsafe")
    except OSError as exc:
        raise CustodianEnvironmentError(
            "Managed Codex passwd home authority is unsafe."
        ) from exc


def _create_private_directory(path: Path, owner_uid: int) -> None:
    path.mkdir(mode=0o700)
    _validate_private_directory(path, owner_uid)
    _fsync_directory(path.parent)


def _validate_private_directory(path: Path, owner_uid: int) -> None:
    status = path.lstat()
    if (
        stat.S_ISLNK(status.st_mode)
        or not stat.S_ISDIR(status.st_mode)
        or status.st_uid != owner_uid
        or stat.S_IMODE(status.st_mode) != 0o700
    ):
        raise OSError("private directory contract is unsafe")


def _validate_binding(path: Path, expected: bytes, owner_uid: int) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        status = os.fstat(descriptor)
        if (
            not stat.S_ISREG(status.st_mode)
            or status.st_uid != owner_uid
            or stat.S_IMODE(status.st_mode) != 0o600
            or status.st_nlink != 1
        ):
            raise OSError("binding ownership, mode, or link identity is unsafe")
        content = os.read(descriptor, 16 * 1024)
        if content != expected or os.read(descriptor, 1):
            raise OSError("binding changed")
        after = os.fstat(descriptor)
        current = path.lstat()
        if (
            status.st_dev,
            status.st_ino,
            status.st_size,
            status.st_mtime_ns,
            status.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ) or (status.st_dev, status.st_ino) != (current.st_dev, current.st_ino):
            raise OSError("binding changed while being verified")
    finally:
        os.close(descriptor)


def _test_home_contract(data_root: Path) -> ManagedCodexHomeAuthorityContract:
    absolute = Path(os.path.abspath(data_root))
    trust_root = absolute.parent
    receipt_root = trust_root / ".managed-codex-test-authority"
    receipt_root.mkdir(mode=0o700, exist_ok=True)
    return ManagedCodexHomeAuthorityContract(
        receipt=receipt_root / f"{hashlib.sha256(str(absolute).encode()).hexdigest()}.json",
        receipt_trust_root=receipt_root,
        authority_uid=os.getuid(),
        authority_gid=os.getgid(),
        operator_uid=os.getuid(),
        expected_data_root=absolute,
        data_trust_root=trust_root,
    )


def _write_test_authority(
    contract: ManagedCodexHomeAuthorityContract, content: bytes
) -> None:
    contract.receipt.write_bytes(content)
    contract.receipt.chmod(0o644)


def _required_string(value: dict[str, Any], name: str) -> str:
    selected = value.get(name)
    if not isinstance(selected, str):
        raise ManagedCodexSecurityError(f"{name} is invalid")
    return selected


def _version_tuple(value: str) -> tuple[int, int, int]:
    if _VERSION.fullmatch(value) is None:
        raise ManagedCodexSecurityError("managed Codex version is malformed")
    major, minor, patch = value.split(".")
    return int(major), int(minor), int(patch)


def _render_json(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=True, sort_keys=True) + "\n").encode("ascii")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
