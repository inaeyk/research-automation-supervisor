from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from research_automation_supervisor.custodian import CampaignCustodian, WizardSubmissionV1
from research_automation_supervisor.custodian_errors import (
    CustodianStateError,
    QualifiedCampaignInputError,
)
from research_automation_supervisor.custodian_models import (
    CampaignProfileSettingsV1,
    EnvironmentIssueV1,
    EnvironmentReportV1,
    FrozenInputFileV1,
)
from research_automation_supervisor.prelaunch_authority import (
    CampaignLaunchRequestV1,
    freeze_launch_intent,
    load_launch_intent,
)
from research_automation_supervisor.safe_git import inspect_requested_repository
from tests.custodian_helpers import FakeQualifiedRunner, create_repository


def _submission(repository: Path, *, contract: str = "ORIGINAL CONTRACT\n") -> WizardSubmissionV1:
    return WizardSubmissionV1(
        human_name="Frozen authority campaign",
        repository_kind="existing_folder",
        repository_locator=str(repository),
        research_contract=contract,
        research_plan="ORIGINAL PLAN\n",
        initial_task="ORIGINAL TASK\n",
        supporting_files=(FrozenInputFileV1.from_bytes("support.dat", b"ORIGINAL SUPPORT\n"),),
    )


def _blocked_environment(code: str = "infrastructure_blocked") -> EnvironmentReportV1:
    issue = EnvironmentIssueV1(
        code=code,
        title="Setup needed",
        message="The environment is deliberately blocked.",
        action="sign_in" if code == "codex_authentication_required" else "install_dependency",
        campaign_not_started=True,
    )
    return EnvironmentReportV1(
        ready=False,
        backend="wsl",
        managed_python_ready=True,
        supervisor_package_ready=True,
        git_ready=True,
        codex_ready=True,
        codex_authenticated=code != "codex_authentication_required",
        isolation_ready=code == "codex_authentication_required",
        filesystem_ready=True,
        issues=(issue,),
    )


def _request(root: Path, preview: str, marker: str) -> CampaignLaunchRequestV1:
    root.mkdir(parents=True)
    repository = create_repository(root)
    requested = inspect_requested_repository(
        "existing_folder", str(repository), sterile_root=root / "sterile"
    )
    return CampaignLaunchRequestV1(
        preview_id=preview,
        client_start_key_sha256=hashlib.sha256(marker.encode()).hexdigest(),
        human_name=marker,
        repository=requested,
        research_contract=FrozenInputFileV1.from_bytes("contract.md", marker.encode()),
        research_plan=FrozenInputFileV1.from_bytes("plan.md", b"plan\n"),
        initial_task=FrozenInputFileV1.from_bytes("task.md", b"task\n"),
        requested_settings=CampaignProfileSettingsV1(),
    )


def test_start_freezes_in_core_before_environment_and_git_then_survives_restart(
    tmp_path: Path,
) -> None:
    repository = create_repository(tmp_path)
    runner = FakeQualifiedRunner()
    observations: list[tuple[bool, bool]] = []
    custodian: CampaignCustodian

    def blocked(_root: Path) -> EnvironmentReportV1:
        receipts = tuple((custodian.launch_authority_root / "receipts").glob("*.json"))
        workspaces = tuple((custodian.repository_preparation_root / "workspaces").glob("*"))
        observations.append((len(receipts) == 1, bool(workspaces)))
        return _blocked_environment()

    custodian = CampaignCustodian(
        tmp_path / "app-data/custodian",
        runner=runner,
        environment_inspector=blocked,
    )
    preview = custodian.preview(_submission(repository))
    record = custodian.start(preview.preview_id, client_start_key="start_abcdefghijklmnop")
    assert observations[-1] == (True, False)
    assert record.projection.status == "blocked"
    assert runner.started == 0
    assert custodian.launch_authority_root not in custodian.data_root.parents
    assert custodian.data_root not in custodian.launch_authority_root.parents
    intent = load_launch_intent(
        custodian.launch_authority_root,
        record.launch_token,
        expected_campaign_public_id=record.campaign_public_id,
        expected_intent_sha256=record.launch_intent_sha256,
    )
    assert intent.research_contract.content_bytes() == b"ORIGINAL CONTRACT\n"
    assert intent.research_plan.content_bytes() == b"ORIGINAL PLAN\n"
    assert intent.initial_task.content_bytes() == b"ORIGINAL TASK\n"
    assert intent.supporting_files[0].content_bytes() == b"ORIGINAL SUPPORT\n"

    restarted = CampaignCustodian(
        custodian.data_root,
        runner=runner,
        environment_inspector=lambda _root: _blocked_environment("codex_authentication_required"),
    )
    observed = restarted.get_record(record.campaign_public_id, refresh=False)
    restarted.continue_campaign(record.campaign_public_id)
    assert observed.launch_intent_sha256 == record.launch_intent_sha256
    assert runner.started == 0


def test_replaced_card_and_preview_bytes_cannot_change_frozen_authority(tmp_path: Path) -> None:
    repository = create_repository(tmp_path)
    runner = FakeQualifiedRunner()
    custodian = CampaignCustodian(
        tmp_path / "app-data/custodian",
        runner=runner,
        environment_inspector=lambda _root: _blocked_environment(),
    )
    preview = custodian.preview(_submission(repository))
    record = custodian.start(preview.preview_id, client_start_key="start_abcdefghijklmnop")
    preview_path = custodian.previews / f"{preview.preview_id}.json"
    replacement = json.loads(preview_path.read_text(encoding="utf-8"))
    replacement["submission"]["research_contract"] = "REPLACED CONTRACT\n"
    preview_path.unlink()
    preview_path.write_text(json.dumps(replacement), encoding="utf-8")
    record_path = custodian.records / f"{record.campaign_public_id}.json"
    card = json.loads(record_path.read_text(encoding="utf-8"))
    card["projection"]["human_name"] = "Replaced card display"
    record_path.write_text(json.dumps(card), encoding="utf-8")
    custodian.continue_campaign(record.campaign_public_id)
    intent = load_launch_intent(custodian.launch_authority_root, record.launch_token)
    assert intent.research_contract.content_bytes() == b"ORIGINAL CONTRACT\n"


def test_cross_campaign_substituted_and_stale_tokens_fail_closed(tmp_path: Path) -> None:
    first = _request(tmp_path / "first", "preview-" + "a" * 24, "first\n")
    second = _request(tmp_path / "second", "preview-" + "b" * 24, "second\n")
    authority = tmp_path / "authority"
    one = freeze_launch_intent(first, authority)
    two = freeze_launch_intent(second, authority)
    with pytest.raises(QualifiedCampaignInputError, match="another campaign"):
        load_launch_intent(
            authority,
            two.launch_token,
            expected_campaign_public_id=one.campaign_public_id,
        )
    stale = one.launch_token[:-1] + ("0" if one.launch_token[-1] != "0" else "1")
    with pytest.raises(QualifiedCampaignInputError, match="stale or invalid"):
        load_launch_intent(authority, stale)


@pytest.mark.parametrize("target", ["receipt", "object"])
def test_corrupt_or_missing_frozen_authority_fails_closed(tmp_path: Path, target: str) -> None:
    request = _request(tmp_path / "request", "preview-" + "c" * 24, "authority\n")
    authority = tmp_path / "authority"
    reference = freeze_launch_intent(request, authority)
    if target == "receipt":
        path = authority / "receipts" / f"{reference.launch_intent_sha256}.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        value["campaign_public_id"] = "campaign-corrupt0000000000"
        path.unlink()
        path.write_text(json.dumps(value), encoding="utf-8")
    else:
        path = (
            authority
            / "objects"
            / reference.launch_intent_sha256[:2]
            / f"{reference.launch_intent_sha256}.json"
        )
        path.unlink()
    with pytest.raises(QualifiedCampaignInputError):
        load_launch_intent(authority, reference.launch_token)
    if target == "object":
        with pytest.raises(QualifiedCampaignInputError):
            freeze_launch_intent(request, authority)


def test_interrupted_authority_write_recovers_but_repeated_start_is_idempotent(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path / "request", "preview-" + "d" * 24, "interrupted\n")
    authority = tmp_path / "authority"
    first = freeze_launch_intent(request, authority)
    receipt = authority / "receipts" / f"{first.launch_intent_sha256}.json"
    receipt.unlink()  # Crash simulation: durable object exists, receipt commit did not.
    recovered = freeze_launch_intent(request, authority)
    repeated = freeze_launch_intent(request, authority)
    assert recovered == first == repeated
    assert (
        load_launch_intent(authority, repeated.launch_token).intent_sha256
        == first.launch_intent_sha256
    )


def test_substituted_valid_intent_in_custodian_card_is_rejected(tmp_path: Path) -> None:
    repository = create_repository(tmp_path)
    runner = FakeQualifiedRunner()
    custodian = CampaignCustodian(
        tmp_path / "app-data/custodian",
        runner=runner,
        environment_inspector=lambda _root: _blocked_environment(),
    )
    first_preview = custodian.preview(_submission(repository))
    first = custodian.start(first_preview.preview_id, client_start_key="start_abcdefghijklmnop")
    second_preview = custodian.preview(_submission(repository, contract="SECOND CONTRACT\n"))
    second = custodian.start(second_preview.preview_id, client_start_key="start_qrstuvwxyzabcdef")
    path = custodian.records / f"{first.campaign_public_id}.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    value["launch_token"] = second.launch_token
    value["launch_intent_sha256"] = second.launch_intent_sha256
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(CustodianStateError, match="Frozen core launch authority"):
        custodian.continue_campaign(first.campaign_public_id)


def test_custodian_record_contains_no_authoritative_scientific_bytes(tmp_path: Path) -> None:
    repository = create_repository(tmp_path)
    custodian = CampaignCustodian(
        tmp_path / "app-data/custodian",
        runner=FakeQualifiedRunner(),
        environment_inspector=lambda _root: _blocked_environment(),
    )
    preview = custodian.preview(_submission(repository))
    record = custodian.start(preview.preview_id, client_start_key="start_abcdefghijklmnop")
    content = (custodian.records / f"{record.campaign_public_id}.json").read_text()
    assert "ORIGINAL CONTRACT" not in content
    assert "ORIGINAL PLAN" not in content
    assert "ORIGINAL TASK" not in content
    assert "ORIGINAL SUPPORT" not in content
    assert "launch_token" in content


def test_authority_object_prefix_symlink_fails_closed(tmp_path: Path) -> None:
    request = _request(tmp_path / "request", "preview-" + "e" * 24, "symlink\n")
    authority = tmp_path / "authority"
    reference = freeze_launch_intent(request, authority)
    prefix = reference.launch_intent_sha256[:2]
    (authority / "receipts" / f"{reference.launch_intent_sha256}.json").unlink()
    object_path = authority / "objects" / prefix / f"{reference.launch_intent_sha256}.json"
    object_path.unlink()
    object_path.parent.rmdir()
    target = tmp_path / "outside"
    target.mkdir()
    object_prefix = authority / "objects" / prefix
    object_prefix.symlink_to(target, target_is_directory=True)
    with pytest.raises(QualifiedCampaignInputError, match="unsafe"):
        freeze_launch_intent(request, authority)
