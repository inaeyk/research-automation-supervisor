"""Unprivileged, offline preparation for the protected production release.

Nothing produced here is authority by itself.  The candidate remains untrusted data,
and the staged helper, verifier, and manifest become authority only after an
administrator explicitly approves and installs their exact bytes with trusted system
tools.
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from dataclasses import dataclass
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from typing import Any

from research_automation_supervisor.protected_release import (
    PROTECTED_RELEASE_APPROVAL,
    PROTECTED_RELEASE_CANDIDATE,
    PROTECTED_RELEASE_INSTALLER,
    PROTECTED_RELEASE_ROOT,
    PROTECTED_RELEASE_UPDATE_APPROVAL,
    PROTECTED_RELEASE_VERIFIER,
)

PROTECTED_RELEASE_AUTHORITY_STAGING = Path(
    "/var/tmp/research-supervisor-release-authority-candidate"
)
PREPARATION_VENV_NAME = "research-supervisor-release-preparation-venv"
BOOTSTRAP_INVENTORY_NAME = "bootstrap-files-v1.json"
APPROVAL_NAME = "approved-release-v1.json"
INSTALLER_NAME = "install-protected-release"
VERIFIER_NAME = "verify-protected-release"

_RELEASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_REQUIREMENT_NAME = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)")
_MINIMUM_CODEX_VERSION = (0, 144, 0)
_PAYLOAD_FILES: tuple[tuple[str, int], ...] = (
    ("scripts/install-research-supervisor.sh", 0o755),
    ("scripts/install-managed-codex.sh", 0o755),
    ("scripts/install-core-authority-service.sh", 0o755),
    ("scripts/run-protected-python.sh", 0o755),
    ("scripts/protected-managed-codex-entry.py", 0o755),
    ("scripts/research-supervisor-core-authority.service", 0o644),
)
_WHEEL_FORCE_INCLUDES: tuple[tuple[str, str], ...] = (
    (
        "examples/physics_auditor/synthetic",
        "research_automation_supervisor/example_data/physics_auditor_synthetic",
    ),
    (
        "examples/physics_auditor/benchmark_v1/auditor_visible",
        "research_automation_supervisor/example_data/physics_benchmark_blind_v1/auditor_visible",
    ),
    (
        "examples/physics_auditor/benchmark_v1/scorer_only",
        "research_automation_supervisor/example_data/physics_benchmark_blind_v1/scorer_only",
    ),
)


class ReleasePreparationError(ValueError):
    """The unprivileged release data is incomplete, unsafe, or inconsistent."""


@dataclass(frozen=True)
class ReleasePreparationConfig:
    """Explicit inputs and output roots for one immutable preparation attempt."""

    repository: Path
    candidate_root: Path
    authority_staging_root: Path
    release_id: str
    codex_artifact: Path
    code_mode_host_artifact: Path
    codex_version: str
    wheelhouse_source: Path
    update_from_manifest_sha256: str | None = None


@dataclass(frozen=True)
class PreparedRelease:
    """Locations and hashes emitted by successful unprivileged preparation."""

    candidate_root: Path
    authority_staging_root: Path
    approval: Path
    approval_sha256: str
    bootstrap_inventory: Path
    resolution_python: Path
    release_id: str


def prepare_protected_release(config: ReleasePreparationConfig) -> PreparedRelease:
    """Create a complete candidate and separate, explicitly untrusted authority data."""
    repository = config.repository.resolve(strict=True)
    candidate_root = _absolute_output(config.candidate_root, "candidate root")
    authority_root = _absolute_output(
        config.authority_staging_root, "authority staging root"
    )
    if candidate_root == authority_root or candidate_root in authority_root.parents:
        raise ReleasePreparationError("authority staging must be separate from candidate data")
    if authority_root in candidate_root.parents:
        raise ReleasePreparationError("candidate data must be separate from authority staging")
    if os.path.lexists(candidate_root) or os.path.lexists(authority_root):
        raise ReleasePreparationError("release preparation outputs must not already exist")
    if _RELEASE_ID.fullmatch(config.release_id) is None:
        raise ReleasePreparationError("release ID is invalid")
    if _VERSION.fullmatch(config.codex_version) is None:
        raise ReleasePreparationError("Codex version is invalid")
    if _version_tuple(config.codex_version) < _MINIMUM_CODEX_VERSION:
        raise ReleasePreparationError("Codex version is below the qualified minimum")
    if _RELEASE_ID.fullmatch(f"{config.release_id}-codex") is None:
        raise ReleasePreparationError("release ID is too long for managed Codex approval")
    if config.update_from_manifest_sha256 is not None and re.fullmatch(
        r"[0-9a-f]{64}", config.update_from_manifest_sha256
    ) is None:
        raise ReleasePreparationError("protected release update identity is invalid")

    project = _load_project(repository)
    version = _required_string(project, "version")
    expected_wheel_name = (
        f"research_automation_supervisor-{version}-py3-none-any.whl"
    )
    expected_script_wheel = (
        repository
        / "scripts/install-core-authority-service.sh"
    ).read_text(encoding="utf-8")
    if f"artifacts/{expected_wheel_name}" not in expected_script_wheel:
        raise ReleasePreparationError(
            "protected Core installer and project version disagree on the wheel path"
        )

    candidate_root.parent.mkdir(parents=True, exist_ok=True)
    authority_root.parent.mkdir(parents=True, exist_ok=True)
    resolution_python = _private_resolution_python(candidate_root)
    candidate_temporary = Path(
        tempfile.mkdtemp(prefix=f".{candidate_root.name}.", dir=candidate_root.parent)
    )
    authority_temporary = Path(
        tempfile.mkdtemp(prefix=f".{authority_root.name}.", dir=authority_root.parent)
    )
    candidate_installed = False
    authority_installed = False
    try:
        _set_directory_mode(candidate_temporary)
        _set_directory_mode(authority_temporary)
        _populate_candidate(
            candidate_temporary,
            repository=repository,
            project=project,
            release_id=config.release_id,
            codex_artifact=config.codex_artifact,
            code_mode_host_artifact=config.code_mode_host_artifact,
            codex_version=config.codex_version,
            wheelhouse_source=config.wheelhouse_source,
            product_wheel_name=expected_wheel_name,
            resolution_python=resolution_python,
        )
        approval_raw = render_candidate_manifest(
            candidate_temporary,
            config.release_id,
            update_from_manifest_sha256=config.update_from_manifest_sha256,
        )
        helper_source = repository / "src/research_automation_supervisor/protected_release.py"
        helper_raw = _read_plain_file(helper_source)
        if not helper_raw.startswith(b"#!/usr/bin/python3 -I\n"):
            raise ReleasePreparationError(
                "protected authority helper lacks the fixed isolated interpreter"
            )
        _write_file(authority_temporary / INSTALLER_NAME, helper_raw, 0o755)
        _write_file(authority_temporary / VERIFIER_NAME, helper_raw, 0o755)
        _write_file(authority_temporary / APPROVAL_NAME, approval_raw, 0o644)
        inventory_raw = _render_json(
            {
                "schema_version": 1,
                "authority_is_trusted": False,
                "prepared_candidate_root": str(candidate_root),
                "production_candidate_destination": str(PROTECTED_RELEASE_CANDIDATE),
                "production_release_destination": str(PROTECTED_RELEASE_ROOT),
                "files": [
                    _bootstrap_file(
                        authority_temporary / INSTALLER_NAME,
                        PROTECTED_RELEASE_INSTALLER,
                        0o755,
                    ),
                    _bootstrap_file(
                        authority_temporary / VERIFIER_NAME,
                        PROTECTED_RELEASE_VERIFIER,
                        0o755,
                    ),
                    _bootstrap_file(
                        authority_temporary / APPROVAL_NAME,
                        PROTECTED_RELEASE_APPROVAL
                        if config.update_from_manifest_sha256 is None
                        else PROTECTED_RELEASE_UPDATE_APPROVAL,
                        0o644,
                    ),
                ],
            }
        )
        _write_file(
            authority_temporary / BOOTSTRAP_INVENTORY_NAME,
            inventory_raw,
            0o644,
        )
        verify_prepared_release(candidate_temporary, authority_temporary / APPROVAL_NAME)
        _verify_authority_staging(authority_temporary)
        os.rename(candidate_temporary, candidate_root)
        candidate_installed = True
        try:
            os.rename(authority_temporary, authority_root)
        except OSError:
            shutil.rmtree(candidate_root)
            candidate_installed = False
            raise
        authority_installed = True
        approval = authority_root / APPROVAL_NAME
        verify_prepared_release(candidate_root, approval)
        return PreparedRelease(
            candidate_root=candidate_root,
            authority_staging_root=authority_root,
            approval=approval,
            approval_sha256=_sha256_file(approval),
            bootstrap_inventory=authority_root / BOOTSTRAP_INVENTORY_NAME,
            resolution_python=resolution_python,
            release_id=config.release_id,
        )
    except (OSError, zipfile.BadZipFile) as exc:
        raise ReleasePreparationError("release preparation failed") from exc
    finally:
        if not authority_installed and os.path.lexists(authority_temporary):
            shutil.rmtree(authority_temporary)
        if not candidate_installed and os.path.lexists(candidate_temporary):
            shutil.rmtree(candidate_temporary)


def render_candidate_manifest(
    candidate_root: Path,
    release_id: str,
    *,
    update_from_manifest_sha256: str | None = None,
) -> bytes:
    """Render canonical path/mode/digest approval data for one exact candidate tree."""
    if _RELEASE_ID.fullmatch(release_id) is None:
        raise ReleasePreparationError("release ID is invalid")
    if update_from_manifest_sha256 is not None and re.fullmatch(
        r"[0-9a-f]{64}", update_from_manifest_sha256
    ) is None:
        raise ReleasePreparationError("protected release update identity is invalid")
    files: list[dict[str, str]] = []
    for path in _candidate_files(candidate_root):
        relative = path.relative_to(candidate_root).as_posix()
        status = path.lstat()
        mode = stat.S_IMODE(status.st_mode)
        if mode not in {0o644, 0o755}:
            raise ReleasePreparationError("candidate file has an unsupported mode")
        files.append(
            {
                "path": relative,
                "mode": f"{mode:04o}",
                "sha256": _sha256_file(path),
            }
        )
    if not files:
        raise ReleasePreparationError("candidate contains no release data")
    return _render_json(
        {
            "schema_version": 2,
            "release_id": release_id,
            "files": files,
            "update_from_manifest_sha256": update_from_manifest_sha256,
        }
    )


def verify_prepared_release(candidate_root: Path, approval: Path) -> str:
    """Verify exact candidate bytes, file modes, and absence of missing/extra files."""
    raw = _read_plain_file(approval)
    try:
        value: Any = json.loads(raw.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleasePreparationError("candidate approval manifest is malformed") from exc
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
        raise ReleasePreparationError("candidate approval manifest schema is invalid")
    release_id = value.get("release_id")
    listed = value.get("files")
    update_from = value.get("update_from_manifest_sha256")
    if not isinstance(release_id, str) or _RELEASE_ID.fullmatch(release_id) is None:
        raise ReleasePreparationError("candidate approval release ID is invalid")
    if update_from is not None and (
        not isinstance(update_from, str) or re.fullmatch(r"[0-9a-f]{64}", update_from) is None
    ):
        raise ReleasePreparationError("candidate approval update identity is invalid")
    if not isinstance(listed, list) or not listed:
        raise ReleasePreparationError("candidate approval file list is invalid")
    expected: dict[str, tuple[str, int]] = {}
    for item in listed:
        if not isinstance(item, dict) or set(item) != {"path", "mode", "sha256"}:
            raise ReleasePreparationError("candidate approval file entry is invalid")
        path_text = item.get("path")
        mode_text = item.get("mode")
        digest = item.get("sha256")
        if not all(isinstance(field, str) for field in (path_text, mode_text, digest)):
            raise ReleasePreparationError("candidate approval file identity is invalid")
        assert isinstance(path_text, str)
        assert isinstance(mode_text, str)
        assert isinstance(digest, str)
        relative = PurePosixPath(path_text)
        if (
            relative.is_absolute()
            or relative.as_posix() != path_text
            or not relative.parts
            or "." in relative.parts
            or ".." in relative.parts
            or path_text in expected
            or mode_text not in {"0644", "0755"}
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        ):
            raise ReleasePreparationError("candidate approval file identity is invalid")
        expected[path_text] = (digest, int(mode_text, 8))
    observed: set[str] = set()
    for path in _candidate_files(candidate_root):
        relative_text = path.relative_to(candidate_root).as_posix()
        approved = expected.get(relative_text)
        if approved is None:
            raise ReleasePreparationError("candidate contains an extra file")
        digest, mode = approved
        if stat.S_IMODE(path.lstat().st_mode) != mode:
            raise ReleasePreparationError("candidate file mode does not match approval")
        if _sha256_file(path) != digest:
            raise ReleasePreparationError("candidate bytes do not match approval")
        observed.add(relative_text)
    if observed != set(expected):
        raise ReleasePreparationError("candidate is missing an approved file")
    return hashlib.sha256(raw).hexdigest()


def _populate_candidate(
    candidate: Path,
    *,
    repository: Path,
    project: dict[str, Any],
    release_id: str,
    codex_artifact: Path,
    code_mode_host_artifact: Path,
    codex_version: str,
    wheelhouse_source: Path,
    product_wheel_name: str,
    resolution_python: Path,
) -> None:
    for relative, mode in _PAYLOAD_FILES:
        _copy_file(repository / relative, candidate / relative, mode)

    package_source = repository / "src/research_automation_supervisor"
    for source in _source_files(package_source):
        relative_path = source.relative_to(repository)
        _copy_file(source, candidate / relative_path, 0o644)

    _verify_codex_artifact(codex_artifact, codex_version)
    codex_raw = _read_elf_artifact(codex_artifact, "Codex")
    code_mode_host_raw = _read_elf_artifact(
        code_mode_host_artifact, "Codex code-mode host"
    )
    if hashlib.sha256(code_mode_host_raw).digest() == hashlib.sha256(codex_raw).digest():
        raise ReleasePreparationError(
            "Codex and code-mode host artifacts must be distinct executables"
        )
    _write_file(candidate / "artifacts/codex", codex_raw, 0o755)
    _write_file(
        candidate / "artifacts/codex-code-mode-host",
        code_mode_host_raw,
        0o755,
    )
    _write_file(
        candidate / "managed-codex-approval-v1.json",
        _render_json(
            {
                "schema_version": 2,
                "release_id": f"{release_id}-codex",
                "artifact": "artifacts/codex",
                "sha256": hashlib.sha256(codex_raw).hexdigest(),
                "version": codex_version,
                "update_from_sha256": None,
                "code_mode_host_artifact": "artifacts/codex-code-mode-host",
                "code_mode_host_sha256": hashlib.sha256(
                    code_mode_host_raw
                ).hexdigest(),
            }
        ),
        0o644,
    )

    wheel_raw = _build_product_wheel(repository, project)
    _write_file(candidate / "artifacts" / product_wheel_name, wheel_raw, 0o644)
    wheelhouse = wheelhouse_source.resolve(strict=True)
    if not wheelhouse.is_dir() or wheelhouse.is_symlink():
        raise ReleasePreparationError("wheelhouse source must be a plain directory")
    wheels = sorted(wheelhouse.glob("*.whl"), key=lambda path: path.name)
    if not wheels:
        raise ReleasePreparationError("wheelhouse contains no wheels")
    available: set[str] = set()
    for source in wheels:
        available.add(_wheel_distribution_name(source))
        _copy_file(source, candidate / "wheelhouse" / source.name, 0o644)
    required = {
        _requirement_distribution_name(item)
        for item in _required_string_list(project, "dependencies")
    }
    missing = sorted(required - available)
    if missing:
        raise ReleasePreparationError(
            f"wheelhouse is missing direct project dependencies: {', '.join(missing)}"
        )
    _verify_offline_resolution(
        candidate / "artifacts" / product_wheel_name,
        candidate / "wheelhouse",
        resolution_python,
    )


def _build_product_wheel(repository: Path, project: dict[str, Any]) -> bytes:
    name = _required_string(project, "name")
    version = _required_string(project, "version")
    distribution = name.replace("-", "_")
    dist_info = f"{distribution}-{version}.dist-info"
    entries: dict[str, tuple[bytes, int]] = {}

    package_source = repository / "src/research_automation_supervisor"
    for source in _source_files(package_source):
        relative = source.relative_to(repository / "src").as_posix()
        entries[relative] = (_read_plain_file(source), _source_mode(source))
    for source_text, destination_text in _WHEEL_FORCE_INCLUDES:
        source_root = repository / source_text
        if not source_root.is_dir() or source_root.is_symlink():
            raise ReleasePreparationError("configured wheel data source is unavailable")
        for source in _source_files(source_root):
            suffix = source.relative_to(source_root).as_posix()
            destination = f"{destination_text}/{suffix}"
            if destination in entries:
                raise ReleasePreparationError("wheel contains a duplicate destination")
            entries[destination] = (_read_plain_file(source), _source_mode(source))

    metadata_lines = [
        "Metadata-Version: 2.3",
        f"Name: {name}",
        f"Version: {version}",
        f"Summary: {_required_string(project, 'description')}",
        f"Requires-Python: {_required_string(project, 'requires-python')}",
        "License-Expression: MIT",
        "License-File: LICENSE",
    ]
    for requirement in _required_string_list(project, "dependencies"):
        metadata_lines.append(f"Requires-Dist: {requirement}")
    metadata_lines.extend(("", ""))
    entries[f"{dist_info}/METADATA"] = (
        "\n".join(metadata_lines).encode("utf-8"),
        0o644,
    )
    entries[f"{dist_info}/WHEEL"] = (
        b"Wheel-Version: 1.0\n"
        b"Generator: research-supervisor-release-preparation\n"
        b"Root-Is-Purelib: true\n"
        b"Tag: py3-none-any\n",
        0o644,
    )
    scripts = project.get("scripts")
    if not isinstance(scripts, dict) or not scripts:
        raise ReleasePreparationError("project console scripts are unavailable")
    entry_points = ["[console_scripts]"]
    for script_name, target in sorted(scripts.items()):
        if not isinstance(script_name, str) or not isinstance(target, str):
            raise ReleasePreparationError("project console script metadata is invalid")
        entry_points.append(f"{script_name} = {target}")
    entry_points.extend(("", ""))
    entries[f"{dist_info}/entry_points.txt"] = (
        "\n".join(entry_points).encode("utf-8"),
        0o644,
    )
    entries[f"{dist_info}/licenses/LICENSE"] = (
        _read_plain_file(repository / "LICENSE"),
        0o644,
    )

    record_path = f"{dist_info}/RECORD"
    record_stream = io.StringIO(newline="")
    writer = csv.writer(record_stream, lineterminator="\n")
    for path in sorted(entries):
        content, _mode = entries[path]
        encoded = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=")
        writer.writerow((path, f"sha256={encoded.decode('ascii')}", len(content)))
    writer.writerow((record_path, "", ""))
    entries[record_path] = (record_stream.getvalue().encode("utf-8"), 0o644)

    output = io.BytesIO()
    with zipfile.ZipFile(
        output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as archive:
        for path in sorted(entries):
            content, mode = entries[path]
            info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | mode) << 16
            archive.writestr(info, content)
    return output.getvalue()


def _wheel_distribution_name(path: Path) -> str:
    raw = _read_plain_file(path)
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)):
                raise ReleasePreparationError("wheel contains duplicate archive paths")
            for name in names:
                relative = PurePosixPath(name)
                if (
                    relative.is_absolute()
                    or ".." in relative.parts
                    or "." in relative.parts
                    or "\\" in name
                ):
                    raise ReleasePreparationError("wheel contains an unsafe archive path")
                info = archive.getinfo(name)
                kind = (info.external_attr >> 16) & 0o170000
                if kind not in {0, stat.S_IFREG, stat.S_IFDIR}:
                    raise ReleasePreparationError("wheel contains an unsafe object type")
            metadata_paths = [
                name for name in names if name.endswith(".dist-info/METADATA")
            ]
            if len(metadata_paths) != 1:
                raise ReleasePreparationError("wheel metadata is missing or ambiguous")
            metadata = BytesParser().parsebytes(archive.read(metadata_paths[0]))
    except zipfile.BadZipFile as exc:
        raise ReleasePreparationError("wheelhouse contains an invalid wheel") from exc
    distribution = metadata.get("Name")
    if not isinstance(distribution, str) or not distribution:
        raise ReleasePreparationError("wheel distribution identity is invalid")
    return _normalize_distribution(distribution)


def _verify_offline_resolution(
    product_wheel: Path,
    wheelhouse: Path,
    resolution_python: Path,
) -> None:
    """Ask pip to prove compatible transitive closure without installing anything."""
    try:
        completed = subprocess.run(
            _offline_resolution_command(
                product_wheel,
                wheelhouse,
                resolution_python,
            ),
            check=False,
            close_fds=True,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            text=True,
            timeout=120,
            env=_minimal_environment(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ReleasePreparationError("offline wheelhouse resolution failed") from exc
    if completed.returncode != 0:
        raise ReleasePreparationError(
            "offline wheelhouse does not resolve the complete compatible dependency closure"
        )


def _offline_resolution_command(
    product_wheel: Path,
    wheelhouse: Path,
    resolution_python: Path,
) -> list[str]:
    return [
        str(resolution_python),
        "-I",
        "-m",
        "pip",
        "--isolated",
        "install",
        "--dry-run",
        "--ignore-installed",
        "--disable-pip-version-check",
        "--no-cache-dir",
        "--no-index",
        "--only-binary=:all:",
        "--find-links",
        str(wheelhouse),
        str(product_wheel),
    ]


def _private_resolution_python(candidate_root: Path) -> Path:
    """Create or validate the private venv used only for unprivileged resolution."""
    root = candidate_root.parent / PREPARATION_VENV_NAME
    created = False
    try:
        status = root.lstat()
    except FileNotFoundError:
        try:
            root.mkdir(mode=0o700)
            created = True
        except FileExistsError:
            status = root.lstat()
        except OSError as exc:
            raise ReleasePreparationError(
                "private preparation virtual environment cannot be created"
            ) from exc
    except OSError as exc:
        raise ReleasePreparationError(
            "private preparation virtual environment is unavailable"
        ) from exc
    if not created:
        _validate_private_venv_root(root, status)
        return _validate_private_venv_python(root)

    try:
        completed = subprocess.run(
            [sys.executable, "-I", "-m", "venv", "--copies", str(root)],
            check=False,
            close_fds=True,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            text=True,
            timeout=120,
            env=_minimal_environment(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        shutil.rmtree(root)
        raise ReleasePreparationError(
            "private preparation virtual environment creation failed; "
            "the system venv/ensurepip components are unavailable"
        ) from exc
    if completed.returncode != 0:
        shutil.rmtree(root)
        raise ReleasePreparationError(
            "private preparation virtual environment creation failed; "
            "the system venv/ensurepip components are unavailable"
        )
    root.chmod(0o700)
    try:
        return _validate_private_venv_python(root)
    except ReleasePreparationError:
        shutil.rmtree(root)
        raise


def _validate_private_venv_root(root: Path, status: os.stat_result) -> None:
    if (
        not stat.S_ISDIR(status.st_mode)
        or stat.S_ISLNK(status.st_mode)
        or status.st_uid != os.geteuid()
        or stat.S_IMODE(status.st_mode) != 0o700
    ):
        raise ReleasePreparationError(
            "private preparation virtual environment has unsafe ownership or permissions"
        )


def _validate_private_venv_python(root: Path) -> Path:
    try:
        root_status = root.lstat()
    except OSError as exc:
        raise ReleasePreparationError(
            "private preparation virtual environment is unavailable"
        ) from exc
    _validate_private_venv_root(root, root_status)
    try:
        configuration = _read_plain_file(root / "pyvenv.cfg").decode("utf-8")
    except (ReleasePreparationError, UnicodeDecodeError) as exc:
        raise ReleasePreparationError(
            "private preparation virtual environment configuration is unavailable or unsafe"
        ) from exc
    settings = {
        key.strip().lower(): value.strip()
        for line in configuration.splitlines()
        if "=" in line
        for key, value in (line.split("=", maxsplit=1),)
    }
    if settings.get("include-system-site-packages", "").lower() != "false":
        raise ReleasePreparationError(
            "private preparation virtual environment enables system site packages"
        )
    bootstrap_version = (
        f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    )
    if settings.get("version") != bootstrap_version:
        raise ReleasePreparationError(
            "private preparation virtual environment does not match the bootstrap Python"
        )
    python = root / "bin/python"
    try:
        python_status = python.lstat()
    except OSError as exc:
        raise ReleasePreparationError(
            "private preparation virtual environment Python is unavailable"
        ) from exc
    if (
        not stat.S_ISREG(python_status.st_mode)
        or stat.S_ISLNK(python_status.st_mode)
        or python_status.st_uid != os.geteuid()
        or python_status.st_nlink != 1
        or not stat.S_IMODE(python_status.st_mode) & 0o111
    ):
        raise ReleasePreparationError(
            "private preparation virtual environment Python is unsafe"
        )
    try:
        completed = subprocess.run(
            [str(python), "-I", "-m", "pip", "--version"],
            check=False,
            close_fds=True,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            text=True,
            timeout=30,
            env=_minimal_environment(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ReleasePreparationError(
            "private preparation virtual environment pip is unavailable"
        ) from exc
    if completed.returncode != 0 or str(root) not in completed.stdout:
        raise ReleasePreparationError(
            "private preparation virtual environment pip is unavailable"
        )
    return python


def _verify_authority_staging(root: Path) -> None:
    inventory_path = root / BOOTSTRAP_INVENTORY_NAME
    try:
        value: Any = json.loads(_read_plain_file(inventory_path).decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleasePreparationError("bootstrap inventory is malformed") from exc
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "authority_is_trusted",
        "prepared_candidate_root",
        "production_candidate_destination",
        "production_release_destination",
        "files",
    }:
        raise ReleasePreparationError("bootstrap inventory schema is invalid")
    if value.get("schema_version") != 1 or value.get("authority_is_trusted") is not False:
        raise ReleasePreparationError("bootstrap staging improperly claims authority")
    if value.get("production_candidate_destination") != str(PROTECTED_RELEASE_CANDIDATE):
        raise ReleasePreparationError("bootstrap candidate destination is not fixed")
    if value.get("production_release_destination") != str(PROTECTED_RELEASE_ROOT):
        raise ReleasePreparationError("bootstrap release destination is not fixed")
    files = value.get("files")
    if not isinstance(files, list) or len(files) != 3:
        raise ReleasePreparationError("bootstrap inventory is incomplete")
    expected_destinations = {
        INSTALLER_NAME: (str(PROTECTED_RELEASE_INSTALLER), "0755"),
        VERIFIER_NAME: (str(PROTECTED_RELEASE_VERIFIER), "0755"),
    }
    observed: set[str] = set()
    for item in files:
        if not isinstance(item, dict) or set(item) != {
            "staged_name",
            "destination",
            "mode",
            "sha256",
        }:
            raise ReleasePreparationError("bootstrap inventory entry is invalid")
        staged_name = item.get("staged_name")
        digest = item.get("sha256")
        mode_text = item.get("mode")
        destination = item.get("destination")
        destination_is_expected = (
            isinstance(staged_name, str)
            and (staged_name in expected_destinations)
            and expected_destinations.get(staged_name) == (destination, mode_text)
        ) or (
            staged_name == APPROVAL_NAME
            and (destination, mode_text)
            in {
                (str(PROTECTED_RELEASE_APPROVAL), "0644"),
                (str(PROTECTED_RELEASE_UPDATE_APPROVAL), "0644"),
            }
        )
        if (
            not isinstance(staged_name, str)
            or not isinstance(digest, str)
            or not isinstance(destination, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or staged_name in observed
            or not destination_is_expected
        ):
            raise ReleasePreparationError("bootstrap inventory identity is invalid")
        observed.add(staged_name)
        staged = root / staged_name
        if _sha256_file(staged) != digest:
            raise ReleasePreparationError("bootstrap staging bytes do not match inventory")
        if stat.S_IMODE(staged.lstat().st_mode) != int(str(mode_text), 8):
            raise ReleasePreparationError("bootstrap staging mode does not match inventory")
    if observed != {*expected_destinations, APPROVAL_NAME}:
        raise ReleasePreparationError("bootstrap inventory destinations are incomplete")


def _verify_codex_artifact(path: Path, expected_version: str) -> None:
    try:
        status = path.lstat()
    except OSError as exc:
        raise ReleasePreparationError("Codex artifact is unavailable") from exc
    if not stat.S_ISREG(status.st_mode) or not stat.S_IMODE(status.st_mode) & 0o111:
        raise ReleasePreparationError("Codex artifact is not an executable plain file")
    try:
        completed = subprocess.run(
            [str(path), "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
            env=_minimal_environment(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ReleasePreparationError("Codex artifact version probe failed") from exc
    match = re.search(
        r"(?<![0-9])([0-9]+\.[0-9]+\.[0-9]+)(?![0-9])",
        completed.stdout,
    )
    if completed.returncode != 0 or match is None or match.group(1) != expected_version:
        raise ReleasePreparationError("Codex artifact version does not match approval")


def _read_elf_artifact(path: Path, label: str) -> bytes:
    try:
        status = path.lstat()
    except OSError as exc:
        raise ReleasePreparationError(f"{label} artifact is unavailable") from exc
    if not stat.S_ISREG(status.st_mode) or not stat.S_IMODE(status.st_mode) & 0o111:
        raise ReleasePreparationError(
            f"{label} artifact is not an executable plain file"
        )
    raw = _read_plain_file(path)
    if not raw.startswith(b"\x7fELF"):
        raise ReleasePreparationError(
            f"{label} artifact must be a standalone ELF executable"
        )
    return raw


def _bootstrap_file(path: Path, destination: Path, mode: int) -> dict[str, str]:
    return {
        "destination": str(destination),
        "mode": f"{mode:04o}",
        "sha256": _sha256_file(path),
        "staged_name": path.name,
    }


def _candidate_files(root: Path) -> list[Path]:
    try:
        status = root.lstat()
    except OSError as exc:
        raise ReleasePreparationError("candidate root is unavailable") from exc
    if not stat.S_ISDIR(status.st_mode) or stat.S_ISLNK(status.st_mode):
        raise ReleasePreparationError("candidate root is not a plain directory")
    files: list[Path] = []
    for directory, directories, names in os.walk(root, followlinks=False):
        parent = Path(directory)
        for name in directories:
            child = parent / name
            child_status = child.lstat()
            if (
                not stat.S_ISDIR(child_status.st_mode)
                or stat.S_ISLNK(child_status.st_mode)
                or stat.S_IMODE(child_status.st_mode) != 0o755
            ):
                raise ReleasePreparationError("candidate directory metadata is unsafe")
        for name in names:
            child = parent / name
            child_status = child.lstat()
            if (
                not stat.S_ISREG(child_status.st_mode)
                or stat.S_ISLNK(child_status.st_mode)
                or child_status.st_nlink != 1
            ):
                raise ReleasePreparationError("candidate file metadata is unsafe")
            files.append(child)
    return sorted(files, key=lambda path: path.relative_to(root).as_posix())


def _source_files(root: Path) -> list[Path]:
    if not root.is_dir() or root.is_symlink():
        raise ReleasePreparationError("release source directory is unavailable")
    files: list[Path] = []
    for path in root.rglob("*"):
        if "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
            continue
        if path.is_symlink():
            raise ReleasePreparationError("release source contains a symbolic link")
        if path.is_file():
            files.append(path)
    return sorted(files, key=lambda path: path.relative_to(root).as_posix())


def _copy_file(source: Path, destination: Path, mode: int) -> None:
    _write_file(destination, _read_plain_file(source), mode)


def _read_plain_file(path: Path) -> bytes:
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
        ):
            raise ReleasePreparationError("release input is not a plain file")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
        current = path.lstat()
    except OSError as exc:
        raise ReleasePreparationError("release input is unavailable") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if (
        _stable_identity(before) != _stable_identity(after)
        or (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino)
    ):
        raise ReleasePreparationError("release input changed while being read")
    return b"".join(chunks)


def _write_file(path: Path, content: bytes, mode: int) -> None:
    missing: list[Path] = []
    current = path.parent
    while not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o755)
        directory.chmod(0o755)
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        mode,
    )
    try:
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short release preparation write")
            view = view[written:]
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _set_directory_mode(path: Path) -> None:
    path.chmod(0o755)


def _source_mode(path: Path) -> int:
    return 0o755 if stat.S_IMODE(path.lstat().st_mode) & 0o111 else 0o644


def _load_project(repository: Path) -> dict[str, Any]:
    try:
        value: Any = tomllib.loads((repository / "pyproject.toml").read_text("utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ReleasePreparationError("project metadata is unavailable") from exc
    if not isinstance(value, dict) or not isinstance(value.get("project"), dict):
        raise ReleasePreparationError("project metadata is invalid")
    project = value["project"]
    assert isinstance(project, dict)
    return project


def _required_string(value: dict[str, Any], key: str) -> str:
    selected = value.get(key)
    if not isinstance(selected, str) or not selected:
        raise ReleasePreparationError(f"project {key} is unavailable")
    return selected


def _required_string_list(value: dict[str, Any], key: str) -> list[str]:
    selected = value.get(key)
    if not isinstance(selected, list) or not selected:
        raise ReleasePreparationError(f"project {key} is unavailable")
    if not all(isinstance(item, str) and item for item in selected):
        raise ReleasePreparationError(f"project {key} is invalid")
    return [str(item) for item in selected]


def _requirement_distribution_name(requirement: str) -> str:
    match = _REQUIREMENT_NAME.match(requirement)
    if match is None:
        raise ReleasePreparationError("project requirement name is invalid")
    return _normalize_distribution(match.group(1))


def _normalize_distribution(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _version_tuple(value: str) -> tuple[int, int, int]:
    major, minor, patch = value.split(".")
    return int(major), int(minor), int(patch)


def _minimal_environment() -> dict[str, str]:
    return {
        "HOME": "/nonexistent",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
        "PIP_CONFIG_FILE": "/dev/null",
    }


def _absolute_output(path: Path, label: str) -> Path:
    if not path.is_absolute():
        raise ReleasePreparationError(f"{label} must be absolute")
    return Path(os.path.abspath(path))


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(_read_plain_file(path)).hexdigest()


def _stable_identity(status: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        status.st_dev,
        status.st_ino,
        status.st_size,
        status.st_mtime_ns,
        status.st_ctime_ns,
    )


def _render_json(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "ascii"
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="operation", required=True)
    prepare = subparsers.add_parser("prepare", help="prepare unprivileged release data")
    prepare.add_argument("--repository", type=Path, default=Path(__file__).parents[2])
    prepare.add_argument("--candidate-root", type=Path, default=PROTECTED_RELEASE_CANDIDATE)
    prepare.add_argument(
        "--authority-staging-root",
        type=Path,
        default=PROTECTED_RELEASE_AUTHORITY_STAGING,
    )
    prepare.add_argument("--release-id", required=True)
    prepare.add_argument("--codex-artifact", required=True, type=Path)
    prepare.add_argument("--code-mode-host-artifact", required=True, type=Path)
    prepare.add_argument("--codex-version", required=True)
    prepare.add_argument("--wheelhouse-source", required=True, type=Path)
    prepare.add_argument("--update-from-manifest-sha256")

    verify = subparsers.add_parser("verify", help="verify prepared candidate data")
    verify.add_argument("--candidate-root", type=Path, default=PROTECTED_RELEASE_CANDIDATE)
    verify.add_argument(
        "--approval",
        type=Path,
        default=PROTECTED_RELEASE_AUTHORITY_STAGING / APPROVAL_NAME,
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the unprivileged preparation/verification interface."""
    if os.geteuid() == 0:
        print("ERROR: release preparation must not run with root privilege", file=sys.stderr)
        return 2
    arguments = _parser().parse_args(argv)
    try:
        if arguments.operation == "verify":
            digest = verify_prepared_release(arguments.candidate_root, arguments.approval)
            print(json.dumps({"approval_sha256": digest, "verified": True}, sort_keys=True))
            return 0
        config = ReleasePreparationConfig(
            repository=arguments.repository,
            candidate_root=arguments.candidate_root,
            authority_staging_root=arguments.authority_staging_root,
            release_id=arguments.release_id,
            codex_artifact=arguments.codex_artifact,
            code_mode_host_artifact=arguments.code_mode_host_artifact,
            codex_version=arguments.codex_version,
            wheelhouse_source=arguments.wheelhouse_source,
            update_from_manifest_sha256=arguments.update_from_manifest_sha256,
        )
        result = prepare_protected_release(config)
        print(
            json.dumps(
                {
                    "approval": str(result.approval),
                    "approval_sha256": result.approval_sha256,
                    "authority_is_trusted": False,
                    "authority_staging_root": str(result.authority_staging_root),
                    "bootstrap_inventory": str(result.bootstrap_inventory),
                    "candidate_root": str(result.candidate_root),
                    "release_id": result.release_id,
                    "resolution_python": str(result.resolution_python),
                },
                sort_keys=True,
            )
        )
        return 0
    except ReleasePreparationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
