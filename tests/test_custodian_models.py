from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from research_automation_supervisor.custodian_errors import (
    CustodianInputError,
    CustodianStateError,
)
from research_automation_supervisor.custodian_exchange import (
    load_human_action_request,
    prepare_operator_exchange,
    publish_human_action_request,
    publish_notification,
    submit_human_action_response,
)
from research_automation_supervisor.custodian_models import (
    CampaignInputBundleV1,
    DurableStateAuthorityV1,
    FrozenInputFileV1,
    HumanActionOptionV1,
    HumanActionRequestV1,
    HumanActionResponseV1,
    LocalNotificationV1,
    RepositoryAuthorityV1,
)


def repository_authority(tmp_path: Path) -> RepositoryAuthorityV1:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    return RepositoryAuthorityV1(
        source_kind="existing_folder",
        source_display="research-project",
        source_locator_sha256="1" * 64,
        prepared_workspace=str(workspace),
        baseline_commit="2" * 40,
        baseline_tree="3" * 40,
        repository_id="research-project",
    )


def bundle(tmp_path: Path, campaign_id: str = "campaign-aaaaaaaaaaaa") -> CampaignInputBundleV1:
    return CampaignInputBundleV1.freeze(
        campaign_public_id=campaign_id,
        human_name="Boundary campaign",
        repository=repository_authority(tmp_path),
        research_contract=FrozenInputFileV1.from_bytes("contract.md", b"Contract\n"),
        research_plan=FrozenInputFileV1.from_bytes("plan.md", b"Plan\n"),
        initial_task=FrozenInputFileV1.from_bytes("task.md", b"Task\n"),
    )


def authority(seed: str = "4") -> DurableStateAuthorityV1:
    return DurableStateAuthorityV1(
        authority_kind="visible_campaign",
        state_sha256=seed * 64,
        journal_sha256="5" * 64,
        journal_sequence=14,
        journal_hash="6" * 64,
        frozen_policy_sha256="7" * 64,
    )


def request(value: CampaignInputBundleV1) -> HumanActionRequestV1:
    return HumanActionRequestV1.issue(
        campaign_public_id=value.campaign_public_id,
        input_bundle_sha256=value.bundle_sha256,
        stage="Implementing M2-C",
        substage="M2-C",
        request_id="human-000014",
        reason="Worker and Auditor disagree about a convention.",
        question="Which qualified option should the campaign use?",
        response_type="contract_decision",
        allowed_options=[
            HumanActionOptionV1(
                option_id="keep_existing",
                label="Keep existing convention",
                consequence="Frozen authority remains unchanged.",
            ).model_dump(mode="json"),
            HumanActionOptionV1(
                option_id="request_more_evidence",
                label="Request additional evidence",
                consequence="The workflow gathers evidence before another decision.",
            ).model_dump(mode="json"),
        ],
        evidence_links=[],
        campaign_state_safe=True,
        durable_authority=authority().model_dump(mode="json"),
    )


def response(value: HumanActionRequestV1, campaign_id: str | None = None) -> HumanActionResponseV1:
    return HumanActionResponseV1.bind(
        campaign_public_id=campaign_id or value.campaign_public_id,
        request_id=value.request_id,
        request_sha256=value.request_sha256,
        input_bundle_sha256=value.input_bundle_sha256,
        durable_authority=value.durable_authority.model_dump(mode="json"),
        selected_option_id="keep_existing",
        response_text="Use the already locked convention.",
        uploaded_files=[],
    )


def test_campaign_input_bundle_is_self_hashed_strict_and_frozen(tmp_path: Path) -> None:
    value = bundle(tmp_path)
    assert len(value.bundle_sha256) == 64
    assert value.research_contract.content_bytes() == b"Contract\n"
    with pytest.raises(ValidationError):
        value.model_copy(update={"human_name": "changed"}).__class__.model_validate(
            {**value.model_dump(mode="json"), "human_name": "changed"}
        )
    payload = value.model_dump(mode="json")
    payload["unknown"] = True
    with pytest.raises(ValidationError):
        CampaignInputBundleV1.model_validate(payload)


def test_exchange_rejects_replacement_duplicate_cross_campaign_and_stale_response(
    tmp_path: Path,
) -> None:
    value = bundle(tmp_path)
    paths = prepare_operator_exchange(tmp_path / "operator-exchange", value.campaign_public_id)
    issued = request(value)
    publish_human_action_request(paths, issued)
    assert load_human_action_request(paths, issued.request_sha256) == issued
    with pytest.raises(CustodianStateError, match="already"):
        publish_human_action_request(paths, issued)

    accepted = response(issued)
    with pytest.raises(CustodianInputError, match="advanced"):
        submit_human_action_response(
            paths,
            issued,
            accepted,
            current_authority=authority("8"),
        )
    submit_human_action_response(
        paths,
        issued,
        accepted,
        current_authority=issued.durable_authority,
    )
    with pytest.raises(CustodianStateError, match="already"):
        submit_human_action_response(
            paths,
            issued,
            accepted,
            current_authority=issued.durable_authority,
        )

    other = bundle(tmp_path / "other", "campaign-bbbbbbbbbbbb")
    other_paths = prepare_operator_exchange(
        tmp_path / "operator-exchange", other.campaign_public_id
    )
    with pytest.raises(CustodianInputError, match="another campaign"):
        submit_human_action_response(
            other_paths,
            issued,
            response(issued, other.campaign_public_id),
            current_authority=issued.durable_authority,
        )


def test_exchange_rejects_symlink_request_and_path_traversal_upload(tmp_path: Path) -> None:
    value = bundle(tmp_path)
    paths = prepare_operator_exchange(tmp_path / "exchange", value.campaign_public_id)
    issued = request(value)
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps(issued.model_dump(mode="json")), encoding="utf-8")
    (paths.requests / f"{issued.request_sha256}.json").symlink_to(outside)
    with pytest.raises(CustodianStateError, match="unsafe"):
        load_human_action_request(paths, issued.request_sha256)

    response_payload = response(issued).model_dump(mode="json")
    response_payload["uploaded_files"] = [
        {
            "display_name": "evidence.txt",
            "byte_count": 1,
            "sha256": "9" * 64,
            "exchange_path": "../outside",
        }
    ]
    with pytest.raises(ValidationError):
        HumanActionResponseV1.model_validate(response_payload)


def test_completion_notification_requires_verified_completion(tmp_path: Path) -> None:
    value = bundle(tmp_path)
    paths = prepare_operator_exchange(tmp_path / "exchange", value.campaign_public_id)
    with pytest.raises(ValidationError):
        LocalNotificationV1(
            campaign_public_id=value.campaign_public_id,
            kind="campaign_completed",
            title="Complete",
            message="Process exited.",
            created_at="2026-08-13T00:00:00Z",
            completion_verified=False,
        )
    notification = LocalNotificationV1(
        campaign_public_id=value.campaign_public_id,
        kind="campaign_completed",
        title="Campaign completed",
        message="Durable completion was verified.",
        created_at="2026-08-13T00:00:00Z",
        completion_verified=True,
    )
    assert publish_notification(paths, notification).is_file()
