from __future__ import annotations

import grp
import hashlib
import json
import os
import pwd
import shutil
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import suppress
from pathlib import Path

import pytest

import research_automation_supervisor.gitless_repository as gitless_repository_module
from research_automation_supervisor.core_authority_client import UnixCoreAuthorityClient
from research_automation_supervisor.core_authority_models import CampaignLaunchRequestV1
from research_automation_supervisor.custodian_errors import CustodianStateError
from research_automation_supervisor.custodian_models import (
    CampaignProfileSettingsV1,
    FrozenInputFileV1,
)
from tests.custodian_helpers import create_repository


def _set_restrictive_service_umask() -> None:
    os.umask(0o077)


def _start_service(
    tmp_path: Path,
    *,
    operator_uid: int | None = None,
    crash_at: str | None = None,
    environment: dict[str, str] | None = None,
) -> tuple[subprocess.Popen[bytes], UnixCoreAuthorityClient, Path]:
    socket_path = tmp_path / "run/authority.sock"
    authority = tmp_path / "service/authority"
    snapshots = tmp_path / "service/snapshots"
    previous_socket_inode = socket_path.lstat().st_ino if socket_path.exists() else None
    service_command = [
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
    ]
    if crash_at is not None:
        service_command.extend(("--qualification-crash-at", crash_at))
    process = subprocess.Popen(
        service_command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        preexec_fn=_set_restrictive_service_umask,
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stderr = process.stderr.read().decode() if process.stderr else ""
            raise AssertionError(f"Core service stopped during startup: {stderr}")
        if socket_path.is_socket() and socket_path.lstat().st_ino != previous_socket_inode:
            break
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
        repository.rename(tmp_path / "original-after-commit")
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
        assert stat.S_IMODE((tmp_path / "run").stat().st_mode) == 0o750
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


@pytest.mark.parametrize(
    ("boundary", "committed", "state"),
    (
        ("before_input_object_creation", False, None),
        ("during_input_object_creation", False, None),
        ("after_object_fsync_before_db_transaction", False, None),
        ("during_start_transaction", False, None),
        ("immediately_before_commit", False, None),
        ("immediately_after_commit_before_response", True, "absent"),
        ("during_repository_snapshot_staging", True, "building"),
        ("after_snapshot_content_before_snapshot_db_commit", True, "building"),
        ("after_snapshot_commit_before_campaign_launch", True, "complete"),
        ("after_ipc_response_before_custodian_card", True, "complete"),
    ),
)
def test_actual_service_process_crash_matrix_has_zero_or_one_start(
    tmp_path: Path,
    boundary: str,
    committed: bool,
    state: str | None,
) -> None:
    repository = create_repository(tmp_path)
    preview_process, preview_client, _ = _start_service(tmp_path)
    request = _request(preview_client, repository)
    preview_process.terminate()
    preview_process.wait(timeout=10)

    crashed, client, _ = _start_service(tmp_path, crash_at=boundary)
    try:
        with suppress(CustodianStateError):
            client.create_start_intent(request)
        crashed.wait(timeout=20)
        assert crashed.returncode == 91
    finally:
        if crashed.poll() is None:
            crashed.terminate()
            crashed.wait(timeout=10)

    restarted, recovered, _ = _start_service(tmp_path)
    try:
        starts = recovered.list_operator_campaigns()
        assert len(starts) == int(committed)
        if committed:
            assert starts[0].snapshot_state == state
            resumed = recovered.resume_start_snapshot(starts[0].launch_intent_id)
            assert resumed.snapshot_state == "complete"
        retry = recovered.create_start_intent(request)
        assert len(recovered.list_operator_campaigns()) == 1
        assert recovered.create_start_intent(request) == retry
    finally:
        restarted.terminate()
        restarted.wait(timeout=10)


def test_runtime_exec_interception_observes_zero_pre_snapshot_execs(tmp_path: Path) -> None:
    interceptor = tmp_path / "exec_interceptor.c"
    library = tmp_path / "exec_interceptor.so"
    interceptor.write_text(
        r"""
#define _GNU_SOURCE
#include <dlfcn.h>
#include <fcntl.h>
#include <spawn.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
static void record(const char *path) {
    const char *log = getenv("RAS_EXEC_INTERCEPT_LOG");
    if (!log) return;
    int fd = open(log, O_WRONLY | O_CREAT | O_APPEND, 0600);
    if (fd >= 0) { write(fd, path, strlen(path)); write(fd, "\n", 1); close(fd); }
}
int execve(const char *path, char *const argv[], char *const envp[]) {
    static int (*real_execve)(const char *, char *const[], char *const[]) = 0;
    if (!real_execve) real_execve = dlsym(RTLD_NEXT, "execve");
    record(path); return real_execve(path, argv, envp);
}
int posix_spawn(pid_t *pid, const char *path,
    const posix_spawn_file_actions_t *actions, const posix_spawnattr_t *attributes,
    char *const argv[], char *const envp[]) {
    static int (*real_spawn)(pid_t *, const char *, const posix_spawn_file_actions_t *,
        const posix_spawnattr_t *, char *const[], char *const[]) = 0;
    if (!real_spawn) real_spawn = dlsym(RTLD_NEXT, "posix_spawn");
    record(path); return real_spawn(pid, path, actions, attributes, argv, envp);
}
int system(const char *command) {
    static int (*real_system)(const char *) = 0;
    if (!real_system) real_system = dlsym(RTLD_NEXT, "system");
    record(command ? command : "<null-system>"); return real_system(command);
}
""",
        encoding="utf-8",
    )
    compiled = subprocess.run(
        ["cc", "-shared", "-fPIC", "-o", str(library), str(interceptor), "-ldl"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert compiled.returncode == 0, compiled.stderr
    repository = create_repository(tmp_path)
    service_log = tmp_path / "service-execs.log"
    client_log = tmp_path / "client-execs.log"
    preload = {**os.environ, "LD_PRELOAD": str(library)}
    preload["RAS_EXEC_INTERCEPT_LOG"] = str(service_log)
    process, _client, _ = _start_service(tmp_path, environment=preload)
    try:
        driver = (
            "import hashlib,sys; from pathlib import Path; "
            "from research_automation_supervisor.core_authority_client import "
            "UnixCoreAuthorityClient; "
            "from research_automation_supervisor.core_authority_models import "
            "CampaignLaunchRequestV1; "
            "from research_automation_supervisor.custodian_models import "
            "CampaignProfileSettingsV1,FrozenInputFileV1; "
            "client=UnixCoreAuthorityClient(Path(sys.argv[1])); "
            "requested=client.inspect_repository('existing_folder',sys.argv[2]); "
            "request=CampaignLaunchRequestV1(preview_id='preview-'+('e'*24),"
            "client_start_key_sha256=hashlib.sha256(b'exec intercept').hexdigest(),"
            "human_name='Exec interception',repository=requested,"
            "research_contract=FrozenInputFileV1.from_bytes('c',b'c\\n'),"
            "research_plan=FrozenInputFileV1.from_bytes('p',b'p\\n'),"
            "initial_task=FrozenInputFileV1.from_bytes('t',b't\\n'),"
            "requested_settings=CampaignProfileSettingsV1()); "
            "reference=client.create_start_intent(request); assert reference.snapshot_identity"
        )
        client_environment = {
            **os.environ,
            "LD_PRELOAD": str(library),
            "RAS_EXEC_INTERCEPT_LOG": str(client_log),
        }
        launched = subprocess.run(
            [sys.executable, "-c", driver, str(_client.socket_path), str(repository)],
            check=False,
            capture_output=True,
            text=True,
            env=client_environment,
        )
        assert launched.returncode == 0, launched.stderr
        assert not service_log.exists() or not service_log.read_text().strip()
        assert not client_log.exists() or not client_log.read_text().strip()
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
            "/usr/bin/python3",
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
def test_actual_service_identity_delegates_only_the_sanitized_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core_uid = pwd.getpwnam("research-supervisor-core").pw_uid
    custodian_uid = int(os.environ.get("SUDO_UID", "1000"))
    shared_gid = grp.getgrnam("research-supervisor-custodian").gr_gid
    assert core_uid != custodian_uid
    trust_parent = Path(os.environ.get("PA5C4_ROOT_TEST_TRUST_PARENT", "/var/lib"))
    trust_directory = tempfile.TemporaryDirectory(
        dir=trust_parent, prefix="ras-core-trust-anchor-"
    )
    trust_service_root = Path(trust_directory.name)
    os.chown(trust_service_root, core_uid, shared_gid)
    trust_service_root.chmod(0o711)
    trust_snapshot_root = trust_service_root / "snapshots"
    trust_snapshot_root.mkdir(mode=0o710)
    os.chown(trust_snapshot_root, core_uid, shared_gid)
    trust_workspace_root = trust_snapshot_root / "workspaces"
    trust_workspace_root.mkdir(mode=0o710)
    os.chown(trust_workspace_root, core_uid, shared_gid)
    trust_workspace_root.chmod(0o2710)
    monkeypatch.setattr(
        gitless_repository_module,
        "_PRODUCTION_SNAPSHOT_ROOT",
        trust_snapshot_root,
    )
    gitless_repository_module._verify_production_trust_anchor(
        trust_snapshot_root, core_uid
    )
    qualification_root = Path(tempfile.mkdtemp(prefix="ras-core-authority-uids-"))
    process: subprocess.Popen[bytes] | None = None
    try:
        qualification_root.chmod(0o755)
        runtime = qualification_root / "run"
        service_root = qualification_root / "service"
        operator_root = qualification_root / "operator"
        for path, uid, mode in (
            (runtime, core_uid, 0o750),
            (service_root, core_uid, 0o710),
            (operator_root, custodian_uid, 0o700),
        ):
            path.mkdir()
            os.chown(path, uid, shared_gid)
            path.chmod(mode)
        repository = create_repository(operator_root)
        for path in (repository, *repository.rglob("*")):
            os.chown(path, custodian_uid, shared_gid, follow_symlinks=False)
            if path.is_dir():
                path.chmod(0o700)
            elif not path.is_symlink():
                path.chmod(0o700 if path.stat().st_mode & 0o111 else 0o600)

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
        installed_python = Path(
            os.environ.get(
                "PA5C4_INSTALLED_CORE_PYTHON",
                "/opt/research-supervisor-core/venv/bin/python",
            )
        )
        assert installed_python.is_file() and os.access(installed_python, os.X_OK)
        process = subprocess.Popen(
            [
                "/usr/bin/setpriv",
                "--reuid",
                str(core_uid),
                "--regid",
                str(shared_gid),
                "--groups",
                str(Path.cwd().stat().st_gid),
                str(installed_python),
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
        socket_attack = subprocess.run(
            [
                "setpriv",
                "--reuid",
                str(custodian_uid),
                "--regid",
                str(shared_gid),
                "--clear-groups",
                "/usr/bin/python3",
                "-c",
                (
                    "import os,socket,sys; failed=False; path=sys.argv[1]; "
                    "\ntry:\n os.unlink(path); failed=True\nexcept OSError:\n pass\n"
                    "\ntry:\n s=socket.socket(socket.AF_UNIX); s.bind(path); failed=True\n"
                    "except OSError:\n pass\n"
                    "raise SystemExit(1 if failed else 0)"
                ),
                str(socket_path),
            ],
            check=False,
            env={"PATH": "/usr/bin:/bin"},
        )
        assert socket_attack.returncode == 0
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
            "import research_automation_supervisor.gitless_repository as gitless; "
            "gitless._required_snapshot_root=lambda:Path(sys.argv[5]); "
            "from research_automation_supervisor.safe_git import safe_git_text; "
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
            "os.umask(0o007); mutable=workspace/'worker-created'; "
            "mutable.mkdir(mode=0o770); "
            "(mutable/'result.txt').write_text('worker output\\n'); "
            "assert safe_git_text(workspace,'rev-parse','HEAD') == "
            "material.input_bundle.repository.baseline_commit; "
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
                str(snapshots),
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
        assert stat.S_IMODE((snapshots / "workspaces").stat().st_mode) == 0o2710
        assert (snapshots / "workspaces").stat().st_gid == shared_gid
        workspace = Path(identities["workspace"])
        worker_directory = workspace / "worker-created"
        worker_output = worker_directory / "result.txt"
        assert stat.S_IMODE(workspace.stat().st_mode) == 0o3770
        assert workspace.stat().st_uid == core_uid
        assert workspace.stat().st_gid == shared_gid
        assert stat.S_IMODE(worker_directory.stat().st_mode) == 0o2770
        assert worker_directory.stat().st_uid == custodian_uid
        assert worker_directory.stat().st_gid == shared_gid
        assert stat.S_IMODE(worker_output.stat().st_mode) == 0o660
        assert worker_output.stat().st_uid == custodian_uid
        assert worker_output.stat().st_gid == shared_gid

        with sqlite3.connect(authority / "authority.sqlite3") as connection:
            row = connection.execute(
                "SELECT intent_object_sha256, frozen_object_sha256, current_snapshot_id "
                "FROM starts"
            ).fetchone()
        assert row is not None
        intent_object, frozen_object, snapshot_id = (str(item) for item in row)
        protected = (
            authority / "store-key-v1",
            authority / "authority.sqlite3",
            authority / "intents" / intent_object[:2] / f"{intent_object}.json",
            authority / "frozen-inputs" / frozen_object[:2] / f"{frozen_object}.json",
            snapshots / "complete" / snapshot_id / "snapshot-v1.json",
        )
        mutation_driver = (
            "import os,sys; failed=False; "
            "targets=sys.argv[1:]; "
            "\ntry:\n fd=os.open(targets[0],os.O_RDONLY); os.close(fd); failed=True\n"
            "except OSError:\n pass\n"
            "\nfor target in targets:\n"
            " try:\n  fd=os.open(target,os.O_WRONLY|os.O_TRUNC); os.close(fd); failed=True\n"
            " except OSError:\n  pass\n"
            " try:\n  fd=os.open(os.path.join(os.path.dirname(target),'forged-start'),"
            "os.O_WRONLY|os.O_CREAT,0o600); os.close(fd); failed=True\n"
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

        config_mutation_driver = (
            "import os,sys; config=sys.argv[1]; control=os.path.dirname(config); "
            "repository=os.path.dirname(control); replacement=config+'.replacement'; "
            "failed=False; targets=[config,os.path.join(control,'HEAD'),"
            "os.path.join(control,'config.worktree'),os.path.join(control,'info','attributes'),"
            "os.path.join(control,'objects','info','alternates')]; "
            "\nfor target in targets:\n"
            " try:\n  fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600); "
            "os.close(fd); failed=True\n except OSError:\n  pass\n"
            "\ntry:\n fd=os.open(replacement,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600); "
            "os.write(fd,b'[alias]\\n hostile = !false\\n'); os.close(fd); "
            "os.replace(replacement,config); failed=True\n"
            "except OSError:\n pass\n"
            "\ntry:\n os.rename(control,os.path.join(repository,'.git-replaced')); failed=True\n"
            "except OSError:\n pass\n"
            "\ntry:\n os.unlink(replacement)\nexcept OSError:\n pass\n"
            "raise SystemExit(1 if failed else 0)"
        )
        config_attempt = subprocess.run(
            [
                "setpriv",
                "--reuid",
                str(custodian_uid),
                "--regid",
                str(shared_gid),
                "--clear-groups",
                "/usr/bin/python3",
                "-c",
                config_mutation_driver,
                str(workspace / ".git" / "config"),
            ],
            check=False,
            env={"PATH": "/usr/bin:/bin"},
        )
        assert config_attempt.returncode == 0
    finally:
        if process is not None:
            process.terminate()
            process.wait(timeout=10)
        if qualification_root.parent == Path("/tmp") and qualification_root.name.startswith(
            "ras-core-authority-uids-"
        ):
            shutil.rmtree(qualification_root)
        trust_directory.cleanup()
