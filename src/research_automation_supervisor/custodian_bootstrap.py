"""Safe first-run checks and plain-language setup diagnostics."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

from research_automation_supervisor import __version__
from research_automation_supervisor.custodian_errors import CustodianEnvironmentError
from research_automation_supervisor.custodian_models import (
    EnvironmentIssueV1,
    EnvironmentReportV1,
)
from research_automation_supervisor.doctor import (
    CommandRunner,
    _check_codex,
    subprocess_runner,
)
from research_automation_supervisor.managed_codex import (
    ManagedCodexIdentity,
    ManagedCodexSecurityError,
    managed_codex_home_for_data_root,
    trusted_system_executable,
    verified_managed_codex_home,
    verify_managed_codex_installation,
)


def inspect_environment(
    data_root: Path,
    *,
    runner: CommandRunner = subprocess_runner,
    which: Callable[[str], str | None] = shutil.which,
    allow_program_execution: bool = False,
) -> EnvironmentReportV1:
    """Create safe local directories and inspect every required launch capability."""
    del which  # Qualified readiness never resolves executable authority from PATH.
    root = _prepare_data_root(data_root)
    # The public ``git_ready`` field is retained for schema compatibility.  Its
    # implementation is the imported Dulwich object reader, not a Git probe.
    trusted_codex = _verified_managed_codex_identity()
    codex_path = str(trusted_codex.executable) if trusted_codex is not None else None
    codex = (
        _check_codex(runner, codex_path)
        if allow_program_execution and codex_path is not None
        else None
    )
    expected_codex_home = managed_codex_home_for_data_root(root)
    try:
        managed_codex_home_ready = (
            verified_managed_codex_home() == expected_codex_home
        )
    except CustodianEnvironmentError:
        managed_codex_home_ready = False
    managed_python = sys.prefix != sys.base_prefix or os.environ.get("RAS_MANAGED_RUNTIME") == "1"
    package_ready = bool(__version__)
    git_ready = True
    codex_ready = codex_path is not None if codex is None else (
        codex.present
        and codex.supported
        and codex.error is None
        and trusted_codex is not None
        and codex.version == trusted_codex.version
    )
    authenticated = False if codex is None else codex.authenticated is True
    bwrap = _trusted_system_executable(Path("/usr/bin/bwrap"))
    isolation_ready = (
        bwrap is not None
        if not allow_program_execution
        else _bubblewrap_ready(str(bwrap) if bwrap is not None else None)
    )
    filesystem_ready = _verify_filesystem(root)
    issues: list[EnvironmentIssueV1] = []
    if not managed_python:
        issues.append(
            EnvironmentIssueV1(
                code="managed_python_unavailable",
                title="Managed Python setup is incomplete",
                message="Run the first-time launcher again. The campaign has not started.",
                action="install_dependency",
                campaign_not_started=True,
            )
        )
    if not package_ready:
        issues.append(
            EnvironmentIssueV1(
                code="supervisor_package_unavailable",
                title="Research Supervisor setup is incomplete",
                message="Run the first-time launcher again. No scientific state has changed.",
                action="install_dependency",
                campaign_not_started=True,
            )
        )
    if not git_ready:
        issues.append(
            EnvironmentIssueV1(
                code="git_unavailable",
                title="Git is needed",
                message=(
                    "Repair the Gitless repository reader, then choose Continue. "
                    "The campaign has not started."
                ),
                action="request_admin",
                campaign_not_started=True,
            )
        )
    if not codex_ready:
        issues.append(
            EnvironmentIssueV1(
                code="codex_unavailable",
                title="Codex needs setup",
                message=(
                    "Ask your administrator to run the one-time Research Supervisor setup "
                    "with an approved Codex artifact. The campaign has not started."
                ),
                action="request_admin",
                campaign_not_started=True,
            )
        )
    elif not managed_codex_home_ready:
        issues.append(
            EnvironmentIssueV1(
                code="managed_codex_home_unavailable",
                title="Managed Codex sign-in storage needs setup",
                message=(
                    "Close Research Supervisor and double-click it again. If this persists, "
                    "ask your administrator for help. The campaign has not started."
                ),
                action="install_dependency",
                campaign_not_started=True,
            )
        )
    elif not authenticated:
        issues.append(
            EnvironmentIssueV1(
                code="codex_authentication_required",
                title="Codex needs authentication",
                message=(
                    "Choose Sign in. The campaign has not started and no scientific state changed."
                ),
                action="sign_in",
                campaign_not_started=True,
            )
        )
    if not isolation_ready:
        issues.append(
            EnvironmentIssueV1(
                code="bubblewrap_unavailable",
                title="Isolation support is needed",
                message=(
                    "Bubblewrap is unavailable. Installation may require administrator approval."
                ),
                action="request_admin",
                campaign_not_started=True,
            )
        )
    if not filesystem_ready:
        issues.append(
            EnvironmentIssueV1(
                code="filesystem_capability_unavailable",
                title="This folder cannot safely store campaigns",
                message=(
                    "Choose a local WSL data location with atomic rename and hard-link support."
                ),
                action="review_repository",
                campaign_not_started=True,
            )
        )
    ready = (
        all(
            (
                managed_python,
                package_ready,
                git_ready,
                codex_ready,
                managed_codex_home_ready,
                authenticated,
                isolation_ready,
                filesystem_ready,
            )
        )
        and not issues
    )
    return EnvironmentReportV1(
        ready=ready,
        backend="wsl" if _is_wsl() else "linux",
        managed_python_ready=managed_python,
        supervisor_package_ready=package_ready,
        git_ready=git_ready,
        codex_ready=codex_ready,
        codex_authenticated=authenticated,
        isolation_ready=isolation_ready,
        filesystem_ready=filesystem_ready,
        issues=tuple(issues),
    )


def _bubblewrap_ready(executable: str | None) -> bool:
    if executable is None:
        return False
    try:
        completed = subprocess.run(
            [
                executable,
                "--die-with-parent",
                "--new-session",
                "--unshare-all",
                "--ro-bind",
                "/usr",
                "/usr",
                "--ro-bind",
                "/bin",
                "/bin",
                "--ro-bind",
                "/lib",
                "/lib",
                "--ro-bind",
                "/lib64",
                "/lib64",
                "/bin/dash",
                "-c",
                "exit 0",
            ],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            env={"PATH": "/usr/bin:/bin"},
            cwd="/",
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def _trusted_system_executable(path: Path) -> Path | None:
    """Compatibility wrapper around the shared qualified executable contract."""
    return trusted_system_executable(path)


def _verified_managed_codex_identity() -> ManagedCodexIdentity | None:
    """Compatibility seam around the one installation identity verifier."""
    try:
        return verify_managed_codex_installation(require_code_mode_host=True)
    except ManagedCodexSecurityError:
        return None


def _prepare_data_root(path: Path) -> Path:
    try:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        resolved = Path(os.path.abspath(path))
        if resolved != path.resolve(strict=True) or path.is_symlink():
            raise OSError("application data resolves elsewhere")
        status = path.lstat()
        if status.st_uid != os.getuid() or stat.S_IMODE(status.st_mode) != 0o700:
            raise OSError("application data metadata is unsafe")
    except (OSError, RuntimeError, ValueError) as exc:
        raise CustodianEnvironmentError("Application data is unsafe.") from exc
    for name in (
        "custodian-state",
        "operator-exchange",
        "managed-repositories",
        "workspaces",
        "qualified-campaigns",
        "exports",
    ):
        child = resolved / name
        child.mkdir(exist_ok=True, mode=0o700)
        if child.is_symlink() or not child.is_dir():
            raise CustodianEnvironmentError("Campaign storage contains an unsafe directory.")
    return resolved


def _verify_filesystem(root: Path) -> bool:
    try:
        with tempfile.TemporaryDirectory(dir=root, prefix=".capability-") as name:
            directory = Path(name)
            source = directory / "source"
            link = directory / "link"
            renamed = directory / "renamed"
            source.write_bytes(b"capability\n")
            os.link(source, link)
            os.replace(source, renamed)
            return link.read_bytes() == renamed.read_bytes() == b"capability\n"
    except OSError:
        return False


def _is_wsl() -> bool:
    try:
        text = Path("/proc/sys/kernel/osrelease").read_text(encoding="utf-8")
    except OSError:
        return False
    return "microsoft" in text.casefold()
