"""OS-enforced Bubblewrap isolation for Stage 4 supervisor turns."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Literal, Protocol

from research_automation_supervisor.codex_adapter import CodexProcessLaunch
from research_automation_supervisor.codex_models import PreparedCodexRequest
from research_automation_supervisor.errors import (
    LiveShadowDependencyError,
    LiveShadowIntegrityError,
)
from research_automation_supervisor.shadow_models import PendingSupervisorAction
from research_automation_supervisor.workflow_integrity import CodexMetadata

ISOLATION_SCHEMA_VERSION = 1
DEFAULT_BUBBLEWRAP_EXECUTABLE = "/usr/bin/bwrap"
ISOLATED_CODEX_PATH = "/opt/ras/codex"
ISOLATED_WORKSPACE_PATH = "/workspace"
ISOLATED_ACTION_DIRECTORY = "/action"
ISOLATED_OUTPUT_SCHEMA_PATH = "/control/output-schema.json"
ISOLATED_HOME = "/home/supervisor"
ISOLATED_AUTH_PATH = "/home/supervisor/auth.json"
ISOLATED_TMPDIR = "/tmp"

_TRUSTED_EXECUTABLE_DIRECTORIES = (
    Path("/usr/bin"),
    Path("/usr/sbin"),
    Path("/bin"),
    Path("/sbin"),
)
_REQUIRED_OPTIONS = frozenset(
    {
        "--bind",
        "--chdir",
        "--dev",
        "--die-with-parent",
        "--dir",
        "--new-session",
        "--proc",
        "--ro-bind",
        "--tmpfs",
        "--unshare-cgroup-try",
        "--unshare-ipc",
        "--unshare-pid",
        "--unshare-user",
        "--unshare-uts",
    }
)


@dataclass(frozen=True)
class BubblewrapBackendIdentity:
    """Nonsecret durable identity of one successful isolation preflight."""

    schema_version: Literal[1]
    isolation_schema_version: Literal[1]
    backend: Literal["bubblewrap"]
    canonical_bubblewrap_path: str
    bubblewrap_version: str
    capability_result: Literal["passed"]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "isolation_schema_version": self.isolation_schema_version,
            "backend": self.backend,
            "canonical_bubblewrap_path": self.canonical_bubblewrap_path,
            "bubblewrap_version": self.bubblewrap_version,
            "capability_result": self.capability_result,
        }

    @classmethod
    def from_dict(cls, value: object) -> BubblewrapBackendIdentity:
        expected_keys = {
            "schema_version",
            "isolation_schema_version",
            "backend",
            "canonical_bubblewrap_path",
            "bubblewrap_version",
            "capability_result",
        }
        if not isinstance(value, dict) or set(value) != expected_keys:
            raise LiveShadowIntegrityError("isolation backend evidence is invalid")
        try:
            identity = cls(
                schema_version=value["schema_version"],
                isolation_schema_version=value["isolation_schema_version"],
                backend=value["backend"],
                canonical_bubblewrap_path=value["canonical_bubblewrap_path"],
                bubblewrap_version=value["bubblewrap_version"],
                capability_result=value["capability_result"],
            )
        except TypeError as exc:
            raise LiveShadowIntegrityError(
                "isolation backend evidence is invalid"
            ) from exc
        if (
            identity.schema_version != 1
            or identity.isolation_schema_version != ISOLATION_SCHEMA_VERSION
            or identity.backend != "bubblewrap"
            or not isinstance(identity.canonical_bubblewrap_path, str)
            or not identity.canonical_bubblewrap_path
            or not isinstance(identity.bubblewrap_version, str)
            or not identity.bubblewrap_version
            or identity.capability_result != "passed"
        ):
            raise LiveShadowIntegrityError("isolation backend evidence is invalid")
        return identity


@dataclass(frozen=True)
class BubblewrapCapability:
    """Successful active preflight plus the non-artifact authentication locator."""

    identity: BubblewrapBackendIdentity
    authentication_file: Path = field(repr=False, compare=False)


class IsolationPreflight(Protocol):
    """Injectable active dependency boundary used before Stage 2 launch/recovery."""

    def __call__(
        self,
        *,
        bubblewrap_executable: str | None,
        codex_executable: str,
        authentication_file: Path | None,
        environ: Mapping[str, str] | None,
        forbidden_roots: Sequence[Path],
    ) -> BubblewrapCapability: ...


@dataclass(frozen=True)
class _Mount:
    option: Literal["--bind", "--ro-bind"]
    source: Path
    destination: str
    purpose: str


def preflight_bubblewrap_isolation(
    *,
    bubblewrap_executable: str | None,
    codex_executable: str,
    authentication_file: Path | None,
    environ: Mapping[str, str] | None,
    forbidden_roots: Sequence[Path],
) -> BubblewrapCapability:
    """Actively prove a synthetic root without invoking Codex or a model."""
    bwrap = _resolve_trusted_bubblewrap(bubblewrap_executable)
    codex = _canonical_engine_path(
        Path(codex_executable),
        kind="file",
        label="Codex executable",
    )
    if not os.access(codex, os.X_OK):
        raise LiveShadowDependencyError("Codex executable is not executable")
    auth = _resolve_authentication_file(authentication_file, environ)
    help_text = _run_dependency_probe((str(bwrap), "--help"))
    missing = sorted(option for option in _REQUIRED_OPTIONS if option not in help_text)
    if missing:
        raise LiveShadowDependencyError(
            "Bubblewrap does not support the required isolation options"
        )
    version_output = _run_dependency_probe((str(bwrap), "--version"))
    version = version_output.strip()
    if not version.startswith("bubblewrap ") or len(version) > 128:
        raise LiveShadowDependencyError("Bubblewrap version could not be verified")

    with tempfile.TemporaryDirectory(
        prefix="ras-stage4-isolation-preflight-"
    ) as temporary:
        root = Path(temporary)
        workspace = root / "workspace"
        action = root / "action"
        runtime_home = root / "codex-home"
        control = root / "output-schema.json"
        workspace.mkdir()
        action.mkdir()
        runtime_home.mkdir()
        control.write_text("{}\n", encoding="ascii")
        mounts = _build_mounts(
            workspace=workspace,
            action_directory=action,
            output_schema=control,
            runtime_home=runtime_home,
            codex_executable=codex,
            authentication_file=auth,
        )
        _validate_mount_allowlist(
            mounts,
            forbidden_roots=forbidden_roots,
            stage4_run_root=None,
            workspace=workspace,
            action_directory=action,
            output_schema=control,
            runtime_home=runtime_home,
        )
        command = _bubblewrap_prefix(bwrap, mounts)
        command.extend(
            (
                "--",
                "/usr/bin/sh",
                "-c",
                (
                    "test -x /opt/ras/codex"
                    " && test -r /control/output-schema.json"
                    " && test -w /action"
                    " && : > /action/write-probe"
                    " && test ! -e \"$1\""
                    " && test ! -e \"$2\""
                ),
                "ras-stage4-probe",
                str(_canonical_forbidden_root(forbidden_roots, 0)),
                str(_canonical_forbidden_root(forbidden_roots, 1)),
            )
        )
        try:
            completed = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=15.0,
                close_fds=True,
                env={
                    "HOME": ISOLATED_HOME,
                    "CODEX_HOME": ISOLATED_HOME,
                    "TMPDIR": ISOLATED_TMPDIR,
                    "PATH": "/usr/bin:/bin",
                },
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise LiveShadowDependencyError(
                "Bubblewrap capability probe could not be executed"
            ) from exc
        if completed.returncode != 0 or not (action / "write-probe").is_file():
            raise LiveShadowDependencyError(
                "Bubblewrap synthetic-root capability probe failed"
            )

    return BubblewrapCapability(
        identity=BubblewrapBackendIdentity(
            schema_version=1,
            isolation_schema_version=1,
            backend="bubblewrap",
            canonical_bubblewrap_path=str(bwrap),
            bubblewrap_version=version,
            capability_result="passed",
        ),
        authentication_file=auth,
    )


def build_bubblewrap_process_launch(
    semantic_command: Sequence[str],
    prepared: PreparedCodexRequest,
    environment: Mapping[str, str],
    final_message_path: Path,
    output_schema: Path | None,
    *,
    capability: BubblewrapCapability,
    stage4_run_root: Path,
    runtime_home: Path,
    forbidden_roots: Sequence[Path],
) -> CodexProcessLaunch:
    """Wrap one exact semantic Codex command in a verified synthetic filesystem."""
    if output_schema is None:
        raise LiveShadowIntegrityError(
            "isolated Stage 4 supervisor requires an output schema"
        )
    workspace = _canonical_engine_path(
        prepared.workspace,
        kind="directory",
        label="quarantine workspace",
    )
    action_directory = _canonical_engine_path(
        final_message_path.parent,
        kind="directory",
        label="supervisor action-output directory",
    )
    schema = _canonical_engine_path(
        output_schema,
        kind="file",
        label="supervisor output schema",
    )
    home = _canonical_engine_path(
        runtime_home,
        kind="directory",
        label="Codex runtime home",
    )
    root = _canonical_engine_path(
        stage4_run_root,
        kind="directory",
        label="Stage 4 run root",
    )
    expected_workspace = root / "quarantine" / "workspace"
    expected_home = root / "quarantine" / "codex-home"
    if workspace != expected_workspace or home != expected_home:
        raise LiveShadowIntegrityError(
            "quarantine workspace or runtime home has an invalid layout"
        )
    if (
        schema.parent.parent.parent != root
        or schema.parent.parent.name != "decisions"
        or schema.name != "output-schema.json"
    ):
        raise LiveShadowIntegrityError(
            "isolated output schema is outside the exact current decision path"
        )
    validate_runtime_home_contents(home)

    codex = _canonical_engine_path(
        Path(semantic_command[0]),
        kind="file",
        label="Codex executable",
    )
    mounts = _build_mounts(
        workspace=workspace,
        action_directory=action_directory,
        output_schema=schema,
        runtime_home=home,
        codex_executable=codex,
        authentication_file=capability.authentication_file,
    )
    _validate_mount_allowlist(
        mounts,
        forbidden_roots=forbidden_roots,
        stage4_run_root=root,
        workspace=workspace,
        action_directory=action_directory,
        output_schema=schema,
        runtime_home=home,
    )
    rewritten = _rewrite_engine_owned_arguments(
        semantic_command,
        expected_workspace=workspace,
        expected_final_message=final_message_path,
        expected_output_schema=schema,
    )
    command = _bubblewrap_prefix(
        Path(capability.identity.canonical_bubblewrap_path),
        mounts,
    )
    command.extend(("--", *rewritten))
    isolated_environment = dict(environment)
    isolated_environment.update(
        {
            "HOME": ISOLATED_HOME,
            "CODEX_HOME": ISOLATED_HOME,
            "TMPDIR": ISOLATED_TMPDIR,
        }
    )
    return CodexProcessLaunch(
        command=tuple(command),
        cwd=Path("/"),
        environment=isolated_environment,
    )


def write_backend_identity(path: Path, identity: BubblewrapBackendIdentity) -> None:
    """Write only the nonsecret backend identity as stable ASCII JSON."""
    rendered = json.dumps(
        identity.to_dict(),
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    path.write_text(f"{rendered}\n", encoding="ascii")


def load_backend_identity(path: Path) -> BubblewrapBackendIdentity:
    try:
        value = json.loads(path.read_text(encoding="ascii"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LiveShadowIntegrityError(
            "isolation backend evidence is unreadable"
        ) from exc
    return BubblewrapBackendIdentity.from_dict(value)


def verify_recorded_bubblewrap_command(
    pending: PendingSupervisorAction,
    metadata: CodexMetadata,
    *,
    identity: BubblewrapBackendIdentity,
    runtime_home: Path,
    authentication_file: Path | None,
) -> None:
    """Prove that immutable Stage 1 metadata records the isolated command."""
    command = metadata.command
    try:
        separator = command.index("--")
    except ValueError as exc:
        raise LiveShadowIntegrityError(
            "supervisor command lacks the Bubblewrap command separator"
        ) from exc
    mount_arguments = command[:separator]
    action_sources = [
        Path(mount_arguments[index + 1])
        for index, item in enumerate(mount_arguments[:-2])
        if item == "--bind"
        and mount_arguments[index + 2] == ISOLATED_ACTION_DIRECTORY
    ]
    auth_sources = [
        Path(mount_arguments[index + 1])
        for index, item in enumerate(mount_arguments[:-2])
        if item == "--ro-bind"
        and mount_arguments[index + 2] == ISOLATED_AUTH_PATH
    ]
    if len(action_sources) != 1 or len(auth_sources) != 1:
        raise LiveShadowIntegrityError(
            "supervisor command changed an isolated action or authentication mount"
        )
    action_source = action_sources[0]
    auth_source = auth_sources[0]
    if (
        not action_source.is_absolute()
        or ".." in action_source.parts
        or action_source.parent != Path(tempfile.gettempdir())
        or not action_source.name.startswith("research-supervisor-codex-")
        or (
            authentication_file is not None
            and auth_source != authentication_file
        )
    ):
        raise LiveShadowIntegrityError(
            "supervisor command changed an engine-owned mount source"
        )
    mounts = _build_mounts(
        workspace=Path(pending.workspace),
        action_directory=action_source,
        output_schema=Path(pending.output_schema_path),
        runtime_home=runtime_home,
        codex_executable=Path(pending.codex_executable),
        authentication_file=auth_source,
    )
    expected = _bubblewrap_prefix(
        Path(identity.canonical_bubblewrap_path),
        mounts,
    )
    common = [
        "-c",
        f"model_reasoning_effort={pending.reasoning_effort}",
        "-c",
        'web_search="disabled"',
        "-c",
        "sandbox_workspace_write.network_access=false",
        "-c",
        "features.skill_mcp_dependency_install=false",
    ]
    if pending.resume_session_id is None:
        nested = [
            ISOLATED_CODEX_PATH,
            "--ask-for-approval",
            "never",
            "exec",
            "--json",
            "--output-last-message",
            "<FINAL_MESSAGE_TEMP>",
            "--model",
            pending.model,
            *common,
            "--sandbox",
            "read-only",
            "--ignore-user-config",
            "--ignore-rules",
            "--strict-config",
            "--cd",
            ISOLATED_WORKSPACE_PATH,
        ]
    else:
        nested = [
            ISOLATED_CODEX_PATH,
            "--ask-for-approval",
            "never",
            "--sandbox",
            "read-only",
            "--cd",
            ISOLATED_WORKSPACE_PATH,
            "exec",
            "resume",
            pending.resume_session_id,
            "--json",
            "--output-last-message",
            "<FINAL_MESSAGE_TEMP>",
            "--model",
            pending.model,
            *common,
            "--ignore-user-config",
            "--ignore-rules",
            "--strict-config",
        ]
    nested.extend(
        (
            "--output-schema",
            ISOLATED_OUTPUT_SCHEMA_PATH,
            "<PROMPT_FROM_STDIN>",
        )
    )
    expected.extend(("--", *nested))
    if tuple(expected) != command or "--unshare-net" in command:
        raise LiveShadowIntegrityError(
            "supervisor command does not preserve Bubblewrap isolation and policy"
        )


def _resolve_trusted_bubblewrap(configured: str | None) -> Path:
    candidate = Path(configured or DEFAULT_BUBBLEWRAP_EXECUTABLE)
    try:
        resolved = candidate.resolve(strict=True)
        status = resolved.lstat()
    except (OSError, RuntimeError, ValueError) as exc:
        raise LiveShadowDependencyError(
            "Bubblewrap executable is required for live shadow"
        ) from exc
    trusted = tuple(
        directory.resolve(strict=True) for directory in _TRUSTED_EXECUTABLE_DIRECTORIES
    )
    if (
        not stat.S_ISREG(status.st_mode)
        or not os.access(resolved, os.X_OK)
        or not any(resolved.parent == directory for directory in trusted)
    ):
        raise LiveShadowDependencyError(
            "Bubblewrap executable is not a trusted system executable"
        )
    return resolved


def _resolve_authentication_file(
    configured: Path | None,
    environ: Mapping[str, str] | None,
) -> Path:
    source = os.environ if environ is None else environ
    if configured is not None:
        candidate = configured
    elif source.get("CODEX_HOME"):
        candidate = Path(source["CODEX_HOME"]) / "auth.json"
    elif source.get("HOME"):
        candidate = Path(source["HOME"]) / ".codex" / "auth.json"
    else:
        raise LiveShadowDependencyError(
            "Codex subscription authentication is unavailable"
        )
    try:
        resolved = _canonical_engine_path(
            candidate,
            kind="file",
            label="Codex authentication file",
        )
    except LiveShadowIntegrityError as exc:
        raise LiveShadowDependencyError(
            "Codex subscription authentication is unavailable"
        ) from exc
    return resolved


def _run_dependency_probe(command: Sequence[str]) -> str:
    try:
        completed = subprocess.run(
            list(command),
            capture_output=True,
            check=False,
            encoding="utf-8",
            errors="replace",
            timeout=10.0,
            close_fds=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise LiveShadowDependencyError(
            "Bubblewrap dependency probe could not be executed"
        ) from exc
    if completed.returncode != 0:
        raise LiveShadowDependencyError("Bubblewrap dependency probe failed")
    return completed.stdout


def _build_mounts(
    *,
    workspace: Path,
    action_directory: Path,
    output_schema: Path,
    runtime_home: Path,
    codex_executable: Path,
    authentication_file: Path,
) -> tuple[_Mount, ...]:
    mounts: list[_Mount] = []
    for source_text, destination in (
        ("/usr", "/usr"),
        ("/bin", "/bin"),
        ("/sbin", "/sbin"),
        ("/lib", "/lib"),
        ("/lib64", "/lib64"),
    ):
        source = Path(source_text)
        if source.exists():
            mounts.append(
                _Mount(
                    "--ro-bind",
                    source.resolve(strict=True),
                    destination,
                    "system-runtime",
                )
            )
    for destination in (
        "/etc/resolv.conf",
        "/etc/hosts",
        "/etc/host.conf",
        "/etc/nsswitch.conf",
        "/etc/gai.conf",
        "/etc/passwd",
        "/etc/group",
        "/etc/ca-certificates.conf",
    ):
        source = Path(destination)
        if source.exists():
            mounts.append(
                _Mount(
                    "--ro-bind",
                    source.resolve(strict=True),
                    destination,
                    "system-configuration",
                )
            )
    certificate_directory = Path("/etc/ssl/certs")
    if certificate_directory.is_dir():
        mounts.append(
            _Mount(
                "--ro-bind",
                certificate_directory.resolve(strict=True),
                "/etc/ssl/certs",
                "tls-certificates",
            )
        )
    mounts.extend(
        (
            _Mount(
                "--ro-bind",
                codex_executable,
                ISOLATED_CODEX_PATH,
                "codex-executable",
            ),
            _Mount(
                "--ro-bind",
                workspace,
                ISOLATED_WORKSPACE_PATH,
                "quarantine-workspace",
            ),
            _Mount(
                "--bind",
                action_directory,
                ISOLATED_ACTION_DIRECTORY,
                "action-output",
            ),
            _Mount(
                "--ro-bind",
                output_schema,
                ISOLATED_OUTPUT_SCHEMA_PATH,
                "output-schema",
            ),
            _Mount(
                "--bind",
                runtime_home,
                ISOLATED_HOME,
                "codex-runtime-home",
            ),
            _Mount(
                "--ro-bind",
                authentication_file,
                ISOLATED_AUTH_PATH,
                "codex-authentication",
            ),
        )
    )
    return tuple(mounts)


def _bubblewrap_prefix(bwrap: Path, mounts: Sequence[_Mount]) -> list[str]:
    command = [
        str(bwrap),
        "--die-with-parent",
        "--new-session",
        "--unshare-user",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--unshare-cgroup-try",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
        "--dir",
        "/home",
        "--dir",
        "/opt",
        "--dir",
        "/opt/ras",
        "--dir",
        "/control",
        "--dir",
        "/etc",
        "--dir",
        "/etc/ssl",
    ]
    for mount in mounts:
        command.extend((mount.option, str(mount.source), mount.destination))
    command.extend(("--chdir", ISOLATED_WORKSPACE_PATH))
    return command


def _rewrite_engine_owned_arguments(
    command: Sequence[str],
    *,
    expected_workspace: Path,
    expected_final_message: Path,
    expected_output_schema: Path,
) -> tuple[str, ...]:
    rewritten = list(command)
    if not rewritten:
        raise LiveShadowIntegrityError("semantic Codex command is empty")
    rewritten[0] = ISOLATED_CODEX_PATH
    replacements = (
        ("--cd", str(expected_workspace), ISOLATED_WORKSPACE_PATH),
        (
            "--output-last-message",
            str(expected_final_message),
            f"{ISOLATED_ACTION_DIRECTORY}/{expected_final_message.name}",
        ),
        (
            "--output-schema",
            str(expected_output_schema),
            ISOLATED_OUTPUT_SCHEMA_PATH,
        ),
    )
    for option, expected, replacement in replacements:
        indexes = [index for index, item in enumerate(rewritten) if item == option]
        if len(indexes) != 1:
            raise LiveShadowIntegrityError(
                f"semantic Codex command has an invalid {option} argument"
            )
        value_index = indexes[0] + 1
        if value_index >= len(rewritten) or rewritten[value_index] != expected:
            raise LiveShadowIntegrityError(
                f"semantic Codex command changed its {option} path"
            )
        rewritten[value_index] = replacement
    return tuple(rewritten)


def _validate_mount_allowlist(
    mounts: Sequence[_Mount],
    *,
    forbidden_roots: Sequence[Path],
    stage4_run_root: Path | None,
    workspace: Path,
    action_directory: Path,
    output_schema: Path,
    runtime_home: Path,
) -> None:
    canonical_forbidden = tuple(
        _canonical_forbidden_root(forbidden_roots, index)
        for index in range(len(forbidden_roots))
    )
    destinations: set[str] = set()
    for mount in mounts:
        destination = PurePosixPath(mount.destination)
        if (
            not destination.is_absolute()
            or ".." in destination.parts
            or mount.destination in {"/", "/home", "/mnt"}
        ):
            raise LiveShadowIntegrityError(
                "Bubblewrap mount destination is outside the allowlist"
            )
        if mount.destination in destinations:
            raise LiveShadowIntegrityError(
                "Bubblewrap mount destinations contain a duplicate"
            )
        destinations.add(mount.destination)
        source = mount.source.resolve(strict=True)
        for forbidden in canonical_forbidden:
            if _is_relative_to(source, forbidden) or _is_relative_to(
                forbidden, source
            ):
                raise LiveShadowIntegrityError(
                    "Bubblewrap mount source overlaps a forbidden root"
                )
        if (
            stage4_run_root is not None
            and _is_relative_to(source, stage4_run_root)
            and source not in {workspace, output_schema, runtime_home}
        ):
            raise LiveShadowIntegrityError(
                "Bubblewrap mount exposes an unapproved Stage 4 artifact"
            )
    expected_writable = {
        (action_directory, ISOLATED_ACTION_DIRECTORY),
        (runtime_home, ISOLATED_HOME),
    }
    actual_writable = {
        (mount.source, mount.destination)
        for mount in mounts
        if mount.option == "--bind"
    }
    if actual_writable != expected_writable:
        raise LiveShadowIntegrityError(
            "Bubblewrap writable mount allowlist changed"
        )
    nested_destinations = [
        (left, right)
        for left in destinations
        for right in destinations
        if left != right and _is_posix_relative_to(right, left)
    ]
    if nested_destinations != [(ISOLATED_HOME, ISOLATED_AUTH_PATH)]:
        raise LiveShadowIntegrityError(
            "Bubblewrap mount destinations overlap unexpectedly"
        )


def _canonical_engine_path(path: Path, *, kind: str, label: str) -> Path:
    if ".." in path.parts:
        raise LiveShadowIntegrityError(f"{label} contains parent traversal")
    try:
        absolute = Path(os.path.abspath(path))
        resolved = path.resolve(strict=True)
        status = resolved.lstat()
    except (OSError, RuntimeError, ValueError) as exc:
        raise LiveShadowIntegrityError(f"{label} could not be resolved") from exc
    if absolute != resolved:
        raise LiveShadowIntegrityError(f"{label} contains a symlink")
    if kind == "file" and not stat.S_ISREG(status.st_mode):
        raise LiveShadowIntegrityError(f"{label} is not a regular file")
    if kind == "directory" and not stat.S_ISDIR(status.st_mode):
        raise LiveShadowIntegrityError(f"{label} is not a directory")
    return resolved


def _canonical_forbidden_root(roots: Sequence[Path], index: int) -> Path:
    if len(roots) < 2:
        raise LiveShadowIntegrityError(
            "authoritative forbidden-root set is incomplete"
        )
    path = roots[index]
    if ".." in path.parts:
        raise LiveShadowIntegrityError("forbidden root contains parent traversal")
    try:
        return path.resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise LiveShadowIntegrityError("forbidden root could not be resolved") from exc


def validate_runtime_home_contents(runtime_home: Path) -> None:
    try:
        for current, directories, files in os.walk(
            runtime_home,
            topdown=True,
            followlinks=False,
        ):
            current_path = Path(current)
            for name in (*directories, *files):
                path = current_path / name
                if path.is_symlink():
                    raise LiveShadowIntegrityError(
                        "Codex runtime home contains a symlink"
                    )
            if ".git" in directories or ".git" in files:
                raise LiveShadowIntegrityError(
                    "Codex runtime home contains repository material"
                )
    except OSError as exc:
        raise LiveShadowIntegrityError(
            "Codex runtime home could not be checked"
        ) from exc


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _is_posix_relative_to(path: str, root: str) -> bool:
    try:
        PurePosixPath(path).relative_to(PurePosixPath(root))
    except ValueError:
        return False
    return True
