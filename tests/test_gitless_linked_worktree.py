from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest
from dulwich.objects import Commit
from dulwich.repo import Repo

import research_automation_supervisor.gitless_repository as gitless
from research_automation_supervisor.core_authority_models import (
    RequestedRepositoryAuthorityV1,
)
from research_automation_supervisor.custodian_errors import QualifiedCampaignInputError
from research_automation_supervisor.gitless_repository import (
    build_sanitized_snapshot,
    create_local_repository_transfer,
    freeze_repository_import,
    inspect_requested_repository,
    open_repository_directory,
    plan_sanitized_snapshot,
)
from tests.custodian_helpers import create_repository, git


@dataclass(frozen=True)
class LinkedLayout:
    primary: Path
    worktree: Path
    admin: Path
    common: Path


def _linked_layout(
    tmp_path: Path, *, name: str = "linked", relative_paths: bool = False
) -> LinkedLayout:
    primary_root = tmp_path / "primary-root"
    primary_root.mkdir()
    primary = create_repository(primary_root)
    worktree = tmp_path / name
    arguments = ["worktree", "add", "-q"]
    if relative_paths:
        arguments.append("--relative-paths")
    arguments.extend(("-b", f"branch-{name}", str(worktree), "HEAD"))
    git(primary, *arguments)
    marker = worktree / ".git"
    directive = marker.read_text(encoding="utf-8")
    assert directive.startswith("gitdir: ")
    admin = Path(directive.removeprefix("gitdir: ").strip())
    if not admin.is_absolute():
        admin = worktree / admin
    admin = admin.resolve(strict=True)
    common_pointer = (admin / "commondir").read_text(encoding="utf-8").strip()
    common = Path(common_pointer)
    if not common.is_absolute():
        common = admin / common
    return LinkedLayout(primary, worktree, admin, common.resolve(strict=True))


def _inspect(repository: Path, scratch: Path) -> RequestedRepositoryAuthorityV1:
    return inspect_requested_repository(
        "existing_folder", str(repository), sterile_root=scratch
    )


def _inspect_through_transfer(
    repository: Path, scratch: Path
) -> RequestedRepositoryAuthorityV1:
    locator, repository_fd = open_repository_directory(str(repository))
    transfer_fd = -1
    try:
        identity = os.fstat(repository_fd)
        transfer_fd = create_local_repository_transfer(repository_fd)
        return inspect_requested_repository(
            "existing_folder",
            locator,
            sterile_root=scratch,
            repository_transfer_descriptor=transfer_fd,
            source_device=identity.st_dev,
            source_inode=identity.st_ino,
        )
    finally:
        if transfer_fd >= 0:
            os.close(transfer_fd)
        os.close(repository_fd)


def test_standalone_git_directory_intake_is_unchanged(tmp_path: Path) -> None:
    repository = create_repository(tmp_path)
    requested = _inspect(repository, tmp_path / "scratch")
    assert requested.requested_commit == git(repository, "rev-parse", "HEAD")
    assert requested.requested_tree == git(repository, "rev-parse", "HEAD^{tree}")


def test_valid_absolute_linked_worktree_uses_production_transfer_path(
    tmp_path: Path,
) -> None:
    layout = _linked_layout(tmp_path)
    pointer = (layout.worktree / ".git").read_text(encoding="utf-8").strip()
    assert Path(pointer.removeprefix("gitdir: ")).is_absolute()
    requested = _inspect_through_transfer(layout.worktree, tmp_path / "scratch")
    assert requested.requested_commit == git(layout.worktree, "rev-parse", "HEAD")
    assert requested.requested_tree == git(layout.worktree, "rev-parse", "HEAD^{tree}")


def test_valid_relative_linked_worktree_matches_standalone_snapshot_authority(
    tmp_path: Path,
) -> None:
    layout = _linked_layout(tmp_path, relative_paths=True)
    assert not Path(
        (layout.worktree / ".git")
        .read_text(encoding="utf-8")
        .removeprefix("gitdir: ")
        .strip()
    ).is_absolute()
    assert git(layout.worktree, "rev-parse", "HEAD") == git(
        layout.primary, "rev-parse", "HEAD"
    )
    primary_request = _inspect(layout.primary, tmp_path / "primary-preview")
    linked_request = _inspect(layout.worktree, tmp_path / "linked-preview")
    assert (
        linked_request.requested_commit,
        linked_request.requested_tree,
    ) == (
        primary_request.requested_commit,
        primary_request.requested_tree,
    )
    primary_import = freeze_repository_import(
        primary_request, import_root=tmp_path / "primary-import"
    )
    linked_import = freeze_repository_import(
        linked_request, import_root=tmp_path / "linked-import"
    )
    primary_plan = plan_sanitized_snapshot(
        primary_import, python_executable=sys.executable
    )
    linked_plan = plan_sanitized_snapshot(linked_import, python_executable=sys.executable)
    assert (
        linked_plan.prepared_commit,
        linked_plan.prepared_tree,
    ) == (
        primary_plan.prepared_commit,
        primary_plan.prepared_tree,
    )
    primary_snapshot = build_sanitized_snapshot(
        primary_import,
        primary_plan,
        snapshot_root=tmp_path / "primary-snapshots",
        python_executable=sys.executable,
    )
    linked_snapshot = build_sanitized_snapshot(
        linked_import,
        linked_plan,
        snapshot_root=tmp_path / "linked-snapshots",
        python_executable=sys.executable,
    )
    primary_repo = Repo(str(primary_snapshot / "repository"))
    linked_repo = Repo(str(linked_snapshot / "repository"))
    assert linked_repo.head() == primary_repo.head()
    linked_commit = cast(Commit, linked_repo[linked_repo.head()])
    primary_commit = cast(Commit, primary_repo[primary_repo.head()])
    assert linked_commit.tree == primary_commit.tree


def test_current_checkout_linked_worktree_is_accepted_when_applicable(
    tmp_path: Path,
) -> None:
    repository = Path(__file__).resolve().parents[1]
    if not (repository / ".git").is_file():
        pytest.skip("qualification checkout is not a linked worktree")
    requested = _inspect_through_transfer(repository, tmp_path / "scratch")
    assert requested.requested_commit == git(repository, "rev-parse", "HEAD")
    assert requested.requested_tree == git(repository, "rev-parse", "HEAD^{tree}")


def test_linked_worktree_git_marker_symlink_is_rejected(tmp_path: Path) -> None:
    layout = _linked_layout(tmp_path)
    marker = layout.worktree / ".git"
    marker.rename(layout.worktree / ".git-file")
    marker.symlink_to(layout.worktree / ".git-file")
    with pytest.raises(QualifiedCampaignInputError):
        _inspect(layout.worktree, tmp_path / "scratch")


@pytest.mark.parametrize(
    "content",
    (
        b"",
        b"gitdir:\n",
        b"gitdir: \n",
        b"Gitdir: admin\n",
        b"gitdir: admin\ngitdir: other\n",
        b"gitdir: admin\x00ignored\n",
        b"gitdir: admin\nmalicious trailing content\n",
        b"gitdir: admin \n",
    ),
)
def test_malformed_linked_worktree_gitdir_files_are_rejected(
    tmp_path: Path, content: bytes
) -> None:
    layout = _linked_layout(tmp_path)
    (layout.worktree / ".git").write_bytes(content)
    with pytest.raises(QualifiedCampaignInputError):
        _inspect(layout.worktree, tmp_path / "scratch")


def test_linked_worktree_gitdir_target_must_be_real_directory(tmp_path: Path) -> None:
    layout = _linked_layout(tmp_path)
    not_directory = tmp_path / "not-a-directory"
    not_directory.write_text("metadata", encoding="utf-8")
    (layout.worktree / ".git").write_text(
        f"gitdir: {not_directory}\n", encoding="utf-8"
    )
    with pytest.raises(QualifiedCampaignInputError):
        _inspect(layout.worktree, tmp_path / "scratch")


def test_linked_worktree_gitdir_target_symlink_is_rejected(tmp_path: Path) -> None:
    layout = _linked_layout(tmp_path)
    link = tmp_path / "admin-link"
    link.symlink_to(layout.admin, target_is_directory=True)
    (layout.worktree / ".git").write_text(f"gitdir: {link}\n", encoding="utf-8")
    with pytest.raises(QualifiedCampaignInputError):
        _inspect(layout.worktree, tmp_path / "scratch")


def test_linked_worktree_admin_back_reference_must_match_selected_git_file(
    tmp_path: Path,
) -> None:
    layout = _linked_layout(tmp_path)
    unrelated = tmp_path / "unrelated-git-file"
    unrelated.write_text("gitdir: nowhere\n", encoding="utf-8")
    (layout.admin / "gitdir").write_text(f"{unrelated}\n", encoding="utf-8")
    with pytest.raises(QualifiedCampaignInputError):
        _inspect(layout.worktree, tmp_path / "scratch")


@pytest.mark.parametrize("name", ("gitdir", "commondir", "HEAD"))
def test_linked_worktree_admin_control_files_must_be_regular(
    tmp_path: Path, name: str
) -> None:
    layout = _linked_layout(tmp_path)
    control = layout.admin / name
    real = layout.admin / f"{name}-real"
    control.rename(real)
    control.symlink_to(real)
    with pytest.raises(QualifiedCampaignInputError):
        _inspect(layout.worktree, tmp_path / "scratch")


@pytest.mark.parametrize("mode", ("missing", "empty", "file"))
def test_linked_worktree_commondir_must_be_valid(
    tmp_path: Path, mode: str
) -> None:
    layout = _linked_layout(tmp_path)
    commondir = layout.admin / "commondir"
    if mode == "missing":
        commondir.unlink()
    elif mode == "empty":
        commondir.write_bytes(b"")
    else:
        target = tmp_path / "not-common"
        target.write_text("not a directory", encoding="utf-8")
        commondir.write_text(f"{target}\n", encoding="utf-8")
    with pytest.raises(QualifiedCampaignInputError):
        _inspect(layout.worktree, tmp_path / "scratch")


def test_linked_worktree_commondir_topology_mismatch_is_rejected(
    tmp_path: Path,
) -> None:
    layout = _linked_layout(tmp_path)
    unrelated_common = tmp_path / "unrelated-common"
    (unrelated_common / "worktrees").mkdir(parents=True)
    (layout.admin / "commondir").write_text(
        f"{unrelated_common}\n", encoding="utf-8"
    )
    with pytest.raises(QualifiedCampaignInputError):
        _inspect(layout.worktree, tmp_path / "scratch")


def test_pointer_to_another_worktree_admin_is_rejected(tmp_path: Path) -> None:
    layout = _linked_layout(tmp_path, name="first")
    second = tmp_path / "second"
    git(
        layout.primary,
        "worktree",
        "add",
        "-q",
        "-b",
        "branch-second",
        str(second),
        "HEAD",
    )
    second_admin = Path(
        (second / ".git").read_text(encoding="utf-8").removeprefix("gitdir: ").strip()
    )
    (layout.worktree / ".git").write_text(
        f"gitdir: {second_admin}\n", encoding="utf-8"
    )
    with pytest.raises(QualifiedCampaignInputError):
        _inspect(layout.worktree, tmp_path / "scratch")


def test_linked_worktree_symlinked_common_objects_are_rejected(tmp_path: Path) -> None:
    layout = _linked_layout(tmp_path)
    objects = layout.common / "objects"
    moved = tmp_path / "moved-objects"
    objects.rename(moved)
    objects.symlink_to(moved, target_is_directory=True)
    with pytest.raises(QualifiedCampaignInputError):
        _inspect(layout.worktree, tmp_path / "scratch")


def test_linked_worktree_hostile_config_alternates_hooks_and_attributes_are_inert(
    tmp_path: Path,
) -> None:
    primary_root = tmp_path / "primary-root"
    primary_root.mkdir()
    primary = create_repository(primary_root)
    (primary / ".gitattributes").write_text(
        "* filter=hostile diff=hostile\n", encoding="utf-8"
    )
    git(primary, "add", ".gitattributes")
    git(primary, "commit", "-q", "-m", "hostile attributes")
    worktree = tmp_path / "linked"
    git(primary, "worktree", "add", "-q", "-b", "branch-linked", str(worktree), "HEAD")
    admin = Path(
        (worktree / ".git").read_text(encoding="utf-8").removeprefix("gitdir: ").strip()
    )
    common = (admin / (admin / "commondir").read_text(encoding="utf-8").strip()).resolve()
    marker = tmp_path / "HOST_AUTHORITY_READ_OR_EXECUTED"
    hostile = tmp_path / "hostile-config"
    hostile.write_text(f"[alias]\n x = !touch {marker}\n", encoding="utf-8")
    (common / "config").unlink()
    (common / "config").symlink_to(hostile)
    hooks = common / "hooks"
    hooks.mkdir(exist_ok=True)
    hook = hooks / "post-checkout"
    hook.write_text(f"#!/bin/sh\ntouch '{marker}'\n", encoding="utf-8")
    hook.chmod(0o755)
    info = common / "objects" / "info"
    info.mkdir(exist_ok=True)
    hostile_objects = tmp_path / "hostile-objects"
    hostile_objects.mkdir()
    (info / "alternates").symlink_to(hostile_objects)
    common_info = common / "info"
    common_info.mkdir(exist_ok=True)
    (common_info / "attributes").symlink_to(hostile)
    (admin / "config.worktree").write_text(
        f"[include]\n path = {hostile}\n", encoding="utf-8"
    )
    requested = _inspect(worktree, tmp_path / "scratch")
    assert requested.requested_commit == git(primary, "rev-parse", "HEAD")
    assert not marker.exists()


def test_git_file_swap_after_open_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = _linked_layout(tmp_path)
    marker = layout.worktree / ".git"
    original = gitless._read_pointer_file
    swapped = False

    def read_pointer(descriptor: int, *, prefix: bytes | None, label: str) -> str:
        nonlocal swapped
        value = original(descriptor, prefix=prefix, label=label)
        if label == "linked-worktree .git file" and not swapped:
            content = marker.read_bytes()
            marker.rename(layout.worktree / ".git-opened")
            marker.write_bytes(content)
            swapped = True
        return value

    monkeypatch.setattr(gitless, "_read_pointer_file", read_pointer)
    with pytest.raises(QualifiedCampaignInputError):
        _inspect(layout.worktree, tmp_path / "scratch")


def test_admin_directory_replacement_during_intake_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = _linked_layout(tmp_path)
    original = gitless._transfer_required_file
    replaced = False

    def transfer_file(
        parent_fd: int,
        name: str,
        transfer_fd: int,
        relative: tuple[str, ...],
        *,
        required: bool = True,
        expected: os.stat_result | None = None,
    ) -> None:
        nonlocal replaced
        original(
            parent_fd,
            name,
            transfer_fd,
            relative,
            required=required,
            expected=expected,
        )
        if name == "HEAD" and not replaced:
            moved = layout.admin.with_name(layout.admin.name + "-opened")
            layout.admin.rename(moved)
            shutil.copytree(moved, layout.admin)
            replaced = True

    monkeypatch.setattr(gitless, "_transfer_required_file", transfer_file)
    with pytest.raises(QualifiedCampaignInputError):
        _inspect_through_transfer(layout.worktree, tmp_path / "scratch")


def test_common_directory_replacement_during_intake_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = _linked_layout(tmp_path)
    original = gitless._transfer_required_file
    replaced = False

    def transfer_file(
        parent_fd: int,
        name: str,
        transfer_fd: int,
        relative: tuple[str, ...],
        *,
        required: bool = True,
        expected: os.stat_result | None = None,
    ) -> None:
        nonlocal replaced
        original(
            parent_fd,
            name,
            transfer_fd,
            relative,
            required=required,
            expected=expected,
        )
        if name == "HEAD" and not replaced:
            moved = layout.common.with_name(layout.common.name + "-opened")
            layout.common.rename(moved)
            shutil.copytree(moved, layout.common)
            replaced = True

    monkeypatch.setattr(gitless, "_transfer_required_file", transfer_file)
    with pytest.raises(QualifiedCampaignInputError):
        _inspect_through_transfer(layout.worktree, tmp_path / "scratch")


def test_common_control_file_replacement_during_intake_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = _linked_layout(tmp_path)
    git(layout.primary, "pack-refs", "--all")
    packed_refs = layout.common / "packed-refs"
    original = gitless._transfer_required_file
    replaced = False

    def transfer_file(
        parent_fd: int,
        name: str,
        transfer_fd: int,
        relative: tuple[str, ...],
        *,
        required: bool = True,
        expected: os.stat_result | None = None,
    ) -> None:
        nonlocal replaced
        original(
            parent_fd,
            name,
            transfer_fd,
            relative,
            required=required,
            expected=expected,
        )
        if name == "packed-refs" and not replaced:
            content = packed_refs.read_bytes()
            packed_refs.rename(layout.common / "packed-refs-opened")
            packed_refs.write_bytes(content)
            replaced = True

    monkeypatch.setattr(gitless, "_transfer_required_file", transfer_file)
    with pytest.raises(QualifiedCampaignInputError):
        _inspect_through_transfer(layout.worktree, tmp_path / "scratch")
