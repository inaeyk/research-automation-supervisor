from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from research_automation_supervisor.core_authority_models import CampaignLaunchRequestV1
from research_automation_supervisor.custodian_errors import QualifiedCampaignInputError
from research_automation_supervisor.custodian_models import (
    CampaignProfileSettingsV1,
    FrozenInputFileV1,
)
from research_automation_supervisor.prelaunch_authority import (
    consume_start_intent_for_qualified_launch,
    create_start_intent,
    get_start_intent,
    list_operator_campaigns,
    load_launch_intent,
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
    assert len(tuple((authority / "receipts").glob("*.json"))) == 1


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
        ("before_core_transaction", False),
        ("during_durable_object_write", False),
        ("after_durable_objects_before_receipt", False),
        ("after_receipt_before_response", True),
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
    receipts = tuple((authority / "receipts").glob("*.json")) if authority.exists() else ()
    assert bool(receipts) is committed
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


def test_replaced_receipt_or_frozen_input_fails_closed(tmp_path: Path) -> None:
    request = _request(tmp_path)
    authority = tmp_path / "authority"
    reference = create_start_intent(request, authority, tmp_path / "snapshots")
    receipt_path = authority / "receipts" / f"{request.client_start_key_sha256}.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["campaign_public_id"] = "campaign-corrupt0000000000"
    receipt_path.unlink()
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(QualifiedCampaignInputError):
        load_launch_intent(authority, reference.launch_intent_id)
