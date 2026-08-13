from __future__ import annotations

import ast
import json
import os
from pathlib import Path

import pytest

from research_automation_supervisor.custodian import (
    CampaignCustodian,
    WizardSubmissionV1,
    _runner_environment,
)
from research_automation_supervisor.custodian_errors import CustodianStateError
from research_automation_supervisor.custodian_exchange import (
    prepare_operator_exchange,
    publish_human_action_request,
)
from research_automation_supervisor.custodian_models import (
    CampaignInputBundleV1,
    DurableStateAuthorityV1,
    EnvironmentIssueV1,
    EnvironmentReportV1,
    FrozenInputFileV1,
    HumanActionOptionV1,
    HumanActionRequestV1,
)
from tests.custodian_helpers import (
    FakeQualifiedRunner,
    create_repository,
    projection,
    ready_environment,
)


def submission(repository: Path) -> WizardSubmissionV1:
    return WizardSubmissionV1(
        human_name="Boundary campaign",
        repository_kind="existing_folder",
        repository_locator=str(repository),
        research_contract="Frozen scientific contract.\n",
        research_plan="Implement, test, and independently audit milestone M2-C.\n",
        initial_task="Implement milestone M2-C under the existing convention.\n",
    )


def token_factory() -> object:
    values = iter(("a" * 24, "b" * 24, "c" * 24, "d" * 24))
    return lambda: next(values)


def human_request(campaign_id: str, bundle_sha256: str) -> HumanActionRequestV1:
    durable = DurableStateAuthorityV1(
        authority_kind="visible_campaign",
        state_sha256="1" * 64,
        journal_sha256="2" * 64,
        journal_sequence=14,
        journal_hash="3" * 64,
        frozen_policy_sha256="4" * 64,
    )
    return HumanActionRequestV1.issue(
        campaign_public_id=campaign_id,
        input_bundle_sha256=bundle_sha256,
        stage="Implementing task-boundary",
        substage="task-boundary",
        request_id="human-000014",
        reason="Worker and Auditor disagree about the boundary-condition convention.",
        question="Choose how the qualified workflow should proceed.",
        response_type="contract_decision",
        allowed_options=[
            HumanActionOptionV1(
                option_id="continue_existing",
                label="Keep existing locked convention",
                consequence="Frozen inputs remain unchanged.",
            ).model_dump(mode="json"),
            HumanActionOptionV1(
                option_id="request_more_evidence",
                label="Request additional evidence",
                consequence="The workflow gathers evidence before another decision.",
            ).model_dump(mode="json"),
            HumanActionOptionV1(
                option_id="stop_safely",
                label="Stop safely",
                consequence="The campaign stops through core ingress.",
            ).model_dump(mode="json"),
        ],
        evidence_links=[],
        campaign_state_safe=True,
        durable_authority=durable.model_dump(mode="json"),
    )


def test_zero_shell_service_journey_duplicate_start_restart_and_notifications(
    tmp_path: Path,
) -> None:
    repository = create_repository(tmp_path)
    runner = FakeQualifiedRunner()
    custodian = CampaignCustodian(
        tmp_path / "data",
        runner=runner,
        environment_inspector=lambda _path: ready_environment(),
        token_factory=token_factory(),  # type: ignore[arg-type]
    )
    preview = custodian.preview(submission(repository))
    assert preview.repository == "source-repository"
    assert preview.immutable_after_start
    record = custodian.start(preview.preview_id, client_start_key="start_abcdefghijklmnop")
    campaign_id = record.campaign_public_id
    assert runner.started == 1
    duplicate = custodian.start(preview.preview_id, client_start_key="start_abcdefghijklmnop")
    assert duplicate.campaign_public_id == campaign_id
    assert runner.started == 1
    duplicate_other_click = custodian.start(
        preview.preview_id, client_start_key="start_qrstuvwxyzabcdef"
    )
    assert duplicate_other_click.campaign_public_id == campaign_id
    assert runner.started == 1
    assert Path(record.bundle_path).stat().st_mode & 0o222 == 0

    runner.current = projection(campaign_id, status="blocked")
    blocked = custodian.get_record(campaign_id, refresh=True)
    assert blocked.projection.status == "blocked"
    custodian.continue_campaign(campaign_id)
    assert runner.resumed == 1

    issued = human_request(campaign_id, record.bundle_sha256)
    paths = prepare_operator_exchange(custodian.exchange_root, campaign_id)
    publish_human_action_request(paths, issued)
    runner.current = projection(
        campaign_id,
        status="needs_input",
        request_sha256=issued.request_sha256,
    )
    needs_input = custodian.get_record(campaign_id, refresh=True)
    assert needs_input.projection.status == "needs_input"
    assert custodian.request(campaign_id) == issued
    custodian.respond(
        campaign_id,
        selected_option_id="continue_existing",
        response_text="Use the existing locked convention.",
    )
    assert runner.responded == 1
    completed = custodian.get_record(campaign_id, refresh=True)
    assert completed.projection.completion_verified
    notification = paths.notifications / "campaign_completed.json"
    assert json.loads(notification.read_text(encoding="utf-8"))["completion_verified"] is True
    assert custodian.export_campaign(campaign_id).is_file()

    restarted = CampaignCustodian(
        tmp_path / "data",
        runner=runner,
        environment_inspector=lambda _path: ready_environment(),
    )
    assert restarted.get_record(campaign_id, refresh=True).projection.status == "completed"


def test_setup_failure_is_plain_language_and_does_not_launch_core(tmp_path: Path) -> None:
    repository = create_repository(tmp_path)
    runner = FakeQualifiedRunner()
    issue = EnvironmentIssueV1(
        code="codex_authentication_required",
        title="Codex needs authentication",
        message="Choose Sign in. The campaign has not started.",
        action="sign_in",
        campaign_not_started=True,
    )
    environment = EnvironmentReportV1(
        ready=False,
        backend="linux",
        managed_python_ready=True,
        supervisor_package_ready=True,
        git_ready=True,
        codex_ready=True,
        codex_authenticated=False,
        isolation_ready=True,
        filesystem_ready=True,
        issues=(issue,),
    )
    custodian = CampaignCustodian(
        tmp_path / "data",
        runner=runner,
        environment_inspector=lambda _path: environment,
        token_factory=token_factory(),  # type: ignore[arg-type]
    )
    preview = custodian.preview(submission(repository))
    record = custodian.start(preview.preview_id, client_start_key="start_abcdefghijklmnop")
    assert record.projection.status == "blocked"
    assert record.projection.action_title == "Codex needs authentication"
    assert runner.started == 0
    assert not Path(record.core_authority_directory).exists()
    assert custodian.sign_in() == 910_004
    assert runner.authenticated == 1


def test_custodian_source_has_no_campaign_authority_or_direct_model_surface() -> None:
    source_path = Path("src/research_automation_supervisor/custodian.py")
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    forbidden = {
        "workflow_engine",
        "workflow_recovery",
        "replay_campaign_engine",
        "codex_adapter",
        "physics_auditor_execution",
        "physics_oracle_execution",
        "physics_benchmark_scoring",
    }
    assert not any(any(item in name for item in forbidden) for name in imported)
    text = source_path.read_text(encoding="utf-8")
    assert "state.json" not in text
    assert "journal.jsonl" not in text
    assert "execute_codex" not in text


def test_runner_environment_does_not_leak_unapproved_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SCORER_ONLY_SECRET", "hidden")
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    monkeypatch.setenv("PATH", os.environ["PATH"])
    environment = _runner_environment()
    assert "SCORER_ONLY_SECRET" not in environment
    assert "OPENAI_API_KEY" not in environment
    assert "PATH" in environment


def test_custodian_rejects_symlink_data_root(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(Exception, match="unsafe"):
        CampaignCustodian(link, runner=FakeQualifiedRunner())


def test_replaced_campaign_record_fails_closed(tmp_path: Path) -> None:
    repository = create_repository(tmp_path)
    runner = FakeQualifiedRunner()
    custodian = CampaignCustodian(
        tmp_path / "data",
        runner=runner,
        environment_inspector=lambda _path: ready_environment(),
        token_factory=token_factory(),  # type: ignore[arg-type]
    )
    preview = custodian.preview(submission(repository))
    record = custodian.start(preview.preview_id, client_start_key="start_abcdefghijklmnop")
    record_path = custodian.records / f"{record.campaign_public_id}.json"
    record_path.unlink()
    record_path.symlink_to(tmp_path / "missing")
    with pytest.raises(CustodianStateError):
        custodian.get_record(record.campaign_public_id)


def test_frozen_bundle_substitution_before_launch_fails_closed(tmp_path: Path) -> None:
    repository = create_repository(tmp_path)
    runner = FakeQualifiedRunner()
    issue = EnvironmentIssueV1(
        code="codex_authentication_required",
        title="Codex needs authentication",
        message="Sign in before launch.",
        action="sign_in",
        campaign_not_started=True,
    )
    blocked_environment = EnvironmentReportV1(
        ready=False,
        backend="linux",
        managed_python_ready=True,
        supervisor_package_ready=True,
        git_ready=True,
        codex_ready=True,
        codex_authenticated=False,
        isolation_ready=True,
        filesystem_ready=True,
        issues=(issue,),
    )
    custodian = CampaignCustodian(
        tmp_path / "data",
        runner=runner,
        environment_inspector=lambda _path: blocked_environment,
        token_factory=token_factory(),  # type: ignore[arg-type]
    )
    preview = custodian.preview(submission(repository))
    record = custodian.start(preview.preview_id, client_start_key="start_abcdefghijklmnop")
    original = CampaignInputBundleV1.model_validate(
        json.loads(Path(record.bundle_path).read_text(encoding="utf-8"))
    )
    altered = CampaignInputBundleV1.freeze(
        campaign_public_id=original.campaign_public_id,
        human_name=original.human_name,
        repository=original.repository,
        research_contract=FrozenInputFileV1.from_bytes("contract.md", b"altered contract\n"),
        research_plan=original.research_plan,
        initial_task=original.initial_task,
        supporting_files=original.supporting_files,
        requested_settings=original.requested_settings,
    )
    bundle_path = Path(record.bundle_path)
    bundle_path.unlink()
    bundle_path.write_text(altered.model_dump_json(), encoding="utf-8")
    with pytest.raises(CustodianStateError, match="no longer match"):
        custodian.continue_campaign(record.campaign_public_id)
    assert runner.started == 0


def test_record_cannot_redirect_recovery_or_fabricate_completion(tmp_path: Path) -> None:
    repository = create_repository(tmp_path)
    runner = FakeQualifiedRunner()
    custodian = CampaignCustodian(
        tmp_path / "data",
        runner=runner,
        environment_inspector=lambda _path: ready_environment(),
        token_factory=token_factory(),  # type: ignore[arg-type]
    )
    preview = custodian.preview(submission(repository))
    record = custodian.start(preview.preview_id, client_start_key="start_abcdefghijklmnop")
    record_path = custodian.records / f"{record.campaign_public_id}.json"
    redirected = record.model_copy(
        update={"core_authority_directory": str(custodian.authorities / "campaign-other000000")}
    )
    record_path.write_text(redirected.model_dump_json(), encoding="utf-8")
    with pytest.raises(CustodianStateError, match="locators were substituted"):
        custodian.continue_campaign(record.campaign_public_id)
    assert runner.resumed == 0

    record_path.write_text(record.model_dump_json(), encoding="utf-8")
    Path(record.core_authority_directory).rmdir()
    fabricated = record.model_copy(
        update={"projection": projection(record.campaign_public_id, status="completed")}
    )
    record_path.write_text(fabricated.model_dump_json(), encoding="utf-8")
    observed = custodian.get_record(record.campaign_public_id, refresh=True)
    assert observed.projection.status == "blocked"
    assert observed.projection.completion_verified is False
