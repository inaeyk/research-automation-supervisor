"""Crash-durable Start authority used only by the privileged Core service.

The public Custodian talks to :mod:`core_authority_service` over a typed Unix
socket.  It must never import this module in production: every function here
expects direct access to the service-owned authority directory.
"""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from research_automation_supervisor.core_authority_models import (
    CampaignLaunchReferenceV1,
    CampaignLaunchRequestV1,
    CampaignLaunchSummaryV1,
    QualifiedLaunchMaterialV1,
    RequestedRepositoryAuthorityV1,
)
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
StartIntentId = Annotated[
    str,
    Field(pattern=r"^intent_[0-9a-f]{64}_[0-9a-f]{64}$", min_length=136, max_length=136),
]
START_INTENT_ID = re.compile(r"^intent_([0-9a-f]{64})_([0-9a-f]{64})$")
MAX_AUTHORITY_BYTES = 64 * 1024 * 1024
ModelT = TypeVar("ModelT", bound=BaseModel)
CrashInjector = Callable[[str], None]


def initialize_authority_store(authority_root: Path, snapshot_root: Path) -> None:
    """Create durable empty service state before accepting any IPC request."""
    root = _authority_root(authority_root)
    _snapshot_root(snapshot_root)
    _store_secret(root)


def _self_hash(value: BaseModel, field: str) -> str:
    return hashlib.sha256(
        canonical_json(value.model_dump(mode="json", exclude={field}))
    ).hexdigest()


class CampaignLaunchIntentV1(BaseModel):
    """Content-addressed immutable Start intent, before receipt publication."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    campaign_public_id: Annotated[
        str, Field(min_length=12, max_length=80, pattern=r"^campaign-[a-z0-9-]+$")
    ]
    start_request_sha256: Sha256
    preview_id: str
    client_start_key_sha256: Sha256
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


class FrozenCampaignInputV1(BaseModel):
    """Complete canonical CampaignInputBundle prepared during atomic Start."""

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


class CampaignLaunchReceiptV1(BaseModel):
    """Atomic Start commit record, addressed by request identity."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    campaign_public_id: str
    preview_id: str
    client_start_key_sha256: Sha256
    start_request_sha256: Sha256
    launch_intent_sha256: Sha256
    launch_intent_id_sha256: Sha256
    frozen_input_sha256: Sha256
    input_bundle_sha256: Sha256
    repository_preparation_sha256: Sha256
    created_at: Annotated[str, Field(min_length=20, max_length=40)]
    receipt_sha256: Sha256

    @model_validator(mode="after")
    def validate_receipt(self) -> CampaignLaunchReceiptV1:
        if self.receipt_sha256 != _self_hash(self, "receipt_sha256"):
            raise ValueError("launch receipt self-hash is invalid")
        return self


def create_start_intent(
    request: CampaignLaunchRequestV1,
    authority_root: Path,
    snapshot_root: Path,
    *,
    operator_uid: int | None = None,
    operator_gid: int | None = None,
    repository_bundle_descriptor: int | None = None,
    require_repository_descriptor: bool = False,
    now: datetime | None = None,
    crash_injector: CrashInjector | None = None,
) -> CampaignLaunchReferenceV1:
    """Perform Start as one locked, receipt-committed single assignment."""
    inject = crash_injector or (lambda _boundary: None)
    inject("before_core_transaction")
    root = _authority_root(authority_root)
    snapshots = _snapshot_root(snapshot_root)
    secret = _store_secret(root)
    request_sha = request.canonical_sha256()
    with _start_lock(root):
        existing = _receipt_for_request(root, request.client_start_key_sha256)
        if existing is not None:
            if existing.start_request_sha256 != request_sha:
                raise QualifiedCampaignInputError(
                    "Start request identity was already bound to different fields"
                )
            return _reference_from_receipt(root, existing, secret)
        preview_receipt = _receipt_for_preview(root, request.preview_id)
        if preview_receipt is not None:
            if preview_receipt.start_request_sha256 != request_sha:
                raise QualifiedCampaignInputError(
                    "campaign preview was already started with different fields"
                )
            return _reference_from_receipt(root, preview_receipt, secret)

        if (
            require_repository_descriptor
            and request.repository.source_kind == "existing_folder"
            and repository_bundle_descriptor is None
        ):
            raise QualifiedCampaignInputError("new Start requires the selected repository object")

        campaign_mac = hmac.new(
            secret,
            f"campaign:{request.client_start_key_sha256}".encode("ascii"),
            "sha256",
        ).hexdigest()
        campaign_id = f"campaign-{campaign_mac[:24]}"
        intent_payload = {
            "schema_version": 1,
            "campaign_public_id": campaign_id,
            "start_request_sha256": request_sha,
            **request.model_dump(mode="python", exclude={"schema_version"}),
        }
        intent_sha = hashlib.sha256(canonical_json(intent_payload)).hexdigest()
        intent = CampaignLaunchIntentV1.model_validate(
            {**intent_payload, "intent_sha256": intent_sha}
        )
        intent_mac = hmac.new(secret, f"intent:{intent_sha}".encode("ascii"), "sha256").hexdigest()
        intent_id = f"intent_{intent_sha}_{intent_mac}"

        # Local import happens exactly once at Start, while the service holds a
        # stable descriptor for an existing-folder source.
        from research_automation_supervisor.safe_git import prepare_repository_snapshot

        repository, preparation = prepare_repository_snapshot(
            intent,
            snapshot_root=snapshots,
            operator_uid=operator_uid,
            operator_gid=operator_gid,
            repository_bundle_descriptor=repository_bundle_descriptor,
        )
        bundle = CampaignInputBundleV1.freeze(
            campaign_public_id=campaign_id,
            human_name=request.human_name,
            repository=repository,
            research_contract=request.research_contract,
            research_plan=request.research_plan,
            initial_task=request.initial_task,
            supporting_files=request.supporting_files,
            requested_settings=request.requested_settings,
        )
        frozen_payload = {
            "schema_version": 1,
            "launch_intent_sha256": intent_sha,
            "repository_preparation_sha256": preparation.receipt_sha256,
            "input_bundle": bundle.model_dump(mode="python"),
        }
        frozen = FrozenCampaignInputV1.model_validate(
            {
                **frozen_payload,
                "frozen_input_sha256": hashlib.sha256(canonical_json(frozen_payload)).hexdigest(),
            }
        )
        _publish_object(
            root / "objects" / request_sha[:2] / f"{request_sha}.json",
            canonical_json(request.model_dump(mode="json")),
            "Start request object",
            inject=inject,
        )
        _publish_object(
            root / "objects" / intent_sha[:2] / f"{intent_sha}.json",
            canonical_json(intent.model_dump(mode="json")),
            "launch intent object",
        )
        _publish_object(
            root / "frozen-inputs" / f"{intent_sha}.json",
            canonical_json(frozen.model_dump(mode="json")),
            "frozen input object",
        )
        inject("after_durable_objects_before_receipt")
        created_at = (now or datetime.now(UTC)).astimezone(UTC).isoformat().replace("+00:00", "Z")
        receipt_payload = {
            "schema_version": 1,
            "campaign_public_id": campaign_id,
            "preview_id": request.preview_id,
            "client_start_key_sha256": request.client_start_key_sha256,
            "start_request_sha256": request_sha,
            "launch_intent_sha256": intent_sha,
            "launch_intent_id_sha256": hashlib.sha256(intent_id.encode("ascii")).hexdigest(),
            "frozen_input_sha256": frozen.frozen_input_sha256,
            "input_bundle_sha256": bundle.bundle_sha256,
            "repository_preparation_sha256": preparation.receipt_sha256,
            "created_at": created_at,
        }
        receipt = CampaignLaunchReceiptV1.model_validate(
            {
                **receipt_payload,
                "receipt_sha256": hashlib.sha256(canonical_json(receipt_payload)).hexdigest(),
            }
        )
        _publish_object(
            root / "receipts" / f"{request.client_start_key_sha256}.json",
            canonical_json(receipt.model_dump(mode="json")),
            "launch receipt",
        )
        inject("after_receipt_before_response")
        return CampaignLaunchReferenceV1(
            campaign_public_id=campaign_id,
            launch_intent_id=intent_id,
            launch_intent_sha256=intent_sha,
            input_bundle_sha256=bundle.bundle_sha256,
        )


def get_start_intent(authority_root: Path, launch_intent_id: str) -> CampaignLaunchSummaryV1:
    root = _existing_authority_root(authority_root)
    secret = _read_store_secret(root)
    receipt, intent, frozen = _load_committed_intent(root, launch_intent_id, secret)
    return CampaignLaunchSummaryV1(
        campaign_public_id=intent.campaign_public_id,
        preview_id=intent.preview_id,
        launch_intent_id=launch_intent_id,
        launch_intent_sha256=intent.intent_sha256,
        input_bundle_sha256=frozen.input_bundle.bundle_sha256,
        human_name=intent.human_name,
        repository_display=intent.repository.source_display,
        created_at=receipt.created_at,
    )


def list_operator_campaigns(authority_root: Path) -> tuple[CampaignLaunchSummaryV1, ...]:
    root = _existing_authority_root(authority_root)
    secret = _read_store_secret(root)
    summaries: list[CampaignLaunchSummaryV1] = []
    for path in _regular_receipts(root):
        receipt = _read_model(path, CampaignLaunchReceiptV1, "launch receipt")
        intent_id = _intent_id(secret, receipt.launch_intent_sha256)
        summaries.append(get_start_intent(root, intent_id))
    return tuple(sorted(summaries, key=lambda item: (item.created_at, item.campaign_public_id)))


def verify_start_intent(
    authority_root: Path,
    launch_intent_id: str,
    *,
    expected_campaign_public_id: str,
    expected_intent_sha256: str | None = None,
    expected_bundle_sha256: str | None = None,
) -> CampaignLaunchSummaryV1:
    summary = get_start_intent(authority_root, launch_intent_id)
    if summary.campaign_public_id != expected_campaign_public_id:
        raise QualifiedCampaignInputError("launch intent belongs to another campaign")
    if (
        expected_intent_sha256 is not None
        and summary.launch_intent_sha256 != expected_intent_sha256
    ):
        raise QualifiedCampaignInputError("launch intent identity was substituted")
    if expected_bundle_sha256 is not None and summary.input_bundle_sha256 != expected_bundle_sha256:
        raise QualifiedCampaignInputError("launch intent bundle was substituted")
    return summary


def consume_start_intent_for_qualified_launch(
    authority_root: Path,
    launch_intent_id: str,
    *,
    expected_campaign_public_id: str,
) -> QualifiedLaunchMaterialV1:
    root = _existing_authority_root(authority_root)
    secret = _read_store_secret(root)
    _, intent, frozen = _load_committed_intent(root, launch_intent_id, secret)
    if intent.campaign_public_id != expected_campaign_public_id:
        raise QualifiedCampaignInputError("launch intent belongs to another campaign")
    return QualifiedLaunchMaterialV1(
        campaign_public_id=intent.campaign_public_id,
        launch_intent_id=launch_intent_id,
        launch_intent_sha256=intent.intent_sha256,
        frozen_input_sha256=frozen.frozen_input_sha256,
        input_bundle=frozen.input_bundle,
    )


# Compatibility helpers are intentionally private-store APIs.  They are useful
# for low-level recovery tests but are not exposed by the installed runner.
def freeze_launch_intent(
    request: CampaignLaunchRequestV1,
    authority_root: Path,
    *,
    now: datetime | None = None,
) -> CampaignLaunchReferenceV1:
    return create_start_intent(
        request,
        authority_root,
        authority_root.parent / "repository-snapshots",
        now=now,
    )


def load_launch_intent(
    authority_root: Path,
    launch_intent_id: str,
    *,
    expected_campaign_public_id: str | None = None,
    expected_intent_sha256: str | None = None,
) -> CampaignLaunchIntentV1:
    root = _existing_authority_root(authority_root)
    _, intent, _ = _load_committed_intent(root, launch_intent_id, _read_store_secret(root))
    if (
        expected_campaign_public_id is not None
        and intent.campaign_public_id != expected_campaign_public_id
    ):
        raise QualifiedCampaignInputError("launch intent belongs to another campaign")
    if expected_intent_sha256 is not None and intent.intent_sha256 != expected_intent_sha256:
        raise QualifiedCampaignInputError("launch intent identity was substituted")
    return intent


def load_launch_summary(
    authority_root: Path,
    launch_intent_id: str,
    *,
    expected_campaign_public_id: str,
    expected_intent_sha256: str,
) -> CampaignLaunchSummaryV1:
    return verify_start_intent(
        authority_root,
        launch_intent_id,
        expected_campaign_public_id=expected_campaign_public_id,
        expected_intent_sha256=expected_intent_sha256,
    )


def load_frozen_campaign_input(
    authority_root: Path, intent: CampaignLaunchIntentV1
) -> FrozenCampaignInputV1 | None:
    path = (
        _existing_authority_root(authority_root) / "frozen-inputs" / f"{intent.intent_sha256}.json"
    )
    if not path.exists():
        return None
    return _read_model(path, FrozenCampaignInputV1, "frozen campaign input")


def seal_frozen_campaign_input(
    authority_root: Path,
    intent: CampaignLaunchIntentV1,
    repository: RepositoryAuthorityV1,
    repository_preparation_sha256: str,
) -> FrozenCampaignInputV1:
    del authority_root, intent, repository, repository_preparation_sha256
    raise QualifiedCampaignInputError("frozen campaign input is committed only by atomic Start")


def _load_committed_intent(
    root: Path, launch_intent_id: str, secret: bytes
) -> tuple[CampaignLaunchReceiptV1, CampaignLaunchIntentV1, FrozenCampaignInputV1]:
    match = START_INTENT_ID.fullmatch(launch_intent_id)
    if match is None:
        raise QualifiedCampaignInputError("launch intent is stale or invalid")
    intent_sha, supplied_mac = match.groups()
    expected_mac = hmac.new(secret, f"intent:{intent_sha}".encode("ascii"), "sha256").hexdigest()
    if not hmac.compare_digest(supplied_mac, expected_mac):
        raise QualifiedCampaignInputError("launch intent is stale or invalid")
    receipts = [
        _read_model(path, CampaignLaunchReceiptV1, "launch receipt")
        for path in _regular_receipts(root)
    ]
    matches = [item for item in receipts if item.launch_intent_sha256 == intent_sha]
    if len(matches) != 1:
        raise QualifiedCampaignInputError("launch intent is stale or ambiguous")
    receipt = matches[0]
    if not hmac.compare_digest(
        receipt.launch_intent_id_sha256,
        hashlib.sha256(launch_intent_id.encode("ascii")).hexdigest(),
    ):
        raise QualifiedCampaignInputError("launch intent receipt is corrupt")
    intent = _read_model(
        root / "objects" / intent_sha[:2] / f"{intent_sha}.json",
        CampaignLaunchIntentV1,
        "launch intent",
    )
    frozen = _read_model(
        root / "frozen-inputs" / f"{intent_sha}.json",
        FrozenCampaignInputV1,
        "frozen campaign input",
    )
    if (
        intent.intent_sha256 != intent_sha
        or intent.campaign_public_id != receipt.campaign_public_id
        or intent.start_request_sha256 != receipt.start_request_sha256
        or frozen.launch_intent_sha256 != intent_sha
        or frozen.frozen_input_sha256 != receipt.frozen_input_sha256
        or frozen.input_bundle.bundle_sha256 != receipt.input_bundle_sha256
        or frozen.repository_preparation_sha256 != receipt.repository_preparation_sha256
        or frozen.input_bundle.campaign_public_id != receipt.campaign_public_id
    ):
        raise QualifiedCampaignInputError("frozen launch authority is corrupt")
    return receipt, intent, frozen


def _reference_from_receipt(
    root: Path, receipt: CampaignLaunchReceiptV1, secret: bytes
) -> CampaignLaunchReferenceV1:
    intent_id = _intent_id(secret, receipt.launch_intent_sha256)
    _load_committed_intent(root, intent_id, secret)
    return CampaignLaunchReferenceV1(
        campaign_public_id=receipt.campaign_public_id,
        launch_intent_id=intent_id,
        launch_intent_sha256=receipt.launch_intent_sha256,
        input_bundle_sha256=receipt.input_bundle_sha256,
    )


def _intent_id(secret: bytes, intent_sha: str) -> str:
    mac = hmac.new(secret, f"intent:{intent_sha}".encode("ascii"), "sha256").hexdigest()
    return f"intent_{intent_sha}_{mac}"


def _receipt_for_request(root: Path, request_key_sha: str) -> CampaignLaunchReceiptV1 | None:
    path = root / "receipts" / f"{request_key_sha}.json"
    if not path.exists():
        return None
    return _read_model(path, CampaignLaunchReceiptV1, "launch receipt")


def _receipt_for_preview(root: Path, preview_id: str) -> CampaignLaunchReceiptV1 | None:
    matches: list[CampaignLaunchReceiptV1] = []
    for path in _regular_receipts(root):
        receipt = _read_model(path, CampaignLaunchReceiptV1, "launch receipt")
        if receipt.preview_id == preview_id:
            matches.append(receipt)
    if len(matches) > 1:
        raise QualifiedCampaignInputError("campaign preview Start authority is ambiguous")
    return matches[0] if matches else None


def _regular_receipts(root: Path) -> tuple[Path, ...]:
    directory = root / "receipts"
    _validate_directory(directory, "receipt directory")
    values: list[Path] = []
    try:
        for entry in os.scandir(directory):
            if not entry.name.endswith(".json") or not entry.is_file(follow_symlinks=False):
                raise QualifiedCampaignInputError("launch receipt directory is unsafe")
            values.append(Path(entry.path))
    except OSError as exc:
        raise QualifiedCampaignInputError("launch receipt directory is unavailable") from exc
    return tuple(sorted(values))


@contextmanager
def _start_lock(root: Path) -> Iterator[None]:
    path = root / "start.lock"
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    except OSError as exc:
        raise QualifiedCampaignInputError("core Start lock is unavailable") from exc
    finally:
        if "descriptor" in locals():
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)


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
            os.chmod(child, 0o700)
            _validate_directory(child, f"{name} directory")
        fsync_directory(resolved)
        return resolved
    except (OSError, RuntimeError, ValueError) as exc:
        raise QualifiedCampaignInputError("core authority store is unavailable") from exc


def _snapshot_root(path: Path) -> Path:
    try:
        path.mkdir(parents=True, exist_ok=True, mode=0o711)
        resolved = path.resolve(strict=True)
        if resolved != Path(os.path.abspath(path)) or stat.S_ISLNK(resolved.lstat().st_mode):
            raise OSError
        os.chmod(resolved, 0o711)
        return resolved
    except (OSError, RuntimeError, ValueError) as exc:
        raise QualifiedCampaignInputError("core repository snapshot store is unavailable") from exc


def _existing_authority_root(path: Path) -> Path:
    try:
        absolute = Path(os.path.abspath(path))
        resolved = path.resolve(strict=True)
        status = resolved.lstat()
    except (OSError, RuntimeError, ValueError) as exc:
        raise QualifiedCampaignInputError("core authority store is unavailable") from exc
    if absolute != resolved or stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
        raise QualifiedCampaignInputError("core authority store is unsafe")
    return resolved


def _store_secret(root: Path) -> bytes:
    path = root / "store-key-v1"
    if path.exists():
        return _read_store_secret(root)
    value = secrets.token_bytes(32)
    _publish_object(path, value, "core authority key", mode=0o600)
    return value


def _read_store_secret(root: Path) -> bytes:
    value = _read_regular(root / "store-key-v1", "core authority key", max_bytes=32)
    if len(value) != 32:
        raise QualifiedCampaignInputError("core authority key is invalid")
    return value


def _publish_object(
    path: Path,
    content: bytes,
    label: str,
    *,
    mode: int = 0o400,
    inject: CrashInjector | None = None,
) -> None:
    parent_created = not path.parent.exists()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _validate_directory(path.parent, f"{label} directory")
    if parent_created:
        fsync_directory(path.parent.parent)
    if path.exists():
        if _read_regular(path, label, max_bytes=MAX_AUTHORITY_BYTES) != content:
            raise QualifiedCampaignInputError(f"{label} was replaced")
        return
    temporary = path.parent / f".{path.name}.{secrets.token_hex(12)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    linked = False
    try:
        descriptor = os.open(temporary, flags, mode)
        midpoint = max(1, len(content) // 2)
        _write_all(descriptor, content[:midpoint])
        if inject is not None:
            inject("during_durable_object_write")
        _write_all(descriptor, content[midpoint:])
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.link(temporary, path, follow_symlinks=False)
        linked = True
        temporary.unlink()
        fsync_directory(path.parent)
    except FileExistsError:
        if _read_regular(path, label, max_bytes=MAX_AUTHORITY_BYTES) != content:
            raise QualifiedCampaignInputError(f"{label} was replaced") from None
    except OSError as exc:
        raise QualifiedCampaignInputError(f"{label} could not be committed") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if not linked:
            temporary.unlink(missing_ok=True)


def _write_all(descriptor: int, content: bytes) -> None:
    offset = 0
    while offset < len(content):
        written = os.write(descriptor, content[offset:])
        if written <= 0:
            raise OSError("short authority write")
        offset += written


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
