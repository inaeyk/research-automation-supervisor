"""Privilege-separated Core Authority Service with narrow local IPC."""

from __future__ import annotations

import argparse
import array
import grp
import json
import os
import socket
import struct
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from research_automation_supervisor.core_authority_client import MAX_IPC_BYTES
from research_automation_supervisor.core_authority_models import CampaignLaunchRequestV1
from research_automation_supervisor.custodian_errors import QualifiedCampaignInputError
from research_automation_supervisor.prelaunch_authority import (
    consume_start_intent_for_qualified_launch,
    create_start_intent,
    get_start_intent,
    initialize_authority_store,
    list_operator_campaigns,
    verify_start_intent,
)
from research_automation_supervisor.safe_git import inspect_requested_repository


class _RequestEnvelopeV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1]
    operation: Literal[
        "inspect_repository",
        "create_start_intent",
        "get_start_intent",
        "list_operator_campaigns",
        "verify_start_intent",
        "consume_start_intent_for_qualified_launch",
    ]
    payload: dict[str, object]


class _InspectRepositoryV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    source_kind: Literal["existing_folder", "git_url"]
    locator: Annotated[str, Field(min_length=1, max_length=4096)]
    source_device: Annotated[int, Field(ge=0)] | None = None
    source_inode: Annotated[int, Field(gt=0)] | None = None


class _CreateStartIntentV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    request: CampaignLaunchRequestV1


class _IntentIdV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    launch_intent_id: Annotated[str, Field(min_length=136, max_length=136)]


class _VerifyIntentV1(_IntentIdV1):
    expected_campaign_public_id: Annotated[str, Field(min_length=12, max_length=80)]
    expected_intent_sha256: Annotated[str | None, Field(pattern=r"^[0-9a-f]{64}$")] = None
    expected_bundle_sha256: Annotated[str | None, Field(pattern=r"^[0-9a-f]{64}$")] = None


class _ConsumeIntentV1(_IntentIdV1):
    expected_campaign_public_id: Annotated[str, Field(min_length=12, max_length=80)]


class _EmptyV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class CoreAuthorityService:
    """One-request connections authenticated with Linux ``SO_PEERCRED``."""

    def __init__(
        self,
        socket_path: Path,
        authority_root: Path,
        snapshot_root: Path,
        *,
        operator_uid: int,
        socket_gid: int | None = None,
    ) -> None:
        self.socket_path = socket_path
        self.authority_root = authority_root
        self.snapshot_root = snapshot_root
        self.operator_uid = operator_uid
        self.socket_gid = socket_gid
        self._server: socket.socket | None = None

    def serve_forever(self) -> None:
        initialize_authority_store(self.authority_root, self.snapshot_root)
        self.socket_path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        if self.socket_path.exists() or self.socket_path.is_symlink():
            status = self.socket_path.lstat()
            if not stat_is_socket(status.st_mode):
                raise RuntimeError("Core Authority socket path is unsafe")
            self.socket_path.unlink()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server = server
        try:
            server.bind(str(self.socket_path))
            os.chmod(self.socket_path, 0o660)
            if self.socket_gid is not None:
                os.chown(self.socket_path, -1, self.socket_gid)
            server.listen(32)
            while True:
                connection, _ = server.accept()
                with connection:
                    self.handle_connection(connection)
        finally:
            server.close()
            self._server = None
            self.socket_path.unlink(missing_ok=True)

    def handle_connection(self, connection: socket.socket) -> None:
        descriptors: tuple[int, ...] = ()
        try:
            request_bytes, descriptors = _read_request(connection)
            if _peer_uid(connection) != self.operator_uid:
                _send_response(
                    connection,
                    ok=False,
                    error_code="unauthorized_peer",
                    message="Unauthorized local peer.",
                )
                return
            request = _RequestEnvelopeV1.model_validate_json(request_bytes)
            result = self._dispatch(request, descriptors)
            _send_response(connection, ok=True, result=result)
        except (ValidationError, ValueError, QualifiedCampaignInputError) as exc:
            _send_response(
                connection,
                ok=False,
                error_code="request_rejected",
                message=str(exc)[:2048] or "Core authority rejected the request.",
            )
        except Exception:
            _send_response(
                connection,
                ok=False,
                error_code="core_internal_error",
                message="Core authority stopped safely.",
            )
        finally:
            for descriptor in descriptors:
                os.close(descriptor)

    def _dispatch(self, request: _RequestEnvelopeV1, descriptors: tuple[int, ...]) -> object:
        operation = request.operation
        if operation == "create_start_intent":
            start_payload = _CreateStartIntentV1.model_validate(request.payload)
            if len(descriptors) > 1 or (
                start_payload.request.repository.source_kind == "git_url" and descriptors
            ):
                raise ValueError("Start repository descriptor binding is invalid")
            reference = create_start_intent(
                start_payload.request,
                self.authority_root,
                self.snapshot_root,
                operator_uid=self.operator_uid,
                operator_gid=self.socket_gid,
                repository_bundle_descriptor=descriptors[0] if descriptors else None,
                require_repository_descriptor=True,
            )
            return reference.model_dump(mode="json")
        if descriptors and (operation != "inspect_repository" or len(descriptors) != 1):
            raise ValueError("IPC operation does not accept filesystem descriptors")
        if operation == "inspect_repository":
            inspect_payload = _InspectRepositoryV1.model_validate(request.payload)
            expects_descriptor = inspect_payload.source_kind == "existing_folder"
            if expects_descriptor != (len(descriptors) == 1):
                raise ValueError("repository inspection descriptor binding is invalid")
            result = inspect_requested_repository(
                inspect_payload.source_kind,
                inspect_payload.locator,
                sterile_root=self.snapshot_root / "preview-sterile",
                repository_bundle_descriptor=descriptors[0] if descriptors else None,
                source_device=inspect_payload.source_device,
                source_inode=inspect_payload.source_inode,
            )
            return result.model_dump(mode="json")
        if operation == "get_start_intent":
            intent_payload = _IntentIdV1.model_validate(request.payload)
            return get_start_intent(
                self.authority_root, intent_payload.launch_intent_id
            ).model_dump(mode="json")
        if operation == "list_operator_campaigns":
            _EmptyV1.model_validate(request.payload)
            return [
                item.model_dump(mode="json")
                for item in list_operator_campaigns(self.authority_root)
            ]
        if operation == "verify_start_intent":
            verify_payload = _VerifyIntentV1.model_validate(request.payload)
            return verify_start_intent(
                self.authority_root,
                verify_payload.launch_intent_id,
                expected_campaign_public_id=verify_payload.expected_campaign_public_id,
                expected_intent_sha256=verify_payload.expected_intent_sha256,
                expected_bundle_sha256=verify_payload.expected_bundle_sha256,
            ).model_dump(mode="json")
        consume_payload = _ConsumeIntentV1.model_validate(request.payload)
        return consume_start_intent_for_qualified_launch(
            self.authority_root,
            consume_payload.launch_intent_id,
            expected_campaign_public_id=consume_payload.expected_campaign_public_id,
        ).model_dump(mode="json")


def _peer_uid(connection: socket.socket) -> int:
    credentials = connection.getsockopt(
        socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
    )
    _, uid, _ = struct.unpack("3i", credentials)
    return int(uid)


def _read_request(connection: socket.socket) -> tuple[bytes, tuple[int, ...]]:
    chunks: list[bytes] = []
    descriptors: list[int] = []
    size = 0
    try:
        while True:
            chunk, ancillary, flags, _ = connection.recvmsg(
                min(64 * 1024, MAX_IPC_BYTES + 1 - size),
                socket.CMSG_SPACE(array.array("i", (0,)).itemsize),
            )
            for level, kind, content in ancillary:
                if level != socket.SOL_SOCKET or kind != socket.SCM_RIGHTS:
                    raise ValueError("Core authority request ancillary data is invalid")
                received = array.array("i")
                received.frombytes(content[: len(content) - (len(content) % received.itemsize)])
                descriptors.extend(received)
                if len(descriptors) > 1:
                    raise ValueError("Core authority accepts at most one repository descriptor")
            if flags & (socket.MSG_CTRUNC | socket.MSG_TRUNC):
                raise ValueError("Core authority request ancillary data is invalid")
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > MAX_IPC_BYTES:
                raise ValueError("Core authority request exceeds the safe size limit")
        value = b"".join(chunks)
        if not value.endswith(b"\n") or value.count(b"\n") != 1:
            raise ValueError("Core authority request framing is invalid")
        return value, tuple(descriptors)
    except BaseException:
        for descriptor in descriptors:
            os.close(descriptor)
        raise


def _send_response(
    connection: socket.socket,
    *,
    ok: bool,
    result: object | None = None,
    error_code: str | None = None,
    message: str | None = None,
) -> None:
    value = {
        "schema_version": 1,
        "ok": ok,
        "result": result,
        "error_code": error_code,
        "message": message,
    }
    connection.sendall(
        json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("utf-8")
        + b"\n"
    )


def stat_is_socket(mode: int) -> bool:
    import stat

    return stat.S_ISSOCK(mode)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Research Supervisor Core Authority Service")
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--authority-root", type=Path, required=True)
    parser.add_argument("--snapshot-root", type=Path, required=True)
    parser.add_argument("--operator-uid", type=int, required=True)
    parser.add_argument("--socket-group")
    args = parser.parse_args(argv)
    socket_gid = grp.getgrnam(args.socket_group).gr_gid if args.socket_group else None
    service = CoreAuthorityService(
        args.socket,
        args.authority_root,
        args.snapshot_root,
        operator_uid=args.operator_uid,
        socket_gid=socket_gid,
    )
    service.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
