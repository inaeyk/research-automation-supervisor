"""Strict public models for the low-privilege Campaign Custodian boundary."""

from __future__ import annotations

import base64
import binascii
import hashlib
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from research_automation_supervisor.codex_models import ModelName, ReasoningEffort
from research_automation_supervisor.durable_state import canonical_json
from research_automation_supervisor.workflow_models import (
    Identifier,
    _freeze_sequence,
    normalize_path_pattern,
)

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
PublicCampaignId = Annotated[
    str,
    Field(min_length=12, max_length=80, pattern=r"^campaign-[a-z0-9-]+$"),
]
BoundedText = Annotated[str, Field(min_length=1, max_length=16_384)]
OptionalText = Annotated[str, Field(max_length=16_384)]
StringTuple = Annotated[tuple[str, ...], BeforeValidator(_freeze_sequence)]

MAX_PRIMARY_INPUT_BYTES = 2 * 1024 * 1024
MAX_SUPPORTING_FILE_BYTES = 16 * 1024 * 1024
MAX_SUPPORTING_FILES = 20

QUALIFIED_ACCEPTANCE_RUNNER_V1 = b"""\
import os
import shutil
import subprocess
import sys
from pathlib import Path

profile = sys.argv[1] if len(sys.argv) == 2 else "invalid"
repository = Path.cwd().resolve(strict=True)
qualified_python = Path("__RAS_QUALIFIED_PYTHON__")
if profile == "python_pytest":
    inner = [str(qualified_python), "-m", "pytest", "-q"]
elif profile == "python_unittest":
    inner = [str(qualified_python), "-m", "unittest", "discover", "-s", "tests"]
elif profile == "repository_integrity":
    inner = [str(qualified_python), "-c",
             "from research_automation_supervisor.safe_git import "
             "run_repository_integrity_acceptance as run; run()"]
else:
    raise SystemExit(64)
bwrap = shutil.which("bwrap")
if bwrap is None:
    raise SystemExit(69)
command = [bwrap, "--die-with-parent", "--new-session", "--unshare-all",
           "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
           "--ro-bind", "/usr", "/usr", "--ro-bind", "/bin", "/bin"]
for system_path in ("/lib", "/lib64", "/etc"):
    if Path(system_path).exists():
        command += ["--ro-bind", system_path, system_path]
runtime_prefix = qualified_python.parent.parent
if runtime_prefix not in (Path("/usr"), Path("/usr/local")):
    command += ["--ro-bind", str(runtime_prefix), str(runtime_prefix)]
command += ["--bind", str(repository), "/workspace", "--chdir", "/workspace",
            "--setenv", "HOME", "/tmp/operator", "--setenv", "PATH", "/usr/bin:/bin",
            "--setenv", "PYTHONNOUSERSITE", "1", "--"] + inner
result = subprocess.run(command, cwd=repository, check=False,
                        env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"})
raise SystemExit(result.returncode)
"""


def render_qualified_acceptance_runner(python_executable: str) -> bytes:
    """Bind one absolute managed interpreter into the immutable acceptance runner."""
    if not python_executable.startswith("/") or any(
        character not in "/ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-"
        for character in python_executable
    ):
        raise ValueError("qualified Python executable path is unsafe")
    return QUALIFIED_ACCEPTANCE_RUNNER_V1.replace(
        b"__RAS_QUALIFIED_PYTHON__", python_executable.encode("ascii")
    )


def _self_hash(value: BaseModel, field: str) -> str:
    payload = value.model_dump(mode="json", exclude={field})
    return hashlib.sha256(canonical_json(payload)).hexdigest()


class FrozenInputFileV1(BaseModel):
    """One exact input represented portably as base64 plus its byte identity."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    display_name: Annotated[str, Field(min_length=1, max_length=255, pattern=r"^[^/\\\x00-\x1f]+$")]
    media_type: Annotated[str, Field(min_length=1, max_length=255)]
    content_base64: Annotated[str, Field(min_length=1)]
    byte_count: Annotated[int, Field(ge=1, le=MAX_SUPPORTING_FILE_BYTES)]
    sha256: Sha256

    @model_validator(mode="after")
    def validate_content(self) -> FrozenInputFileV1:
        try:
            content = base64.b64decode(self.content_base64, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("input content is not canonical base64") from exc
        if base64.b64encode(content).decode("ascii") != self.content_base64:
            raise ValueError("input content is not canonical base64")
        if len(content) != self.byte_count:
            raise ValueError("input byte count is invalid")
        if hashlib.sha256(content).hexdigest() != self.sha256:
            raise ValueError("input content hash is invalid")
        return self

    @classmethod
    def from_bytes(
        cls,
        display_name: str,
        content: bytes,
        *,
        media_type: str = "text/plain; charset=utf-8",
    ) -> FrozenInputFileV1:
        return cls(
            display_name=display_name,
            media_type=media_type,
            content_base64=base64.b64encode(content).decode("ascii"),
            byte_count=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
        )

    def content_bytes(self) -> bytes:
        return base64.b64decode(self.content_base64, validate=True)


class RepositoryAuthorityV1(BaseModel):
    """Prepared Git authority; credentials and mutable branch names are excluded."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    source_kind: Literal["existing_folder", "git_url"]
    source_display: Annotated[str, Field(min_length=1, max_length=1024)]
    source_locator_sha256: Sha256
    prepared_workspace: Annotated[str, Field(min_length=1, max_length=4096)]
    baseline_commit: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    baseline_tree: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    repository_id: Identifier


class CampaignProfileSettingsV1(BaseModel):
    """Plain wizard choices compiled into exact qualified campaign settings."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    profile: Literal["standard", "python_pytest", "python_unittest"] = "standard"
    worker_model: ModelName = "gpt-5.6-sol"
    worker_reasoning_effort: ReasoningEffort = "high"
    auditor_model: ModelName = "gpt-5.6-sol"
    auditor_reasoning_effort: ReasoningEffort = "high"
    supervisor_model: ModelName = "gpt-5.6-sol"
    supervisor_reasoning_effort: ReasoningEffort = "high"
    max_repair_rounds: Annotated[int, Field(ge=0, le=10)] = 2
    editable_areas: Annotated[
        tuple[str, ...], BeforeValidator(_freeze_sequence), Field(min_length=1, max_length=100)
    ] = ("**",)

    @field_validator("editable_areas")
    @classmethod
    def normalize_editable_areas(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(normalize_path_pattern(item) for item in value)
        if len(normalized) != len(set(normalized)):
            raise ValueError("editable areas must be unique")
        return normalized


class CampaignInputBundleV1(BaseModel):
    """Canonical, self-hashed scientific input bundle frozen at Start."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    campaign_public_id: PublicCampaignId
    human_name: Annotated[str, Field(min_length=1, max_length=160)]
    repository: RepositoryAuthorityV1
    research_contract: FrozenInputFileV1
    research_plan: FrozenInputFileV1
    initial_task: FrozenInputFileV1
    supporting_files: Annotated[
        tuple[FrozenInputFileV1, ...],
        BeforeValidator(_freeze_sequence),
        Field(max_length=MAX_SUPPORTING_FILES),
    ] = ()
    requested_settings: CampaignProfileSettingsV1
    bundle_sha256: Sha256

    @model_validator(mode="after")
    def validate_bundle(self) -> CampaignInputBundleV1:
        for item, label in (
            (self.research_contract, "research contract"),
            (self.research_plan, "research plan"),
            (self.initial_task, "initial task"),
        ):
            if item.byte_count > MAX_PRIMARY_INPUT_BYTES:
                raise ValueError(f"{label} exceeds the input size limit")
            try:
                text = item.content_bytes().decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError(f"{label} must be UTF-8 text") from exc
            if not text.strip():
                raise ValueError(f"{label} must not be blank")
        names = [item.display_name.casefold() for item in self.supporting_files]
        if len(names) != len(set(names)):
            raise ValueError("supporting file names must be unique")
        if self.bundle_sha256 != _self_hash(self, "bundle_sha256"):
            raise ValueError("campaign input bundle self-hash is invalid")
        return self

    @classmethod
    def freeze(
        cls,
        *,
        campaign_public_id: str,
        human_name: str,
        repository: RepositoryAuthorityV1,
        research_contract: FrozenInputFileV1,
        research_plan: FrozenInputFileV1,
        initial_task: FrozenInputFileV1,
        supporting_files: tuple[FrozenInputFileV1, ...] = (),
        requested_settings: CampaignProfileSettingsV1 | None = None,
    ) -> CampaignInputBundleV1:
        payload: dict[str, object] = {
            "schema_version": 1,
            "campaign_public_id": campaign_public_id,
            "human_name": human_name,
            "repository": repository.model_dump(mode="json"),
            "research_contract": research_contract.model_dump(mode="json"),
            "research_plan": research_plan.model_dump(mode="json"),
            "initial_task": initial_task.model_dump(mode="json"),
            "supporting_files": [item.model_dump(mode="json") for item in supporting_files],
            "requested_settings": (requested_settings or CampaignProfileSettingsV1()).model_dump(
                mode="json"
            ),
        }
        digest = hashlib.sha256(canonical_json(payload)).hexdigest()
        return cls.model_validate({**payload, "bundle_sha256": digest})


class DurableStateAuthorityV1(BaseModel):
    """Opaque response binding to an exact verified durable campaign head."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    authority_kind: Literal["visible_campaign", "workflow", "physics_campaign"]
    state_sha256: Sha256
    journal_sha256: Sha256
    journal_sequence: Annotated[int, Field(ge=0)]
    journal_hash: Sha256
    frozen_policy_sha256: Sha256

    def canonical_sha256(self) -> str:
        return hashlib.sha256(canonical_json(self.model_dump(mode="json"))).hexdigest()


class HumanActionOptionV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    option_id: Identifier
    label: Annotated[str, Field(min_length=1, max_length=240)]
    consequence: Annotated[str, Field(min_length=1, max_length=2048)] | None = None


class SafeEvidenceLinkV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    token: Annotated[str, Field(min_length=16, max_length=80, pattern=r"^[a-f0-9]+$")]
    label: Annotated[str, Field(min_length=1, max_length=160)]
    description: Annotated[str, Field(min_length=1, max_length=1024)]


class HumanActionRequestV1(BaseModel):
    """Core-issued, create-once question presented verbatim by the Custodian."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    campaign_public_id: PublicCampaignId
    input_bundle_sha256: Sha256
    stage: Annotated[str, Field(min_length=1, max_length=240)]
    substage: Annotated[str, Field(min_length=1, max_length=240)] | None = None
    request_id: Identifier
    reason: BoundedText
    question: BoundedText
    response_type: Literal[
        "approve_reject", "bounded_choice", "free_text", "file_upload", "contract_decision"
    ]
    allowed_options: Annotated[
        tuple[HumanActionOptionV1, ...],
        BeforeValidator(_freeze_sequence),
        Field(max_length=20),
    ] = ()
    evidence_links: Annotated[
        tuple[SafeEvidenceLinkV1, ...],
        BeforeValidator(_freeze_sequence),
        Field(max_length=20),
    ] = ()
    campaign_state_safe: bool
    durable_authority: DurableStateAuthorityV1
    request_sha256: Sha256

    @model_validator(mode="after")
    def validate_request(self) -> HumanActionRequestV1:
        if self.response_type in {"approve_reject", "bounded_choice", "contract_decision"}:
            if len(self.allowed_options) < 2:
                raise ValueError("choice requests require at least two allowed options")
        elif self.allowed_options:
            raise ValueError("non-choice requests must not contain allowed options")
        option_ids = [item.option_id for item in self.allowed_options]
        if len(option_ids) != len(set(option_ids)):
            raise ValueError("human-action option IDs must be unique")
        link_tokens = [item.token for item in self.evidence_links]
        if len(link_tokens) != len(set(link_tokens)):
            raise ValueError("evidence tokens must be unique")
        if self.request_sha256 != _self_hash(self, "request_sha256"):
            raise ValueError("human-action request self-hash is invalid")
        return self

    @classmethod
    def issue(cls, **values: object) -> HumanActionRequestV1:
        payload = {"schema_version": 1, **values}
        digest = hashlib.sha256(canonical_json(payload)).hexdigest()
        return cls.model_validate({**payload, "request_sha256": digest})


class UploadedResponseFileV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    display_name: Annotated[str, Field(min_length=1, max_length=255, pattern=r"^[^/\\\x00-\x1f]+$")]
    byte_count: Annotated[int, Field(ge=1, le=MAX_SUPPORTING_FILE_BYTES)]
    sha256: Sha256
    exchange_path: Annotated[
        str,
        Field(
            min_length=1,
            max_length=1024,
            pattern=r"^uploads/[A-Za-z0-9][A-Za-z0-9._-]{0,199}$",
        ),
    ]


class HumanActionResponseV1(BaseModel):
    """User-authored response bound to one request and durable-state authority."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    campaign_public_id: PublicCampaignId
    request_id: Identifier
    request_sha256: Sha256
    input_bundle_sha256: Sha256
    durable_authority: DurableStateAuthorityV1
    selected_option_id: Identifier | None = None
    response_text: OptionalText = ""
    uploaded_files: Annotated[
        tuple[UploadedResponseFileV1, ...],
        BeforeValidator(_freeze_sequence),
        Field(max_length=10),
    ] = ()
    response_sha256: Sha256

    @model_validator(mode="after")
    def validate_response(self) -> HumanActionResponseV1:
        if not (self.selected_option_id or self.response_text.strip() or self.uploaded_files):
            raise ValueError("human-action response is empty")
        if self.response_sha256 != _self_hash(self, "response_sha256"):
            raise ValueError("human-action response self-hash is invalid")
        paths = [item.exchange_path for item in self.uploaded_files]
        if len(paths) != len(set(paths)):
            raise ValueError("uploaded response paths must be unique")
        return self

    @classmethod
    def bind(cls, **values: object) -> HumanActionResponseV1:
        payload = {"schema_version": 1, **values}
        digest = hashlib.sha256(canonical_json(payload)).hexdigest()
        return cls.model_validate({**payload, "response_sha256": digest})


class CampaignResultSummaryV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    outcome: Annotated[str, Field(min_length=1, max_length=240)]
    final_stage: Annotated[str, Field(min_length=1, max_length=240)]
    final_commit: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")] | None = None
    worker_run_count: Annotated[int, Field(ge=0)]
    auditor_run_count: Annotated[int, Field(ge=0)]
    repair_count: Annotated[int, Field(ge=0)]
    human_decision_count: Annotated[int, Field(ge=0)]
    executive_summary: Annotated[str, Field(min_length=1, max_length=4096)]


class OperatorCampaignProjectionV1(BaseModel):
    """Progressively disclosed view; internal run and proof identities are forbidden."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    campaign_public_id: PublicCampaignId
    human_name: Annotated[str, Field(min_length=1, max_length=160)]
    repository: Annotated[str, Field(min_length=1, max_length=1024)]
    status: Literal["preparing", "running", "needs_input", "blocked", "completed"]
    stage: Annotated[str, Field(min_length=1, max_length=240)]
    last_activity: Annotated[str, Field(min_length=1, max_length=1024)]
    human_input_needed: bool
    campaign_state_safe: bool
    action_title: Annotated[str, Field(min_length=1, max_length=240)] | None = None
    action_message: Annotated[str, Field(min_length=1, max_length=2048)] | None = None
    technical_code: Identifier | None = None
    active_request_sha256: Sha256 | None = None
    result: CampaignResultSummaryV1 | None = None
    result_links: Annotated[
        tuple[SafeEvidenceLinkV1, ...], BeforeValidator(_freeze_sequence), Field(max_length=20)
    ] = ()
    completion_verified: bool = False

    @model_validator(mode="after")
    def validate_projection(self) -> OperatorCampaignProjectionV1:
        if self.human_input_needed != (self.status == "needs_input"):
            raise ValueError("operator projection input flag contradicts status")
        if self.completion_verified != (self.status == "completed"):
            raise ValueError("only verified completion may use completed status")
        if (self.status == "completed") != (self.result is not None):
            raise ValueError("completed projection requires one result summary")
        if (self.status == "needs_input") != (self.active_request_sha256 is not None):
            raise ValueError("needs-input projection requires one active request")
        return self


class CustodianCampaignRecordV1(BaseModel):
    """Replaceable non-authoritative UI locator kept in custodian-state/."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    campaign_public_id: PublicCampaignId
    preview_id: Identifier
    launch_intent_id: Annotated[
        str,
        Field(
            min_length=136,
            max_length=136,
            pattern=r"^intent_[0-9a-f]{64}_[0-9a-f]{64}$",
        ),
    ]
    launch_intent_sha256: Sha256
    input_bundle_sha256: Sha256
    qualified_campaign_directory: Annotated[str, Field(min_length=1, max_length=4096)]
    exchange_directory: Annotated[str, Field(min_length=1, max_length=4096)]
    created_at: Annotated[str, Field(min_length=1, max_length=80)]
    runner_operation: Literal["start", "resume", "respond", "idle"] = "idle"
    runner_pid: Annotated[int, Field(gt=0)] | None = None
    projection: OperatorCampaignProjectionV1


class LocalNotificationV1(BaseModel):
    """Create-once local notification derived from an operator-safe projection."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    campaign_public_id: PublicCampaignId
    kind: Literal["human_input_required", "infrastructure_blocked", "campaign_completed"]
    title: Annotated[str, Field(min_length=1, max_length=240)]
    message: Annotated[str, Field(min_length=1, max_length=1024)]
    created_at: Annotated[str, Field(min_length=1, max_length=80)]
    completion_verified: bool = False

    @model_validator(mode="after")
    def validate_completion(self) -> LocalNotificationV1:
        if self.completion_verified != (self.kind == "campaign_completed"):
            raise ValueError("completion notification requires verified completion")
        return self


class EnvironmentIssueV1(BaseModel):
    """One setup issue that safe automatic repair could not resolve."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    code: Identifier
    title: Annotated[str, Field(min_length=1, max_length=240)]
    message: Annotated[str, Field(min_length=1, max_length=2048)]
    action: Literal["sign_in", "install_dependency", "request_admin", "review_repository"]
    campaign_not_started: bool


class EnvironmentReportV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    ready: bool
    backend: Literal["wsl", "linux"]
    managed_python_ready: bool
    supervisor_package_ready: bool
    git_ready: bool
    codex_ready: bool
    codex_authenticated: bool
    isolation_ready: bool
    filesystem_ready: bool
    issues: Annotated[
        tuple[EnvironmentIssueV1, ...],
        BeforeValidator(_freeze_sequence),
        Field(max_length=20),
    ] = ()

    @model_validator(mode="after")
    def validate_readiness(self) -> EnvironmentReportV1:
        checks = (
            self.managed_python_ready,
            self.supervisor_package_ready,
            self.git_ready,
            self.codex_ready,
            self.codex_authenticated,
            self.isolation_ready,
            self.filesystem_ready,
        )
        if self.ready != (all(checks) and not self.issues):
            raise ValueError("environment readiness contradicts its checks")
        return self


class CampaignPreviewV1(BaseModel):
    """Plain-language, pre-Start summary; it is not durable campaign authority."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    preview_id: Identifier
    human_name: Annotated[str, Field(min_length=1, max_length=160)]
    repository: Annotated[str, Field(min_length=1, max_length=1024)]
    baseline_commit_short: Annotated[str, Field(pattern=r"^[0-9a-f]{12}$")]
    contract_sha256: Sha256
    research_plan_sha256: Sha256
    initial_task_sha256: Sha256
    supporting_file_count: Annotated[int, Field(ge=0, le=MAX_SUPPORTING_FILES)]
    profile_summary: Annotated[str, Field(min_length=1, max_length=1024)]
    editable_areas_summary: Annotated[str, Field(min_length=1, max_length=2048)]
    immutable_after_start: Literal[True] = True
    environment: EnvironmentReportV1
