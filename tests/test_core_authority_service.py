from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from research_automation_supervisor.core_authority_client import UnixCoreAuthorityClient
from research_automation_supervisor.core_authority_models import CampaignLaunchRequestV1
from research_automation_supervisor.custodian_errors import CustodianStateError
from research_automation_supervisor.custodian_models import (
    CampaignProfileSettingsV1,
    FrozenInputFileV1,
)
from tests.custodian_helpers import create_repository


def _start_service(
    tmp_path: Path, *, operator_uid: int | None = None
) -> tuple[subprocess.Popen[bytes], UnixCoreAuthorityClient, Path]:
    socket_path = tmp_path / "run/authority.sock"
    authority = tmp_path / "service/authority"
    snapshots = tmp_path / "service/snapshots"
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "research_automation_supervisor.core_authority_service",
            "--socket",
            str(socket_path),
            "--authority-root",
            str(authority),
            "--snapshot-root",
            str(snapshots),
            "--operator-uid",
            str(os.getuid() if operator_uid is None else operator_uid),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not socket_path.is_socket():
        if process.poll() is not None:
            stderr = process.stderr.read().decode() if process.stderr else ""
            raise AssertionError(f"Core service stopped during startup: {stderr}")
        time.sleep(0.02)
    assert socket_path.is_socket()
    return process, UnixCoreAuthorityClient(socket_path), authority


def _request(client: UnixCoreAuthorityClient, repository: Path) -> CampaignLaunchRequestV1:
    requested = client.inspect_repository("existing_folder", str(repository))
    return CampaignLaunchRequestV1(
        preview_id="preview-" + "a" * 24,
        client_start_key_sha256=hashlib.sha256(b"one Start request").hexdigest(),
        human_name="IPC Start campaign",
        repository=requested,
        research_contract=FrozenInputFileV1.from_bytes("contract.md", b"contract\n"),
        research_plan=FrozenInputFileV1.from_bytes("plan.md", b"plan\n"),
        initial_task=FrozenInputFileV1.from_bytes("task.md", b"task\n"),
        requested_settings=CampaignProfileSettingsV1(),
    )


def test_production_ipc_start_list_verify_and_consume(tmp_path: Path) -> None:
    repository = create_repository(tmp_path)
    process, client, authority = _start_service(tmp_path)
    try:
        request = _request(client, repository)
        reference = client.create_start_intent(request)
        assert client.create_start_intent(request) == reference
        assert client.list_operator_campaigns()[0].launch_intent_id == reference.launch_intent_id
        summary = client.verify_start_intent(
            reference.launch_intent_id,
            expected_campaign_public_id=reference.campaign_public_id,
            expected_intent_sha256=reference.launch_intent_sha256,
            expected_bundle_sha256=reference.input_bundle_sha256,
        )
        material = client.consume_start_intent_for_qualified_launch(
            reference.launch_intent_id,
            expected_campaign_public_id=reference.campaign_public_id,
        )
        assert material.input_bundle.bundle_sha256 == summary.input_bundle_sha256
        assert stat.S_IMODE(authority.stat().st_mode) == 0o700
        assert stat.S_IMODE((authority / "store-key-v1").stat().st_mode) == 0o600
        assert all(
            stat.S_IMODE(path.stat().st_mode) == 0o400
            for directory in ("objects", "receipts", "frozen-inputs")
            for path in (authority / directory).rglob("*.json")
        )
    finally:
        process.terminate()
        process.wait(timeout=10)
        stderr = process.stderr.read().decode(errors="replace") if process.stderr else ""
        assert not stderr, stderr


def test_ipc_unknown_and_extra_fields_fail_closed(tmp_path: Path) -> None:
    process, client, _ = _start_service(tmp_path)
    del client
    try:
        for value in (
            {
                "schema_version": 1,
                "operation": "list_operator_campaigns",
                "payload": {"unexpected": True},
            },
            {
                "schema_version": 1,
                "operation": "get_start_intent",
                "payload": {"launch_intent_id": "x" * 136, "unexpected": True},
            },
            {
                "schema_version": 1,
                "operation": "arbitrary_file_read",
                "payload": {"path": "/etc/shadow"},
            },
        ):
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.connect(str(tmp_path / "run/authority.sock"))
                connection.sendall(json.dumps(value).encode() + b"\n")
                connection.shutdown(socket.SHUT_WR)
                response = json.loads(connection.makefile("rb").readline())
            assert response["ok"] is False
            assert response["result"] is None
    finally:
        process.terminate()
        process.wait(timeout=10)


def test_ipc_rejects_a_peer_outside_the_configured_operator_identity(tmp_path: Path) -> None:
    process, client, _ = _start_service(tmp_path, operator_uid=os.getuid() + 1)
    try:
        with pytest.raises(CustodianStateError, match="Unauthorized local peer"):
            client.list_operator_campaigns()
    finally:
        process.terminate()
        process.wait(timeout=10)


def test_changed_field_reuse_and_cross_campaign_substitution_fail_over_ipc(
    tmp_path: Path,
) -> None:
    repository = create_repository(tmp_path)
    process, client, _ = _start_service(tmp_path)
    try:
        request = _request(client, repository)
        reference = client.create_start_intent(request)
        with pytest.raises(CustodianStateError, match="different fields"):
            client.create_start_intent(request.model_copy(update={"human_name": "Changed"}))
        with pytest.raises(CustodianStateError, match="another campaign"):
            client.verify_start_intent(
                reference.launch_intent_id,
                expected_campaign_public_id="campaign-wrong000000000000000",
            )
    finally:
        process.terminate()
        process.wait(timeout=10)


def test_installed_runner_has_no_arbitrary_bundle_start_surface(tmp_path: Path) -> None:
    bundle = tmp_path / "mutable-bundle.json"
    bundle.write_text("{}", encoding="utf-8")
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "research_automation_supervisor.qualified_runner",
            "start",
            "--bundle",
            str(bundle),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 2
    assert "unrecognized arguments: --bundle" in completed.stderr


@pytest.mark.skipif(os.geteuid() != 0, reason="actual two-UID privilege proof requires root")
def test_actual_custodian_uid_cannot_mutate_core_authority(tmp_path: Path) -> None:
    # This qualification is run in the privileged installation test job.  The
    # service and Custodian use unrelated numeric identities even when passwd
    # entries are unavailable in a minimal CI image.
    core_uid = 61_001
    custodian_uid = 61_002
    authority = tmp_path / "authority"
    authority.mkdir(mode=0o700)
    os.chown(authority, core_uid, core_uid)
    targets = (
        authority / "objects",
        authority / "receipts",
        authority / "frozen-inputs",
    )
    for target in targets:
        target.mkdir(mode=0o700)
        os.chown(target, core_uid, core_uid)
    key = authority / "store-key-v1"
    key.write_bytes(b"x" * 32)
    os.chown(key, core_uid, core_uid)
    key.chmod(0o600)
    attempted = subprocess.run(
        [
            "setpriv",
            "--reuid",
            str(custodian_uid),
            "--regid",
            str(custodian_uid),
            "--clear-groups",
            sys.executable,
            "-c",
            (
                "import os,pathlib,sys; root=pathlib.Path(sys.argv[1]); "
                "targets=[root/'store-key-v1',root/'receipts',root/'frozen-inputs',"
                "root/'objects']; "
                "raise SystemExit(1 if any(t.exists() and os.access(t,os.W_OK) "
                "for t in targets) else 0)"
            ),
            str(authority),
        ],
        check=False,
        env={"PATH": "/usr/bin:/bin"},
    )
    assert attempted.returncode == 0


@pytest.mark.skipif(os.geteuid() != 0, reason="actual service/Custodian identities require root")
def test_actual_service_identity_delegates_only_the_sanitized_snapshot() -> None:
    core_uid = 61_001
    custodian_uid = 61_002
    shared_gid = 61_003
    qualification_root = Path(tempfile.mkdtemp(prefix="ras-core-authority-uids-"))
    process: subprocess.Popen[bytes] | None = None
    try:
        qualification_root.chmod(0o755)
        runtime = qualification_root / "run"
        service_root = qualification_root / "service"
        operator_root = qualification_root / "operator"
        for path, uid, mode in (
            (runtime, core_uid, 0o770),
            (service_root, core_uid, 0o710),
            (operator_root, custodian_uid, 0o700),
        ):
            path.mkdir()
            os.chown(path, uid, shared_gid)
            path.chmod(mode)
        repository = create_repository(operator_root)
        for path in (repository, *repository.rglob("*")):
            os.chown(path, custodian_uid, shared_gid, follow_symlinks=False)

        socket_path = runtime / "authority.sock"
        authority = service_root / "authority"
        snapshots = service_root / "snapshots"
        service_driver = (
            "import os,sys; from pathlib import Path; "
            "from research_automation_supervisor.core_authority_service import "
            "CoreAuthorityService; "
            "import research_automation_supervisor.prelaunch_authority; "
            "import research_automation_supervisor.safe_git; "
            "CoreAuthorityService(Path(sys.argv[1]),Path(sys.argv[2]),Path(sys.argv[3]),"
            "operator_uid=int(sys.argv[4]),socket_gid=int(sys.argv[5])).serve_forever()"
        )
        process = subprocess.Popen(
            [
                "/usr/bin/setpriv",
                "--reuid",
                str(core_uid),
                "--regid",
                str(shared_gid),
                "--groups",
                str(Path.cwd().stat().st_gid),
                sys.executable,
                "-c",
                service_driver,
                str(socket_path),
                str(authority),
                str(snapshots),
                str(custodian_uid),
                str(shared_gid),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not socket_path.is_socket():
            if process.poll() is not None:
                stderr = process.stderr.read().decode() if process.stderr else ""
                raise AssertionError(f"Core service stopped during startup: {stderr}")
            time.sleep(0.02)
        assert socket_path.is_socket()
        process_status = Path(f"/proc/{process.pid}/status").read_text(encoding="utf-8")
        assert f"Uid:\t{core_uid}\t{core_uid}\t{core_uid}\t{core_uid}" in process_status
        assert "CapAmb:\t0000000000000000" in process_status

        client_driver = (
            "import hashlib,json,os,sys; from pathlib import Path; "
            "from research_automation_supervisor.core_authority_client import "
            "UnixCoreAuthorityClient; "
            "from research_automation_supervisor.core_authority_models import "
            "CampaignLaunchRequestV1; "
            "from research_automation_supervisor.custodian_models import "
            "CampaignProfileSettingsV1,FrozenInputFileV1; "
            "os.setgroups([]); os.setgid(int(sys.argv[3])); os.setuid(int(sys.argv[4])); "
            "client=UnixCoreAuthorityClient(Path(sys.argv[1])); repo=Path(sys.argv[2]); "
            "requested=client.inspect_repository('existing_folder',str(repo)); "
            "request=CampaignLaunchRequestV1(preview_id='preview-'+('c'*24),"
            "client_start_key_sha256=hashlib.sha256(b'actual uid start').hexdigest(),"
            "human_name='Actual UID campaign',repository=requested,"
            "research_contract=FrozenInputFileV1.from_bytes('contract.md',b'contract\\n'),"
            "research_plan=FrozenInputFileV1.from_bytes('plan.md',b'plan\\n'),"
            "initial_task=FrozenInputFileV1.from_bytes('task.md',b'task\\n'),"
            "requested_settings=CampaignProfileSettingsV1()); "
            "reference=client.create_start_intent(request); "
            "material=client.consume_start_intent_for_qualified_launch("
            "reference.launch_intent_id,expected_campaign_public_id="
            "reference.campaign_public_id); "
            "workspace=Path(material.input_bundle.repository.prepared_workspace); "
            "assert (workspace/'README.md').is_file(); "
            "print(json.dumps({'intent':reference.launch_intent_sha256,"
            "'request':request.canonical_sha256(),'key':request.client_start_key_sha256,"
            "'workspace':str(workspace)}))"
        )
        launched = subprocess.run(
            [
                sys.executable,
                "-c",
                client_driver,
                str(socket_path),
                str(repository),
                str(shared_gid),
                str(custodian_uid),
            ],
            check=False,
            capture_output=True,
            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
        )
        if launched.returncode != 0:
            process.terminate()
            process.wait(timeout=10)
            service_error = process.stderr.read().decode(errors="replace") if process.stderr else ""
            process = None
            raise AssertionError(
                launched.stderr.decode(errors="replace") + "\nCore service:\n" + service_error
            )
        identities = json.loads(launched.stdout)
        assert Path(identities["workspace"]).is_dir()
        assert stat.S_IMODE((snapshots / "workspaces").stat().st_mode) == 0o710
        assert (snapshots / "workspaces").stat().st_gid == shared_gid

        protected = (
            authority / "store-key-v1",
            authority / "receipts" / f"{identities['key']}.json",
            authority / "frozen-inputs" / f"{identities['intent']}.json",
            authority / "objects" / identities["intent"][:2] / f"{identities['intent']}.json",
            authority / "objects" / identities["request"][:2] / f"{identities['request']}.json",
        )
        mutation_driver = (
            "import os,sys; failed=False; "
            "targets=sys.argv[1:]; "
            "\nfor target in targets:\n"
            " try:\n  fd=os.open(target,os.O_WRONLY|os.O_TRUNC); os.close(fd); failed=True\n"
            " except OSError:\n  pass\n"
            "raise SystemExit(1 if failed else 0)"
        )
        attempted = subprocess.run(
            [
                "setpriv",
                "--reuid",
                str(custodian_uid),
                "--regid",
                str(shared_gid),
                "--clear-groups",
                "/usr/bin/python3",
                "-c",
                mutation_driver,
                *(str(path) for path in protected),
            ],
            check=False,
            env={"PATH": "/usr/bin:/bin"},
        )
        assert attempted.returncode == 0
    finally:
        if process is not None:
            process.terminate()
            process.wait(timeout=10)
        if qualification_root.parent == Path("/tmp") and qualification_root.name.startswith(
            "ras-core-authority-uids-"
        ):
            shutil.rmtree(qualification_root)
