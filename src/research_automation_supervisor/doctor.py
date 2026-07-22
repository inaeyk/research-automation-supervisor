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

from packaging.version import Version

MINIMUM_PYTHON = (3, 11)
MINIMUM_CODEX = Version("0.144.0")
COMMAND_TIMEOUT_SECONDS = 10.0
_CODEX_VERSION_OUTPUT = re.compile(
    r"(?:codex|codex-cli)(?: +version)? +v?(?P<version>[0-9]+\.[0-9]+\.[0-9]+)",
    flags=re.IGNORECASE,
)
_GIT_VERSION_OUTPUT = re.compile(
    r"git version +(?P<version>\d+(?:\.\d+){1,3})"
    r"(?:\.windows\.\d+(?:\.\d+)*| \(Apple Git-\d+(?:\.\d+)*\))?"
)


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
    inside_repository: bool | None = None
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
        return (
            not self.dependency_errors
            and self.python.supported
            and self.git.present
            and self.git.version is not None
            and self.git.error is None
            and self.git.inside_repository is not None
            and (not self.git.inside_repository or self.git.clean is not None)
            and self.codex.present
            and self.codex.version is not None
            and self.codex.supported
            and self.codex.authenticated is True
            and self.codex.error is None
        )

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
        encoding="utf-8",
        errors="replace",
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
    elif git.error is not None:
        errors.append(git.error)
    if not codex.present:
        errors.append("Codex executable is required.")
    else:
        if codex.error is not None:
            errors.append(codex.error)
        if codex.version is not None and not codex.supported:
            errors.append(f"Codex {MINIMUM_CODEX} or newer is required.")
        if codex.authenticated is not True:
            errors.append("Codex login is required.")

    return DoctorReport(python_diagnostic, git, codex, tuple(errors))


def _check_git(runner: CommandRunner, executable: str | None, cwd: Path) -> GitDiagnostic:
    if executable is None:
        return GitDiagnostic(present=False, error="Git executable was not found.")

    version_result = _safe_run(runner, [executable, "--version"])
    version: str | None = None
    errors: list[str] = []
    if version_result is None or version_result.returncode != 0:
        errors.append("Git version command failed or timed out.")
    else:
        version = _parse_git_version(version_result.stdout)
        if version is None:
            errors.append("Git version could not be determined.")

    repository_result = _safe_run(
        runner, [executable, "-C", str(cwd), "rev-parse", "--show-toplevel"]
    )
    if repository_result is None:
        errors.append("Git repository probe failed or timed out.")
        return GitDiagnostic(present=True, version=version, error=_join_errors(errors))
    if repository_result.returncode != 0:
        if _is_not_git_repository(repository_result):
            return GitDiagnostic(
                present=True,
                version=version,
                inside_repository=False,
                error=_join_errors(errors),
            )
        errors.append("Git repository probe failed or timed out.")
        return GitDiagnostic(present=True, version=version, error=_join_errors(errors))

    root_text = repository_result.stdout.strip()
    if not root_text:
        errors.append("Git repository root could not be determined.")
        return GitDiagnostic(
            present=True,
            version=version,
            error=_join_errors(errors),
        )
    root = str(Path(root_text).resolve())
    status_result = _safe_run(
        runner,
        [
            executable,
            "--no-optional-locks",
            "-C",
            root,
            "status",
            "--porcelain",
            "--untracked-files=normal",
        ],
    )
    clean = (
        status_result.returncode == 0 and not status_result.stdout.strip()
        if status_result is not None
        else None
    )
    if status_result is None or status_result.returncode != 0:
        errors.append("Git repository status check failed or timed out.")
        clean = None
    return GitDiagnostic(
        present=True,
        version=version,
        inside_repository=True,
        repository_root=root,
        clean=clean,
        error=_join_errors(errors),
    )


def _check_codex(runner: CommandRunner, executable: str | None) -> CodexDiagnostic:
    if executable is None:
        return CodexDiagnostic(present=False, error="Codex executable was not found.")

    version_result = _safe_run(runner, [executable, "--version"])
    version: str | None = None
    errors: list[str] = []
    if version_result is None or version_result.returncode != 0:
        errors.append("Codex version command failed or timed out.")
    else:
        version = _parse_codex_version(version_result.stdout)
        if version is None:
            errors.append("Codex version could not be determined.")
    supported = version is not None and Version(version) >= MINIMUM_CODEX

    login_result = _safe_run(runner, [executable, "login", "status"])
    if login_result is None:
        authenticated = None
        login_status = "check failed"
        errors.append("Codex login status check failed or timed out.")
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
        error=_join_errors(errors),
    )


def _safe_run(
    runner: CommandRunner, args: Sequence[str], timeout: float = COMMAND_TIMEOUT_SECONDS
) -> CommandResult | None:
    try:
        return runner(args, timeout=timeout)
    except (OSError, UnicodeError, subprocess.SubprocessError):
        return None


def _parse_git_version(output: str) -> str | None:
    match = _GIT_VERSION_OUTPUT.fullmatch(output.strip())
    return match.group("version") if match else None


def _parse_codex_version(output: str) -> str | None:
    match = _CODEX_VERSION_OUTPUT.fullmatch(output.strip())
    return match.group("version") if match else None


def _is_not_git_repository(result: CommandResult) -> bool:
    output = f"{result.stdout}\n{result.stderr}".lower()
    return result.returncode == 128 and "not a git repository" in output


def _join_errors(errors: Sequence[str]) -> str | None:
    return " ".join(errors) if errors else None
