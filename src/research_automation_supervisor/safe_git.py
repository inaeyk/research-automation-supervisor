"""Post-snapshot Git execution policy.

Repository intake lives in :mod:`gitless_repository` and never reaches this
module's process boundary.  These helpers execute Git only for a derived
campaign workspace carrying a core-issued snapshot binding.
"""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Literal

from research_automation_supervisor.core_authority_models import (
    RequestedRepositoryAuthorityV1,
)
from research_automation_supervisor.custodian_errors import (
    QualifiedCampaignInputError,
    QualifiedCampaignStateError,
)
from research_automation_supervisor.gitless_repository import (
    inspect_requested_repository as _inspect_requested_repository,
)
from research_automation_supervisor.gitless_repository import (
    verify_operator_campaign_workspace,
)

_GIT = "/usr/bin/git"


def inspect_requested_repository(
    source_kind: Literal["existing_folder", "git_url"],
    locator: str,
    *,
    sterile_root: Path,
    repository_bundle_descriptor: int | None = None,
    source_device: int | None = None,
    source_inode: int | None = None,
) -> RequestedRepositoryAuthorityV1:
    """Compatibility spelling for the non-executing object reader."""
    return _inspect_requested_repository(
        source_kind,
        locator,
        sterile_root=sterile_root,
        repository_descriptor=repository_bundle_descriptor,
        source_device=source_device,
        source_inode=source_inode,
    )


def safe_git_text(workspace: Path, *arguments: str) -> str:
    """Run Git only in a core-bound, post-snapshot campaign workspace."""
    repository = _bound_workspace(workspace)
    completed = _run_post_snapshot_git(repository, arguments, timeout=120)
    try:
        return completed.stdout.decode("utf-8", errors="strict").strip()
    except UnicodeDecodeError as exc:
        raise QualifiedCampaignInputError("Git returned invalid text") from exc


def run_repository_integrity_acceptance() -> None:
    """Run the generated integrity profile only after snapshot binding."""
    workspace = _bound_workspace(Path.cwd())
    _run_post_snapshot_git(
        workspace,
        ("diff", "--no-ext-diff", "--no-textconv", "--check"),
        timeout=120,
    )


def safe_git_archive_sha256(workspace: Path, destination_directory: Path) -> str:
    """Archive sanitized-workspace HEAD and return its exact byte digest."""
    repository = _bound_workspace(workspace)
    destination_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=destination_directory, prefix=".baseline-", delete=False
        ) as handle:
            temporary = Path(handle.name)
        _run_post_snapshot_git(
            repository,
            ("archive", "--format=tar", f"--output={temporary}", "HEAD"),
            timeout=120,
        )
        return hashlib.sha256(temporary.read_bytes()).hexdigest()
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _run_post_snapshot_git(
    workspace: Path, arguments: tuple[str, ...], *, timeout: int
) -> subprocess.CompletedProcess[bytes]:
    command = [
        _GIT,
        "--no-optional-locks",
        "-c",
        "safe.directory=*",
        "-C",
        str(workspace),
        *arguments,
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=workspace,
            check=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            close_fds=True,
            env={
                "PATH": "/usr/bin:/bin",
                "HOME": "/nonexistent",
                "XDG_CONFIG_HOME": "/nonexistent",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_SYSTEM": "/dev/null",
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_TERMINAL_PROMPT": "0",
                "LANG": "C.UTF-8",
            },
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise QualifiedCampaignStateError("post-snapshot Git could not run") from exc
    if completed.returncode != 0:
        raise QualifiedCampaignInputError("sanitized repository Git operation failed")
    return completed


def _bound_workspace(path: Path) -> Path:
    """Accept the workspace root or one of its real descendants."""
    try:
        absolute = Path(os.path.abspath(path))
        resolved = path.resolve(strict=True)
        status = resolved.lstat()
        if absolute != resolved or stat.S_ISLNK(status.st_mode):
            raise OSError("workspace resolves elsewhere")
        candidate = resolved if (resolved / ".git").is_dir() else _find_workspace_root(resolved)
        binding = candidate.parent / "snapshot-binding-v1.json"
        binding_status = binding.lstat()
        campaign_status = candidate.parent.lstat()
        if (
            stat.S_ISLNK(binding_status.st_mode)
            or not stat.S_ISREG(binding_status.st_mode)
            or binding_status.st_mode & 0o022
            or stat.S_ISLNK(campaign_status.st_mode)
            or not stat.S_ISDIR(campaign_status.st_mode)
            or stat.S_IMODE(campaign_status.st_mode) != 0o3770
        ):
            raise OSError("workspace binding is unsafe")
        verify_operator_campaign_workspace(candidate)
        return candidate
    except (OSError, RuntimeError, ValueError) as exc:
        raise QualifiedCampaignInputError(
            "Git is restricted to a sanitized campaign workspace"
        ) from exc


def _find_workspace_root(path: Path) -> Path:
    for candidate in (path, *path.parents):
        if (candidate / ".git").is_dir() and (
            candidate.parent / "snapshot-binding-v1.json"
        ).is_file():
            return candidate
    raise OSError("no sanitized workspace root")
