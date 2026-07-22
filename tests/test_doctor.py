from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from research_automation_supervisor.doctor import CommandResult, run_doctor

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
    rendered = str(report.to_dict())

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
