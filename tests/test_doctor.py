from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from research_automation_supervisor.cli import _format_doctor
from research_automation_supervisor.doctor import (
    CodexDiagnostic,
    CommandResult,
    DoctorReport,
    GitDiagnostic,
    PythonDiagnostic,
    run_doctor,
)

GIT = "/tools/git"
CODEX = "/tools/codex"
CWD = Path("/workspace/project")


class FakeRunner:
    def __init__(self, results: dict[tuple[str, ...], CommandResult]) -> None:
        self.results = results
        self.calls: list[tuple[tuple[str, ...], float]] = []

    def __call__(self, args: Sequence[str], *, timeout: float) -> CommandResult:
        key = tuple(args)
        self.calls.append((key, timeout))
        return self.results[key]


class TimeoutRunner(FakeRunner):
    def __init__(
        self,
        results: dict[tuple[str, ...], CommandResult],
        timed_out_call: tuple[str, ...],
    ) -> None:
        super().__init__(results)
        self.timed_out_call = timed_out_call

    def __call__(self, args: Sequence[str], *, timeout: float) -> CommandResult:
        if tuple(args) == self.timed_out_call:
            raise subprocess.TimeoutExpired(list(args), timeout)
        return super().__call__(args, timeout=timeout)


def successful_results(*, dirty: bool = False) -> dict[tuple[str, ...], CommandResult]:
    return {
        (GIT, "--version"): CommandResult(0, "git version 2.45.1\n"),
        (GIT, "-C", str(CWD), "rev-parse", "--show-toplevel"): CommandResult(
            0, f"{CWD}\n"
        ),
        (
            GIT,
            "-C",
            str(CWD),
            "status",
            "--porcelain",
            "--untracked-files=normal",
        ): CommandResult(0, " M changed.py\n" if dirty else ""),
        (CODEX, "--version"): CommandResult(0, "codex-cli 0.144.0\n"),
        (CODEX, "login", "status"): CommandResult(0, "Logged in using SECRET_TOKEN\n"),
    }


def which_with(*commands: str):
    paths = {"git": GIT, "codex": CODEX}

    def find(command: str) -> str | None:
        return paths[command] if command in commands else None

    return find


@pytest.mark.parametrize(("dirty", "expected_clean"), [(False, True), (True, False)])
def test_reports_clean_and_dirty_repository_states(dirty: bool, expected_clean: bool) -> None:
    runner = FakeRunner(successful_results(dirty=dirty))

    report = run_doctor(
        runner=runner,
        which=which_with("git", "codex"),
        cwd=CWD,
        python_version=(3, 12, 1),
    )

    assert report.ok
    assert report.git.inside_repository
    assert report.git.repository_root == str(CWD)
    assert report.git.clean is expected_clean
    assert all(timeout == 10.0 for _, timeout in runner.calls)


def test_missing_git_degrades_gracefully() -> None:
    results = successful_results()
    runner = FakeRunner({key: value for key, value in results.items() if key[0] == CODEX})

    report = run_doctor(
        runner=runner,
        which=which_with("codex"),
        cwd=CWD,
        python_version=(3, 11, 0),
    )

    assert not report.ok
    assert not report.git.present
    assert report.git.clean is None
    assert "Git executable is required." in report.dependency_errors


def test_missing_codex_degrades_gracefully() -> None:
    results = successful_results()
    runner = FakeRunner({key: value for key, value in results.items() if key[0] == GIT})

    report = run_doctor(
        runner=runner,
        which=which_with("git"),
        cwd=CWD,
        python_version=(3, 11, 0),
    )

    assert not report.ok
    assert not report.codex.present
    assert report.codex.login_status == "not checked"
    assert "Codex executable is required." in report.dependency_errors


def test_old_codex_version_is_unsupported() -> None:
    results = successful_results()
    results[(CODEX, "--version")] = CommandResult(0, "codex-cli 0.143.9\n")

    report = run_doctor(
        runner=FakeRunner(results),
        which=which_with("git", "codex"),
        cwd=CWD,
        python_version=(3, 11, 0),
    )

    assert report.codex.version == "0.143.9"
    assert not report.codex.supported
    assert "Codex 0.144.0 or newer is required." in report.dependency_errors


def test_failed_login_is_sanitized() -> None:
    results = successful_results()
    results[(CODEX, "login", "status")] = CommandResult(
        1, "token=super-secret", "credential file: /private/auth.json"
    )

    report = run_doctor(
        runner=FakeRunner(results),
        which=which_with("git", "codex"),
        cwd=CWD,
        python_version=(3, 11, 0),
    )
    rendered = f"{report.to_dict()}\n{_format_doctor(report)}"

    assert not report.ok
    assert report.codex.authenticated is False
    assert report.codex.login_status == "not authenticated"
    assert "super-secret" not in rendered
    assert "/private/auth.json" not in rendered


def test_outside_repository_is_reported_without_dependency_failure() -> None:
    results = successful_results()
    results[(GIT, "-C", str(CWD), "rev-parse", "--show-toplevel")] = CommandResult(
        128, "", "not a git repository"
    )

    report = run_doctor(
        runner=FakeRunner(results),
        which=which_with("git", "codex"),
        cwd=CWD,
        python_version=(3, 11, 0),
    )

    assert report.ok
    assert not report.git.inside_repository
    assert report.git.repository_root is None
    assert report.git.clean is None


def test_failed_git_version_command_does_not_parse_stdout() -> None:
    results = successful_results()
    results[(GIT, "--version")] = CommandResult(1, "git version 99.0.0\n")

    report = run_doctor(
        runner=FakeRunner(results),
        which=which_with("git", "codex"),
        cwd=CWD,
        python_version=(3, 11, 0),
    )

    assert report.git.version is None
    assert report.git.error == "Git version command failed or timed out."
    assert "Git version command failed or timed out." in report.dependency_errors
    assert not report.ok


def test_failed_codex_version_command_does_not_parse_stdout() -> None:
    results = successful_results()
    results[(CODEX, "--version")] = CommandResult(1, "codex-cli 99.0.0\n")

    report = run_doctor(
        runner=FakeRunner(results),
        which=which_with("git", "codex"),
        cwd=CWD,
        python_version=(3, 11, 0),
    )

    assert report.codex.version is None
    assert not report.codex.supported
    assert report.codex.error == "Codex version command failed or timed out."
    assert not report.ok


@pytest.mark.parametrize(
    "output",
    [
        "runtime 99.0.0; codex version unknown\n",
        "wrapper 1.2.3\ncodex-cli 0.145.0\n",
        "version 0.145.0\n",
    ],
)
def test_codex_version_rejects_unrecognized_output(output: str) -> None:
    results = successful_results()
    results[(CODEX, "--version")] = CommandResult(0, output)

    report = run_doctor(
        runner=FakeRunner(results),
        which=which_with("git", "codex"),
        cwd=CWD,
        python_version=(3, 11, 0),
    )

    assert report.codex.version is None
    assert report.codex.error == "Codex version could not be determined."
    assert not report.ok


def test_failed_repository_probe_is_indeterminate_and_unready() -> None:
    results = successful_results()
    results[(GIT, "-C", str(CWD), "rev-parse", "--show-toplevel")] = CommandResult(
        1, "", "permission denied"
    )

    report = run_doctor(
        runner=FakeRunner(results),
        which=which_with("git", "codex"),
        cwd=CWD,
        python_version=(3, 11, 0),
    )

    assert report.git.inside_repository is None
    assert report.git.repository_root is None
    assert report.git.error == "Git repository probe failed or timed out."
    assert not report.ok


def test_timed_out_repository_probe_is_indeterminate_and_unready() -> None:
    results = successful_results()
    probe = (GIT, "-C", str(CWD), "rev-parse", "--show-toplevel")

    report = run_doctor(
        runner=TimeoutRunner(results, probe),
        which=which_with("git", "codex"),
        cwd=CWD,
        python_version=(3, 11, 0),
    )

    assert report.git.inside_repository is None
    assert report.git.error == "Git repository probe failed or timed out."
    assert "Git repository probe failed or timed out." in report.dependency_errors
    assert not report.ok


def test_repository_root_can_differ_from_current_directory() -> None:
    root = Path("/workspace")
    results = successful_results()
    results[(GIT, "-C", str(CWD), "rev-parse", "--show-toplevel")] = CommandResult(
        0, f"{root}\n"
    )
    results[
        (GIT, "-C", str(root), "status", "--porcelain", "--untracked-files=normal")
    ] = CommandResult(0, "")
    runner = FakeRunner(results)

    report = run_doctor(
        runner=runner,
        which=which_with("git", "codex"),
        cwd=CWD,
        python_version=(3, 11, 0),
    )

    assert report.ok
    assert report.git.repository_root == str(root)
    assert (
        (GIT, "-C", str(root), "status", "--porcelain", "--untracked-files=normal"),
        10.0,
    ) in runner.calls


def test_operational_error_defensively_prevents_ready_report() -> None:
    report = DoctorReport(
        python=PythonDiagnostic("3.12.0", True),
        git=GitDiagnostic(
            True,
            "2.45.1",
            None,
            error="Git repository probe failed or timed out.",
        ),
        codex=CodexDiagnostic(
            True,
            "0.145.0",
            True,
            authenticated=True,
            login_status="authenticated",
        ),
        dependency_errors=(),
    )

    assert not report.ok
