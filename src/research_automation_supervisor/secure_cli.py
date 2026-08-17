"""Installed CLI gate for legacy commands that retain post-snapshot Git probes.

The protected scientific CLI remains byte-for-byte unchanged.  This installed
entrypoint ensures its two generic Git-worktree diagnostics are reachable only
for a Core-signed sanitized workspace.  Direct module imports are not a
supported campaign authority path.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from typing import NoReturn

from research_automation_supervisor.codex_models import (
    _load_request_yaml,
    _read_request_source,
)
from research_automation_supervisor.gitless_repository import (
    verify_operator_campaign_workspace,
)

_GATED_REQUEST_COMMANDS = frozenset({"run-codex", "validate-codex-request"})


def main() -> None:
    """Apply the snapshot gate, then enter the unchanged Typer application."""
    try:
        gated = _require_signed_workspace_for_legacy_git(sys.argv[1:])
        if gated:
            _seal_legacy_environment()
    except Exception:
        _fail_closed()
    from research_automation_supervisor.cli import app

    app()


def _require_signed_workspace_for_legacy_git(
    arguments: list[str], *, current_directory: Path | None = None
) -> bool:
    if not arguments:
        return False
    if "--" in arguments:
        raise ValueError("ambiguous root option terminator is forbidden")
    command = arguments[0]
    if command.startswith("-"):
        # The only installed root options are eager help/version.  Unknown or
        # reordered layouts never reach a legacy command through this gate.
        if command not in {"--help", "--version"} or len(arguments) != 1:
            raise ValueError("ambiguous root option layout is forbidden")
        return False
    if command == "doctor":
        verify_operator_campaign_workspace(current_directory or Path.cwd())
        return True
    if command not in _GATED_REQUEST_COMMANDS:
        return False
    request_argument = _legacy_request_path_argument(command, arguments[1:])
    if request_argument is None:
        raise ValueError("legacy request path is missing")
    request_path = Path(request_argument).resolve(strict=True)
    data = _load_request_yaml(_read_request_source(request_path))
    workspace_value = data.get("workspace")
    if not isinstance(workspace_value, str):
        raise ValueError("legacy request workspace is invalid")
    workspace = Path(workspace_value)
    if not workspace.is_absolute():
        workspace = request_path.parent / workspace
    verify_operator_campaign_workspace(workspace)
    return True


def _legacy_request_path_argument(command: str, arguments: list[str]) -> str | None:
    request_path: str | None = None
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument in {"--json"}:
            index += 1
            continue
        if command == "run-codex" and argument == "--runs-dir":
            if index + 1 >= len(arguments):
                raise ValueError("runs directory option is incomplete")
            index += 2
            continue
        if command == "run-codex" and argument.startswith("--runs-dir="):
            index += 1
            continue
        if argument.startswith("-") or request_path is not None:
            raise ValueError("legacy request command layout is ambiguous")
        request_path = argument
        index += 1
    return request_path


def _fail_closed() -> NoReturn:
    print(
        "This legacy Git diagnostic is restricted to a Core-sanitized campaign workspace.",
        file=sys.stderr,
    )
    raise SystemExit(4)


def _seal_legacy_environment() -> None:
    """Remove ambient executable authority before importing the frozen CLI."""
    preserved: dict[str, str] = {}
    codex_home = os.environ.get("CODEX_HOME")
    if codex_home is not None:
        path = Path(codex_home)
        try:
            resolved = path.resolve(strict=True)
            status = path.lstat()
            if (
                not path.is_absolute()
                or path != resolved
                or stat.S_ISLNK(status.st_mode)
                or not stat.S_ISDIR(status.st_mode)
                or status.st_uid != os.getuid()
                or status.st_mode & 0o022
            ):
                raise OSError("unsafe Codex credential directory")
            preserved["CODEX_HOME"] = str(resolved)
        except (OSError, RuntimeError) as exc:
            raise ValueError("managed Codex credential directory is unsafe") from exc
    os.environ.clear()
    os.environ.update(
        {
            **preserved,
            "PATH": "/usr/bin:/bin",
            "HOME": "/nonexistent",
            "XDG_CONFIG_HOME": "/nonexistent",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "safe.directory",
            "GIT_CONFIG_VALUE_0": "*",
            "LANG": "C.UTF-8",
        }
    )


if __name__ == "__main__":
    main()
