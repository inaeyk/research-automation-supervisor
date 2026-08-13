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
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationError

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
from research_automation_supervisor.prelaunch_authority import (
    CampaignLaunchReferenceV1,
    CampaignLaunchRequestV1,
    CampaignLaunchSummaryV1,
    RequestedRepositoryAuthorityV1,
)
from research_automation_supervisor.safe_git import inspect_requested_repository

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

    def freeze_launch(
        self, request: CampaignLaunchRequestV1, launch_authority_root: Path
    ) -> CampaignLaunchReferenceV1: ...

    def launch_summary(
        self,
        launch_token: str,
        launch_authority_root: Path,
        campaign_public_id: str,
        launch_intent_sha256: str,
    ) -> CampaignLaunchSummaryV1: ...

    def launch_start(
        self,
        launch_token: str,
        launch_authority_root: Path,
        authority: Path,
        exchange: Path,
        log: Path,
    ) -> int: ...

    def launch_resume(self, authority: Path, exchange: Path, log: Path) -> int: ...

    def launch_response(
        self, authority: Path, exchange: Path, request_sha256: str, log: Path
    ) -> int: ...

    def status(self, authority: Path, exchange: Path) -> OperatorCampaignProjectionV1: ...

    def artifact(self, authority: Path, exchange: Path, token: str) -> tuple[str, bytes]: ...

    def export(self, authority: Path, exchange: Path, destination: Path) -> Path: ...

    def repository(self, authority: Path, exchange: Path) -> Path: ...

    def launch_authentication(self, log: Path) -> int: ...


class SubprocessQualifiedRunner:
    """Invoke only the qualified runner module, never any model or workflow directly."""

    def freeze_launch(
        self, request: CampaignLaunchRequestV1, launch_authority_root: Path
    ) -> CampaignLaunchReferenceV1:
        value = self._run_prelaunch_json(
            "freeze",
            launch_authority_root,
            ("--request-json", request.model_dump_json()),
        )
        try:
            return CampaignLaunchReferenceV1.model_validate(value)
        except ValidationError as exc:
            raise CustodianStateError(
                "Qualified Start authority returned an invalid reference."
            ) from exc

    def launch_summary(
        self,
        launch_token: str,
        launch_authority_root: Path,
        campaign_public_id: str,
        launch_intent_sha256: str,
    ) -> CampaignLaunchSummaryV1:
        value = self._run_prelaunch_json(
            "launch-summary",
            launch_authority_root,
            (
                "--launch-token",
                launch_token,
                "--expected-campaign",
                campaign_public_id,
                "--expected-intent",
                launch_intent_sha256,
            ),
        )
        try:
            return CampaignLaunchSummaryV1.model_validate(value)
        except ValidationError as exc:
            raise CustodianStateError(
                "Frozen core launch authority returned invalid status."
            ) from exc

    def __init__(self, executable: str | None = None) -> None:
        self.executable = executable or sys.executable

    def launch_start(
        self,
        launch_token: str,
        launch_authority_root: Path,
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
                "--launch-token",
                launch_token,
                "--launch-authority-root",
                str(launch_authority_root),
            ),
        )

    def launch_resume(self, authority: Path, exchange: Path, log: Path) -> int:
        return self._launch("resume", authority, exchange, log, ())

    def launch_response(
        self,
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
            ("--request", request_sha256),
        )

    def status(self, authority: Path, exchange: Path) -> OperatorCampaignProjectionV1:
        value = self._run_json("status", authority, exchange, ())
        try:
            return OperatorCampaignProjectionV1.model_validate(value)
        except ValidationError as exc:
            raise CustodianStateError(
                "Qualified status returned an invalid safe projection."
            ) from exc

    def artifact(self, authority: Path, exchange: Path, token: str) -> tuple[str, bytes]:
        value = self._run_json("artifact", authority, exchange, ("--token", token))
        media_type = value.get("media_type")
        content_base64 = value.get("content_base64")
        if not isinstance(media_type, str) or not isinstance(content_base64, str):
            raise CustodianStateError("Qualified result was unavailable.")
        try:
            content = base64.b64decode(content_base64, validate=True)
        except ValueError as exc:
            raise CustodianStateError("Qualified result was invalid.") from exc
        return media_type, content

    def export(self, authority: Path, exchange: Path, destination: Path) -> Path:
        value = self._run_json(
            "export",
            authority,
            exchange,
            ("--destination", str(destination)),
        )
        path = value.get("path")
        if not isinstance(path, str) or Path(path) != destination:
            raise CustodianStateError("Qualified export returned an invalid destination.")
        return destination

    def repository(self, authority: Path, exchange: Path) -> Path:
        value = self._run_json("repository", authority, exchange, ())
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

    def _run_prelaunch_json(
        self,
        operation: str,
        launch_authority_root: Path,
        extra: Sequence[str],
    ) -> dict[str, object]:
        command = [
            self.executable,
            "-m",
            "research_automation_supervisor.qualified_runner",
            operation,
            "--launch-authority-root",
            str(launch_authority_root),
            *extra,
        ]
        try:
            completed = subprocess.run(
                command,
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
                "Qualified Start authority is temporarily unavailable."
            ) from exc
        stream = completed.stdout if completed.returncode == 0 else completed.stderr
        try:
            value = json.loads(stream)
        except (json.JSONDecodeError, ValueError) as exc:
            raise CustodianStateError(
                "Qualified Start authority is temporarily unavailable."
            ) from exc
        if completed.returncode != 0 or not isinstance(value, dict):
            message = value.get("message") if isinstance(value, dict) else None
            raise CustodianStateError(
                message if isinstance(message, str) else "Qualified Start authority stopped safely."
            )
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
            *extra,
        ]


class CampaignCustodian:
    """Operator-facing state and repository preparation outside campaign authority."""

    def __init__(
        self,
        data_root: Path,
        *,
        runner: QualifiedRunner | None = None,
        environment_inspector: Callable[[Path], EnvironmentReportV1] = inspect_environment,
        token_factory: Callable[[], str] = lambda: secrets.token_hex(12),
        utc_now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.data_root = _safe_data_root(data_root)
        self.runner = runner or SubprocessQualifiedRunner()
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
        self.start_keys = _safe_child_directory(self.custodian_state, "start-keys")
        self.preview_starts = _safe_child_directory(self.custodian_state, "preview-starts")
        self.runner_logs = _safe_child_directory(self.custodian_state, "runner-logs")
        application_data = self.data_root.parent
        self.launch_authority_root = _safe_external_core_root(
            application_data / "research-automation-supervisor-core" / "prelaunch-authority",
            self.data_root,
        )
        self.repository_preparation_root = _safe_external_core_root(
            application_data / "research-automation-supervisor-core" / "repository-preparation",
            self.data_root,
        )

    def environment(self) -> EnvironmentReportV1:
        return self.environment_inspector(self.data_root)

    def sign_in(self) -> int:
        """Launch authentication only through the qualified runner boundary."""
        return self.runner.launch_authentication(self.runner_logs / "authentication.log")

    def preview(self, submission: WizardSubmissionV1) -> CampaignPreviewV1:
        _validate_submission_text(submission)
        repository = inspect_requested_repository(
            submission.repository_kind,
            submission.repository_locator,
            sterile_root=self.repository_preparation_root / "preview-sterile",
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
        key_path = self.start_keys / f"{key_digest}.json"
        if key_path.exists():
            value = _read_json_regular(key_path, "Start request")
            campaign_id = value.get("campaign_public_id")
            if not isinstance(campaign_id, str):
                raise CustodianStateError("Start request record is invalid.")
            return self.get_record(campaign_id, refresh=True)
        preview_digest = hashlib.sha256(preview_id.encode("ascii")).hexdigest()
        preview_start_path = self.preview_starts / f"{preview_digest}.json"
        if preview_start_path.exists():
            value = _read_json_regular(preview_start_path, "Preview Start record")
            campaign_id = value.get("campaign_public_id")
            if not isinstance(campaign_id, str):
                raise CustodianStateError("Preview Start record is invalid.")
            return self.get_record(campaign_id, refresh=True)
        for existing_path in _regular_json_files(self.records):
            existing = self.get_record(existing_path.stem, refresh=True)
            if existing.preview_id == preview_id:
                _write_once_json(
                    preview_start_path,
                    {
                        "schema_version": 1,
                        "preview_id": preview_id,
                        "campaign_public_id": existing.campaign_public_id,
                    },
                )
                return existing
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
        # This is the first authoritative Start operation. It fsyncs the exact
        # scientific bytes before Git, environment doctor, auth, or isolation checks.
        reference = self.runner.freeze_launch(launch_request, self.launch_authority_root)
        intent = self.runner.launch_summary(
            reference.launch_token,
            self.launch_authority_root,
            reference.campaign_public_id,
            reference.launch_intent_sha256,
        )
        campaign_id = reference.campaign_public_id
        exchange = prepare_operator_exchange(self.exchange_root, campaign_id)
        authority = self.authorities / campaign_id
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
            launch_intent_sha256=reference.launch_intent_sha256,
            launch_token=reference.launch_token,
            core_authority_directory=str(authority),
            exchange_directory=str(exchange.root),
            created_at=_utc_string(self.utc_now()),
            projection=projection,
        )
        self._persist_record(record)
        _write_once_json(
            preview_start_path,
            {
                "schema_version": 1,
                "preview_id": preview_id,
                "campaign_public_id": campaign_id,
            },
        )
        _write_once_json(key_path, {"schema_version": 1, "campaign_public_id": campaign_id})
        environment = self.environment()
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
            reference.launch_token,
            self.launch_authority_root,
            authority,
            self.exchange_root,
            self.runner_logs / f"{campaign_id}.log",
        )
        launched = record.model_copy(update={"runner_operation": "start", "runner_pid": pid})
        self._persist_record(launched)
        return launched

    def list_campaigns(self, *, refresh: bool = True) -> tuple[OperatorCampaignProjectionV1, ...]:
        values: list[OperatorCampaignProjectionV1] = []
        for path in _regular_json_files(self.records):
            record = self.get_record(path.stem, refresh=refresh)
            values.append(record.projection)
        return tuple(sorted(values, key=lambda item: (item.status, item.human_name.casefold())))

    def get_record(
        self, campaign_public_id: str, *, refresh: bool = True
    ) -> CustodianCampaignRecordV1:
        _validate_campaign_id(campaign_public_id)
        value = _read_json_regular(self.records / f"{campaign_public_id}.json", "Campaign card")
        try:
            record = CustodianCampaignRecordV1.model_validate(value)
        except ValidationError as exc:
            raise CustodianStateError("Campaign card is invalid.") from exc
        if record.campaign_public_id != campaign_public_id:
            raise CustodianStateError("Campaign card identity changed.")
        self._validate_record_locators(record)
        return self._refresh_record(record) if refresh else record

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
            Path(record.core_authority_directory),
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
        environment = self.environment()
        if not environment.ready:
            updated = record.model_copy(
                update={"projection": _environment_blocked_projection(intent, environment)}
            )
            self._persist_record(updated)
            return updated
        authority = Path(record.core_authority_directory)
        log = self.runner_logs / f"{campaign_public_id}.log"
        if authority.exists():
            pid = self.runner.launch_resume(authority, self.exchange_root, log)
            operation = "resume"
        else:
            pid = self.runner.launch_start(
                record.launch_token,
                self.launch_authority_root,
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
            Path(record.core_authority_directory), self.exchange_root, token
        )

    def export_campaign(self, campaign_public_id: str) -> Path:
        record = self.get_record(campaign_public_id, refresh=True)
        if not record.projection.completion_verified:
            raise CustodianInputError("Only a verified completed campaign can be exported.")
        destination = self.exports / f"{campaign_public_id}.zip"
        return self.runner.export(
            Path(record.core_authority_directory),
            self.exchange_root,
            destination,
        )

    def repository_path(self, campaign_public_id: str) -> Path:
        record = self.get_record(campaign_public_id, refresh=False)
        return self.runner.repository(Path(record.core_authority_directory), self.exchange_root)

    def _refresh_record(self, record: CustodianCampaignRecordV1) -> CustodianCampaignRecordV1:
        intent = self._validated_intent(record)
        authority = Path(record.core_authority_directory)
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
            projection = self.runner.status(authority, self.exchange_root)
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
            return self.runner.launch_summary(
                record.launch_token,
                self.launch_authority_root,
                record.campaign_public_id,
                record.launch_intent_sha256,
            )
        except Exception as exc:
            raise CustodianStateError(
                "Frozen core launch authority is missing, corrupt, stale, or belongs elsewhere."
            ) from exc

    def _validate_record_locators(self, record: CustodianCampaignRecordV1) -> None:
        campaign_id = record.campaign_public_id
        expected = (
            (Path(record.core_authority_directory), self.authorities / campaign_id),
            (Path(record.exchange_directory), self.exchange_root / campaign_id),
        )
        if any(actual != wanted for actual, wanted in expected):
            raise CustodianStateError("Campaign card locators were substituted.")

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
    return {key: value for key, value in os.environ.items() if key in allowed}


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
