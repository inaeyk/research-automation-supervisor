"""OS-enforced Bubblewrap isolation for Stage 4 supervisor turns."""

from __future__ import annotations

import errno
import json
import os
import stat
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Literal, Protocol

from research_automation_supervisor.auth_confidentiality import (
    AuthenticationConfidentiality,
    load_authentication_confidentiality,
)
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
RECORDED_AUTH_SOURCE = "<AUTHENTICATION_FILE>"
MAX_RUNTIME_HOME_FILES = 4096
MAX_RUNTIME_HOME_DEPTH = 16
MAX_RUNTIME_HOME_BYTES = 64 * 1024 * 1024
MAX_RUNTIME_HOME_FILE_BYTES = 16 * 1024 * 1024
MAX_RUNTIME_HOME_SCAN_ATTEMPTS = 3

_RUNTIME_REPOSITORY_MARKERS = (
    b"gitdir: ",
    b"repositoryformatversion",
)

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
    authentication_confidentiality: AuthenticationConfidentiality = field(
        default_factory=lambda: AuthenticationConfidentiality(
            enabled=True,
            protected_logical_value_count=1,
            scan_completed=True,
            _fragments=(),
            _byte_fragments=(),
        ),
        repr=False,
        compare=False,
    )


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
    authentication_confidentiality = load_authentication_confidentiality(
        auth,
        forbidden_roots=forbidden_roots,
    )
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
        authentication_confidentiality=authentication_confidentiality,
    )


def resolve_authentication_confidentiality(
    *,
    authentication_file: Path | None,
    environ: Mapping[str, str] | None,
    forbidden_roots: Sequence[Path],
) -> AuthenticationConfidentiality:
    """Derive current auth protection without launching Bubblewrap or Codex."""
    auth = _resolve_authentication_file(authentication_file, environ)
    return load_authentication_confidentiality(
        auth,
        forbidden_roots=forbidden_roots,
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
    validate_runtime_home_contents(
        home,
        authentication_confidentiality=capability.authentication_confidentiality,
        forbidden_fragments=tuple(
            os.fsencode(root)
            for root in (
                *forbidden_roots,
                root,
                workspace,
            )
        ),
    )

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
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(f"{rendered}\n".encode("ascii"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except OSError as exc:
        raise LiveShadowIntegrityError(
            "isolation backend evidence could not be persisted"
        ) from exc
    finally:
        with suppress(FileNotFoundError):
            temporary.unlink()


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
            and auth_source != Path(RECORDED_AUTH_SOURCE)
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
        authentication_file=(
            authentication_file
            if authentication_file is not None
            else auth_source
        ),
    )
    expected = _bubblewrap_prefix(
        Path(identity.canonical_bubblewrap_path),
        mounts,
    )
    expected = [
        (
            RECORDED_AUTH_SOURCE
            if authentication_file is not None
            and item == str(authentication_file)
            else item
        )
        for item in expected
    ]
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


def validate_runtime_home_contents(
    runtime_home: Path,
    *,
    authentication_confidentiality: AuthenticationConfidentiality | None = None,
    forbidden_fragments: Sequence[bytes] = (),
) -> None:
    """Read-only validation of the complete bounded persistent runtime home."""
    findings = _scan_runtime_home(
        runtime_home,
        authentication_confidentiality=authentication_confidentiality,
        forbidden_fragments=forbidden_fragments,
        scrub=False,
    )
    if findings:
        raise LiveShadowIntegrityError(
            "Codex runtime home failed its clean-content invariant"
        )


def scrub_runtime_home_contamination(
    runtime_home: Path,
    *,
    authentication_confidentiality: AuthenticationConfidentiality,
    forbidden_fragments: Sequence[bytes] = (),
) -> tuple[str, ...]:
    """Remove contaminated engine-owned entries and return only safe categories."""
    return _scan_runtime_home(
        runtime_home,
        authentication_confidentiality=authentication_confidentiality,
        forbidden_fragments=forbidden_fragments,
        scrub=True,
    )


def _scan_runtime_home(
    runtime_home: Path,
    *,
    authentication_confidentiality: AuthenticationConfidentiality | None,
    forbidden_fragments: Sequence[bytes],
    scrub: bool,
) -> tuple[str, ...]:
    """Inspect an inactive runtime home through no-follow directory descriptors."""
    try:
        root_status = runtime_home.lstat()
    except OSError as exc:
        raise LiveShadowIntegrityError(
            "Codex runtime home could not be checked"
        ) from exc
    if (
        not stat.S_ISDIR(root_status.st_mode)
        or runtime_home.is_symlink()
        or os.path.ismount(runtime_home)
    ):
        raise LiveShadowIntegrityError(
            "Codex runtime home is not an exact engine-owned directory"
        )
    forbidden = tuple(
        sorted(
            {fragment for fragment in forbidden_fragments if len(fragment) >= 8},
            key=lambda item: (-len(item), item),
        )
    )
    findings: set[str] = set()
    for attempt in range(MAX_RUNTIME_HOME_SCAN_ATTEMPTS):
        try:
            _scan_runtime_home_once(
                runtime_home,
                authentication_confidentiality=authentication_confidentiality,
                forbidden_fragments=forbidden,
                scrub=scrub,
                findings=findings,
            )
            break
        except _RuntimeHomeDisappearanceError as exc:
            if attempt == MAX_RUNTIME_HOME_SCAN_ATTEMPTS - 1:
                raise LiveShadowIntegrityError(
                    "Codex runtime home did not remain stable while checked"
                ) from exc
        except _RuntimeHomeConsistencyError as exc:
            raise LiveShadowIntegrityError(
                "Codex runtime-home entry identity changed while checked"
            ) from exc
        except OSError as exc:
            if scrub:
                try:
                    _scrub_runtime_home(runtime_home)
                except OSError as scrub_exc:
                    raise LiveShadowIntegrityError(
                        "Codex runtime-home contamination could not be scrubbed"
                    ) from scrub_exc
            raise LiveShadowIntegrityError(
                "Codex runtime home could not be checked"
            ) from exc
    if scrub and findings:
        validate_runtime_home_contents(
            runtime_home,
            authentication_confidentiality=authentication_confidentiality,
            forbidden_fragments=forbidden_fragments,
        )
    return tuple(sorted(findings))


class _RuntimeHomeConsistencyError(OSError):
    """An untrusted entry changed identity during a bounded walk."""


class _RuntimeHomeDisappearanceError(_RuntimeHomeConsistencyError):
    """An untrusted entry disappeared and permits a bounded fresh attempt."""


@dataclass
class _RuntimeHomeWalk:
    entry_count: int = 0
    total_bytes: int = 0
    contaminated_parents: set[tuple[bytes, ...]] = field(default_factory=set)


def _scan_runtime_home_once(
    runtime_home: Path,
    *,
    authentication_confidentiality: AuthenticationConfidentiality | None,
    forbidden_fragments: Sequence[bytes],
    scrub: bool,
    findings: set[str],
) -> None:
    root_descriptor = _open_runtime_directory(runtime_home)
    try:
        root_identity = _directory_identity(os.fstat(root_descriptor))
        walk = _RuntimeHomeWalk()
        _walk_runtime_directory(
            root_descriptor,
            components=(),
            authentication_confidentiality=authentication_confidentiality,
            forbidden_fragments=forbidden_fragments,
            scrub=scrub,
            findings=findings,
            walk=walk,
        )
        if _directory_identity(os.fstat(root_descriptor)) != root_identity:
            raise _RuntimeHomeConsistencyError
        if scrub:
            _remove_empty_contaminated_parents(
                root_descriptor,
                walk.contaminated_parents,
            )
    finally:
        os.close(root_descriptor)


def _walk_runtime_directory(
    directory_descriptor: int,
    *,
    components: tuple[bytes, ...],
    authentication_confidentiality: AuthenticationConfidentiality | None,
    forbidden_fragments: Sequence[bytes],
    scrub: bool,
    findings: set[str],
    walk: _RuntimeHomeWalk,
) -> None:
    over_entry_bound = False
    try:
        with os.scandir(directory_descriptor) as iterator:
            names: list[bytes] = []
            remaining = MAX_RUNTIME_HOME_FILES - walk.entry_count
            for entry in iterator:
                if len(names) >= remaining:
                    over_entry_bound = True
                    break
                names.append(os.fsencode(entry.name))
            names.sort()
    except FileNotFoundError as exc:
        raise _RuntimeHomeDisappearanceError from exc
    if over_entry_bound:
        findings.add("runtime_home_bound_violation")
        if scrub:
            _scrub_runtime_descriptor(directory_descriptor)
            walk.contaminated_parents.update(
                components[:index]
                for index in range(1, len(components) + 1)
            )
        return
    for name in names:
        walk.entry_count += 1
        entry_components = (*components, name)
        relative_path = b"/".join(entry_components)
        try:
            status = os.stat(
                name,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError as exc:
            raise _RuntimeHomeDisappearanceError from exc

        auth_path_match = (
            authentication_confidentiality is not None
            and (
                authentication_confidentiality.contains_bytes(name)
                or authentication_confidentiality.contains_bytes(relative_path)
                or any(
                    authentication_confidentiality.contains_bytes(component)
                    for component in entry_components
                )
            )
        )
        forbidden_path_match = any(
            fragment in name or fragment in relative_path
            for fragment in forbidden_fragments
        )
        repository_path_match = name == b".git"
        if auth_path_match:
            findings.add("auth_confidentiality_violation")
        if forbidden_path_match:
            findings.add("runtime_home_forbidden_content")
        if repository_path_match:
            findings.add("runtime_home_repository_material")
        if auth_path_match or forbidden_path_match or repository_path_match:
            if scrub:
                _remove_runtime_entry_at(
                    directory_descriptor,
                    name,
                    status,
                )
                walk.contaminated_parents.update(
                    entry_components[:index]
                    for index in range(1, len(entry_components))
                )
            continue

        if stat.S_ISLNK(status.st_mode):
            findings.add("runtime_home_symlink")
            if scrub:
                _remove_runtime_entry_at(directory_descriptor, name, status)
            continue
        if stat.S_ISDIR(status.st_mode):
            if len(entry_components) > MAX_RUNTIME_HOME_DEPTH:
                findings.add("runtime_home_bound_violation")
                if scrub:
                    _remove_runtime_entry_at(directory_descriptor, name, status)
                    walk.contaminated_parents.update(
                        entry_components[:index]
                        for index in range(1, len(entry_components))
                    )
                continue
            child_descriptor = _open_runtime_directory_at(
                directory_descriptor,
                name,
                status,
            )
            try:
                _walk_runtime_directory(
                    child_descriptor,
                    components=entry_components,
                    authentication_confidentiality=authentication_confidentiality,
                    forbidden_fragments=forbidden_fragments,
                    scrub=scrub,
                    findings=findings,
                    walk=walk,
                )
            finally:
                os.close(child_descriptor)
            _verify_runtime_entry_identity(
                directory_descriptor,
                name,
                status,
                allow_missing=scrub,
            )
            continue
        if not stat.S_ISREG(status.st_mode):
            findings.add("runtime_home_nonregular_entry")
            if scrub:
                _remove_runtime_entry_at(directory_descriptor, name, status)
            continue
        if status.st_nlink != 1:
            findings.add("runtime_home_hard_link")
            if scrub:
                _remove_runtime_entry_at(directory_descriptor, name, status)
            continue
        walk.total_bytes += status.st_size
        if (
            status.st_size > MAX_RUNTIME_HOME_FILE_BYTES
            or walk.total_bytes > MAX_RUNTIME_HOME_BYTES
        ):
            findings.add("runtime_home_bound_violation")
            if scrub:
                _scrub_runtime_descriptor(directory_descriptor)
                walk.contaminated_parents.update(
                    components[:index]
                    for index in range(1, len(components) + 1)
                )
                return
            continue
        content = _read_runtime_regular_file_at(
            directory_descriptor,
            name,
            status,
        )
        auth_content_match = (
            authentication_confidentiality is not None
            and authentication_confidentiality.contains_bytes(content)
        )
        forbidden_content_match = any(
            fragment in content for fragment in forbidden_fragments
        )
        repository_content_match = any(
            marker in content for marker in _RUNTIME_REPOSITORY_MARKERS
        )
        if auth_content_match:
            findings.add("auth_confidentiality_violation")
        if forbidden_content_match:
            findings.add("runtime_home_forbidden_content")
        if repository_content_match:
            findings.add("runtime_home_repository_material")
        if scrub and (
            auth_content_match
            or forbidden_content_match
            or repository_content_match
        ):
            _remove_runtime_entry_at(directory_descriptor, name, status)
            walk.contaminated_parents.update(
                entry_components[:index]
                for index in range(1, len(entry_components))
            )


def _read_runtime_regular_file_at(
    directory_descriptor: int,
    name: bytes,
    expected: os.stat_result,
) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(name, flags, dir_fd=directory_descriptor)
    except FileNotFoundError as exc:
        raise _RuntimeHomeDisappearanceError from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or (before.st_dev, before.st_ino, before.st_size)
            != (expected.st_dev, expected.st_ino, expected.st_size)
        ):
            raise _RuntimeHomeConsistencyError
        content = bytearray()
        while len(content) <= MAX_RUNTIME_HOME_FILE_BYTES:
            chunk = os.read(
                descriptor,
                min(
                    64 * 1024,
                    MAX_RUNTIME_HOME_FILE_BYTES + 1 - len(content),
                ),
            )
            if not chunk:
                break
            content.extend(chunk)
        after = os.fstat(descriptor)
        if (
            len(content) > MAX_RUNTIME_HOME_FILE_BYTES
            or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        ):
            raise _RuntimeHomeConsistencyError
        return bytes(content)
    finally:
        os.close(descriptor)


def _open_runtime_directory(path: Path) -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return os.open(path, flags)


def _open_runtime_directory_at(
    parent_descriptor: int,
    name: bytes,
    expected: os.stat_result,
) -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(name, flags, dir_fd=parent_descriptor)
    except FileNotFoundError as exc:
        raise _RuntimeHomeDisappearanceError from exc
    if _directory_identity(os.fstat(descriptor)) != _directory_identity(expected):
        os.close(descriptor)
        raise _RuntimeHomeConsistencyError
    return descriptor


def _directory_identity(status: os.stat_result) -> tuple[int, int, int]:
    return (status.st_dev, status.st_ino, status.st_mode)


def _verify_runtime_entry_identity(
    directory_descriptor: int,
    name: bytes,
    expected: os.stat_result,
    *,
    allow_missing: bool,
) -> None:
    try:
        current = os.stat(
            name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        if allow_missing:
            return
        raise _RuntimeHomeDisappearanceError from None
    if (
        current.st_dev,
        current.st_ino,
        current.st_mode,
    ) != (
        expected.st_dev,
        expected.st_ino,
        expected.st_mode,
    ):
        raise _RuntimeHomeConsistencyError


def _remove_runtime_entry_at(
    directory_descriptor: int,
    name: bytes,
    expected: os.stat_result,
) -> None:
    if stat.S_ISDIR(expected.st_mode):
        child_descriptor = _open_runtime_directory_at(
            directory_descriptor,
            name,
            expected,
        )
        try:
            _scrub_runtime_descriptor(child_descriptor)
        finally:
            os.close(child_descriptor)
        _verify_runtime_entry_identity(
            directory_descriptor,
            name,
            expected,
            allow_missing=False,
        )
        os.rmdir(name, dir_fd=directory_descriptor)
    else:
        _verify_runtime_entry_identity(
            directory_descriptor,
            name,
            expected,
            allow_missing=False,
        )
        os.unlink(name, dir_fd=directory_descriptor)
    try:
        os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        _fsync_descriptor(directory_descriptor)
        return
    raise OSError(errno.EBUSY, "runtime entry removal was not durable")


def _scrub_runtime_descriptor(directory_descriptor: int) -> None:
    while True:
        os.lseek(directory_descriptor, 0, os.SEEK_SET)
        with os.scandir(directory_descriptor) as iterator:
            names = tuple(
                os.fsencode(entry.name)
                for _, entry in zip(range(128), iterator, strict=False)
            )
        if not names:
            break
        for name in names:
            try:
                status = os.stat(
                    name,
                    dir_fd=directory_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError as exc:
                raise _RuntimeHomeDisappearanceError from exc
            _remove_runtime_entry_at(directory_descriptor, name, status)
    _fsync_descriptor(directory_descriptor)


def _remove_empty_contaminated_parents(
    root_descriptor: int,
    parents: set[tuple[bytes, ...]],
) -> None:
    for components in sorted(parents, key=lambda item: (-len(item), item)):
        if not components:
            continue
        parent_components = components[:-1]
        name = components[-1]
        parent_descriptor = _open_runtime_components(
            root_descriptor,
            parent_components,
        )
        try:
            try:
                os.rmdir(name, dir_fd=parent_descriptor)
            except FileNotFoundError:
                continue
            except OSError as exc:
                if exc.errno in {errno.ENOTEMPTY, errno.EEXIST}:
                    continue
                raise
            _fsync_descriptor(parent_descriptor)
        finally:
            if parent_descriptor != root_descriptor:
                os.close(parent_descriptor)


def _open_runtime_components(
    root_descriptor: int,
    components: tuple[bytes, ...],
) -> int:
    if not components:
        return root_descriptor
    current = os.dup(root_descriptor)
    try:
        for component in components:
            status = os.stat(
                component,
                dir_fd=current,
                follow_symlinks=False,
            )
            child = _open_runtime_directory_at(current, component, status)
            os.close(current)
            current = child
        return current
    except BaseException:
        os.close(current)
        raise


def _scrub_runtime_home(runtime_home: Path) -> None:
    descriptor = _open_runtime_directory(runtime_home)
    try:
        _scrub_runtime_descriptor(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = _open_runtime_directory(path)
    primary: OSError | None = None
    try:
        _fsync_descriptor(descriptor)
    except OSError as exc:
        primary = exc
    try:
        os.close(descriptor)
    except OSError:
        if primary is None:
            raise
    if primary is not None:
        raise primary


def _fsync_descriptor(descriptor: int) -> None:
    os.fsync(descriptor)


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
