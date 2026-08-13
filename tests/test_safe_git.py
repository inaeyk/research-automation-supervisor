from __future__ import annotations

import os
from pathlib import Path

import pytest

from research_automation_supervisor.custodian_errors import (
    QualifiedCampaignInputError,
)
from research_automation_supervisor.custodian_models import (
    CampaignProfileSettingsV1,
    FrozenInputFileV1,
)
from research_automation_supervisor.prelaunch_authority import (
    CampaignLaunchRequestV1,
    freeze_launch_intent,
    load_launch_intent,
)
from research_automation_supervisor.safe_git import (
    inspect_requested_repository,
    prepare_repository,
)
from tests.custodian_helpers import create_repository, git


def _hostile_repository(root: Path, marker: Path) -> Path:
    repository = create_repository(root)
    hooks = repository / ".hostile-hooks"
    hooks.mkdir()
    hook = hooks / "post-checkout"
    hook.write_text(f"#!/bin/sh\ntouch '{marker}'\n", encoding="utf-8")
    hook.chmod(0o755)
    attributes = repository / ".gitattributes"
    attributes.write_text(
        "*.md filter=hostile diff=hostile\n.gitattributes filter=hostile-process\n",
        encoding="utf-8",
    )
    helper = f"!sh -c 'touch {marker}'"
    configs = (
        ("core.hooksPath", ".hostile-hooks"),
        ("core.fsmonitor", helper),
        ("filter.hostile.clean", f"sh -c 'touch {marker}; cat'"),
        ("filter.hostile.smudge", f"sh -c 'touch {marker}; cat'"),
        ("filter.hostile-process.process", f"sh -c 'touch {marker}; exit 1'"),
        ("diff.external", f"sh -c 'touch {marker}'"),
        ("diff.hostile.textconv", f"sh -c 'touch {marker}; cat \"$1\"' --"),
        ("credential.helper", helper),
        ("alias.hostile", f"!sh -c 'touch {marker}'"),
    )
    git(repository, "add", ".gitattributes", ".hostile-hooks/post-checkout")
    git(repository, "commit", "-q", "-m", "hostile repository controls")
    for key, value in configs:
        git(repository, "config", key, value)
    return repository


def _intent(root: Path, repository: Path):  # type: ignore[no-untyped-def]
    requested = inspect_requested_repository(
        "existing_folder", str(repository), sterile_root=root / "preview-sterile"
    )
    request = CampaignLaunchRequestV1(
        preview_id="preview-" + "a" * 24,
        client_start_key_sha256="b" * 64,
        human_name="Hostile repository test",
        repository=requested,
        research_contract=FrozenInputFileV1.from_bytes("contract.md", b"contract\n"),
        research_plan=FrozenInputFileV1.from_bytes("plan.md", b"plan\n"),
        initial_task=FrozenInputFileV1.from_bytes("task.md", b"task\n"),
        requested_settings=CampaignProfileSettingsV1(),
    )
    authority = root / "authority"
    reference = freeze_launch_intent(request, authority)
    return authority, load_launch_intent(authority, reference.launch_token)


def test_hostile_repository_programs_never_execute_before_bubblewrap(tmp_path: Path) -> None:
    marker = tmp_path / "HOSTILE_PROGRAM_EXECUTED"
    repository = _hostile_repository(tmp_path, marker)
    authority, intent = _intent(tmp_path, repository)
    del authority
    assert not marker.exists()
    prepared, receipt = prepare_repository(intent, preparation_root=tmp_path / "preparation")
    assert Path(prepared.prepared_workspace).is_dir()
    assert not marker.exists()
    assert receipt.checkout_outside_isolation is False
    assert receipt.allowed_clone_protocol == "existing_folder"
    pre_isolation = [
        command for command in receipt.commands if command.isolation == "pre_isolation_nonexecuting"
    ]
    isolated = [
        command for command in receipt.commands if command.isolation == "bubblewrap_unshare_all_v1"
    ]
    assert {command.phase for command in isolated} == {
        "clone_no_checkout",
        "isolated_checkout",
        "isolated_commit",
    }
    assert all(command.argv[0] == "/usr/bin/git" for command in pre_isolation)
    exact = " ".join(" ".join(command.argv) for command in receipt.commands)
    for restriction in (
        "GIT_CONFIG_NOSYSTEM",
        "core.hooksPath=/dev/null",
        "core.fsmonitor=false",
        "core.attributesFile=/dev/null",
        "credential.helper=",
        "protocol.ext.allow=never",
        "protocol.file.allow=never",
        "--no-checkout",
        "--no-recurse-submodules",
    ):
        assert restriction in (exact + repr(pre_isolation))


@pytest.mark.parametrize(
    "locator",
    (
        "ext::sh -c 'touch /tmp/hostile'",
        "file:///tmp/repository",
        "/tmp/repository",
        "ssh://example.invalid/repository.git",
        "git://example.invalid/repository.git",
        "https://user@example.invalid/repository.git",
        "https://example.invalid:8443/repository.git",
    ),
)
def test_forbidden_clone_transports_fail_closed(tmp_path: Path, locator: str) -> None:
    with pytest.raises(QualifiedCampaignInputError, match="HTTPS"):
        inspect_requested_repository("git_url", locator, sterile_root=tmp_path / "sterile")


def test_existing_repository_symlink_and_path_tricks_fail_closed(tmp_path: Path) -> None:
    repository = create_repository(tmp_path)
    link = tmp_path / "repository-link"
    link.symlink_to(repository, target_is_directory=True)
    with pytest.raises(QualifiedCampaignInputError, match="unsafe"):
        inspect_requested_repository(
            "existing_folder", str(link), sterile_root=tmp_path / "sterile"
        )
    nested = repository / "nested"
    nested.mkdir()
    with pytest.raises(QualifiedCampaignInputError, match="top-level"):
        inspect_requested_repository(
            "existing_folder", str(nested), sterile_root=tmp_path / "other"
        )


def test_stale_repository_identity_is_rejected_before_checkout(tmp_path: Path) -> None:
    repository = create_repository(tmp_path)
    _, intent = _intent(tmp_path, repository)
    (repository / "changed.txt").write_text("changed\n", encoding="utf-8")
    git(repository, "add", "changed.txt")
    git(repository, "commit", "-q", "-m", "changed after Start")
    marker = tmp_path / "preparation/workspaces"
    with pytest.raises(QualifiedCampaignInputError, match="changed after preview"):
        prepare_repository(intent, preparation_root=tmp_path / "preparation")
    # A no-checkout clone may exist, but no working-tree file was materialized.
    workspaces = tuple(marker.glob("*/repository/changed.txt")) if marker.exists() else ()
    assert not workspaces


def test_local_upload_pack_hook_is_confined_inside_bubblewrap(tmp_path: Path) -> None:
    repository = create_repository(tmp_path)
    marker = tmp_path / "UPLOAD_PACK_HOOK_EXECUTED_OUTSIDE"
    git(
        repository,
        "config",
        "uploadpack.packObjectsHook",
        f"sh -c 'touch {marker}; exit 1'",
    )
    _, intent = _intent(tmp_path, repository)
    _, receipt = prepare_repository(intent, preparation_root=tmp_path / "preparation")
    clone = next(command for command in receipt.commands if command.phase == "clone_no_checkout")
    assert clone.isolation == "bubblewrap_unshare_all_v1"
    assert not marker.exists()


def test_sterile_environment_ignores_host_global_git_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = create_repository(tmp_path)
    marker = tmp_path / "GLOBAL_CONFIG_EXECUTED"
    home = tmp_path / "hostile-home"
    home.mkdir()
    (home / ".gitconfig").write_text(
        f"[core]\n\tfsmonitor = !sh -c 'touch {marker}'\n[alias]\n\tx = !touch {marker}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    requested = inspect_requested_repository(
        "existing_folder", str(repository), sterile_root=tmp_path / "sterile"
    )
    assert requested.requested_commit == git(repository, "rev-parse", "HEAD")
    assert not marker.exists()
    assert os.environ["HOME"] == str(home)
