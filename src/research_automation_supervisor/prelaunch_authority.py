"""Core-owned, crash-durable authority for scientific inputs frozen at Start.

The Campaign Custodian may invoke these entrypoints and retain the opaque return
reference.  It never receives a writable path to, or rewrites, the authoritative
scientific bytes held here.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from research_automation_supervisor.custodian_errors import QualifiedCampaignInputError
from research_automation_supervisor.custodian_models import (
    CampaignInputBundleV1,
    CampaignProfileSettingsV1,
    FrozenInputFileV1,
    RepositoryAuthorityV1,
)
from research_automation_supervisor.durable_state import canonical_json, fsync_directory

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
CommitId = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
LAUNCH_TOKEN = re.compile(r"^launch_([0-9a-f]{64})_([0-9a-f]{64})$")
MAX_AUTHORITY_BYTES = 64 * 1024 * 1024
ModelT = TypeVar("ModelT", bound=BaseModel)


def _self_hash(value: BaseModel, field: str) -> str:
    return hashlib.sha256(
        canonical_json(value.model_dump(mode="json", exclude={field}))
    ).hexdigest()


class RequestedRepositoryAuthorityV1(BaseModel):
    """Exact repository authority requested by the human before Start."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    source_kind: Literal["existing_folder", "git_url"]
    source_display: Annotated[str, Field(min_length=1, max_length=1024)]
    source_locator: Annotated[str, Field(min_length=1, max_length=4096)]
    source_locator_sha256: Sha256
    requested_commit: CommitId
    requested_tree: CommitId | None = None
    repository_id: Annotated[
        str, Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
    ]

    @model_validator(mode="after")
    def validate_locator(self) -> RequestedRepositoryAuthorityV1:
        digest = hashlib.sha256(self.source_locator.encode("utf-8")).hexdigest()
        if digest != self.source_locator_sha256:
            raise ValueError("requested repository locator hash is invalid")
        return self


class CampaignLaunchRequestV1(BaseModel):
    """Validated Start payload crossing from the Custodian into trusted core."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    preview_id: Annotated[str, Field(pattern=r"^preview-[a-f0-9]{12,24}$")]
    client_start_key_sha256: Sha256
    human_name: Annotated[str, Field(min_length=1, max_length=160)]
    repository: RequestedRepositoryAuthorityV1
    research_contract: FrozenInputFileV1
    research_plan: FrozenInputFileV1
    initial_task: FrozenInputFileV1
    supporting_files: tuple[FrozenInputFileV1, ...] = ()
    requested_settings: CampaignProfileSettingsV1

    @model_validator(mode="after")
    def validate_primary_inputs(self) -> CampaignLaunchRequestV1:
        # CampaignInputBundleV1 performs the same checks after repository preparation.
        for item, label in (
            (self.research_contract, "research contract"),
            (self.research_plan, "research plan"),
            (self.initial_task, "initial task"),
        ):
            try:
                text = item.content_bytes().decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError(f"{label} must be UTF-8 text") from exc
            if not text.strip():
                raise ValueError(f"{label} must not be blank")
        names = [item.display_name.casefold() for item in self.supporting_files]
        if len(names) != len(set(names)):
            raise ValueError("supporting file names must be unique")
        return self


class CampaignLaunchIntentV1(BaseModel):
    """Content-addressed canonical identity of every Start-time scientific input."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    campaign_public_id: Annotated[
        str, Field(min_length=12, max_length=80, pattern=r"^campaign-[a-z0-9-]+$")
    ]
    preview_id: str
    human_name: str
    repository: RequestedRepositoryAuthorityV1
    research_contract: FrozenInputFileV1
    research_plan: FrozenInputFileV1
    initial_task: FrozenInputFileV1
    supporting_files: tuple[FrozenInputFileV1, ...] = ()
    requested_settings: CampaignProfileSettingsV1
    intent_sha256: Sha256

    @model_validator(mode="after")
    def validate_identity(self) -> CampaignLaunchIntentV1:
        if self.intent_sha256 != _self_hash(self, "intent_sha256"):
            raise ValueError("launch intent self-hash is invalid")
        return self


class CampaignLaunchReceiptV1(BaseModel):
    """Atomic commit record binding an opaque token to one persisted intent."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    campaign_public_id: str
    launch_intent_sha256: Sha256
    launch_token_sha256: Sha256
    first_start_request_sha256: Sha256
    created_at: Annotated[str, Field(min_length=20, max_length=40)]
    receipt_sha256: Sha256

    @model_validator(mode="after")
    def validate_receipt(self) -> CampaignLaunchReceiptV1:
        if self.receipt_sha256 != _self_hash(self, "receipt_sha256"):
            raise ValueError("launch receipt self-hash is invalid")
        return self


class CampaignLaunchReferenceV1(BaseModel):
    """Only Start result retained by replaceable Custodian state."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    campaign_public_id: str
    launch_intent_sha256: Sha256
    launch_token: Annotated[str, Field(min_length=136, max_length=136)]


class CampaignLaunchSummaryV1(BaseModel):
    """Non-scientific projection returned across the core/Custodian boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    campaign_public_id: str
    launch_intent_sha256: Sha256
    human_name: Annotated[str, Field(min_length=1, max_length=160)]
    repository_display: Annotated[str, Field(min_length=1, max_length=1024)]


class FrozenCampaignInputV1(BaseModel):
    """Prepared PA-5C3 bundle derived only from a frozen launch intent."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    launch_intent_sha256: Sha256
    repository_preparation_sha256: Sha256
    input_bundle: CampaignInputBundleV1
    frozen_input_sha256: Sha256

    @model_validator(mode="after")
    def validate_identity(self) -> FrozenCampaignInputV1:
        if self.frozen_input_sha256 != _self_hash(self, "frozen_input_sha256"):
            raise ValueError("frozen campaign input self-hash is invalid")
        return self


def freeze_launch_intent(
    request: CampaignLaunchRequestV1,
    authority_root: Path,
    *,
    now: datetime | None = None,
) -> CampaignLaunchReferenceV1:
    """Atomically commit exact Start bytes before environment or repository work."""
    root = _authority_root(authority_root)
    secret = _store_secret(root)
    preview_digest = hashlib.sha256(request.preview_id.encode("ascii")).hexdigest()
    campaign_digest = hmac.new(secret, f"campaign:{preview_digest}".encode("ascii"), "sha256")
    campaign_id = f"campaign-{campaign_digest.hexdigest()[:24]}"
    payload = {
        "schema_version": 1,
        "campaign_public_id": campaign_id,
        "preview_id": request.preview_id,
        "human_name": request.human_name,
        "repository": request.repository.model_dump(mode="json"),
        "research_contract": request.research_contract.model_dump(mode="json"),
        "research_plan": request.research_plan.model_dump(mode="json"),
        "initial_task": request.initial_task.model_dump(mode="json"),
        "supporting_files": tuple(
            item.model_dump(mode="json") for item in request.supporting_files
        ),
        "requested_settings": request.requested_settings.model_dump(mode="json"),
    }
    intent_digest = hashlib.sha256(canonical_json(payload)).hexdigest()
    intent = CampaignLaunchIntentV1.model_validate({**payload, "intent_sha256": intent_digest})
    mac = hmac.new(secret, f"launch:{intent_digest}".encode("ascii"), "sha256").hexdigest()
    token = f"launch_{intent_digest}_{mac}"
    receipt_payload = {
        "schema_version": 1,
        "campaign_public_id": campaign_id,
        "launch_intent_sha256": intent_digest,
        "launch_token_sha256": hashlib.sha256(token.encode("ascii")).hexdigest(),
        "first_start_request_sha256": request.client_start_key_sha256,
        "created_at": (now or datetime.now(UTC)).astimezone(UTC).isoformat().replace("+00:00", "Z"),
    }
    receipt = CampaignLaunchReceiptV1.model_validate(
        {
            **receipt_payload,
            "receipt_sha256": hashlib.sha256(canonical_json(receipt_payload)).hexdigest(),
        }
    )
    object_path = root / "objects" / intent_digest[:2] / f"{intent_digest}.json"
    receipt_path = root / "receipts" / f"{intent_digest}.json"
    if receipt_path.exists():
        # Once committed, never reconstruct a deleted/replaced object from the
        # replaceable wizard preview. Verification must use core authority only.
        load_launch_intent(
            root,
            token,
            expected_campaign_public_id=campaign_id,
            expected_intent_sha256=intent_digest,
        )
        return CampaignLaunchReferenceV1(
            campaign_public_id=campaign_id,
            launch_intent_sha256=intent_digest,
            launch_token=token,
        )
    _write_or_verify(object_path, intent.model_dump(mode="json"), "launch intent")
    # The receipt is the commit point. The object and its directory were fsynced first.
    _write_or_verify(receipt_path, receipt.model_dump(mode="json"), "launch receipt")
    return CampaignLaunchReferenceV1(
        campaign_public_id=campaign_id,
        launch_intent_sha256=intent_digest,
        launch_token=token,
    )


def load_launch_intent(
    authority_root: Path,
    launch_token: str,
    *,
    expected_campaign_public_id: str | None = None,
    expected_intent_sha256: str | None = None,
) -> CampaignLaunchIntentV1:
    """Verify token, receipt, content address, and optional campaign binding."""
    match = LAUNCH_TOKEN.fullmatch(launch_token)
    if match is None:
        raise QualifiedCampaignInputError("launch token is stale or invalid")
    intent_digest, supplied_mac = match.groups()
    root = _existing_authority_root(authority_root)
    expected_mac = hmac.new(
        _read_store_secret(root), f"launch:{intent_digest}".encode("ascii"), "sha256"
    ).hexdigest()
    if not hmac.compare_digest(supplied_mac, expected_mac):
        raise QualifiedCampaignInputError("launch token is stale or invalid")
    receipt = _read_model(
        root / "receipts" / f"{intent_digest}.json",
        CampaignLaunchReceiptV1,
        "frozen launch receipt",
    )
    token_digest = hashlib.sha256(launch_token.encode("ascii")).hexdigest()
    if receipt.launch_intent_sha256 != intent_digest or not hmac.compare_digest(
        receipt.launch_token_sha256, token_digest
    ):
        raise QualifiedCampaignInputError("frozen launch receipt is corrupt")
    intent = _read_model(
        root / "objects" / intent_digest[:2] / f"{intent_digest}.json",
        CampaignLaunchIntentV1,
        "frozen launch intent",
    )
    if (
        intent.intent_sha256 != intent_digest
        or intent.campaign_public_id != receipt.campaign_public_id
    ):
        raise QualifiedCampaignInputError("frozen launch authority is corrupt")
    if (
        expected_campaign_public_id is not None
        and intent.campaign_public_id != expected_campaign_public_id
    ):
        raise QualifiedCampaignInputError("launch token belongs to another campaign")
    if expected_intent_sha256 is not None and intent.intent_sha256 != expected_intent_sha256:
        raise QualifiedCampaignInputError("launch intent identity was substituted")
    return intent


def load_launch_summary(
    authority_root: Path,
    launch_token: str,
    *,
    expected_campaign_public_id: str,
    expected_intent_sha256: str,
) -> CampaignLaunchSummaryV1:
    intent = load_launch_intent(
        authority_root,
        launch_token,
        expected_campaign_public_id=expected_campaign_public_id,
        expected_intent_sha256=expected_intent_sha256,
    )
    return CampaignLaunchSummaryV1(
        campaign_public_id=intent.campaign_public_id,
        launch_intent_sha256=intent.intent_sha256,
        human_name=intent.human_name,
        repository_display=intent.repository.source_display,
    )


def seal_frozen_campaign_input(
    authority_root: Path,
    intent: CampaignLaunchIntentV1,
    repository: RepositoryAuthorityV1,
    repository_preparation_sha256: str,
) -> FrozenCampaignInputV1:
    """Create or verify the exact PA-5C3 bundle derived from core authority."""
    bundle = CampaignInputBundleV1.freeze(
        campaign_public_id=intent.campaign_public_id,
        human_name=intent.human_name,
        repository=repository,
        research_contract=intent.research_contract,
        research_plan=intent.research_plan,
        initial_task=intent.initial_task,
        supporting_files=intent.supporting_files,
        requested_settings=intent.requested_settings,
    )
    payload = {
        "schema_version": 1,
        "launch_intent_sha256": intent.intent_sha256,
        "repository_preparation_sha256": repository_preparation_sha256,
        "input_bundle": bundle.model_dump(mode="json"),
    }
    frozen = FrozenCampaignInputV1.model_validate(
        {**payload, "frozen_input_sha256": hashlib.sha256(canonical_json(payload)).hexdigest()}
    )
    root = _existing_authority_root(authority_root)
    _write_or_verify(
        root / "frozen-inputs" / f"{intent.intent_sha256}.json",
        frozen.model_dump(mode="json"),
        "frozen campaign input",
    )
    return frozen


def load_frozen_campaign_input(
    authority_root: Path, intent: CampaignLaunchIntentV1
) -> FrozenCampaignInputV1 | None:
    path = (
        _existing_authority_root(authority_root) / "frozen-inputs" / f"{intent.intent_sha256}.json"
    )
    if not path.exists():
        return None
    frozen = _read_model(path, FrozenCampaignInputV1, "frozen campaign input")
    if (
        frozen.launch_intent_sha256 != intent.intent_sha256
        or frozen.input_bundle.campaign_public_id != intent.campaign_public_id
        or frozen.input_bundle.human_name != intent.human_name
        or frozen.input_bundle.research_contract != intent.research_contract
        or frozen.input_bundle.research_plan != intent.research_plan
        or frozen.input_bundle.initial_task != intent.initial_task
        or frozen.input_bundle.supporting_files != intent.supporting_files
        or frozen.input_bundle.requested_settings != intent.requested_settings
    ):
        raise QualifiedCampaignInputError("frozen campaign input does not match launch intent")
    return frozen


def _authority_root(path: Path) -> Path:
    try:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        resolved = path.resolve(strict=True)
        if resolved != Path(os.path.abspath(path)) or stat.S_ISLNK(resolved.lstat().st_mode):
            raise OSError
        os.chmod(resolved, 0o700)
        for name in ("objects", "receipts", "frozen-inputs"):
            child = resolved / name
            child.mkdir(exist_ok=True, mode=0o700)
            if stat.S_ISLNK(child.lstat().st_mode) or not stat.S_ISDIR(child.lstat().st_mode):
                raise OSError
        fsync_directory(resolved)
        return resolved
    except (OSError, RuntimeError, ValueError) as exc:
        raise QualifiedCampaignInputError("core launch authority store is unavailable") from exc


def _existing_authority_root(path: Path) -> Path:
    try:
        absolute = Path(os.path.abspath(path))
        resolved = path.resolve(strict=True)
        status = resolved.lstat()
    except (OSError, RuntimeError, ValueError) as exc:
        raise QualifiedCampaignInputError("core launch authority store is unavailable") from exc
    if absolute != resolved or stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
        raise QualifiedCampaignInputError("core launch authority store is unsafe")
    return resolved


def _store_secret(root: Path) -> bytes:
    path = root / "store-key-v1"
    if path.exists():
        return _read_store_secret(root)
    value = secrets.token_bytes(32)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
        try:
            os.write(descriptor, value)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        fsync_directory(root)
    except FileExistsError:
        return _read_store_secret(root)
    except OSError as exc:
        raise QualifiedCampaignInputError("core launch authority key could not be created") from exc
    return value


def _read_store_secret(root: Path) -> bytes:
    content = _read_regular(root / "store-key-v1", "core launch authority key", max_bytes=32)
    if len(content) != 32:
        raise QualifiedCampaignInputError("core launch authority key is invalid")
    return content


def _write_or_verify(path: Path, value: object, label: str) -> None:
    content = canonical_json(value)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _validate_directory(path.parent, f"{label} directory")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o400)
        try:
            offset = 0
            while offset < len(content):
                offset += os.write(descriptor, content[offset:])
            os.fchmod(descriptor, 0o400)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        fsync_directory(path.parent)
    except FileExistsError:
        observed = _read_regular(path, label, max_bytes=MAX_AUTHORITY_BYTES)
        if observed != content:
            raise QualifiedCampaignInputError(f"{label} was replaced") from None
    except OSError as exc:
        raise QualifiedCampaignInputError(f"{label} could not be committed") from exc


def _read_regular(path: Path, label: str, *, max_bytes: int) -> bytes:
    _validate_directory(path.parent, f"{label} directory")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            status = os.fstat(descriptor)
            if not stat.S_ISREG(status.st_mode) or status.st_size > max_bytes:
                raise OSError
            chunks: list[bytes] = []
            remaining = max_bytes + 1
            while remaining:
                chunk = os.read(descriptor, min(64 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise QualifiedCampaignInputError(f"{label} is missing or unsafe") from exc
    content = b"".join(chunks)
    if len(content) > max_bytes:
        raise QualifiedCampaignInputError(f"{label} is too large")
    return content


def _validate_directory(path: Path, label: str) -> None:
    try:
        absolute = Path(os.path.abspath(path))
        resolved = path.resolve(strict=True)
        status = resolved.lstat()
    except (OSError, RuntimeError, ValueError) as exc:
        raise QualifiedCampaignInputError(f"{label} is missing or unsafe") from exc
    if absolute != resolved or stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
        raise QualifiedCampaignInputError(f"{label} is missing or unsafe")


def _read_model(path: Path, model: type[ModelT], label: str) -> ModelT:
    try:
        content = _read_regular(path, label, max_bytes=MAX_AUTHORITY_BYTES)
        return model.model_validate_json(content)
    except (json.JSONDecodeError, UnicodeDecodeError, ValidationError, ValueError) as exc:
        raise QualifiedCampaignInputError(f"{label} is corrupt") from exc
