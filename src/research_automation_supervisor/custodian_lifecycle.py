"""Durable systemd-user lifecycle for the Windows/WSL Custodian backend."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

SYSTEMCTL = "/usr/bin/systemctl"
SYSTEMD_RUN = "/usr/bin/systemd-run"
_COMMIT = re.compile(r"^[0-9a-f]{64}$")

CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


class CustodianLifecycleError(RuntimeError):
    """The intended Custodian service could not be identified or controlled."""


@dataclass(frozen=True)
class CustodianServiceIdentity:
    unit_name: str
    description: str


def custodian_service_identity(data_root: Path) -> CustodianServiceIdentity:
    """Derive one non-secret service identity for one canonical application root."""
    canonical = Path(os.path.abspath(data_root))
    digest = hashlib.sha256(str(canonical).encode("utf-8")).hexdigest()
    return CustodianServiceIdentity(
        unit_name=f"research-supervisor-custodian-{digest}.service",
        description=f"Research Supervisor Custodian {digest}",
    )


def replace_custodian_service(
    *,
    data_root: Path,
    working_directory: Path,
    backend_log: Path,
    codex_home: Path,
    qualified_commit: str,
    command: Sequence[str],
    runner: CommandRunner = subprocess.run,
) -> CustodianServiceIdentity:
    """Replace only this data root's identified service and launch it detached."""
    root = _canonical_directory(data_root, "application data")
    working = _canonical_directory(working_directory, "working directory")
    log = Path(os.path.abspath(backend_log))
    home = _canonical_directory(codex_home, "managed Codex home")
    if log.parent != root / "custodian-state" or log.name != "backend.log":
        raise CustodianLifecycleError("Custodian backend log identity is invalid.")
    if home != root / "codex-home":
        raise CustodianLifecycleError("Managed Codex home identity is invalid.")
    if not _COMMIT.fullmatch(qualified_commit):
        raise CustodianLifecycleError("Qualified source identity is invalid.")
    if not command or not Path(command[0]).is_absolute():
        raise CustodianLifecycleError("Custodian command identity is invalid.")
    executable = Path(command[0])
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise CustodianLifecycleError("Custodian executable is unavailable.")

    identity = custodian_service_identity(root)
    environment = _control_environment()
    state = _inspect_unit(identity, runner=runner, environment=environment)
    if state.get("LoadState") != "not-found":
        if state.get("Description") != identity.description:
            raise CustodianLifecycleError(
                "Existing Custodian service identity did not match this data root."
            )
        stopped = runner(
            [SYSTEMCTL, "--user", "stop", identity.unit_name],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
            env=environment,
        )
        if stopped.returncode != 0:
            raise CustodianLifecycleError("Existing Custodian service could not be stopped.")
        after = _inspect_unit(identity, runner=runner, environment=environment)
        if after.get("LoadState") != "not-found" and after.get("ActiveState") not in {
            "failed",
            "inactive",
        }:
            raise CustodianLifecycleError("Existing Custodian service did not stop.")

    launched = runner(
        [
            SYSTEMD_RUN,
            "--user",
            "--quiet",
            "--collect",
            f"--unit={identity.unit_name}",
            f"--description={identity.description}",
            "--property=Type=exec",
            "--property=KillMode=control-group",
            "--property=TimeoutStopSec=10s",
            "--property=KillSignal=SIGTERM",
            "--property=FinalKillSignal=SIGKILL",
            "--property=UMask=0077",
            "--property=NoNewPrivileges=yes",
            f"--property=StandardOutput=append:{log}",
            f"--property=StandardError=append:{log}",
            f"--working-directory={working}",
            "--setenv=PATH=/usr/bin:/bin",
            f"--setenv=CODEX_HOME={home}",
            "--setenv=RAS_MANAGED_RUNTIME=1",
            f"--setenv=RAS_QUALIFIED_COMMIT={qualified_commit}",
            "--",
            *command,
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
        env=environment,
    )
    if launched.returncode != 0:
        raise CustodianLifecycleError("Custodian service could not be launched.")
    return identity


def _inspect_unit(
    identity: CustodianServiceIdentity,
    *,
    runner: CommandRunner,
    environment: dict[str, str],
) -> dict[str, str]:
    inspected = runner(
        [
            SYSTEMCTL,
            "--user",
            "show",
            identity.unit_name,
            "--no-pager",
            "--property=LoadState",
            "--property=ActiveState",
            "--property=Description",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
        env=environment,
    )
    if inspected.returncode != 0:
        raise CustodianLifecycleError("Custodian user-service manager is unavailable.")
    values: dict[str, str] = {}
    for line in inspected.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator and key not in values:
            values[key] = value
    if values.get("LoadState") not in {"loaded", "not-found"}:
        raise CustodianLifecycleError("Custodian service state is ambiguous.")
    return values


def _control_environment() -> dict[str, str]:
    runtime = f"/run/user/{os.getuid()}"
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": str(Path.home()),
        "XDG_RUNTIME_DIR": runtime,
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path={runtime}/bus",
    }


def _canonical_directory(path: Path, label: str) -> Path:
    try:
        absolute = Path(os.path.abspath(path))
        if absolute != path or path.is_symlink() or not path.is_dir():
            raise OSError(f"{label} is not canonical")
        return path.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise CustodianLifecycleError(f"Custodian {label} is invalid.") from exc


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Launch the durable Custodian user service")
    parser.add_argument("--data-root", required=True, type=Path)
    parser.add_argument("--working-directory", required=True, type=Path)
    parser.add_argument("--backend-log", required=True, type=Path)
    parser.add_argument("--codex-home", required=True, type=Path)
    parser.add_argument("--qualified-commit", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    try:
        replace_custodian_service(
            data_root=args.data_root,
            working_directory=args.working_directory,
            backend_log=args.backend_log,
            codex_home=args.codex_home,
            qualified_commit=args.qualified_commit,
            command=command,
        )
    except CustodianLifecycleError as exc:
        print(str(exc), file=sys.stderr)
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
