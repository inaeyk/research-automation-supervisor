"""Audited non-executing Git preparation before qualified campaign launch."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from research_automation_supervisor.custodian_errors import (
    QualifiedCampaignInputError,
    QualifiedCampaignStateError,
)
from research_automation_supervisor.custodian_models import (
    RepositoryAuthorityV1,
    render_qualified_acceptance_runner,
)
from research_automation_supervisor.durable_state import canonical_json, fsync_directory
from research_automation_supervisor.prelaunch_authority import (
    CampaignLaunchIntentV1,
    RequestedRepositoryAuthorityV1,
)

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
_GIT = "/usr/bin/git"
_BWRAP = "/usr/bin/bwrap"
_SAFE_CONFIG = (
    "core.hooksPath=/dev/null",
    "core.fsmonitor=false",
    "core.attributesFile=/dev/null",
    "credential.helper=",
    "diff.external=",
    "protocol.ext.allow=never",
    "protocol.file.allow=never",
    "protocol.ssh.allow=never",
    "protocol.git.allow=never",
)
_SAFE_ENVIRONMENT = (
    ("GIT_CONFIG_NOSYSTEM", "1"),
    ("GIT_CONFIG_SYSTEM", "/dev/null"),
    ("GIT_CONFIG_GLOBAL", "/dev/null"),
    ("GIT_TERMINAL_PROMPT", "0"),
    ("GIT_ASKPASS", "/bin/false"),
    ("SSH_ASKPASS", "/bin/false"),
    ("GIT_EXTERNAL_DIFF", ""),
    ("GIT_OPTIONAL_LOCKS", "0"),
    ("GIT_CONFIG_COUNT", "0"),
    ("PATH", "/usr/bin:/bin"),
    ("LANG", "C.UTF-8"),
)


class SafeGitCommandProofV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    phase: Literal[
        "identity", "clone_no_checkout", "isolated_checkout", "isolated_commit", "verify"
    ]
    isolation: Literal["pre_isolation_nonexecuting", "bubblewrap_unshare_all_v1"]
    argv: tuple[str, ...]
    environment: tuple[tuple[str, str], ...]


class RepositoryPreparationReceiptV1(BaseModel):
    """Exact proof of every Git process and its sterile environment."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    launch_intent_sha256: Sha256
    source_locator_sha256: Sha256
    source_commit: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    source_tree: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    prepared_commit: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    prepared_tree: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    prepared_workspace: str
    allowed_clone_protocol: Literal["existing_folder", "https"]
    checkout_outside_isolation: Literal[False] = False
    commands: tuple[SafeGitCommandProofV1, ...]
    receipt_sha256: Sha256

    @model_validator(mode="after")
    def validate_receipt(self) -> RepositoryPreparationReceiptV1:
        payload = self.model_dump(mode="json", exclude={"receipt_sha256"})
        if hashlib.sha256(canonical_json(payload)).hexdigest() != self.receipt_sha256:
            raise ValueError("repository preparation receipt self-hash is invalid")
        if any(
            command.phase in {"isolated_checkout", "isolated_commit"}
            and command.isolation != "bubblewrap_unshare_all_v1"
            for command in self.commands
        ):
            raise ValueError("repository materialization escaped isolation")
        return self


def inspect_requested_repository(
    source_kind: Literal["existing_folder", "git_url"],
    locator: str,
    *,
    sterile_root: Path,
) -> RequestedRepositoryAuthorityV1:
    """Inspect only repository identity; never checkout or run configured programs."""
    home = _sterile_home(sterile_root)
    locator = locator.strip()
    locator_hash = hashlib.sha256(locator.encode("utf-8")).hexdigest()
    if source_kind == "existing_folder":
        source = _canonical_existing_repository(Path(locator))
        display = source.name
        commit = _git_text(source, ("rev-parse", "--verify", "HEAD^{commit}"), home=home)
        tree = _git_text(source, ("rev-parse", "--verify", "HEAD^{tree}"), home=home)
        top = _git_text(source, ("rev-parse", "--show-toplevel"), home=home)
        if Path(top).resolve(strict=True) != source:
            raise QualifiedCampaignInputError("choose the repository top-level folder")
        requested_tree: str | None = tree
    else:
        display = _validate_https_url(locator)
        completed, _ = _run_git(
            ("-c", "protocol.https.allow=always", "ls-remote", "--exit-code", locator, "HEAD"),
            home=home,
            cwd=None,
            timeout=120,
        )
        lines = completed.stdout.decode("ascii", errors="strict").splitlines()
        commits = {line.split("\t", 1)[0] for line in lines if "\t" in line}
        if len(commits) != 1 or not re.fullmatch(r"[0-9a-f]{40}", next(iter(commits), "")):
            raise QualifiedCampaignInputError("HTTPS repository HEAD could not be identified")
        commit = next(iter(commits))
        requested_tree = None
    repository_name = Path(display).name or "repository"
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "-", repository_name).strip("-") or "repository"
    return RequestedRepositoryAuthorityV1(
        source_kind=source_kind,
        source_display=display,
        source_locator=str(source) if source_kind == "existing_folder" else locator,
        source_locator_sha256=locator_hash,
        requested_commit=commit,
        requested_tree=requested_tree,
        repository_id=f"{cleaned[:60]}-{locator_hash[:12]}"[:80],
    )


def prepare_repository(
    intent: CampaignLaunchIntentV1,
    *,
    preparation_root: Path,
) -> tuple[RepositoryAuthorityV1, RepositoryPreparationReceiptV1]:
    """Clone without checkout, then materialize only inside Bubblewrap."""
    root = _safe_root(preparation_root)
    receipt_path = root / "receipts" / f"{intent.intent_sha256}.json"
    if receipt_path.exists():
        receipt = _load_receipt(receipt_path)
        return _authority_from_receipt(intent, receipt), receipt
    workspace_root = root / "workspaces" / intent.campaign_public_id
    workspace_root.mkdir(parents=True, exist_ok=False, mode=0o700)
    workspace = workspace_root / "repository"
    home = _sterile_home(root / "sterile")
    requested = intent.repository
    commands: list[SafeGitCommandProofV1] = []
    if requested.source_kind == "existing_folder":
        source = _canonical_existing_repository(Path(requested.source_locator))
        _verify_requested_identity(source, requested, home=home, commands=commands)
        protocol: Literal["existing_folder", "https"] = "existing_folder"
        clone_command = _isolated_local_clone_command(source, workspace_root)
        _run_isolated(clone_command, cwd=root, timeout=600)
        commands.append(_isolated_proof("clone_no_checkout", clone_command))
    else:
        _validate_https_url(requested.source_locator)
        protocol = "https"
        clone_args = (
            "-c",
            "protocol.https.allow=always",
            "clone",
            "--no-checkout",
            "--no-tags",
            "--no-recurse-submodules",
            "--",
            requested.source_locator,
            str(workspace),
        )
        _, clone_proof = _run_git(clone_args, home=home, cwd=root, timeout=600)
        commands.append(
            clone_proof.model_copy(
                update={
                    "phase": "clone_no_checkout",
                    "isolation": "pre_isolation_nonexecuting",
                }
            )
        )
    source_commit = _git_text(workspace, ("rev-parse", "--verify", "HEAD^{commit}"), home=home)
    source_tree = _git_text(workspace, ("rev-parse", "--verify", "HEAD^{tree}"), home=home)
    commands.extend(
        _proofs_for_text_commands(
            workspace,
            home,
            (("rev-parse", "--verify", "HEAD^{commit}"), ("rev-parse", "--verify", "HEAD^{tree}")),
        )
    )
    if source_commit != requested.requested_commit:
        raise QualifiedCampaignInputError("repository changed after preview; review it again")
    if requested.requested_tree is not None and source_tree != requested.requested_tree:
        raise QualifiedCampaignInputError("repository tree changed after preview; review it again")
    checkout = _isolated_git_command(
        workspace,
        ("checkout", "--detach", "--force", source_commit),
        home=home,
    )
    _run_isolated(checkout, cwd=root, timeout=180)
    commands.append(_isolated_proof("isolated_checkout", checkout))
    support = workspace / ".research-supervisor"
    support.mkdir(mode=0o700)
    acceptance = support / "acceptance.py"
    acceptance.write_bytes(render_qualified_acceptance_runner(sys.executable))
    commit_script = (
        "set -eu; "
        "git " + " ".join(_quoted_git_config()) + " add -- .research-supervisor/acceptance.py; "
        "git "
        + " ".join(_quoted_git_config())
        + " -c user.name='Research Supervisor Core' -c user.email='core@localhost.invalid' "
        "commit -q -m 'chore: prepare qualified campaign acceptance'"
    )
    commit_command = _bubblewrap_prefix(workspace, home) + ["--", "/bin/sh", "-c", commit_script]
    _run_isolated(commit_command, cwd=root, timeout=180)
    commands.append(_isolated_proof("isolated_commit", commit_command))
    expected_acceptance = render_qualified_acceptance_runner(sys.executable)
    if acceptance.read_bytes() != expected_acceptance:
        raise QualifiedCampaignStateError(
            "repository attributes changed the qualified acceptance runner"
        )
    prepared_commit = _git_text(workspace, ("rev-parse", "--verify", "HEAD^{commit}"), home=home)
    prepared_tree = _git_text(workspace, ("rev-parse", "--verify", "HEAD^{tree}"), home=home)
    status = _git_text(workspace, ("status", "--porcelain", "--untracked-files=normal"), home=home)
    if status:
        raise QualifiedCampaignStateError("prepared repository is not clean")
    commands.extend(
        _proofs_for_text_commands(
            workspace,
            home,
            (
                ("rev-parse", "--verify", "HEAD^{commit}"),
                ("rev-parse", "--verify", "HEAD^{tree}"),
                ("status", "--porcelain", "--untracked-files=normal"),
            ),
        )
    )
    payload = {
        "schema_version": 1,
        "launch_intent_sha256": intent.intent_sha256,
        "source_locator_sha256": requested.source_locator_sha256,
        "source_commit": source_commit,
        "source_tree": source_tree,
        "prepared_commit": prepared_commit,
        "prepared_tree": prepared_tree,
        "prepared_workspace": str(workspace),
        "allowed_clone_protocol": protocol,
        "checkout_outside_isolation": False,
        "commands": tuple(item.model_dump(mode="json") for item in commands),
    }
    receipt = RepositoryPreparationReceiptV1.model_validate(
        {
            **payload,
            "commands": tuple(commands),
            "receipt_sha256": hashlib.sha256(canonical_json(payload)).hexdigest(),
        }
    )
    _write_once(receipt_path, canonical_json(receipt.model_dump(mode="json")))
    return _authority_from_receipt(intent, receipt), receipt


def _authority_from_receipt(
    intent: CampaignLaunchIntentV1, receipt: RepositoryPreparationReceiptV1
) -> RepositoryAuthorityV1:
    if (
        receipt.launch_intent_sha256 != intent.intent_sha256
        or receipt.source_locator_sha256 != intent.repository.source_locator_sha256
    ):
        raise QualifiedCampaignInputError("repository preparation belongs to another launch intent")
    workspace = Path(receipt.prepared_workspace)
    if not workspace.is_dir() or workspace.is_symlink():
        raise QualifiedCampaignInputError("prepared repository is missing or unsafe")
    return RepositoryAuthorityV1(
        source_kind=intent.repository.source_kind,
        source_display=intent.repository.source_display,
        source_locator_sha256=intent.repository.source_locator_sha256,
        prepared_workspace=receipt.prepared_workspace,
        baseline_commit=receipt.prepared_commit,
        baseline_tree=receipt.prepared_tree,
        repository_id=intent.repository.repository_id,
    )


def _verify_requested_identity(
    source: Path,
    requested: RequestedRepositoryAuthorityV1,
    *,
    home: Path,
    commands: list[SafeGitCommandProofV1],
) -> None:
    commit_args = ("rev-parse", "--verify", "HEAD^{commit}")
    tree_args = ("rev-parse", "--verify", "HEAD^{tree}")
    commit = _git_text(source, commit_args, home=home)
    tree = _git_text(source, tree_args, home=home)
    commands.extend(_proofs_for_text_commands(source, home, (commit_args, tree_args)))
    if commit != requested.requested_commit or tree != requested.requested_tree:
        raise QualifiedCampaignInputError("repository changed after preview; review it again")


def _proofs_for_text_commands(
    repository: Path, home: Path, arguments: tuple[tuple[str, ...], ...]
) -> list[SafeGitCommandProofV1]:
    return [
        SafeGitCommandProofV1(
            phase="verify" if index else "identity",
            isolation="pre_isolation_nonexecuting",
            argv=tuple(_git_argv((*_repository_prefix(repository), *args))),
            environment=tuple(sorted(_git_environment(home).items())),
        )
        for index, args in enumerate(arguments)
    ]


def _repository_prefix(repository: Path) -> tuple[str, str]:
    return "-C", str(repository)


def _git_text(repository: Path, arguments: tuple[str, ...], *, home: Path) -> str:
    completed, _ = _run_git(
        (*_repository_prefix(repository), *arguments), home=home, cwd=repository, timeout=120
    )
    try:
        return completed.stdout.decode("utf-8", errors="strict").strip()
    except UnicodeDecodeError as exc:
        raise QualifiedCampaignInputError("Git returned invalid repository identity") from exc


def _run_git(
    arguments: tuple[str, ...], *, home: Path, cwd: Path | None, timeout: int
) -> tuple[subprocess.CompletedProcess[bytes], SafeGitCommandProofV1]:
    argv = _git_argv(arguments)
    environment = _git_environment(home)
    try:
        completed = subprocess.run(
            argv,
            cwd=cwd,
            check=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            close_fds=True,
            env=environment,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise QualifiedCampaignStateError("sterile Git preparation could not run") from exc
    if completed.returncode != 0:
        raise QualifiedCampaignInputError("repository could not be inspected or prepared")
    proof = SafeGitCommandProofV1(
        phase="identity",
        isolation="pre_isolation_nonexecuting",
        argv=tuple(argv),
        environment=tuple(sorted(environment.items())),
    )
    return completed, proof


def _git_argv(arguments: tuple[str, ...]) -> list[str]:
    argv = [_GIT, "--no-optional-locks"]
    for value in _SAFE_CONFIG:
        argv.extend(("-c", value))
    argv.extend(arguments)
    return argv


def _git_environment(home: Path) -> dict[str, str]:
    return {**dict(_SAFE_ENVIRONMENT), "HOME": str(home), "XDG_CONFIG_HOME": str(home / "config")}


def _isolated_environment() -> dict[str, str]:
    return {
        **dict(_SAFE_ENVIRONMENT),
        "HOME": "/home/repository-preparation",
        "XDG_CONFIG_HOME": "/home/repository-preparation/config",
    }


def _isolated_git_command(workspace: Path, arguments: tuple[str, ...], *, home: Path) -> list[str]:
    return _bubblewrap_prefix(workspace, home) + [
        "--",
        _GIT,
        "--no-optional-locks",
        *[item for value in _SAFE_CONFIG for item in ("-c", value)],
        *arguments,
    ]


def _bubblewrap_prefix(workspace: Path, home: Path) -> list[str]:
    del home
    command = _bubblewrap_system_prefix()
    command.extend(
        (
            "--bind",
            str(workspace),
            "/workspace",
            "--chdir",
            "/workspace",
        )
    )
    for key, value in _isolated_environment().items():
        command.extend(("--setenv", key, value))
    return command


def _bubblewrap_system_prefix() -> list[str]:
    command = [
        _BWRAP,
        "--die-with-parent",
        "--new-session",
        "--unshare-all",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
        "--dir",
        "/home",
        "--dir",
        "/home/repository-preparation",
        "--ro-bind",
        "/usr",
        "/usr",
        "--ro-bind",
        "/bin",
        "/bin",
    ]
    for path in ("/lib", "/lib64", "/etc"):
        if Path(path).exists():
            command.extend(("--ro-bind", path, path))
    return command


def _quoted_git_config() -> tuple[str, ...]:
    values: list[str] = ["--no-optional-locks"]
    for value in _SAFE_CONFIG:
        values.extend(("-c", f"'{value}'"))
    return tuple(values)


def _run_isolated(argv: list[str], *, cwd: Path, timeout: int) -> None:
    if not Path(_BWRAP).is_file():
        raise QualifiedCampaignStateError("Bubblewrap is required before repository checkout")
    try:
        completed = subprocess.run(
            argv,
            cwd=cwd,
            check=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            close_fds=True,
            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise QualifiedCampaignStateError(
            "isolated repository materialization could not run"
        ) from exc
    if completed.returncode != 0:
        raise QualifiedCampaignStateError("isolated repository materialization failed")


def _isolated_proof(
    phase: Literal["clone_no_checkout", "isolated_checkout", "isolated_commit"],
    argv: list[str],
) -> SafeGitCommandProofV1:
    return SafeGitCommandProofV1(
        phase=phase,
        isolation="bubblewrap_unshare_all_v1",
        argv=tuple(argv),
        environment=tuple(sorted(_isolated_environment().items())),
    )


def _isolated_local_clone_command(source: Path, destination_root: Path) -> list[str]:
    command = _bubblewrap_system_prefix()
    command.extend(
        (
            "--ro-bind",
            str(source),
            "/source",
            "--bind",
            str(destination_root),
            "/destination",
            "--chdir",
            "/destination",
        )
    )
    for key, value in _isolated_environment().items():
        command.extend(("--setenv", key, value))
    command.extend(
        (
            "--",
            _GIT,
            "--no-optional-locks",
            *[item for value in _SAFE_CONFIG for item in ("-c", value)],
            "-c",
            "protocol.file.allow=always",
            "clone",
            "--no-checkout",
            "--no-tags",
            "--no-recurse-submodules",
            "--no-local",
            "--",
            "/source",
            "repository",
        )
    )
    return command


def _canonical_existing_repository(path: Path) -> Path:
    try:
        absolute = Path(os.path.abspath(path))
        resolved = path.resolve(strict=True)
        status = resolved.lstat()
    except (OSError, RuntimeError, ValueError) as exc:
        raise QualifiedCampaignInputError("repository folder is unavailable") from exc
    if absolute != resolved or stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
        raise QualifiedCampaignInputError("repository folder is unsafe")
    return resolved


def _validate_https_url(locator: str) -> str:
    parsed = urlsplit(locator)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
        or bool(parsed.query)
        or bool(parsed.fragment)
        or ".." in Path(parsed.path).parts
        or any(character.isspace() for character in locator)
    ):
        raise QualifiedCampaignInputError("only credential-free HTTPS Git URLs are supported")
    return f"{parsed.hostname}{parsed.path.removesuffix('.git')}"


def _sterile_home(root: Path) -> Path:
    home = root / "home"
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    (home / "config").mkdir(exist_ok=True, mode=0o700)
    return home.resolve(strict=True)


def _safe_root(path: Path) -> Path:
    try:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        resolved = path.resolve(strict=True)
        if resolved != Path(os.path.abspath(path)) or stat.S_ISLNK(resolved.lstat().st_mode):
            raise OSError
        for name in ("workspaces", "receipts", "sterile"):
            child = resolved / name
            child.mkdir(exist_ok=True, mode=0o700)
            status = child.lstat()
            if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
                raise OSError
        return resolved
    except (OSError, RuntimeError, ValueError) as exc:
        raise QualifiedCampaignStateError("repository preparation storage is unavailable") from exc


def _write_once(path: Path, content: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o400)
        try:
            os.write(descriptor, content)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        fsync_directory(path.parent)
    except OSError as exc:
        raise QualifiedCampaignStateError(
            "repository preparation receipt could not be committed"
        ) from exc


def _load_receipt(path: Path) -> RepositoryPreparationReceiptV1:
    try:
        status = path.lstat()
        if (
            stat.S_ISLNK(status.st_mode)
            or not stat.S_ISREG(status.st_mode)
            or status.st_size > 8 * 1024 * 1024
        ):
            raise OSError
        return RepositoryPreparationReceiptV1.model_validate_json(path.read_bytes())
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, ValidationError, ValueError) as exc:
        raise QualifiedCampaignInputError("repository preparation receipt is invalid") from exc
