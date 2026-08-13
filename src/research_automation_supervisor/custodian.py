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
from urllib.parse import urlsplit

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
    CampaignInputBundleV1,
    CampaignPreviewV1,
    CampaignProfileSettingsV1,
    CustodianCampaignRecordV1,
    EnvironmentReportV1,
    FrozenInputFileV1,
    HumanActionRequestV1,
    HumanActionResponseV1,
    LocalNotificationV1,
    OperatorCampaignProjectionV1,
    RepositoryAuthorityV1,
    render_qualified_acceptance_runner,
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
    repository_root: str
    repository_display: str
    source_locator_sha256: str
    baseline_commit: str
    baseline_tree: str
    repository_id: str


class QualifiedRunner(Protocol):
    """Only campaign-affecting capabilities available to the Custodian."""

    def launch_start(self, bundle: Path, authority: Path, exchange: Path, log: Path) -> int: ...

    def launch_resume(self, authority: Path, exchange: Path, log: Path) -> int: ...

    def launch_response(
        self, authority: Path, exchange: Path, request_sha256: str, log: Path
    ) -> int: ...

    def status(self, authority: Path, exchange: Path) -> OperatorCampaignProjectionV1: ...

    def artifact(self, authority: Path, exchange: Path, token: str) -> tuple[str, bytes]: ...

    def export(self, authority: Path, exchange: Path, destination: Path) -> Path: ...

    def launch_authentication(self, log: Path) -> int: ...


class SubprocessQualifiedRunner:
    """Invoke only the qualified runner module, never any model or workflow directly."""

    def __init__(self, executable: str | None = None) -> None:
        self.executable = executable or sys.executable

    def launch_start(self, bundle: Path, authority: Path, exchange: Path, log: Path) -> int:
        return self._launch(
            "start",
            authority,
            exchange,
            log,
            ("--bundle", str(bundle)),
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
        self.repositories = _safe_child_directory(self.data_root, "managed-repositories")
        self.workspaces = _safe_child_directory(self.data_root, "workspaces")
        self.authorities = _safe_child_directory(self.data_root, "qualified-campaigns")
        self.exports = _safe_child_directory(self.data_root, "exports")
        self.records = _safe_child_directory(self.custodian_state, "campaigns")
        self.previews = _safe_child_directory(self.custodian_state, "previews")
        self.bundles = _safe_child_directory(self.custodian_state, "bundles")
        self.start_keys = _safe_child_directory(self.custodian_state, "start-keys")
        self.preview_starts = _safe_child_directory(self.custodian_state, "preview-starts")
        self.runner_logs = _safe_child_directory(self.custodian_state, "runner-logs")

    def environment(self) -> EnvironmentReportV1:
        return self.environment_inspector(self.data_root)

    def sign_in(self) -> int:
        """Launch authentication only through the qualified runner boundary."""
        return self.runner.launch_authentication(self.runner_logs / "authentication.log")

    def preview(self, submission: WizardSubmissionV1) -> CampaignPreviewV1:
        _validate_submission_text(submission)
        root, display, locator_hash = self._repository_source(submission)
        commit = _git(root, "rev-parse", "HEAD")
        tree = _git(root, "rev-parse", "HEAD^{tree}")
        preview_id = f"preview-{self.token_factory()[:24]}"
        repository_id = _repository_identifier(display, locator_hash)
        draft = PreviewDraftV1(
            preview_id=preview_id,
            submission=submission,
            repository_root=str(root),
            repository_display=display,
            source_locator_sha256=locator_hash,
            baseline_commit=commit,
            baseline_tree=tree,
            repository_id=repository_id,
        )
        _write_once_json(self.previews / f"{preview_id}.json", draft.model_dump(mode="json"))
        return CampaignPreviewV1(
            preview_id=preview_id,
            human_name=submission.human_name,
            repository=display,
            baseline_commit_short=commit[:12],
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
            return self.get_record(campaign_id, refresh=False)
        preview_digest = hashlib.sha256(preview_id.encode("ascii")).hexdigest()
        preview_start_path = self.preview_starts / f"{preview_digest}.json"
        if preview_start_path.exists():
            value = _read_json_regular(preview_start_path, "Preview Start record")
            campaign_id = value.get("campaign_public_id")
            if not isinstance(campaign_id, str):
                raise CustodianStateError("Preview Start record is invalid.")
            return self.get_record(campaign_id, refresh=False)
        for existing_path in _regular_json_files(self.records):
            existing = self.get_record(existing_path.stem, refresh=False)
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
        campaign_id = f"campaign-{self.token_factory()[:24]}"
        campaign_workspace = self.workspaces / campaign_id
        try:
            campaign_workspace.mkdir(exist_ok=False, mode=0o700)
        except OSError as exc:
            raise CustodianStateError("Campaign workspace could not be prepared.") from exc
        prepared_repository = campaign_workspace / "repository"
        self._create_detached_worktree(Path(draft.repository_root), prepared_repository, draft)
        prepared_commit = _git(prepared_repository, "rev-parse", "HEAD")
        prepared_tree = _git(prepared_repository, "rev-parse", "HEAD^{tree}")
        repository = RepositoryAuthorityV1(
            source_kind=draft.submission.repository_kind,
            source_display=draft.repository_display,
            source_locator_sha256=draft.source_locator_sha256,
            prepared_workspace=str(prepared_repository),
            baseline_commit=prepared_commit,
            baseline_tree=prepared_tree,
            repository_id=draft.repository_id,
        )
        bundle = CampaignInputBundleV1.freeze(
            campaign_public_id=campaign_id,
            human_name=draft.submission.human_name,
            repository=repository,
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
        bundle_path = self.bundles / f"{campaign_id}.json"
        _write_once(bundle_path, render_json_bytes(bundle.model_dump(mode="json")))
        exchange = prepare_operator_exchange(self.exchange_root, campaign_id)
        authority = self.authorities / campaign_id
        projection = OperatorCampaignProjectionV1(
            campaign_public_id=campaign_id,
            human_name=bundle.human_name,
            repository=bundle.repository.source_display,
            status="preparing",
            stage="Preparing campaign",
            last_activity="Checking the local environment before qualified launch",
            human_input_needed=False,
            campaign_state_safe=True,
        )
        record = CustodianCampaignRecordV1(
            campaign_public_id=campaign_id,
            preview_id=preview_id,
            bundle_path=str(bundle_path),
            bundle_sha256=bundle.bundle_sha256,
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
                    "projection": _environment_blocked_projection(bundle, environment),
                    "runner_operation": "start",
                }
            )
            self._persist_record(blocked)
            return blocked
        pid = self.runner.launch_start(
            bundle_path,
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
        environment = self.environment()
        bundle = self._validated_bundle(record)
        if not environment.ready:
            updated = record.model_copy(
                update={"projection": _environment_blocked_projection(bundle, environment)}
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
                Path(record.bundle_path), authority, self.exchange_root, log
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
        return Path(_load_bundle(Path(record.bundle_path)).repository.prepared_workspace)

    def _refresh_record(self, record: CustodianCampaignRecordV1) -> CustodianCampaignRecordV1:
        bundle = self._validated_bundle(record)
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
                            bundle,
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
                        bundle,
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

    def _validated_bundle(self, record: CustodianCampaignRecordV1) -> CampaignInputBundleV1:
        bundle = _load_bundle(Path(record.bundle_path))
        if (
            bundle.campaign_public_id != record.campaign_public_id
            or bundle.bundle_sha256 != record.bundle_sha256
            or Path(bundle.repository.prepared_workspace)
            != self.workspaces / record.campaign_public_id / "repository"
        ):
            raise CustodianStateError("Frozen campaign inputs no longer match Start authority.")
        return bundle

    def _validate_record_locators(self, record: CustodianCampaignRecordV1) -> None:
        campaign_id = record.campaign_public_id
        expected = (
            (Path(record.bundle_path), self.bundles / f"{campaign_id}.json"),
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

    def _repository_source(self, submission: WizardSubmissionV1) -> tuple[Path, str, str]:
        locator = submission.repository_locator.strip()
        locator_hash = hashlib.sha256(locator.encode("utf-8")).hexdigest()
        if submission.repository_kind == "existing_folder":
            root = _existing_repository(Path(locator))
            return root, root.name, locator_hash
        display = _safe_git_display(locator)
        destination = self.repositories / locator_hash
        if destination.exists():
            root = _existing_repository(destination)
        else:
            _clone_repository(locator, destination)
            root = _existing_repository(destination)
        return root, display, locator_hash

    def _create_detached_worktree(
        self,
        source: Path,
        destination: Path,
        draft: PreviewDraftV1,
    ) -> None:
        if _git(source, "rev-parse", "HEAD") != draft.baseline_commit:
            raise CustodianInputError(
                "Repository changed after preview. Review it again before Start."
            )
        if _git(source, "rev-parse", "HEAD^{tree}") != draft.baseline_tree:
            raise CustodianInputError(
                "Repository changed after preview. Review it again before Start."
            )
        _git(source, "worktree", "add", "--detach", str(destination), draft.baseline_commit)
        if _git(destination, "status", "--porcelain", "--untracked-files=normal"):
            raise CustodianStateError("Prepared campaign worktree is not clean.")
        support = destination / ".research-supervisor"
        support.mkdir(mode=0o700)
        (support / "acceptance.py").write_bytes(render_qualified_acceptance_runner(sys.executable))
        _git(destination, "add", "--", ".research-supervisor/acceptance.py")
        _git(
            destination,
            "-c",
            "user.name=Research Supervisor Custodian",
            "-c",
            "user.email=custodian@localhost.invalid",
            "commit",
            "-q",
            "-m",
            "chore: prepare qualified campaign acceptance",
        )
        if _git(destination, "status", "--porcelain", "--untracked-files=normal"):
            raise CustodianStateError("Prepared campaign worktree is not clean.")

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


def _safe_git_display(locator: str) -> str:
    if locator.startswith("git@"):
        if ":" not in locator or any(character.isspace() for character in locator):
            raise CustodianInputError("Git URL is invalid.")
        host, path = locator[4:].split(":", 1)
        if not host or not path or path.startswith("/") or ".." in Path(path).parts:
            raise CustodianInputError("Git URL is invalid.")
        return f"{host}/{path.removesuffix('.git')}"
    parsed = urlsplit(locator)
    if parsed.scheme not in {"https", "ssh"} or not parsed.hostname:
        raise CustodianInputError("Use an HTTPS or SSH Git URL.")
    if parsed.password is not None or (parsed.username and parsed.scheme == "https"):
        raise CustodianInputError("Git URLs must not contain credentials.")
    if ".." in Path(parsed.path).parts:
        raise CustodianInputError("Git URL is invalid.")
    return f"{parsed.hostname}{parsed.path.removesuffix('.git')}"


def _clone_repository(locator: str, destination: Path) -> None:
    try:
        completed = subprocess.run(
            ["git", "clone", "--no-tags", "--", locator, str(destination)],
            check=False,
            capture_output=True,
            text=True,
            timeout=600,
            env={**_runner_environment(), "GIT_TERMINAL_PROMPT": "0"},
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CustodianEnvironmentError("Repository could not be cloned.") from exc
    if completed.returncode != 0:
        raise CustodianEnvironmentError(
            "Repository access needs attention. Check the URL or approved Git credentials."
        )


def _existing_repository(path: Path) -> Path:
    try:
        absolute = Path(os.path.abspath(path))
        resolved = path.resolve(strict=True)
        status = resolved.lstat()
    except (OSError, RuntimeError, ValueError) as exc:
        raise CustodianInputError("Repository folder is unavailable.") from exc
    if absolute != resolved or stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
        raise CustodianInputError("Repository folder must not be a symbolic link.")
    root_text = _git(resolved, "rev-parse", "--show-toplevel")
    root = Path(root_text).resolve(strict=True)
    if root != resolved:
        raise CustodianInputError("Choose the repository's top-level folder.")
    return root


def _git(repository: Path, *arguments: str) -> str:
    try:
        completed = subprocess.run(
            ["git", "--no-optional-locks", "-C", str(repository), *arguments],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            env=_runner_environment(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CustodianEnvironmentError("Git could not inspect the selected repository.") from exc
    if completed.returncode != 0:
        raise CustodianInputError("The selected folder is not a usable Git repository.")
    return completed.stdout.strip()


def _repository_identifier(display: str, locator_hash: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "-", Path(display).name).strip("-") or "repository"
    return f"{cleaned[:60]}-{locator_hash[:12]}"[:80]


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
    bundle: CampaignInputBundleV1,
    environment: EnvironmentReportV1,
) -> OperatorCampaignProjectionV1:
    first = environment.issues[0]
    return OperatorCampaignProjectionV1(
        campaign_public_id=bundle.campaign_public_id,
        human_name=bundle.human_name,
        repository=bundle.repository.source_display,
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
    bundle: CampaignInputBundleV1,
    code: str,
    message: str,
) -> OperatorCampaignProjectionV1:
    return OperatorCampaignProjectionV1(
        campaign_public_id=bundle.campaign_public_id,
        human_name=bundle.human_name,
        repository=bundle.repository.source_display,
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


def _load_bundle(path: Path) -> CampaignInputBundleV1:
    value = _read_json_regular(path, "Frozen campaign input bundle")
    try:
        return CampaignInputBundleV1.model_validate(value)
    except ValidationError as exc:
        raise CustodianStateError("Frozen campaign input bundle is invalid.") from exc


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


def _utc_string(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
