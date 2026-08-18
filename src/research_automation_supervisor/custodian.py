"""Low-privilege Campaign Custodian application service.

The module deliberately has no import of workflow engines, model adapters, physics
actions, recovery implementation, or durable campaign writers. All campaign-affecting
operations cross the process-isolated qualified-runner allowlist.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import threading
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal, Protocol, cast

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationError

from research_automation_supervisor.core_authority_client import (
    DEFAULT_CORE_SOCKET,
    CoreAuthorityClient,
    UnixCoreAuthorityClient,
)
from research_automation_supervisor.core_authority_models import (
    CampaignLaunchRequestV1,
    CampaignLaunchSummaryV1,
    RequestedRepositoryAuthorityV1,
)
from research_automation_supervisor.custodian_bootstrap import inspect_environment
from research_automation_supervisor.custodian_errors import (
    CustodianEnvironmentError,
    CustodianInputError,
    CustodianStateError,
)
from research_automation_supervisor.custodian_exchange import (
    load_human_action_request,
    prepare_operator_exchange,
    publish_notification,
    store_response_upload,
    submit_human_action_response,
)
from research_automation_supervisor.custodian_models import (
    CampaignPreviewV1,
    CampaignProfileSettingsV1,
    CustodianCampaignRecordV1,
    EnvironmentReportV1,
    FrozenInputFileV1,
    HumanActionRequestV1,
    HumanActionResponseV1,
    LocalNotificationV1,
    OperatorCampaignProjectionV1,
)
from research_automation_supervisor.durable_state import atomic_write_json, render_json_bytes

MAX_WIZARD_BODY_BYTES = 64 * 1024 * 1024
_START_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{15,79}$")


class WizardSubmissionV1(BaseModel):
    """Internal draft shape accepted from the human-facing wizard."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    human_name: str = Field(min_length=1, max_length=160)
    repository_kind: Literal["existing_folder", "git_url"]
    repository_locator: str = Field(min_length=1, max_length=4096)
    research_contract: str = Field(min_length=1, max_length=2 * 1024 * 1024)
    research_plan: str = Field(min_length=1, max_length=2 * 1024 * 1024)
    initial_task: str = Field(min_length=1, max_length=2 * 1024 * 1024)
    supporting_files: Annotated[tuple[FrozenInputFileV1, ...], BeforeValidator(tuple)] = ()
    requested_settings: CampaignProfileSettingsV1 = CampaignProfileSettingsV1()


class PreviewDraftV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    preview_id: str
    submission: WizardSubmissionV1
    repository: RequestedRepositoryAuthorityV1


class QualifiedRunner(Protocol):
    """Only campaign-affecting capabilities available to the Custodian."""

    def launch_start(
        self,
        launch_intent_id: str,
        campaign_public_id: str,
        authority: Path,
        exchange: Path,
        log: Path,
    ) -> int: ...

    def launch_resume(
        self,
        launch_intent_id: str,
        campaign_public_id: str,
        authority: Path,
        exchange: Path,
        log: Path,
    ) -> int: ...

    def launch_response(
        self,
        launch_intent_id: str,
        campaign_public_id: str,
        authority: Path,
        exchange: Path,
        request_sha256: str,
        log: Path,
    ) -> int: ...

    def status(
        self, launch_intent_id: str, campaign_public_id: str, authority: Path, exchange: Path
    ) -> OperatorCampaignProjectionV1: ...

    def artifact(
        self,
        launch_intent_id: str,
        campaign_public_id: str,
        authority: Path,
        exchange: Path,
        token: str,
    ) -> tuple[str, bytes]: ...

    def export(
        self,
        launch_intent_id: str,
        campaign_public_id: str,
        authority: Path,
        exchange: Path,
        destination: Path,
    ) -> Path: ...

    def repository(
        self,
        launch_intent_id: str,
        campaign_public_id: str,
        authority: Path,
        exchange: Path,
    ) -> Path: ...

    def launch_authentication(self, log: Path) -> int: ...


class SubprocessQualifiedRunner:
    """Invoke only the qualified runner module, never any model or workflow directly."""

    def __init__(
        self,
        executable: str | None = None,
        *,
        core_socket: Path = DEFAULT_CORE_SOCKET,
    ) -> None:
        self.executable = executable or sys.executable
        self.core_socket = core_socket

    def launch_start(
        self,
        launch_intent_id: str,
        campaign_public_id: str,
        authority: Path,
        exchange: Path,
        log: Path,
    ) -> int:
        return self._launch(
            "start",
            authority,
            exchange,
            log,
            (
                "--launch-intent",
                launch_intent_id,
                "--expected-campaign",
                campaign_public_id,
            ),
        )

    def launch_resume(
        self,
        launch_intent_id: str,
        campaign_public_id: str,
        authority: Path,
        exchange: Path,
        log: Path,
    ) -> int:
        return self._launch(
            "resume",
            authority,
            exchange,
            log,
            ("--launch-intent", launch_intent_id, "--expected-campaign", campaign_public_id),
        )

    def launch_response(
        self,
        launch_intent_id: str,
        campaign_public_id: str,
        authority: Path,
        exchange: Path,
        request_sha256: str,
        log: Path,
    ) -> int:
        return self._launch(
            "respond",
            authority,
            exchange,
            log,
            (
                "--launch-intent",
                launch_intent_id,
                "--expected-campaign",
                campaign_public_id,
                "--request",
                request_sha256,
            ),
        )

    def status(
        self,
        launch_intent_id: str,
        campaign_public_id: str,
        authority: Path,
        exchange: Path,
    ) -> OperatorCampaignProjectionV1:
        value = self._run_json(
            "status",
            authority,
            exchange,
            ("--launch-intent", launch_intent_id, "--expected-campaign", campaign_public_id),
        )
        try:
            return OperatorCampaignProjectionV1.model_validate(value)
        except ValidationError as exc:
            raise CustodianStateError(
                "Qualified status returned an invalid safe projection."
            ) from exc

    def artifact(
        self,
        launch_intent_id: str,
        campaign_public_id: str,
        authority: Path,
        exchange: Path,
        token: str,
    ) -> tuple[str, bytes]:
        value = self._run_json(
            "artifact",
            authority,
            exchange,
            (
                "--launch-intent",
                launch_intent_id,
                "--expected-campaign",
                campaign_public_id,
                "--token",
                token,
            ),
        )
        media_type = value.get("media_type")
        content_base64 = value.get("content_base64")
        if not isinstance(media_type, str) or not isinstance(content_base64, str):
            raise CustodianStateError("Qualified result was unavailable.")
        try:
            content = base64.b64decode(content_base64, validate=True)
        except ValueError as exc:
            raise CustodianStateError("Qualified result was invalid.") from exc
        return media_type, content

    def export(
        self,
        launch_intent_id: str,
        campaign_public_id: str,
        authority: Path,
        exchange: Path,
        destination: Path,
    ) -> Path:
        value = self._run_json(
            "export",
            authority,
            exchange,
            (
                "--launch-intent",
                launch_intent_id,
                "--expected-campaign",
                campaign_public_id,
                "--destination",
                str(destination),
            ),
        )
        path = value.get("path")
        if not isinstance(path, str) or Path(path) != destination:
            raise CustodianStateError("Qualified export returned an invalid destination.")
        return destination

    def repository(
        self,
        launch_intent_id: str,
        campaign_public_id: str,
        authority: Path,
        exchange: Path,
    ) -> Path:
        value = self._run_json(
            "repository",
            authority,
            exchange,
            ("--launch-intent", launch_intent_id, "--expected-campaign", campaign_public_id),
        )
        path = value.get("path")
        if not isinstance(path, str):
            raise CustodianStateError("Qualified repository path was unavailable.")
        return Path(path)

    def launch_authentication(self, log: Path) -> int:
        log.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            handle = log.open("ab", buffering=0)
            process = subprocess.Popen(
                [
                    self.executable,
                    "-m",
                    "research_automation_supervisor.qualified_runner",
                    "authenticate",
                ],
                stdin=subprocess.DEVNULL,
                stdout=handle,
                stderr=subprocess.STDOUT,
                close_fds=True,
                start_new_session=True,
                env=_runner_environment(),
            )
            handle.close()
        except OSError as exc:
            raise CustodianEnvironmentError("The approved sign-in flow could not start.") from exc
        return process.pid

    def _launch(
        self,
        operation: str,
        authority: Path,
        exchange: Path,
        log: Path,
        extra: Sequence[str],
    ) -> int:
        log.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            handle = log.open("ab", buffering=0)
            process = subprocess.Popen(
                self._command(operation, authority, exchange, extra),
                stdin=subprocess.DEVNULL,
                stdout=handle,
                stderr=subprocess.STDOUT,
                close_fds=True,
                start_new_session=True,
                env=_runner_environment(),
            )
            handle.close()
        except OSError as exc:
            raise CustodianEnvironmentError(
                "The qualified campaign runner could not start."
            ) from exc
        return process.pid

    def _run_json(
        self,
        operation: str,
        authority: Path,
        exchange: Path,
        extra: Sequence[str],
    ) -> dict[str, object]:
        try:
            completed = subprocess.run(
                self._command(operation, authority, exchange, extra),
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
                env=_runner_environment(),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise CustodianStateError(
                "Qualified campaign status is temporarily unavailable."
            ) from exc
        stream = completed.stdout if completed.returncode == 0 else completed.stderr
        try:
            value = json.loads(stream)
        except (json.JSONDecodeError, ValueError) as exc:
            raise CustodianStateError(
                "Qualified campaign status is temporarily unavailable."
            ) from exc
        if completed.returncode != 0:
            message = value.get("message") if isinstance(value, dict) else None
            raise CustodianStateError(
                message if isinstance(message, str) else "Qualified campaign stopped safely."
            )
        if not isinstance(value, dict):
            raise CustodianStateError("Qualified campaign returned an invalid response.")
        return value

    def _command(
        self,
        operation: str,
        authority: Path,
        exchange: Path,
        extra: Sequence[str],
    ) -> list[str]:
        return [
            self.executable,
            "-m",
            "research_automation_supervisor.qualified_runner",
            operation,
            "--authority",
            str(authority),
            "--exchange",
            str(exchange),
            "--core-socket",
            str(self.core_socket),
            *extra,
        ]


class CampaignCustodian:
    """Operator-facing state and repository preparation outside campaign authority."""

    def __init__(
        self,
        data_root: Path,
        *,
        runner: QualifiedRunner | None = None,
        core: CoreAuthorityClient | None = None,
        core_socket: Path = DEFAULT_CORE_SOCKET,
        environment_inspector: Callable[[Path], EnvironmentReportV1] = inspect_environment,
        token_factory: Callable[[], str] = lambda: secrets.token_hex(12),
        utc_now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.data_root = _safe_data_root(data_root)
        if core is not None:
            self.core = core
        elif runner is not None and callable(getattr(runner, "configure_core_storage", None)):
            configure = cast(Callable[[Path], None], runner.configure_core_storage)  # type: ignore[attr-defined]
            configure(self.data_root.parent / ".test-core-authority")
            self.core = cast(CoreAuthorityClient, runner)
        else:
            self.core = UnixCoreAuthorityClient(core_socket)
        self.runner = runner or SubprocessQualifiedRunner(core_socket=core_socket)
        self._start_lock = threading.Lock()
        self.environment_inspector = environment_inspector
        self.token_factory = token_factory
        self.utc_now = utc_now
        self.custodian_state = _safe_child_directory(self.data_root, "custodian-state")
        self.exchange_root = _safe_child_directory(self.data_root, "operator-exchange")
        self.authorities = _safe_child_directory(self.data_root, "qualified-campaigns")
        self.exports = _safe_child_directory(self.data_root, "exports")
        self.records = _safe_child_directory(self.custodian_state, "campaigns")
        self.previews = _safe_child_directory(self.custodian_state, "previews")
        self.runner_logs = _safe_child_directory(self.custodian_state, "runner-logs")

    def environment(self, *, snapshot_complete: bool = False) -> EnvironmentReportV1:
        if self.environment_inspector is inspect_environment:
            return inspect_environment(
                self.data_root, allow_program_execution=snapshot_complete
            )
        return self.environment_inspector(self.data_root)

    def sign_in(self) -> int:
        """Launch authentication only through the qualified runner boundary."""
        return self.runner.launch_authentication(self.runner_logs / "authentication.log")

    def preview(self, submission: WizardSubmissionV1) -> CampaignPreviewV1:
        _validate_submission_text(submission)
        repository = self.core.inspect_repository(
            submission.repository_kind,
            submission.repository_locator,
        )
        preview_id = f"preview-{self.token_factory()[:24]}"
        draft = PreviewDraftV1(
            preview_id=preview_id,
            submission=submission,
            repository=repository,
        )
        _write_once_json(self.previews / f"{preview_id}.json", draft.model_dump(mode="json"))
        return CampaignPreviewV1(
            preview_id=preview_id,
            human_name=submission.human_name,
            repository=repository.source_display,
            baseline_commit_short=repository.requested_commit[:12],
            contract_sha256=hashlib.sha256(
                submission.research_contract.encode("utf-8")
            ).hexdigest(),
            research_plan_sha256=hashlib.sha256(
                submission.research_plan.encode("utf-8")
            ).hexdigest(),
            initial_task_sha256=hashlib.sha256(submission.initial_task.encode("utf-8")).hexdigest(),
            supporting_file_count=len(submission.supporting_files),
            profile_summary=_profile_summary(submission.requested_settings),
            editable_areas_summary=", ".join(submission.requested_settings.editable_areas),
            environment=self.environment(),
        )

    def start(self, preview_id: str, *, client_start_key: str) -> CustodianCampaignRecordV1:
        with self._start_lock:
            return self._start_once(preview_id, client_start_key=client_start_key)

    def _start_once(self, preview_id: str, *, client_start_key: str) -> CustodianCampaignRecordV1:
        if not _START_KEY.fullmatch(client_start_key):
            raise CustodianInputError("Start request identity is invalid.")
        key_digest = hashlib.sha256(client_start_key.encode("ascii")).hexdigest()
        draft = self._load_preview(preview_id)
        launch_request = CampaignLaunchRequestV1(
            preview_id=preview_id,
            client_start_key_sha256=key_digest,
            human_name=draft.submission.human_name,
            repository=draft.repository,
            research_contract=FrozenInputFileV1.from_bytes(
                "research-contract.md", draft.submission.research_contract.encode("utf-8")
            ),
            research_plan=FrozenInputFileV1.from_bytes(
                "research-plan.md", draft.submission.research_plan.encode("utf-8")
            ),
            initial_task=FrozenInputFileV1.from_bytes(
                "initial-task.md", draft.submission.initial_task.encode("utf-8")
            ),
            supporting_files=draft.submission.supporting_files,
            requested_settings=draft.submission.requested_settings,
        )
        # The core service imports the repository and commits the complete
        # CampaignInputBundle before returning this immutable identity.
        try:
            reference = self.core.create_start_intent(launch_request)
        except Exception as exc:
            raise CustodianStateError(str(exc) or "Core Start authority stopped safely.") from exc
        intent = self.core.verify_start_intent(
            reference.launch_intent_id,
            expected_campaign_public_id=reference.campaign_public_id,
            expected_intent_sha256=reference.launch_intent_sha256,
            expected_bundle_sha256=reference.input_bundle_sha256,
        )
        campaign_id = reference.campaign_public_id
        authority = self.authorities / campaign_id
        if authority.exists():
            return self._record_from_core(intent, refresh=True)
        exchange = prepare_operator_exchange(self.exchange_root, campaign_id)
        projection = OperatorCampaignProjectionV1(
            campaign_public_id=campaign_id,
            human_name=intent.human_name,
            repository=intent.repository_display,
            status="preparing",
            stage="Preparing campaign",
            last_activity="Checking the local environment before qualified launch",
            human_input_needed=False,
            campaign_state_safe=True,
        )
        record = CustodianCampaignRecordV1(
            campaign_public_id=campaign_id,
            preview_id=preview_id,
            launch_intent_id=reference.launch_intent_id,
            launch_intent_sha256=reference.launch_intent_sha256,
            input_bundle_sha256=reference.input_bundle_sha256,
            qualified_campaign_directory=str(authority),
            exchange_directory=str(exchange.root),
            created_at=intent.created_at,
            projection=projection,
        )
        self._persist_record(record)
        environment = self.environment(snapshot_complete=True)
        if not environment.ready:
            blocked = record.model_copy(
                update={
                    "projection": _environment_blocked_projection(intent, environment),
                    "runner_operation": "start",
                }
            )
            self._persist_record(blocked)
            return blocked
        pid = self.runner.launch_start(
            reference.launch_intent_id,
            campaign_id,
            authority,
            self.exchange_root,
            self.runner_logs / f"{campaign_id}.log",
        )
        launched = record.model_copy(update={"runner_operation": "start", "runner_pid": pid})
        self._persist_record(launched)
        return launched

    def list_campaigns(self, *, refresh: bool = True) -> tuple[OperatorCampaignProjectionV1, ...]:
        values = [
            self._record_from_core(summary, refresh=refresh).projection
            for summary in self._core_summaries()
        ]
        return tuple(sorted(values, key=lambda item: (item.status, item.human_name.casefold())))

    def get_record(
        self, campaign_public_id: str, *, refresh: bool = True
    ) -> CustodianCampaignRecordV1:
        _validate_campaign_id(campaign_public_id)
        summaries = {item.campaign_public_id: item for item in self._core_summaries()}
        summary = summaries.get(campaign_public_id)
        if summary is None:
            raise CustodianStateError("Core campaign authority is unavailable.")
        return self._record_from_core(summary, refresh=refresh)

    def _core_summaries(self) -> tuple[CampaignLaunchSummaryV1, ...]:
        try:
            return self.core.list_operator_campaigns()
        except Exception as exc:
            raise CustodianStateError("Core campaign authority is unavailable.") from exc

    def request(self, campaign_public_id: str) -> HumanActionRequestV1:
        record = self.get_record(campaign_public_id, refresh=True)
        request_sha = record.projection.active_request_sha256
        if request_sha is None:
            raise CustodianInputError("This campaign is not waiting for human input.")
        paths = prepare_operator_exchange(self.exchange_root, campaign_public_id)
        return load_human_action_request(paths, request_sha)

    def respond(
        self,
        campaign_public_id: str,
        *,
        selected_option_id: str | None,
        response_text: str,
        uploads: Sequence[tuple[str, bytes]] = (),
    ) -> CustodianCampaignRecordV1:
        record = self.get_record(campaign_public_id, refresh=True)
        request = self.request(campaign_public_id)
        if record.projection.active_request_sha256 != request.request_sha256:
            raise CustodianInputError(
                "This request is stale. Refresh the campaign before responding."
            )
        paths = prepare_operator_exchange(self.exchange_root, campaign_public_id)
        upload_models = tuple(
            store_response_upload(paths, display_name=name, content=content)
            for name, content in uploads
        )
        response = HumanActionResponseV1.bind(
            campaign_public_id=campaign_public_id,
            request_id=request.request_id,
            request_sha256=request.request_sha256,
            input_bundle_sha256=request.input_bundle_sha256,
            durable_authority=request.durable_authority.model_dump(mode="json"),
            selected_option_id=selected_option_id,
            response_text=response_text,
            uploaded_files=[item.model_dump(mode="json") for item in upload_models],
        )
        submit_human_action_response(
            paths,
            request,
            response,
            current_authority=request.durable_authority,
        )
        pid = self.runner.launch_response(
            record.launch_intent_id,
            campaign_public_id,
            Path(record.qualified_campaign_directory),
            self.exchange_root,
            request.request_sha256,
            self.runner_logs / f"{campaign_public_id}.log",
        )
        updated = record.model_copy(update={"runner_operation": "respond", "runner_pid": pid})
        self._persist_record(updated)
        return updated

    def continue_campaign(self, campaign_public_id: str) -> CustodianCampaignRecordV1:
        record = self.get_record(campaign_public_id, refresh=False)
        intent = self._validated_intent(record)
        environment = self.environment(snapshot_complete=True)
        if not environment.ready:
            updated = record.model_copy(
                update={"projection": _environment_blocked_projection(intent, environment)}
            )
            self._persist_record(updated)
            return updated
        authority = Path(record.qualified_campaign_directory)
        log = self.runner_logs / f"{campaign_public_id}.log"
        if authority.exists():
            pid = self.runner.launch_resume(
                record.launch_intent_id,
                campaign_public_id,
                authority,
                self.exchange_root,
                log,
            )
            operation = "resume"
        else:
            pid = self.runner.launch_start(
                record.launch_intent_id,
                campaign_public_id,
                authority,
                self.exchange_root,
                log,
            )
            operation = "start"
        updated = record.model_copy(update={"runner_operation": operation, "runner_pid": pid})
        self._persist_record(updated)
        return updated

    def read_artifact(self, campaign_public_id: str, token: str) -> tuple[str, bytes]:
        record = self.get_record(campaign_public_id, refresh=True)
        allowed = {item.token for item in record.projection.result_links}
        if record.projection.active_request_sha256:
            allowed.update(item.token for item in self.request(campaign_public_id).evidence_links)
        if token not in allowed:
            raise CustodianInputError("This result link is not available.")
        return self.runner.artifact(
            record.launch_intent_id,
            campaign_public_id,
            Path(record.qualified_campaign_directory),
            self.exchange_root,
            token,
        )

    def export_campaign(self, campaign_public_id: str) -> Path:
        record = self.get_record(campaign_public_id, refresh=True)
        if not record.projection.completion_verified:
            raise CustodianInputError("Only a verified completed campaign can be exported.")
        destination = self.exports / f"{campaign_public_id}.zip"
        return self.runner.export(
            record.launch_intent_id,
            campaign_public_id,
            Path(record.qualified_campaign_directory),
            self.exchange_root,
            destination,
        )

    def repository_path(self, campaign_public_id: str) -> Path:
        record = self.get_record(campaign_public_id, refresh=False)
        return self.runner.repository(
            record.launch_intent_id,
            campaign_public_id,
            Path(record.qualified_campaign_directory),
            self.exchange_root,
        )

    def _refresh_record(self, record: CustodianCampaignRecordV1) -> CustodianCampaignRecordV1:
        intent = self._validated_intent(record)
        authority = Path(record.qualified_campaign_directory)
        if not authority.exists():
            if record.projection.status == "blocked" and record.runner_pid is None:
                return record
            if record.projection.status == "preparing" and _pid_active(record.runner_pid):
                return record
            if record.projection.status != "blocked":
                stopped = record.model_copy(
                    update={
                        "projection": _verification_blocked_projection(
                            intent,
                            "qualified_runner_stopped",
                            "The qualified campaign runner stopped before campaign launch.",
                        ),
                        "runner_pid": None,
                        "runner_operation": "idle",
                    }
                )
                self._persist_record(stopped)
                return stopped
            return record
        try:
            projection = self.runner.status(
                record.launch_intent_id,
                record.campaign_public_id,
                authority,
                self.exchange_root,
            )
        except CustodianStateError:
            blocked = record.model_copy(
                update={
                    "projection": _verification_blocked_projection(
                        intent,
                        "verified_status_unavailable",
                        (
                            "Qualified campaign evidence could not be verified. "
                            "No completion or recovery was inferred."
                        ),
                    ),
                    "runner_pid": None,
                    "runner_operation": "idle",
                }
            )
            self._persist_record(blocked)
            return blocked
        if projection.campaign_public_id != record.campaign_public_id:
            raise CustodianStateError("Qualified projection belongs to another campaign.")
        updated = record.model_copy(
            update={
                "projection": projection,
                "runner_pid": record.runner_pid if _pid_active(record.runner_pid) else None,
                "runner_operation": record.runner_operation
                if _pid_active(record.runner_pid)
                else "idle",
            }
        )
        self._persist_record(updated)
        self._notify_projection(updated)
        return updated

    def _validated_intent(self, record: CustodianCampaignRecordV1) -> CampaignLaunchSummaryV1:
        try:
            return self.core.verify_start_intent(
                record.launch_intent_id,
                expected_campaign_public_id=record.campaign_public_id,
                expected_intent_sha256=record.launch_intent_sha256,
                expected_bundle_sha256=record.input_bundle_sha256,
            )
        except Exception as exc:
            raise CustodianStateError(
                "Frozen core launch authority is missing, corrupt, stale, or belongs elsewhere."
            ) from exc

    def _validate_record_locators(self, record: CustodianCampaignRecordV1) -> None:
        campaign_id = record.campaign_public_id
        expected = (
            (Path(record.qualified_campaign_directory), self.authorities / campaign_id),
            (Path(record.exchange_directory), self.exchange_root / campaign_id),
        )
        if any(actual != wanted for actual, wanted in expected):
            raise CustodianStateError("Campaign card locators were substituted.")

    def _record_from_core(
        self, summary: CampaignLaunchSummaryV1, *, refresh: bool
    ) -> CustodianCampaignRecordV1:
        snapshot_resume_failed = False
        if summary.snapshot_state != "complete":
            try:
                summary = self.core.resume_start_snapshot(summary.launch_intent_id)
            except Exception:
                snapshot_resume_failed = True
        campaign_id = summary.campaign_public_id
        exchange = prepare_operator_exchange(self.exchange_root, campaign_id)
        authority = self.authorities / campaign_id
        projection = OperatorCampaignProjectionV1(
            campaign_public_id=campaign_id,
            human_name=summary.human_name,
            repository=summary.repository_display,
            status="preparing",
            stage="Preparing campaign",
            last_activity="Checking the local environment before qualified launch",
            human_input_needed=False,
            campaign_state_safe=True,
        )
        runner_operation: Literal["start", "resume", "respond", "idle"] = "idle"
        runner_pid: int | None = None
        path = self.records / f"{campaign_id}.json"
        if path.exists() and not path.is_symlink():
            try:
                cached = CustodianCampaignRecordV1.model_validate(
                    _read_json_regular(path, "Campaign card")
                )
                bindings_match = (
                    cached.campaign_public_id == campaign_id
                    and cached.preview_id == summary.preview_id
                    and cached.launch_intent_id == summary.launch_intent_id
                    and cached.launch_intent_sha256 == summary.launch_intent_sha256
                    and cached.input_bundle_sha256 == summary.input_bundle_sha256
                    and Path(cached.qualified_campaign_directory) == authority
                    and Path(cached.exchange_directory) == exchange.root
                )
                if bindings_match and _pid_active(cached.runner_pid):
                    runner_operation = cached.runner_operation
                    runner_pid = cached.runner_pid
                elif (
                    bindings_match
                    and not authority.exists()
                    and cached.projection.status == "blocked"
                    and cached.runner_pid is None
                ):
                    # A pre-launch environment pause is non-authoritative UI state,
                    # but it must remain actionable across a Custodian restart.
                    # Continue re-verifies Core intent and environment before launch.
                    projection = cached.projection
                    runner_operation = cached.runner_operation
            except (CustodianStateError, ValidationError):
                pass
        record = CustodianCampaignRecordV1(
            campaign_public_id=campaign_id,
            preview_id=summary.preview_id,
            launch_intent_id=summary.launch_intent_id,
            launch_intent_sha256=summary.launch_intent_sha256,
            input_bundle_sha256=summary.input_bundle_sha256,
            qualified_campaign_directory=str(authority),
            exchange_directory=str(exchange.root),
            created_at=summary.created_at,
            runner_operation=runner_operation,
            runner_pid=runner_pid,
            projection=projection,
        )
        self._persist_record(record)
        if snapshot_resume_failed or summary.snapshot_state != "complete":
            blocked = record.model_copy(
                update={
                    "projection": _verification_blocked_projection(
                        summary,
                        "sanitized_snapshot_incomplete",
                        (
                            "The committed Start is intact, but its sanitized repository "
                            "snapshot is incomplete. Continue after Core service recovery."
                        ),
                    )
                }
            )
            self._persist_record(blocked)
            return blocked
        if authority.exists():
            return self._refresh_record(record) if refresh else record
        if runner_pid is not None:
            return record
        environment = self.environment(snapshot_complete=True)
        if not environment.ready:
            blocked = record.model_copy(
                update={"projection": _environment_blocked_projection(summary, environment)}
            )
            self._persist_record(blocked)
            return blocked
        return record

    def _notify_projection(self, record: CustodianCampaignRecordV1) -> None:
        projection = record.projection
        if projection.status not in {"needs_input", "blocked", "completed"}:
            return
        kind: Literal["human_input_required", "infrastructure_blocked", "campaign_completed"]
        if projection.status == "needs_input":
            kind = "human_input_required"
        elif projection.status == "blocked":
            kind = "infrastructure_blocked"
        else:
            kind = "campaign_completed"
        paths = prepare_operator_exchange(self.exchange_root, projection.campaign_public_id)
        identity = (
            f"-{projection.active_request_sha256[:16]}"
            if kind == "human_input_required" and projection.active_request_sha256
            else ""
        )
        destination = paths.notifications / f"{kind}{identity}.json"
        if destination.exists():
            return
        notification = LocalNotificationV1(
            campaign_public_id=projection.campaign_public_id,
            kind=kind,
            title=projection.action_title
            or ("Campaign completed" if projection.completion_verified else "Campaign update"),
            message=projection.action_message or projection.last_activity,
            created_at=_utc_string(self.utc_now()),
            completion_verified=projection.completion_verified,
        )
        publish_notification(paths, notification, identity_suffix=identity)

    def _load_preview(self, preview_id: str) -> PreviewDraftV1:
        if not re.fullmatch(r"preview-[a-f0-9]{12,24}", preview_id):
            raise CustodianInputError("Campaign preview identity is invalid.")
        value = _read_json_regular(self.previews / f"{preview_id}.json", "Campaign preview")
        try:
            draft = PreviewDraftV1.model_validate(value)
        except ValidationError as exc:
            raise CustodianStateError("Campaign preview is invalid.") from exc
        if draft.preview_id != preview_id:
            raise CustodianStateError("Campaign preview identity changed.")
        return draft

    def _persist_record(self, record: CustodianCampaignRecordV1) -> None:
        atomic_write_json(
            self.records / f"{record.campaign_public_id}.json",
            record.model_dump(mode="json"),
            error_factory=CustodianStateError,
            error_message="Campaign card could not be updated.",
        )


def _validate_submission_text(submission: WizardSubmissionV1) -> None:
    for value, label in (
        (submission.research_contract, "Research Contract"),
        (submission.research_plan, "Research Plan"),
        (submission.initial_task, "Initial Task"),
    ):
        if not value.strip():
            raise CustodianInputError(f"{label} must not be blank.")
        if "\x00" in value:
            raise CustodianInputError(f"{label} contains an unsupported character.")


def _profile_summary(settings: CampaignProfileSettingsV1) -> str:
    return {
        "standard": (
            "Automatically use qualified Python checks when detected; otherwise "
            "verify repository integrity."
        ),
        "python_pytest": "Run the repository's pytest suite after each Worker change.",
        "python_unittest": "Run Python unittest discovery after each Worker change.",
    }[settings.profile]


def _environment_blocked_projection(
    bundle: CampaignLaunchSummaryV1,
    environment: EnvironmentReportV1,
) -> OperatorCampaignProjectionV1:
    first = environment.issues[0]
    return OperatorCampaignProjectionV1(
        campaign_public_id=bundle.campaign_public_id,
        human_name=bundle.human_name,
        repository=bundle.repository_display,
        status="blocked",
        stage="Setup needed",
        last_activity="Campaign launch is waiting; no scientific action has started",
        human_input_needed=False,
        campaign_state_safe=True,
        action_title=first.title,
        action_message=first.message,
        technical_code=first.code,
    )


def _verification_blocked_projection(
    bundle: CampaignLaunchSummaryV1,
    code: str,
    message: str,
) -> OperatorCampaignProjectionV1:
    return OperatorCampaignProjectionV1(
        campaign_public_id=bundle.campaign_public_id,
        human_name=bundle.human_name,
        repository=bundle.repository_display,
        status="blocked",
        stage="Paused safely",
        last_activity="Qualified verification stopped before another campaign action",
        human_input_needed=False,
        campaign_state_safe=True,
        action_title="Campaign paused safely",
        action_message=message,
        technical_code=code,
    )


def _safe_data_root(path: Path) -> Path:
    try:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        absolute = Path(os.path.abspath(path))
        resolved = path.resolve(strict=True)
        status = resolved.lstat()
    except (OSError, RuntimeError, ValueError) as exc:
        raise CustodianEnvironmentError("Campaign storage is unavailable.") from exc
    if absolute != resolved or stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
        raise CustodianEnvironmentError("Campaign storage is unsafe.")
    return resolved


def _safe_child_directory(parent: Path, name: str) -> Path:
    path = parent / name
    path.mkdir(exist_ok=True, mode=0o700)
    status = path.lstat()
    if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
        raise CustodianEnvironmentError("Campaign storage contains an unsafe directory.")
    return path


def _write_once(path: Path, content: bytes) -> None:
    _validate_regular_directory(path.parent, "Record directory")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
        try:
            offset = 0
            while offset < len(content):
                offset += os.write(descriptor, content[offset:])
            os.fchmod(descriptor, 0o400)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except FileExistsError as exc:
        raise CustodianStateError("Record was already created.") from exc
    except OSError as exc:
        raise CustodianStateError("Record could not be written safely.") from exc


def _write_once_json(path: Path, value: object) -> None:
    _write_once(path, render_json_bytes(value))


def _read_json_regular(path: Path, label: str) -> dict[str, object]:
    _validate_regular_directory(path.parent, f"{label} directory")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or opened.st_size > MAX_WIZARD_BODY_BYTES:
                raise OSError
            content = os.read(descriptor, MAX_WIZARD_BODY_BYTES + 1)
        finally:
            os.close(descriptor)
        value = json.loads(content.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise CustodianStateError(f"{label} is unavailable or invalid.") from exc
    if not isinstance(value, dict):
        raise CustodianStateError(f"{label} is invalid.")
    return value


def _regular_json_files(directory: Path) -> tuple[Path, ...]:
    _validate_regular_directory(directory, "Campaign card directory")
    values: list[Path] = []
    for path in directory.iterdir():
        status = path.lstat()
        if path.suffix == ".json" and stat.S_ISREG(status.st_mode) and not path.is_symlink():
            values.append(path)
        elif path.suffix == ".json":
            raise CustodianStateError("Campaign cards contain an unsafe entry.")
    return tuple(sorted(values))


def _validate_campaign_id(value: str) -> None:
    if not re.fullmatch(r"campaign-[a-z0-9-]{12,71}", value):
        raise CustodianInputError("Campaign link is invalid.")


def _validate_regular_directory(path: Path, label: str) -> None:
    try:
        absolute = Path(os.path.abspath(path))
        resolved = path.resolve(strict=True)
        status = resolved.lstat()
    except (OSError, RuntimeError, ValueError) as exc:
        raise CustodianStateError(f"{label} is unavailable.") from exc
    if absolute != resolved or stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
        raise CustodianStateError(f"{label} is unsafe.")


def _pid_active(pid: int | None) -> bool:
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _runner_environment() -> dict[str, str]:
    allowed = {
        "PATH",
        "HOME",
        "CODEX_HOME",
        "USER",
        "LOGNAME",
        "LANG",
        "LC_ALL",
        "SHELL",
        "TMPDIR",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "VIRTUAL_ENV",
        "WSL_DISTRO_NAME",
        "WSL_INTEROP",
    }
    environment = {key: value for key, value in os.environ.items() if key in allowed}
    environment.update(
        {
            "PATH": "/usr/bin:/bin",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "safe.directory",
            "GIT_CONFIG_VALUE_0": "*",
        }
    )
    return environment


def _safe_external_core_root(path: Path, custodian_root: Path) -> Path:
    """Create a protected sibling root that is never part of Custodian state."""
    try:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        absolute = Path(os.path.abspath(path))
        resolved = path.resolve(strict=True)
        status = resolved.lstat()
        custodian = custodian_root.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise CustodianEnvironmentError("Core application storage is unavailable.") from exc
    if (
        absolute != resolved
        or stat.S_ISLNK(status.st_mode)
        or not stat.S_ISDIR(status.st_mode)
        or resolved == custodian
        or custodian in resolved.parents
        or resolved in custodian.parents
    ):
        raise CustodianEnvironmentError("Core application storage is unsafe.")
    os.chmod(resolved, 0o700)
    return resolved


def _utc_string(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
