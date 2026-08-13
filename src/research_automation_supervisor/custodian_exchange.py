"""Create-once, non-authoritative operator exchange for the Campaign Custodian."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from research_automation_supervisor.custodian_errors import (
    CustodianInputError,
    CustodianStateError,
)
from research_automation_supervisor.custodian_models import (
    DurableStateAuthorityV1,
    HumanActionRequestV1,
    HumanActionResponseV1,
    LocalNotificationV1,
    PublicCampaignId,
    UploadedResponseFileV1,
)
from research_automation_supervisor.durable_state import render_json_bytes

MAX_EXCHANGE_RECORD_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class OperatorExchangePaths:
    root: Path
    requests: Path
    responses: Path
    notifications: Path
    uploads: Path


def prepare_operator_exchange(
    root: Path, campaign_public_id: PublicCampaignId
) -> OperatorExchangePaths:
    """Create a fixed exchange tree outside authoritative campaign durability."""
    base = _trusted_directory(root, create=True)
    campaign = _trusted_child_directory(base, str(campaign_public_id), create=True)
    return OperatorExchangePaths(
        root=campaign,
        requests=_trusted_child_directory(campaign, "requests", create=True),
        responses=_trusted_child_directory(campaign, "responses", create=True),
        notifications=_trusted_child_directory(campaign, "notifications", create=True),
        uploads=_trusted_child_directory(campaign, "uploads", create=True),
    )


def publish_human_action_request(
    paths: OperatorExchangePaths,
    request: HumanActionRequestV1,
) -> Path:
    """Publish one immutable core-issued request; replacement is never allowed."""
    _require_exchange_campaign(paths, request.campaign_public_id)
    destination = paths.requests / f"{request.request_sha256}.json"
    _write_once(destination, render_json_bytes(request.model_dump(mode="json")))
    return destination


def load_human_action_request(
    paths: OperatorExchangePaths,
    request_sha256: str,
) -> HumanActionRequestV1:
    """Read and self-verify one exact request without following symlinks."""
    if len(request_sha256) != 64 or any(
        character not in "0123456789abcdef" for character in request_sha256
    ):
        raise CustodianInputError("human-action request identity is invalid")
    value = _read_json_regular(paths.requests / f"{request_sha256}.json", "human-action request")
    try:
        request = HumanActionRequestV1.model_validate(value)
    except ValidationError as exc:
        raise CustodianStateError("human-action request is invalid") from exc
    if request.request_sha256 != request_sha256:
        raise CustodianStateError("human-action request identity changed")
    _require_exchange_campaign(paths, request.campaign_public_id)
    return request


def active_human_action_request(paths: OperatorExchangePaths) -> HumanActionRequestV1 | None:
    """Return the sole unanswered request, failing closed on ambiguous exchange state."""
    requests = [
        load_human_action_request(paths, path.stem) for path in _regular_json_files(paths.requests)
    ]
    unanswered: list[HumanActionRequestV1] = []
    for request in requests:
        response_path = paths.responses / f"{request.request_sha256}.json"
        if not response_path.exists():
            unanswered.append(request)
    if len(unanswered) > 1:
        raise CustodianStateError("operator exchange contains multiple unanswered requests")
    return unanswered[0] if unanswered else None


def submit_human_action_response(
    paths: OperatorExchangePaths,
    request: HumanActionRequestV1,
    response: HumanActionResponseV1,
    *,
    current_authority: DurableStateAuthorityV1,
) -> Path:
    """Validate all replay/cross-campaign/staleness bindings, then write once."""
    _require_exchange_campaign(paths, response.campaign_public_id)
    if response.campaign_public_id != request.campaign_public_id:
        raise CustodianInputError("response belongs to another campaign")
    if (
        response.request_id != request.request_id
        or response.request_sha256 != request.request_sha256
    ):
        raise CustodianInputError("response belongs to another human-action request")
    if response.input_bundle_sha256 != request.input_bundle_sha256:
        raise CustodianInputError("response belongs to another frozen input bundle")
    if response.durable_authority != request.durable_authority:
        raise CustodianInputError("response durable-state binding does not match its request")
    if response.durable_authority != current_authority:
        raise CustodianInputError("campaign advanced after this request; refresh before responding")
    allowed = {item.option_id for item in request.allowed_options}
    if request.allowed_options and response.selected_option_id not in allowed:
        raise CustodianInputError("response selection is not allowed by this request")
    if not request.allowed_options and response.selected_option_id is not None:
        raise CustodianInputError("this request does not accept a choice")
    if request.response_type == "file_upload" and not response.uploaded_files:
        raise CustodianInputError("this request requires a file upload")
    for uploaded in response.uploaded_files:
        upload_path = paths.root / uploaded.exchange_path
        content = _read_regular(upload_path, "uploaded response file")
        if (
            len(content) != uploaded.byte_count
            or hashlib.sha256(content).hexdigest() != uploaded.sha256
        ):
            raise CustodianInputError("uploaded response file changed")
    destination = paths.responses / f"{request.request_sha256}.json"
    _write_once(destination, render_json_bytes(response.model_dump(mode="json")))
    return destination


def load_human_action_response(
    paths: OperatorExchangePaths,
    request: HumanActionRequestV1,
) -> HumanActionResponseV1:
    value = _read_json_regular(
        paths.responses / f"{request.request_sha256}.json",
        "human-action response",
    )
    try:
        response = HumanActionResponseV1.model_validate(value)
    except ValidationError as exc:
        raise CustodianStateError("human-action response is invalid") from exc
    return response


def publish_notification(
    paths: OperatorExchangePaths,
    notification: LocalNotificationV1,
    *,
    identity_suffix: str = "",
) -> Path:
    _require_exchange_campaign(paths, notification.campaign_public_id)
    if identity_suffix and (
        len(identity_suffix) != 17
        or not identity_suffix.startswith("-")
        or any(character not in "0123456789abcdef" for character in identity_suffix[1:])
    ):
        raise CustodianInputError("notification identity is invalid")
    destination = paths.notifications / f"{notification.kind}{identity_suffix}.json"
    _write_once(destination, render_json_bytes(notification.model_dump(mode="json")))
    return destination


def store_response_upload(
    paths: OperatorExchangePaths,
    *,
    display_name: str,
    content: bytes,
) -> UploadedResponseFileV1:
    """Store one user-selected upload outside campaign authority with a content identity."""
    digest = hashlib.sha256(content).hexdigest()
    suffix = Path(display_name).suffix[:20]
    stored_name = f"{digest[:32]}{suffix}"
    destination = paths.uploads / stored_name
    _write_once(destination, content)
    return UploadedResponseFileV1(
        display_name=display_name,
        byte_count=len(content),
        sha256=digest,
        exchange_path=f"uploads/{stored_name}",
    )


def _require_exchange_campaign(paths: OperatorExchangePaths, campaign_public_id: str) -> None:
    if paths.root.name != campaign_public_id:
        raise CustodianInputError("operator exchange belongs to another campaign")


def _trusted_directory(path: Path, *, create: bool) -> Path:
    try:
        if create:
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
        absolute = Path(os.path.abspath(path))
        resolved = path.resolve(strict=True)
        status = resolved.lstat()
    except (OSError, RuntimeError, ValueError) as exc:
        raise CustodianStateError("operator exchange directory is unavailable") from exc
    if absolute != resolved or stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
        raise CustodianStateError("operator exchange directory is unsafe")
    return resolved


def _trusted_child_directory(parent: Path, name: str, *, create: bool) -> Path:
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        raise CustodianInputError("operator exchange path component is invalid")
    parent = _trusted_directory(parent, create=False)
    child = parent / name
    try:
        if create:
            child.mkdir(exist_ok=True, mode=0o700)
        status = child.lstat()
    except OSError as exc:
        raise CustodianStateError("operator exchange directory is unavailable") from exc
    if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
        raise CustodianStateError("operator exchange directory is unsafe")
    if child.resolve(strict=True).parent != parent:
        raise CustodianStateError("operator exchange directory escaped its root")
    return child


def _write_once(path: Path, content: bytes) -> None:
    _trusted_directory(path.parent, create=False)
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
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except FileExistsError as exc:
        raise CustodianStateError("operator exchange record was already submitted") from exc
    except OSError as exc:
        raise CustodianStateError("operator exchange record could not be written safely") from exc


def _read_regular(path: Path, label: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or opened.st_size > MAX_EXCHANGE_RECORD_BYTES:
                raise OSError
            chunks: list[bytes] = []
            while True:
                chunk = os.read(descriptor, 64 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
                if sum(len(value) for value in chunks) > MAX_EXCHANGE_RECORD_BYTES:
                    raise OSError
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise CustodianStateError(f"{label} is unavailable or unsafe") from exc
    return b"".join(chunks)


def _read_json_regular(path: Path, label: str) -> object:
    try:
        return json.loads(_read_regular(path, label).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise CustodianStateError(f"{label} is malformed") from exc


def _regular_json_files(directory: Path) -> tuple[Path, ...]:
    trusted = _trusted_directory(directory, create=False)
    values: list[Path] = []
    try:
        for path in trusted.iterdir():
            status = path.lstat()
            if (
                path.suffix == ".json"
                and stat.S_ISREG(status.st_mode)
                and not stat.S_ISLNK(status.st_mode)
            ):
                values.append(path)
            elif path.suffix == ".json":
                raise CustodianStateError("operator exchange contains an unsafe record")
    except OSError as exc:
        raise CustodianStateError("operator exchange could not be inspected") from exc
    return tuple(sorted(values))
