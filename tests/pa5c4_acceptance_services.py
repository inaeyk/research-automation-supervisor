"""Deterministic PA service doubles for the real launcher/browser qualification only."""

from __future__ import annotations

import hashlib
import json
import os
import zipfile
from pathlib import Path

from research_automation_supervisor.core_authority_models import (
    CampaignLaunchReferenceV1,
    CampaignLaunchRequestV1,
    CampaignLaunchSummaryV1,
    QualifiedLaunchMaterialV1,
    RequestedRepositoryAuthorityV1,
)
from research_automation_supervisor.custodian import SubprocessQualifiedRunner
from research_automation_supervisor.custodian_exchange import (
    load_human_action_request,
    load_human_action_response,
    prepare_operator_exchange,
    publish_human_action_request,
)
from research_automation_supervisor.custodian_models import (
    CampaignResultSummaryV1,
    DurableStateAuthorityV1,
    EnvironmentIssueV1,
    EnvironmentReportV1,
    HumanActionOptionV1,
    HumanActionRequestV1,
    OperatorCampaignProjectionV1,
    SafeEvidenceLinkV1,
)
from research_automation_supervisor.durable_state import atomic_write_json
from research_automation_supervisor.prelaunch_authority import (
    consume_start_intent_for_qualified_launch,
    create_start_intent,
    get_start_intent,
    list_operator_campaigns,
    verify_start_intent,
)
from research_automation_supervisor.safe_git import inspect_requested_repository


class DeterministicEnvironment:
    """Preview ready, first Start blocked, then ready across backend restart."""

    def __init__(self, scenario_root: Path) -> None:
        self.path = scenario_root / "environment-calls.json"

    def __call__(self, _data_root: Path) -> EnvironmentReportV1:
        calls = 0
        if self.path.exists():
            value = json.loads(self.path.read_text(encoding="utf-8"))
            calls = int(value["calls"])
        calls += 1
        atomic_write_json(
            self.path,
            {"schema_version": 1, "calls": calls},
            error_factory=RuntimeError,
            error_message="acceptance environment state could not be written",
        )
        if calls == 2:
            issue = EnvironmentIssueV1(
                code="acceptance_environment_block",
                title="Local setup needs attention",
                message=(
                    "A qualification-only infrastructure interruption was injected after "
                    "the scientific inputs were frozen. Restart Research Supervisor, then Continue."
                ),
                action="install_dependency",
                campaign_not_started=True,
            )
            return EnvironmentReportV1(
                ready=False,
                backend="wsl",
                managed_python_ready=True,
                supervisor_package_ready=True,
                git_ready=True,
                codex_ready=True,
                codex_authenticated=True,
                isolation_ready=False,
                filesystem_ready=True,
                issues=(issue,),
            )
        return EnvironmentReportV1(
            ready=True,
            backend="wsl",
            managed_python_ready=True,
            supervisor_package_ready=True,
            git_ready=True,
            codex_ready=True,
            codex_authenticated=True,
            isolation_ready=True,
            filesystem_ready=True,
        )


class DeterministicCampaignRunner(SubprocessQualifiedRunner):
    """Persistent fake PA-4/5A/5C3 service behind production Custodian ingress."""

    def configure_core_storage(self, root: Path) -> None:
        self.core_root = root / "authority"
        self.snapshot_root = root / "snapshots"

    def inspect_repository(
        self, source_kind: str, locator: str
    ) -> RequestedRepositoryAuthorityV1:
        return inspect_requested_repository(  # type: ignore[arg-type]
            source_kind,
            locator,
            sterile_root=self.snapshot_root / "preview-sterile",
        )

    def create_start_intent(
        self, request: CampaignLaunchRequestV1
    ) -> CampaignLaunchReferenceV1:
        return create_start_intent(request, self.core_root, self.snapshot_root)

    def get_start_intent(self, launch_intent_id: str) -> CampaignLaunchSummaryV1:
        return get_start_intent(self.core_root, launch_intent_id)

    def list_operator_campaigns(self) -> tuple[CampaignLaunchSummaryV1, ...]:
        if not self.core_root.exists():
            return ()
        return list_operator_campaigns(self.core_root)

    def verify_start_intent(
        self,
        launch_intent_id: str,
        *,
        expected_campaign_public_id: str,
        expected_intent_sha256: str | None = None,
        expected_bundle_sha256: str | None = None,
    ) -> CampaignLaunchSummaryV1:
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
        del exchange, log
        material = self.consume_start_intent_for_qualified_launch(
            launch_intent_id,
            expected_campaign_public_id=campaign_public_id,
        )
        bundle = material.input_bundle
        authority.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._write_state(
            authority,
            {
                "phase": "recoverable_interruption",
                "campaign_public_id": material.campaign_public_id,
                "human_name": bundle.human_name,
                "repository": bundle.repository.source_display,
                "bundle_sha256": bundle.bundle_sha256,
                "launch_intent_sha256": material.launch_intent_sha256,
                "prepared_workspace": bundle.repository.prepared_workspace,
                "final_commit": bundle.repository.baseline_commit,
            },
        )
        return os.getpid()

    def launch_resume(
        self,
        launch_intent_id: str,
        campaign_public_id: str,
        authority: Path,
        exchange: Path,
        log: Path,
    ) -> int:
        del launch_intent_id, campaign_public_id, log
        state = self._state(authority)
        if state["phase"] == "recoverable_interruption":
            campaign = str(state["campaign_public_id"])
            durable = self._durable(str(state["launch_intent_sha256"]))
            request = HumanActionRequestV1.issue(
                campaign_public_id=campaign,
                input_bundle_sha256=str(state["bundle_sha256"]),
                stage="Independent evidence review",
                substage="acceptance-human-boundary",
                request_id="human-000001",
                reason="The injected Auditor requires an explicit scientific-human decision.",
                question="Continue under the exact frozen contract and research plan?",
                response_type="contract_decision",
                allowed_options=(
                    HumanActionOptionV1(
                        option_id="continue_existing",
                        label="Approve and continue frozen authority",
                        consequence="No scientific input changes.",
                    ).model_dump(mode="json"),
                    HumanActionOptionV1(
                        option_id="stop_safely",
                        label="Stop safely",
                        consequence="The campaign stops without completion.",
                    ).model_dump(mode="json"),
                ),
                evidence_links=(
                    SafeEvidenceLinkV1(
                        token=self._token(str(state["launch_intent_sha256"]), "evidence"),
                        label="Independent audit evidence",
                        description="Verified qualification-only Auditor evidence.",
                    ).model_dump(mode="json"),
                ),
                campaign_state_safe=True,
                durable_authority=durable.model_dump(mode="json"),
            )
            paths = prepare_operator_exchange(exchange, campaign)
            destination = paths.requests / f"{request.request_sha256}.json"
            if not destination.exists():
                publish_human_action_request(paths, request)
            state["phase"] = "human_action"
            state["request_sha256"] = request.request_sha256
            self._write_state(authority, state)
        return os.getpid()

    def launch_response(
        self,
        launch_intent_id: str,
        campaign_public_id: str,
        authority: Path,
        exchange: Path,
        request_sha256: str,
        log: Path,
    ) -> int:
        del launch_intent_id, campaign_public_id, log
        state = self._state(authority)
        campaign = str(state["campaign_public_id"])
        paths = prepare_operator_exchange(exchange, campaign)
        request = load_human_action_request(paths, request_sha256)
        response = load_human_action_response(paths, request)
        if (
            state["phase"] != "human_action"
            or request_sha256 != state["request_sha256"]
            or response.selected_option_id != "continue_existing"
            or response.input_bundle_sha256 != state["bundle_sha256"]
            or response.durable_authority != self._durable(str(state["launch_intent_sha256"]))
        ):
            raise RuntimeError("qualification response binding is invalid")
        state["phase"] = "completed"
        self._write_state(authority, state)
        return os.getpid()

    def status(
        self,
        launch_intent_id: str,
        campaign_public_id: str,
        authority: Path,
        exchange: Path,
    ) -> OperatorCampaignProjectionV1:
        del launch_intent_id, campaign_public_id, exchange
        state = self._state(authority)
        common: dict[str, object] = {
            "campaign_public_id": state["campaign_public_id"],
            "human_name": state["human_name"],
            "repository": state["repository"],
            "campaign_state_safe": True,
        }
        if state["phase"] == "recoverable_interruption":
            return OperatorCampaignProjectionV1.model_validate(
                {
                    **common,
                    "status": "blocked",
                    "stage": "Paused safely",
                    "last_activity": "Injected campaign interruption was recorded durably",
                    "human_input_needed": False,
                    "action_title": "Campaign interrupted safely",
                    "action_message": "Choose Continue to invoke qualified recovery and Resume.",
                    "technical_code": "acceptance_recoverable_interruption",
                }
            )
        if state["phase"] == "human_action":
            return OperatorCampaignProjectionV1.model_validate(
                {
                    **common,
                    "status": "needs_input",
                    "stage": "Independent evidence review",
                    "last_activity": "Campaign is waiting at a verified human-action boundary",
                    "human_input_needed": True,
                    "action_title": "Research Supervisor needs input",
                    "action_message": "Inspect the evidence and submit the authorized response.",
                    "active_request_sha256": state["request_sha256"],
                }
            )
        links = tuple(
            SafeEvidenceLinkV1(
                token=self._token(str(state["launch_intent_sha256"]), kind),
                label=label,
                description=description,
            ).model_dump(mode="json")
            for kind, label, description in (
                ("scientific-report", "Scientific Report", "Verified final scientific report."),
                ("worker-reports", "Worker Reports", "Worker execution reports."),
                ("auditor-reports", "Auditor Reports", "Independent Auditor reports."),
                ("changed-files", "Changed Files / Diff", "Verified repository changes."),
                ("provenance", "Provenance", "Frozen inputs and qualified execution lineage."),
            )
        )
        return OperatorCampaignProjectionV1.model_validate(
            {
                **common,
                "status": "completed",
                "stage": "Complete",
                "last_activity": "Verified durable completion and result evidence are available",
                "human_input_needed": False,
                "result": CampaignResultSummaryV1(
                    outcome="Completed with verified durable evidence",
                    final_stage="Qualified completion",
                    final_commit=str(state["final_commit"]),
                    worker_run_count=1,
                    auditor_run_count=1,
                    repair_count=0,
                    human_decision_count=1,
                    executive_summary="The real launcher/browser acceptance campaign completed.",
                ).model_dump(mode="json"),
                "result_links": links,
                "completion_verified": True,
            }
        )

    def artifact(
        self,
        launch_intent_id: str,
        campaign_public_id: str,
        authority: Path,
        exchange: Path,
        token: str,
    ) -> tuple[str, bytes]:
        del launch_intent_id, campaign_public_id, exchange
        state = self._state(authority)
        intent = str(state["launch_intent_sha256"])
        if token == self._token(intent, "evidence"):
            return "text/plain; charset=utf-8", b"Independent evidence: frozen authority intact.\n"
        completed_artifacts = {
            "scientific-report": (
                b"# Scientific Report\n\nVerified durable qualification completion.\n"
            ),
            "worker-reports": b"# Worker Reports\n\nOne deterministic Worker seam completed.\n",
            "auditor-reports": b"# Auditor Reports\n\nIndependent acceptance evidence passed.\n",
            "changed-files": b"# Changed Files / Diff\n\nNo repository changes were required.\n",
            "provenance": b"# Provenance\n\nFrozen launch authority and durable state verified.\n",
        }
        if state["phase"] == "completed":
            for kind, content in completed_artifacts.items():
                if token == self._token(intent, kind):
                    return "text/markdown; charset=utf-8", content
        raise RuntimeError("qualification artifact is not allowlisted")

    def export(
        self,
        launch_intent_id: str,
        campaign_public_id: str,
        authority: Path,
        exchange: Path,
        destination: Path,
    ) -> Path:
        del launch_intent_id, campaign_public_id, exchange
        if self._state(authority)["phase"] != "completed":
            raise RuntimeError("qualification campaign is not complete")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(destination, "x") as archive:
            archive.writestr(
                "scientific-report.md", "# Scientific Report\n\nVerified completion.\n"
            )
            archive.writestr("worker-reports.md", "# Worker Reports\n\nWorker seam complete.\n")
            archive.writestr("auditor-reports.md", "# Auditor Reports\n\nAudit seam complete.\n")
            archive.writestr("changed-files.diff", "No repository changes.\n")
            archive.writestr("provenance.md", "# Provenance\n\nFrozen authority verified.\n")
        return destination

    def repository(
        self,
        launch_intent_id: str,
        campaign_public_id: str,
        authority: Path,
        exchange: Path,
    ) -> Path:
        del launch_intent_id, campaign_public_id, exchange
        return Path(str(self._state(authority)["prepared_workspace"]))

    @staticmethod
    def _durable(intent: str) -> DurableStateAuthorityV1:
        return DurableStateAuthorityV1(
            authority_kind="visible_campaign",
            state_sha256=hashlib.sha256(f"state:{intent}".encode()).hexdigest(),
            journal_sha256=hashlib.sha256(f"journal:{intent}".encode()).hexdigest(),
            journal_sequence=1,
            journal_hash=hashlib.sha256(f"head:{intent}".encode()).hexdigest(),
            frozen_policy_sha256=hashlib.sha256(f"policy:{intent}".encode()).hexdigest(),
        )

    @staticmethod
    def _token(intent: str, kind: str) -> str:
        return hashlib.sha256(f"{intent}:{kind}".encode()).hexdigest()[:32]

    @staticmethod
    def _state(authority: Path) -> dict[str, object]:
        value = json.loads((authority / "acceptance-state.json").read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise RuntimeError("qualification state is invalid")
        return value

    @staticmethod
    def _write_state(authority: Path, value: dict[str, object]) -> None:
        atomic_write_json(
            authority / "acceptance-state.json",
            value,
            error_factory=RuntimeError,
            error_message="qualification state could not be persisted",
        )
