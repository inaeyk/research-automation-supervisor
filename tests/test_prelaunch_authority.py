from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
from pathlib import Path

import pytest

import research_automation_supervisor.gitless_repository as gitless_repository_module
from research_automation_supervisor.core_authority_models import CampaignLaunchRequestV1
from research_automation_supervisor.custodian_errors import QualifiedCampaignInputError
from research_automation_supervisor.custodian_models import (
    CampaignProfileSettingsV1,
    FrozenInputFileV1,
)
from research_automation_supervisor.prelaunch_authority import (
    authoritative_start_count,
    consume_start_intent_for_qualified_launch,
    create_start_intent,
    get_start_intent,
    list_operator_campaigns,
    verify_start_intent,
)
from research_automation_supervisor.safe_git import inspect_requested_repository
from tests.custodian_helpers import create_repository, git


def _request(
    root: Path,
    *,
    preview: str = "preview-" + "a" * 24,
    start_key: str = "start identity",
    name: str = "Atomic Start campaign",
    contract: bytes = b"ORIGINAL CONTRACT\n",
    repository: Path | None = None,
    settings: CampaignProfileSettingsV1 | None = None,
) -> CampaignLaunchRequestV1:
    source = repository or create_repository(root)
    requested = inspect_requested_repository(
        "existing_folder", str(source), sterile_root=root / "preview-sterile"
    )
    return CampaignLaunchRequestV1(
        preview_id=preview,
        client_start_key_sha256=hashlib.sha256(start_key.encode()).hexdigest(),
        human_name=name,
        repository=requested,
        research_contract=FrozenInputFileV1.from_bytes("contract.md", contract),
        research_plan=FrozenInputFileV1.from_bytes("plan.md", b"ORIGINAL PLAN\n"),
        initial_task=FrozenInputFileV1.from_bytes("task.md", b"ORIGINAL TASK\n"),
        supporting_files=(FrozenInputFileV1.from_bytes("support.dat", b"ORIGINAL SUPPORT\n"),),
        requested_settings=settings or CampaignProfileSettingsV1(),
    )


def test_atomic_start_commits_complete_bundle_and_one_sanitized_snapshot(tmp_path: Path) -> None:
    request = _request(tmp_path)
    authority = tmp_path / "authority"
    snapshots = tmp_path / "snapshots"
    reference = create_start_intent(request, authority, snapshots)

    summary = verify_start_intent(
        authority,
        reference.launch_intent_id,
        expected_campaign_public_id=reference.campaign_public_id,
        expected_intent_sha256=reference.launch_intent_sha256,
        expected_bundle_sha256=reference.input_bundle_sha256,
    )
    material = consume_start_intent_for_qualified_launch(
        authority,
        reference.launch_intent_id,
        expected_campaign_public_id=reference.campaign_public_id,
    )
    assert summary.input_bundle_sha256 == material.input_bundle.bundle_sha256
    assert material.input_bundle.research_contract.content_bytes() == b"ORIGINAL CONTRACT\n"
    assert material.input_bundle.research_plan.content_bytes() == b"ORIGINAL PLAN\n"
    assert material.input_bundle.initial_task.content_bytes() == b"ORIGINAL TASK\n"
    assert material.input_bundle.supporting_files[0].content_bytes() == b"ORIGINAL SUPPORT\n"
    workspace = Path(material.input_bundle.repository.prepared_workspace)
    assert workspace.is_dir()
    assert len(tuple((snapshots / "workspaces").glob("*/repository"))) == 1
    assert authoritative_start_count(authority) == 1
    assert summary.snapshot_state == "complete"
    assert len(tuple((snapshots / "complete").glob("*"))) == 1


def test_snapshot_store_modes_are_exact_under_service_umask(tmp_path: Path) -> None:
    snapshots = tmp_path / "snapshots"
    previous_umask = os.umask(0o077)
    try:
        gitless_repository_module.initialize_snapshot_store(snapshots)
    finally:
        os.umask(previous_umask)

    assert stat.S_IMODE(snapshots.stat().st_mode) == 0o710
    for name in ("imports", "staging", "complete"):
        assert stat.S_IMODE((snapshots / name).stat().st_mode) == 0o700
    workspaces = snapshots / "workspaces"
    assert stat.S_IMODE(workspaces.stat().st_mode) == 0o2710

    workspaces.chmod(0o700)
    previous_umask = os.umask(0o077)
    try:
        gitless_repository_module.initialize_snapshot_store(snapshots)
    finally:
        os.umask(previous_umask)
    assert stat.S_IMODE(workspaces.stat().st_mode) == 0o2710


def test_workspace_delegation_retains_core_uid_and_uses_only_shared_gid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _request(tmp_path)
    authority = tmp_path / "authority"
    snapshots = tmp_path / "snapshots"
    core_uid = os.getuid()
    shared_gid = os.getgid()
    chowns: list[tuple[int, int]] = []
    chmod_modes: list[int] = []
    real_chmod = os.chmod

    def group_only_chown(
        path: os.PathLike[str] | str,
        uid: int,
        gid: int,
        *,
        follow_symlinks: bool = True,
    ) -> None:
        chowns.append((uid, gid))
        raise PermissionError("runtime workspace delegation attempted chown")

    def reject_runtime_setid_chmod(
        path: os.PathLike[str] | str,
        mode: int,
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        chmod_modes.append(mode)
        if mode & (stat.S_ISUID | stat.S_ISGID):
            raise PermissionError("RestrictSUIDSGID rejected runtime chmod")
        real_chmod(path, mode, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    gitless_repository_module.initialize_snapshot_store(snapshots)

    monkeypatch.setattr(gitless_repository_module.os, "chown", group_only_chown)
    monkeypatch.setattr(gitless_repository_module.os, "chmod", reject_runtime_setid_chmod)
    reference = create_start_intent(
        request,
        authority,
        snapshots,
        operator_uid=core_uid + 10_000,
        operator_gid=shared_gid,
    )
    material = consume_start_intent_for_qualified_launch(
        authority,
        reference.launch_intent_id,
        expected_campaign_public_id=reference.campaign_public_id,
    )
    workspace = Path(material.input_bundle.repository.prepared_workspace)

    assert chowns == []
    assert chmod_modes
    assert all(not mode & (stat.S_ISUID | stat.S_ISGID) for mode in chmod_modes)
    for path in (workspace, *workspace.rglob("*")):
        status = path.lstat()
        assert not stat.S_ISLNK(status.st_mode)
        assert status.st_uid == core_uid
        assert status.st_gid == shared_gid
        assert not status.st_mode & stat.S_IWOTH

    assert stat.S_IMODE(workspace.stat().st_mode) == 0o3770
    campaign = workspace.parent
    assert stat.S_IMODE(campaign.stat().st_mode) == 0o3770
    assert campaign.stat().st_uid == core_uid
    assert campaign.stat().st_gid == shared_gid
    assert stat.S_IMODE((workspace / "README.md").stat().st_mode) == 0o660
    assert stat.S_IMODE((workspace / ".research-supervisor").stat().st_mode) == 0o2770
    assert stat.S_IMODE(
        (workspace / ".research-supervisor" / "acceptance.py").stat().st_mode
    ) == 0o770
    assert stat.S_IMODE((workspace / ".git").stat().st_mode) == 0o550
    assert all(
        stat.S_IMODE(path.stat().st_mode) == 0o550
        for path in (workspace / ".git").rglob("*")
        if path.is_dir()
    )
    assert all(
        stat.S_IMODE(path.stat().st_mode) == 0o440
        for path in (workspace / ".git").rglob("*")
        if path.is_file()
    )
    previous_umask = os.umask(0o007)
    try:
        inherited = workspace / "worker-created"
        inherited.mkdir(mode=0o770)
        output = inherited / "result.txt"
        output.write_text("worker output\n", encoding="utf-8")
    finally:
        os.umask(previous_umask)
    assert stat.S_IMODE(inherited.stat().st_mode) == 0o2770
    assert inherited.stat().st_gid == shared_gid
    assert stat.S_IMODE(output.stat().st_mode) == 0o660
    assert output.stat().st_gid == shared_gid


def test_multiple_starts_bind_one_shared_complete_snapshot(tmp_path: Path) -> None:
    repository = create_repository(tmp_path)
    authority = tmp_path / "authority"
    snapshots = tmp_path / "snapshots"
    first = create_start_intent(
        _request(tmp_path / "first", repository=repository), authority, snapshots
    )
    second = create_start_intent(
        _request(
            tmp_path / "second",
            preview="preview-" + "b" * 24,
            start_key="second start identity",
            name="Second immutable campaign",
            repository=repository,
        ),
        authority,
        snapshots,
    )

    assert first.snapshot_identity == second.snapshot_identity
    assert first.campaign_public_id != second.campaign_public_id
    assert authoritative_start_count(authority) == 2
    assert len(tuple((snapshots / "complete").glob("*"))) == 1
    for reference in (first, second):
        material = consume_start_intent_for_qualified_launch(
            authority,
            reference.launch_intent_id,
            expected_campaign_public_id=reference.campaign_public_id,
        )
        assert material.snapshot_identity == reference.snapshot_identity


@pytest.mark.parametrize(
    "changed",
    (
        "preview_id",
        "client_start_key_sha256",
        "human_name",
        "repository",
        "research_contract",
        "research_plan",
        "initial_task",
        "supporting_files",
        "requested_settings",
    ),
)
def test_reuse_binds_every_caller_supplied_field(tmp_path: Path, changed: str) -> None:
    first_root = tmp_path / "first"
    first_root.mkdir()
    request = _request(first_root)
    authority = tmp_path / "authority"
    snapshots = tmp_path / "snapshots"
    reference = create_start_intent(request, authority, snapshots)
    assert create_start_intent(request, authority, snapshots) == reference

    update: dict[str, object]
    if changed == "preview_id":
        update = {changed: "preview-" + "b" * 24}
    elif changed == "client_start_key_sha256":
        # The preview index is also single-assignment; another click identity
        # cannot silently reuse authority created by the first identity.
        update = {changed: hashlib.sha256(b"changed key").hexdigest()}
    elif changed == "human_name":
        update = {changed: "Changed name"}
    elif changed == "repository":
        other_root = tmp_path / "other"
        other_root.mkdir()
        other = create_repository(other_root)
        update = {
            changed: inspect_requested_repository(
                "existing_folder", str(other), sterile_root=other_root / "sterile"
            )
        }
    elif changed == "research_contract":
        update = {changed: FrozenInputFileV1.from_bytes("contract.md", b"CHANGED\n")}
    elif changed == "research_plan":
        update = {changed: FrozenInputFileV1.from_bytes("plan.md", b"CHANGED\n")}
    elif changed == "initial_task":
        update = {changed: FrozenInputFileV1.from_bytes("task.md", b"CHANGED\n")}
    elif changed == "supporting_files":
        update = {changed: (FrozenInputFileV1.from_bytes("support.dat", b"CHANGED\n"),)}
    else:
        update = {changed: CampaignProfileSettingsV1(max_repair_rounds=3)}

    changed_request = request.model_copy(update=update)
    with pytest.raises(QualifiedCampaignInputError, match="different fields|already started"):
        create_start_intent(changed_request, authority, snapshots)


@pytest.mark.parametrize(
    ("boundary", "committed"),
    (
        ("before_input_object_creation", False),
        ("during_input_object_creation", False),
        ("after_object_fsync_before_db_transaction", False),
        ("during_start_transaction", False),
        ("immediately_before_commit", False),
        ("immediately_after_commit_before_response", True),
        ("during_repository_snapshot_staging", True),
        ("after_snapshot_content_before_snapshot_db_commit", True),
        ("after_snapshot_commit_before_campaign_launch", True),
    ),
)
def test_start_crash_matrix_recovers_zero_or_exactly_one(
    tmp_path: Path, boundary: str, committed: bool
) -> None:
    request = _request(tmp_path)
    authority = tmp_path / "authority"
    snapshots = tmp_path / "snapshots"

    def crash(observed: str) -> None:
        if observed == boundary:
            raise RuntimeError(f"crash at {boundary}")

    with pytest.raises(RuntimeError, match="crash at"):
        create_start_intent(
            request,
            authority,
            snapshots,
            crash_injector=crash,
        )
    count = authoritative_start_count(authority)
    assert count == int(committed)
    if committed:
        recovered = list_operator_campaigns(authority)
        assert len(recovered) == 1
        reference_id = recovered[0].launch_intent_id
    else:
        reference_id = ""

    retry = create_start_intent(request, authority, snapshots)
    assert len(list_operator_campaigns(authority)) == 1
    if committed:
        assert retry.launch_intent_id == reference_id
    assert get_start_intent(authority, retry.launch_intent_id).campaign_public_id == (
        retry.campaign_public_id
    )


def test_original_repository_is_never_reopened_after_snapshot(tmp_path: Path) -> None:
    repository = create_repository(tmp_path)
    request = _request(tmp_path / "request", repository=repository)
    authority = tmp_path / "authority"
    snapshots = tmp_path / "snapshots"
    reference = create_start_intent(request, authority, snapshots)
    original_workspace = Path(
        consume_start_intent_for_qualified_launch(
            authority,
            reference.launch_intent_id,
            expected_campaign_public_id=reference.campaign_public_id,
        ).input_bundle.repository.prepared_workspace
    )

    (repository / "README.md").write_text("MUTATED ORIGINAL\n", encoding="utf-8")
    git(repository, "add", "README.md")
    git(repository, "commit", "-q", "-m", "mutate original")
    git(repository, "config", "alias.hostile", "!touch SHOULD_NOT_RUN")
    material = consume_start_intent_for_qualified_launch(
        authority,
        reference.launch_intent_id,
        expected_campaign_public_id=reference.campaign_public_id,
    )
    assert Path(material.input_bundle.repository.prepared_workspace) == original_workspace
    assert (original_workspace / "README.md").read_text(encoding="utf-8") != "MUTATED ORIGINAL\n"


def test_stale_and_cross_campaign_intent_substitution_fail_closed(tmp_path: Path) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    authority = tmp_path / "authority"
    snapshots = tmp_path / "snapshots"
    first = create_start_intent(_request(first_root), authority, snapshots)
    second = create_start_intent(
        _request(
            second_root,
            preview="preview-" + "b" * 24,
            start_key="second identity",
        ),
        authority,
        snapshots,
    )
    with pytest.raises(QualifiedCampaignInputError, match="another campaign"):
        verify_start_intent(
            authority,
            second.launch_intent_id,
            expected_campaign_public_id=first.campaign_public_id,
        )
    stale = first.launch_intent_id[:-1] + ("0" if first.launch_intent_id[-1] != "0" else "1")
    with pytest.raises(QualifiedCampaignInputError, match="stale or invalid"):
        get_start_intent(authority, stale)


def test_replaced_transaction_bound_frozen_input_fails_closed(tmp_path: Path) -> None:
    request = _request(tmp_path)
    authority = tmp_path / "authority"
    reference = create_start_intent(request, authority, tmp_path / "snapshots")
    with sqlite3.connect(authority / "authority.sqlite3") as connection:
        digest = str(
            connection.execute(
                "SELECT frozen_object_sha256 FROM starts WHERE start_intent_id = ?",
                (reference.launch_intent_id,),
            ).fetchone()[0]
        )
    frozen = authority / "frozen-inputs" / digest[:2] / f"{digest}.json"
    frozen.chmod(0o600)
    frozen.write_bytes(b"{}")
    with pytest.raises(Exception, match="frozen input|hash"):
        consume_start_intent_for_qualified_launch(
            authority,
            reference.launch_intent_id,
            expected_campaign_public_id=reference.campaign_public_id,
        )
