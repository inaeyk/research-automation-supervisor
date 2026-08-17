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
from research_automation_supervisor.gitless_repository import (
    freeze_repository_import,
    open_repository_directory,
)
from research_automation_supervisor.prelaunch_authority import (
    CampaignLaunchRequestV1,
    consume_start_intent_for_qualified_launch,
    create_start_intent,
    freeze_launch_intent,
)
from research_automation_supervisor.safe_git import inspect_requested_repository
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
        ("protocol.ext.allow", "always"),
    )
    git(repository, "add", ".gitattributes", ".hostile-hooks/post-checkout")
    git(repository, "commit", "-q", "-m", "hostile repository controls")
    for key, value in configs:
        git(repository, "config", key, value)
    included = root / "hostile-included.gitconfig"
    included.write_text(
        f"[credential]\n\thelper = !touch {marker}\n[core]\n\tfsmonitor = !touch {marker}\n",
        encoding="utf-8",
    )
    git(repository, "config", "include.path", str(included))
    git(
        repository,
        "config",
        f"includeIf.gitdir:{repository}/.path",
        str(included),
    )
    return repository


def _start(root: Path, repository: Path) -> Path:
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
    reference = create_start_intent(request, authority, root / "snapshots")
    material = consume_start_intent_for_qualified_launch(
        authority,
        reference.launch_intent_id,
        expected_campaign_public_id=reference.campaign_public_id,
    )
    return Path(material.input_bundle.repository.prepared_workspace)


def test_hostile_repository_programs_never_execute_during_gitless_import(tmp_path: Path) -> None:
    marker = tmp_path / "HOSTILE_PROGRAM_EXECUTED"
    repository = _hostile_repository(tmp_path, marker)
    workspace = _start(tmp_path, repository)
    assert not marker.exists()
    assert workspace.is_dir()
    assert not marker.exists()
    snapshot_config = (workspace / ".git/config").read_text()
    assert "include" not in snapshot_config
    assert "filter" not in snapshot_config
    assert "remote" not in snapshot_config


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
    with pytest.raises(QualifiedCampaignInputError, match="safe|unsafe"):
        inspect_requested_repository(
            "existing_folder", str(link), sterile_root=tmp_path / "sterile"
        )
    nested = repository / "nested"
    nested.mkdir()
    with pytest.raises(QualifiedCampaignInputError, match="safe|HEAD|metadata"):
        inspect_requested_repository(
            "existing_folder", str(nested), sterile_root=tmp_path / "other"
        )


def test_stale_repository_identity_is_rejected_before_checkout(tmp_path: Path) -> None:
    repository = create_repository(tmp_path)
    requested = inspect_requested_repository(
        "existing_folder", str(repository), sterile_root=tmp_path / "sterile"
    )
    request = CampaignLaunchRequestV1(
        preview_id="preview-" + "a" * 24,
        client_start_key_sha256="b" * 64,
        human_name="Stale repository identity",
        repository=requested,
        research_contract=FrozenInputFileV1.from_bytes("contract.md", b"contract\n"),
        research_plan=FrozenInputFileV1.from_bytes("plan.md", b"plan\n"),
        initial_task=FrozenInputFileV1.from_bytes("task.md", b"task\n"),
        requested_settings=CampaignProfileSettingsV1(),
    )
    (repository / "changed.txt").write_text("changed\n", encoding="utf-8")
    git(repository, "add", "changed.txt")
    git(repository, "commit", "-q", "-m", "changed after Start")
    with pytest.raises(QualifiedCampaignInputError, match="changed after preview"):
        create_start_intent(request, tmp_path / "authority", tmp_path / "snapshots")
    assert not tuple((tmp_path / "snapshots/workspaces").glob("*/repository"))


def test_local_upload_pack_hook_is_ignored_by_object_reader(tmp_path: Path) -> None:
    repository = create_repository(tmp_path)
    marker = tmp_path / "UPLOAD_PACK_HOOK_EXECUTED_OUTSIDE"
    git(
        repository,
        "config",
        "uploadpack.packObjectsHook",
        f"sh -c 'touch {marker}; exit 1'",
    )
    _start(tmp_path, repository)
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


def test_repository_path_replacement_between_preview_and_start_fails_closed(
    tmp_path: Path,
) -> None:
    repository = create_repository(tmp_path)
    requested = inspect_requested_repository(
        "existing_folder", str(repository), sterile_root=tmp_path / "sterile"
    )
    moved = tmp_path / "original-moved"
    repository.rename(moved)
    replacement_root = tmp_path / "replacement"
    replacement_root.mkdir()
    replacement = create_repository(replacement_root)
    replacement.rename(repository)
    request = CampaignLaunchRequestV1(
        preview_id="preview-" + "f" * 24,
        client_start_key_sha256="e" * 64,
        human_name="Path replacement",
        repository=requested,
        research_contract=FrozenInputFileV1.from_bytes("contract.md", b"contract\n"),
        research_plan=FrozenInputFileV1.from_bytes("plan.md", b"plan\n"),
        initial_task=FrozenInputFileV1.from_bytes("task.md", b"task\n"),
        requested_settings=CampaignProfileSettingsV1(),
    )
    with pytest.raises(QualifiedCampaignInputError, match="changed after preview"):
        freeze_launch_intent(request, tmp_path / "authority")


def test_repository_path_swap_cannot_substitute_retained_descriptor(
    tmp_path: Path,
) -> None:
    repository = create_repository(tmp_path)
    requested = inspect_requested_repository(
        "existing_folder", str(repository), sterile_root=tmp_path / "sterile"
    )
    _, descriptor = open_repository_directory(str(repository))
    try:
        moved = tmp_path / "swapped-original"
        repository.rename(moved)
        replacement_root = tmp_path / "swap-replacement"
        replacement_root.mkdir()
        create_repository(replacement_root).rename(repository)
        imported = freeze_repository_import(
            requested,
            import_root=tmp_path / "imports",
            repository_descriptor=descriptor,
        )
    finally:
        os.close(descriptor)
    assert imported.source_commit == requested.requested_commit


def test_production_git_invocations_are_mechanically_confined_to_safe_git() -> None:
    for path in (
        Path("src/research_automation_supervisor/gitless_repository.py"),
        Path("src/research_automation_supervisor/prelaunch_authority.py"),
        Path("src/research_automation_supervisor/core_authority_service.py"),
        Path("src/research_automation_supervisor/core_authority_client.py"),
    ):
        source = path.read_text(encoding="utf-8")
        assert "import subprocess" not in source
        assert "subprocess." not in source
    bootstrap = Path("scripts/custodian-bootstrap.sh").read_text(encoding="utf-8")
    assert "git -C" not in bootstrap
