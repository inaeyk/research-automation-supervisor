"""Read-only, injectable environment diagnostics."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

from packaging.version import InvalidVersion, Version

MINIMUM_PYTHON = (3, 11)
MINIMUM_CODEX = Version("0.144.0")
COMMAND_TIMEOUT_SECONDS = 10.0
_SEMANTIC_VERSION = re.compile(r"(?<!\d)(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)(?!\d)")


@dataclass(frozen=True)
class CommandResult:
    """Sanitized portion of a completed subprocess result."""

    returncode: int
    stdout: str = ""
    stderr: str = ""


class CommandRunner(Protocol):
    """Callable boundary used to replace subprocess execution in tests."""

    def __call__(self, args: Sequence[str], *, timeout: float) -> CommandResult: ...


@dataclass(frozen=True)
class PythonDiagnostic:
    version: str
    supported: bool
    minimum_version: str = "3.11"


@dataclass(frozen=True)
class GitDiagnostic:
    present: bool
    version: str | None = None
    inside_repository: bool = False
    repository_root: str | None = None
    clean: bool | None = None
    error: str | None = None


@dataclass(frozen=True)
class CodexDiagnostic:
    present: bool
    version: str | None = None
    supported: bool = False
    minimum_version: str = str(MINIMUM_CODEX)
    authenticated: bool | None = None
    login_status: str = "not checked"
    error: str | None = None


@dataclass(frozen=True)
class DoctorReport:
    python: PythonDiagnostic
    git: GitDiagnostic
    codex: CodexDiagnostic
    dependency_errors: tuple[str, ...]

    @property
    def ok(self) -> bool:
        """Whether required dependencies and authentication are ready."""
        return not self.dependency_errors

    def to_dict(self) -> dict[str, object]:
        """Return a deterministic JSON-compatible representation."""
        return {
            "ok": self.ok,
            "python": asdict(self.python),
            "git": asdict(self.git),
            "codex": asdict(self.codex),
            "dependency_errors": list(self.dependency_errors),
        }


def subprocess_runner(args: Sequence[str], *, timeout: float) -> CommandResult:
    """Run an argument-vector command with bounded execution time."""
    completed = subprocess.run(
        list(args),
        capture_output=True,
        check=False,
        text=True,
        timeout=timeout,
    )
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


def run_doctor(
    *,
    runner: CommandRunner = subprocess_runner,
    which: Callable[[str], str | None] = shutil.which,
    cwd: Path | None = None,
    python_version: tuple[int, int, int] | None = None,
) -> DoctorReport:
    """Inspect the local environment without making any changes."""
    current_python = python_version or sys.version_info[:3]
    python_diagnostic = PythonDiagnostic(
        version=".".join(str(part) for part in current_python),
        supported=current_python[:2] >= MINIMUM_PYTHON,
    )
    working_directory = (cwd or Path.cwd()).resolve()
    git = _check_git(runner, which("git"), working_directory)
    codex = _check_codex(runner, which("codex"))

    errors: list[str] = []
    if not python_diagnostic.supported:
        errors.append("Python 3.11 or newer is required.")
    if not git.present:
        errors.append("Git executable is required.")
    elif git.version is None:
        errors.append("Git version could not be determined.")
    if not codex.present:
        errors.append("Codex executable is required.")
    elif codex.version is None:
        errors.append("Codex version could not be determined.")
    elif not codex.supported:
        errors.append(f"Codex {MINIMUM_CODEX} or newer is required.")
    if codex.present and codex.authenticated is not True:
        errors.append("Codex login is required.")

    return DoctorReport(python_diagnostic, git, codex, tuple(errors))


def _check_git(runner: CommandRunner, executable: str | None, cwd: Path) -> GitDiagnostic:
    if executable is None:
        return GitDiagnostic(present=False, error="Git executable was not found.")

    version_result = _safe_run(runner, [executable, "--version"])
    version = _parse_git_version(version_result.stdout) if version_result else None
    error = None
    if version_result is None or version_result.returncode != 0 or version is None:
        error = "Git version check failed."

    repository_result = _safe_run(
        runner, [executable, "-C", str(cwd), "rev-parse", "--show-toplevel"]
    )
    if repository_result is None or repository_result.returncode != 0:
        return GitDiagnostic(present=True, version=version, error=error)

    root_text = repository_result.stdout.strip()
    if not root_text:
        return GitDiagnostic(
            present=True,
            version=version,
            error="Git repository root could not be determined.",
        )
    root = str(Path(root_text).resolve())
    status_result = _safe_run(
        runner, [executable, "-C", root, "status", "--porcelain", "--untracked-files=normal"]
    )
    clean = (
        status_result.returncode == 0 and not status_result.stdout.strip()
        if status_result is not None
        else None
    )
    if status_result is None or status_result.returncode != 0:
        error = "Git repository status check failed."
        clean = None
    return GitDiagnostic(
        present=True,
        version=version,
        inside_repository=True,
        repository_root=root,
        clean=clean,
        error=error,
    )


def _check_codex(runner: CommandRunner, executable: str | None) -> CodexDiagnostic:
    if executable is None:
        return CodexDiagnostic(present=False, error="Codex executable was not found.")

    version_result = _safe_run(runner, [executable, "--version"])
    version = _parse_codex_version(version_result.stdout) if version_result else None
    supported = version is not None and Version(version) >= MINIMUM_CODEX
    error = None
    if version_result is None or version_result.returncode != 0 or version is None:
        error = "Codex version check failed."

    login_result = _safe_run(runner, [executable, "login", "status"])
    if login_result is None:
        authenticated = None
        login_status = "check failed"
        error = error or "Codex login status check failed."
    elif login_result.returncode == 0:
        authenticated = True
        login_status = "authenticated"
    else:
        authenticated = False
        login_status = "not authenticated"

    return CodexDiagnostic(
        present=True,
        version=version,
        supported=supported,
        authenticated=authenticated,
        login_status=login_status,
        error=error,
    )


def _safe_run(
    runner: CommandRunner, args: Sequence[str], timeout: float = COMMAND_TIMEOUT_SECONDS
) -> CommandResult | None:
    try:
        return runner(args, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None


def _parse_git_version(output: str) -> str | None:
    match = re.search(r"\bgit version\s+([^\s]+)", output, flags=re.IGNORECASE)
    return match.group(1) if match else None


def _parse_codex_version(output: str) -> str | None:
    match = _SEMANTIC_VERSION.search(output)
    if match is None:
        return None
    try:
        return str(Version(match.group(1)))
    except InvalidVersion:
        return None
