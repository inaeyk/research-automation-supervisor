"""Safe first-run checks and plain-language setup diagnostics."""

from __future__ import annotations

import os
import shutil
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
from research_automation_supervisor.doctor import CommandRunner, run_doctor, subprocess_runner


def inspect_environment(
    data_root: Path,
    *,
    runner: CommandRunner = subprocess_runner,
    which: Callable[[str], str | None] = shutil.which,
) -> EnvironmentReportV1:
    """Create safe local directories and inspect every required launch capability."""
    root = _prepare_data_root(data_root)
    report = run_doctor(runner=runner, which=which, cwd=root)
    managed_python = sys.prefix != sys.base_prefix or os.environ.get("RAS_MANAGED_RUNTIME") == "1"
    package_ready = bool(__version__)
    git_ready = report.git.present and report.git.version is not None and report.git.error is None
    codex_ready = report.codex.present and report.codex.supported and report.codex.error is None
    authenticated = report.codex.authenticated is True
    isolation_ready = _bubblewrap_ready(which("bwrap"))
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
                    "Install Git inside WSL, then choose Continue. The campaign has not started."
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
                message="Install or update Codex through your approved software process.",
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
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def _prepare_data_root(path: Path) -> Path:
    try:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        absolute = Path(os.path.abspath(path))
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise CustodianEnvironmentError("Campaign storage could not be prepared.") from exc
    if absolute != resolved or resolved.is_symlink() or not resolved.is_dir():
        raise CustodianEnvironmentError("Campaign storage must be a local regular directory.")
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
