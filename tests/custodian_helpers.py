from __future__ import annotations

import subprocess
from pathlib import Path

from research_automation_supervisor.core_authority_models import (
    CampaignLaunchReferenceV1,
    CampaignLaunchRequestV1,
    CampaignLaunchSummaryV1,
    QualifiedLaunchMaterialV1,
    RequestedRepositoryAuthorityV1,
)
from research_automation_supervisor.custodian_models import (
    CampaignResultSummaryV1,
    EnvironmentReportV1,
    OperatorCampaignProjectionV1,
)
from research_automation_supervisor.prelaunch_authority import (
    consume_start_intent_for_qualified_launch,
    create_start_intent,
    get_start_intent,
    list_operator_campaigns,
    resume_start_snapshot,
    verify_start_intent,
)
from research_automation_supervisor.safe_git import inspect_requested_repository


def git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def create_repository(root: Path) -> Path:
    repository = root / "source-repository"
    repository.mkdir()
    (repository / "README.md").write_text("# Research repository\n", encoding="utf-8")
    git(repository, "init", "-q")
    git(repository, "config", "user.email", "custodian-tests@example.invalid")
    git(repository, "config", "user.name", "Custodian Tests")
    git(repository, "add", ".")
    git(repository, "commit", "-q", "-m", "baseline")
    return repository


def ready_environment() -> EnvironmentReportV1:
    return EnvironmentReportV1(
        ready=True,
        backend="linux",
        managed_python_ready=True,
        supervisor_package_ready=True,
        git_ready=True,
        codex_ready=True,
        codex_authenticated=True,
        isolation_ready=True,
        filesystem_ready=True,
    )


def projection(
    campaign_id: str,
    *,
    status: str = "running",
    request_sha256: str | None = None,
) -> OperatorCampaignProjectionV1:
    common: dict[str, object] = {
        "campaign_public_id": campaign_id,
        "human_name": "Boundary campaign",
        "repository": "source-repository",
        "status": status,
        "stage": "Implementing task-boundary",
        "last_activity": "Auditor reviewing Worker changes",
        "human_input_needed": status == "needs_input",
        "campaign_state_safe": True,
        "active_request_sha256": request_sha256,
        "completion_verified": status == "completed",
    }
    if status == "completed":
        common["stage"] = "Complete"
        common["last_activity"] = "Qualified completion evidence was verified"
        common["result"] = CampaignResultSummaryV1(
            outcome="Completed with verified durable evidence",
            final_stage="Qualified completion",
            worker_run_count=2,
            auditor_run_count=2,
            repair_count=1,
            human_decision_count=1,
            executive_summary="The campaign completed under frozen authority.",
        ).model_dump(mode="json")
    elif status == "blocked":
        common.update(
            {
                "stage": "Paused safely",
                "action_title": "Campaign paused safely",
                "action_message": "Qualified recovery could not prove continuation safe.",
                "technical_code": "unsafe_recovery_blocked",
            }
        )
    elif status == "needs_input":
        common.update(
            {
                "action_title": "Research Supervisor needs input",
                "action_message": "A scientific convention needs human authority.",
            }
        )
    return OperatorCampaignProjectionV1.model_validate(common)


class FakeQualifiedRunner:
    def __init__(self) -> None:
        self.current: OperatorCampaignProjectionV1 | None = None
        self.started = 0
        self.resumed = 0
        self.responded = 0
        self.authenticated = 0
        self.core_root: Path | None = None
        self.snapshot_root: Path | None = None

    def configure_core_storage(self, root: Path) -> None:
        self.core_root = root / "authority"
        self.snapshot_root = root / "snapshots"

    def inspect_repository(
        self,
        source_kind: str,
        locator: str,
    ) -> RequestedRepositoryAuthorityV1:
        assert self.snapshot_root is not None
        assert source_kind in {"existing_folder", "git_url"}
        return inspect_requested_repository(  # type: ignore[arg-type]
            source_kind,
            locator,
            sterile_root=self.snapshot_root / "preview-sterile",
        )

    def create_start_intent(self, request: CampaignLaunchRequestV1) -> CampaignLaunchReferenceV1:
        assert self.core_root is not None and self.snapshot_root is not None
        return create_start_intent(request, self.core_root, self.snapshot_root)

    def get_start_intent(self, launch_intent_id: str) -> CampaignLaunchSummaryV1:
        assert self.core_root is not None
        return get_start_intent(self.core_root, launch_intent_id)

    def list_operator_campaigns(self) -> tuple[CampaignLaunchSummaryV1, ...]:
        assert self.core_root is not None
        return list_operator_campaigns(self.core_root)

    def resume_start_snapshot(self, launch_intent_id: str) -> CampaignLaunchSummaryV1:
        assert self.core_root is not None and self.snapshot_root is not None
        return resume_start_snapshot(self.core_root, self.snapshot_root, launch_intent_id)

    def verify_start_intent(
        self,
        launch_intent_id: str,
        *,
        expected_campaign_public_id: str,
        expected_intent_sha256: str | None = None,
        expected_bundle_sha256: str | None = None,
    ) -> CampaignLaunchSummaryV1:
        assert self.core_root is not None
        return verify_start_intent(
            self.core_root,
            launch_intent_id,
            expected_campaign_public_id=expected_campaign_public_id,
            expected_intent_sha256=expected_intent_sha256,
            expected_bundle_sha256=expected_bundle_sha256,
        )

    def consume_start_intent_for_qualified_launch(
        self, launch_intent_id: str, *, expected_campaign_public_id: str
    ) -> QualifiedLaunchMaterialV1:
        assert self.core_root is not None
        return consume_start_intent_for_qualified_launch(
            self.core_root,
            launch_intent_id,
            expected_campaign_public_id=expected_campaign_public_id,
        )

    def launch_start(
        self,
        launch_intent_id: str,
        campaign_public_id: str,
        authority: Path,
        exchange: Path,
        log: Path,
    ) -> int:
        del launch_intent_id, campaign_public_id, exchange, log
        self.started += 1
        authority.mkdir(parents=True, exist_ok=True)
        campaign_id = authority.name
        self.current = projection(campaign_id)
        return 910_001

    def launch_resume(
        self,
        launch_intent_id: str,
        campaign_public_id: str,
        authority: Path,
        exchange: Path,
        log: Path,
    ) -> int:
        del launch_intent_id, campaign_public_id, authority, exchange, log
        self.resumed += 1
        if self.current is not None:
            self.current = projection(self.current.campaign_public_id)
        return 910_002

    def launch_response(
        self,
        launch_intent_id: str,
        campaign_public_id: str,
        authority: Path,
        exchange: Path,
        request_sha256: str,
        log: Path,
    ) -> int:
        del launch_intent_id, campaign_public_id, authority, exchange, request_sha256, log
        self.responded += 1
        assert self.current is not None
        self.current = projection(self.current.campaign_public_id, status="completed")
        return 910_003

    def status(
        self,
        launch_intent_id: str,
        campaign_public_id: str,
        authority: Path,
        exchange: Path,
    ) -> OperatorCampaignProjectionV1:
        del launch_intent_id, campaign_public_id, authority, exchange
        assert self.current is not None
        return self.current

    def artifact(
        self,
        launch_intent_id: str,
        campaign_public_id: str,
        authority: Path,
        exchange: Path,
        token: str,
    ) -> tuple[str, bytes]:
        del launch_intent_id, campaign_public_id, authority, exchange, token
        return "text/plain; charset=utf-8", b"verified report\n"

    def export(
        self,
        launch_intent_id: str,
        campaign_public_id: str,
        authority: Path,
        exchange: Path,
        destination: Path,
    ) -> Path:
        del launch_intent_id, campaign_public_id, authority, exchange
        destination.write_bytes(b"PK\x05\x06" + b"\x00" * 18)
        return destination

    def repository(
        self,
        launch_intent_id: str,
        campaign_public_id: str,
        authority: Path,
        exchange: Path,
    ) -> Path:
        del launch_intent_id, campaign_public_id, exchange
        return authority.parent / "repository"

    def launch_authentication(self, log: Path) -> int:
        del log
        self.authenticated += 1
        return 910_004
