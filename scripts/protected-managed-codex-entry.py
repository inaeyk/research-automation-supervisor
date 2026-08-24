#!/usr/bin/python3
"""Isolated protected-release entrypoint for privileged managed-Codex operations."""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

PRODUCTION_ENTRYPOINT = Path(
    "/opt/research-supervisor-release/scripts/protected-managed-codex-entry.py"
)
_SAFE_ENVIRONMENT = frozenset({"LANG", "LC_ALL", "PATH"})
_QUALIFICATION_ENVIRONMENT = "RAS_PROTECTED_IMPORT_QUALIFICATION"


def _fail(message: str) -> int:
    print(f"ERROR: {message}", file=sys.stderr)
    return 2


def _safe_release_object(path: Path, *, directory: bool, owner_uid: int) -> bool:
    try:
        status = path.lstat()
    except OSError:
        return False
    expected_type = stat.S_ISDIR if directory else stat.S_ISREG
    return (
        expected_type(status.st_mode)
        and not stat.S_ISLNK(status.st_mode)
        and status.st_uid == owner_uid
        and not status.st_mode & 0o022
    )


def main() -> int:
    actual_entrypoint = Path(__file__).absolute()
    if actual_entrypoint.is_symlink():
        return _fail("protected Python entrypoint must not be a symlink")
    try:
        actual_entrypoint = actual_entrypoint.resolve(strict=True)
    except OSError:
        return _fail("protected Python entrypoint is unavailable")
    qualification = (
        os.geteuid() != 0
        and os.environ.get(_QUALIFICATION_ENVIRONMENT) == "1"
        and sys.argv[1:] == ["--qualification-import-probe"]
    )
    if os.geteuid() == 0 and actual_entrypoint != PRODUCTION_ENTRYPOINT:
        return _fail("privileged Python entrypoint is outside the protected release")
    if os.geteuid() == 0 and _QUALIFICATION_ENVIRONMENT in os.environ:
        return _fail("qualification mode is unavailable under privilege")
    if os.geteuid() != 0 and not qualification:
        return _fail("unprivileged protected Python execution is qualification-only")

    release_root = actual_entrypoint.parents[1]
    source_root = release_root / "src"
    package_root = source_root / "research_automation_supervisor"
    expected_owner = 0 if os.geteuid() == 0 else os.geteuid()
    if Path.cwd().resolve(strict=True) != release_root:
        return _fail("protected Python working directory is not deterministic")
    if not _safe_release_object(release_root, directory=True, owner_uid=expected_owner):
        return _fail("protected Python release root is unsafe")
    if not _safe_release_object(source_root, directory=True, owner_uid=expected_owner):
        return _fail("protected Python source root is unsafe")
    if not _safe_release_object(package_root, directory=True, owner_uid=expected_owner):
        return _fail("protected Python package root is unsafe")
    if not _safe_release_object(
        actual_entrypoint, directory=False, owner_uid=expected_owner
    ):
        return _fail("protected Python entrypoint metadata is unsafe")

    flags = sys.flags
    if not (
        flags.isolated
        and flags.ignore_environment
        and flags.safe_path
        and flags.no_user_site
    ):
        return _fail("protected Python interpreter isolation flags are incomplete")
    inherited_path = tuple(sys.path)
    if "" in inherited_path or str(Path.cwd()) in inherited_path:
        return _fail("caller working directory entered protected Python imports")
    allowed_environment = _SAFE_ENVIRONMENT | (
        {_QUALIFICATION_ENVIRONMENT} if qualification else set()
    )
    if set(os.environ) != allowed_environment:
        return _fail("protected Python environment is not minimal")

    sys.path.insert(0, str(source_root))
    try:
        from research_automation_supervisor import managed_codex as managed_codex_module
        from research_automation_supervisor import (
            managed_codex_installer as installer_module,
        )
    except ImportError as exc:
        return _fail(f"protected Python application import failed: {exc}")

    if qualification:
        print(
            json.dumps(
                {
                    "cwd": str(Path.cwd()),
                    "entrypoint": str(actual_entrypoint),
                    "environment": sorted(os.environ),
                    "isolated": bool(flags.isolated),
                    "managed_codex": str(Path(managed_codex_module.__file__).resolve()),
                    "managed_codex_installer": str(
                        Path(installer_module.__file__).resolve()
                    ),
                    "safe_path": bool(flags.safe_path),
                    "sys_path": sys.path,
                    "user_site_disabled": bool(flags.no_user_site),
                },
                sort_keys=True,
            )
        )
        return installer_module.main(
            ["install"], _qualification_release_root=release_root
        )
    return installer_module.main(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
