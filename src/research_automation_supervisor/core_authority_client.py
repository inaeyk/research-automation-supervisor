"""Typed client for the privilege-separated Core Authority Service."""

from __future__ import annotations

import array
import json
import os
import socket
from pathlib import Path
from typing import Literal, Protocol, TypeVar

from pydantic import BaseModel, ConfigDict, ValidationError

from research_automation_supervisor.core_authority_models import (
    CampaignLaunchReferenceV1,
    CampaignLaunchRequestV1,
    CampaignLaunchSummaryV1,
    QualifiedLaunchMaterialV1,
    RequestedRepositoryAuthorityV1,
)
from research_automation_supervisor.custodian_errors import CustodianStateError
from research_automation_supervisor.gitless_repository import (
    create_local_repository_transfer,
)

DEFAULT_CORE_SOCKET = Path("/run/research-supervisor-core/authority.sock")
MAX_IPC_BYTES = 80 * 1024 * 1024
ModelT = TypeVar("ModelT", bound=BaseModel)


class _RepositoryTransferRequired(CustodianStateError):
    """Typed Core response that authorizes one untrusted-source read."""


class CoreAuthorityClient(Protocol):
    def inspect_repository(
        self, source_kind: Literal["existing_folder", "git_url"], locator: str
    ) -> RequestedRepositoryAuthorityV1: ...

    def create_start_intent(
        self, request: CampaignLaunchRequestV1
    ) -> CampaignLaunchReferenceV1: ...

    def get_start_intent(self, launch_intent_id: str) -> CampaignLaunchSummaryV1: ...

    def list_operator_campaigns(self) -> tuple[CampaignLaunchSummaryV1, ...]: ...

    def resume_start_snapshot(self, launch_intent_id: str) -> CampaignLaunchSummaryV1: ...

    def verify_start_intent(
        self,
        launch_intent_id: str,
        *,
        expected_campaign_public_id: str,
        expected_intent_sha256: str | None = None,
        expected_bundle_sha256: str | None = None,
    ) -> CampaignLaunchSummaryV1: ...

    def consume_start_intent_for_qualified_launch(
        self, launch_intent_id: str, *, expected_campaign_public_id: str
    ) -> QualifiedLaunchMaterialV1: ...


class _CoreResponseV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1]
    ok: bool
    result: object | None = None
    error_code: str | None = None
    message: str | None = None


class UnixCoreAuthorityClient:
    """One authenticated local request per Unix-domain socket connection."""

    def __init__(self, socket_path: Path = DEFAULT_CORE_SOCKET, *, timeout: float = 660.0) -> None:
        self.socket_path = socket_path
        self.timeout = timeout

    def inspect_repository(
        self, source_kind: Literal["existing_folder", "git_url"], locator: str
    ) -> RequestedRepositoryAuthorityV1:
        source_descriptor: int | None = None
        transfer_descriptor: int | None = None
        try:
            payload: dict[str, object] = {"source_kind": source_kind, "locator": locator}
            if source_kind == "existing_folder":
                absolute = str(Path(os.path.abspath(locator)))
                source_descriptor = _open_repository_directory(absolute)
                identity = os.fstat(source_descriptor)
                payload.update(
                    {
                        "locator": absolute,
                        "source_device": identity.st_dev,
                        "source_inode": identity.st_ino,
                    }
                )
                transfer_descriptor = create_local_repository_transfer(source_descriptor)
            return self._model(
                "inspect_repository",
                payload,
                RequestedRepositoryAuthorityV1,
                descriptor=transfer_descriptor,
            )
        except OSError as exc:
            raise CustodianStateError("Selected repository is unavailable.") from exc
        finally:
            if source_descriptor is not None:
                os.close(source_descriptor)
            if transfer_descriptor is not None:
                os.close(transfer_descriptor)

    def create_start_intent(self, request: CampaignLaunchRequestV1) -> CampaignLaunchReferenceV1:
        payload: dict[str, object] = {"request": request.model_dump(mode="json")}
        try:
            # Query committed authority before touching the untrusted source.
            # Core returns an exact existing Start or a typed transfer demand.
            return self._model(
                "create_start_intent", payload, CampaignLaunchReferenceV1
            )
        except _RepositoryTransferRequired:
            pass
        source_descriptor: int | None = None
        transfer_descriptor: int | None = None
        try:
            if request.repository.source_kind == "existing_folder":
                source_descriptor = _open_repository_directory(request.repository.source_locator)
                status = os.fstat(source_descriptor)
                if (
                    status.st_dev != request.repository.source_device
                    or status.st_ino != request.repository.source_inode
                ):
                    raise OSError("selected repository object changed")
                transfer_descriptor = create_local_repository_transfer(source_descriptor)
            return self._model(
                "create_start_intent",
                payload,
                CampaignLaunchReferenceV1,
                descriptor=transfer_descriptor,
            )
        except OSError as exc:
            raise CustodianStateError("Selected repository changed before Start.") from exc
        finally:
            if source_descriptor is not None:
                os.close(source_descriptor)
            if transfer_descriptor is not None:
                os.close(transfer_descriptor)

    def get_start_intent(self, launch_intent_id: str) -> CampaignLaunchSummaryV1:
        return self._model(
            "get_start_intent",
            {"launch_intent_id": launch_intent_id},
            CampaignLaunchSummaryV1,
        )

    def list_operator_campaigns(self) -> tuple[CampaignLaunchSummaryV1, ...]:
        value = self._request("list_operator_campaigns", {})
        if not isinstance(value, list):
            raise CustodianStateError("Core campaign list returned an invalid response.")
        try:
            return tuple(CampaignLaunchSummaryV1.model_validate(item) for item in value)
        except ValidationError as exc:
            raise CustodianStateError("Core campaign list returned an invalid response.") from exc

    def resume_start_snapshot(self, launch_intent_id: str) -> CampaignLaunchSummaryV1:
        return self._model(
            "resume_start_snapshot",
            {"launch_intent_id": launch_intent_id},
            CampaignLaunchSummaryV1,
        )

    def verify_start_intent(
        self,
        launch_intent_id: str,
        *,
        expected_campaign_public_id: str,
        expected_intent_sha256: str | None = None,
        expected_bundle_sha256: str | None = None,
    ) -> CampaignLaunchSummaryV1:
        return self._model(
            "verify_start_intent",
            {
                "launch_intent_id": launch_intent_id,
                "expected_campaign_public_id": expected_campaign_public_id,
                "expected_intent_sha256": expected_intent_sha256,
                "expected_bundle_sha256": expected_bundle_sha256,
            },
            CampaignLaunchSummaryV1,
        )

    def consume_start_intent_for_qualified_launch(
        self, launch_intent_id: str, *, expected_campaign_public_id: str
    ) -> QualifiedLaunchMaterialV1:
        return self._model(
            "consume_start_intent_for_qualified_launch",
            {
                "launch_intent_id": launch_intent_id,
                "expected_campaign_public_id": expected_campaign_public_id,
            },
            QualifiedLaunchMaterialV1,
        )

    def _model(
        self,
        operation: str,
        payload: dict[str, object],
        model: type[ModelT],
        *,
        descriptor: int | None = None,
    ) -> ModelT:
        try:
            return model.model_validate(self._request(operation, payload, descriptor=descriptor))
        except ValidationError as exc:
            raise CustodianStateError("Core authority returned an invalid response.") from exc

    def _request(
        self, operation: str, payload: dict[str, object], *, descriptor: int | None = None
    ) -> object:
        request = (
            json.dumps(
                {"schema_version": 1, "operation": operation, "payload": payload},
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            + b"\n"
        )
        if len(request) > MAX_IPC_BYTES:
            raise CustodianStateError("Core authority request exceeds the safe size limit.")
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(self.timeout)
                connection.connect(str(self.socket_path))
                if descriptor is None:
                    connection.sendall(request)
                else:
                    rights = array.array("i", (descriptor,))
                    sent = connection.sendmsg(
                        (request,),
                        ((socket.SOL_SOCKET, socket.SCM_RIGHTS, rights.tobytes()),),
                    )
                    if sent < len(request):
                        connection.sendall(request[sent:])
                connection.shutdown(socket.SHUT_WR)
                response_bytes = _read_response(connection)
        except (OSError, TimeoutError) as exc:
            raise CustodianStateError("Core authority service is unavailable.") from exc
        try:
            response = _CoreResponseV1.model_validate_json(response_bytes)
        except (ValidationError, ValueError) as exc:
            raise CustodianStateError("Core authority returned an invalid response.") from exc
        if not response.ok:
            if response.error_code == "repository_transfer_required":
                raise _RepositoryTransferRequired(
                    response.message or "Core requires a repository transfer."
                )
            raise CustodianStateError(response.message or "Core authority rejected the request.")
        if response.error_code is not None or response.message is not None:
            raise CustodianStateError("Core authority returned a contradictory response.")
        return response.result


def _read_response(connection: socket.socket) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = connection.recv(min(64 * 1024, MAX_IPC_BYTES + 1 - size))
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
        if size > MAX_IPC_BYTES:
            raise CustodianStateError("Core authority response exceeds the safe size limit.")
    value = b"".join(chunks)
    if not value.endswith(b"\n") or value.count(b"\n") != 1:
        raise CustodianStateError("Core authority returned an invalid response.")
    return value


def _open_repository_directory(locator: str) -> int:
    absolute = Path(os.path.abspath(locator))
    parts = absolute.parts[1:]
    if not parts:
        raise OSError("filesystem root is not a repository selection")
    descriptor = os.open(
        "/",
        getattr(os, "O_PATH", os.O_RDONLY)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0),
    )
    try:
        for index, part in enumerate(parts):
            last = index == len(parts) - 1
            flags = (
                (os.O_RDONLY if last else getattr(os, "O_PATH", os.O_RDONLY))
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise
