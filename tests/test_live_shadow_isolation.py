from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
import subprocess
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from research_automation_supervisor.codex_adapter import (
    CodexProcessLaunch,
    build_codex_command,
)
from research_automation_supervisor.codex_models import (
    ROLE_POLICIES,
    CodexRunRequest,
    PreparedCodexRequest,
)
from research_automation_supervisor.errors import (
    LiveShadowDependencyError,
    LiveShadowInputError,
    LiveShadowIntegrityError,
)
from research_automation_supervisor.live_shadow_engine import (
    live_shadow_report,
    record_live_shadow_review,
    resume_live_shadow,
    run_live_shadow,
)
from research_automation_supervisor.live_shadow_isolation import (
    ISOLATED_ACTION_DIRECTORY,
    ISOLATED_CODEX_PATH,
    ISOLATED_HOME,
    ISOLATED_OUTPUT_SCHEMA_PATH,
    ISOLATED_TMPDIR,
    ISOLATED_WORKSPACE_PATH,
    BubblewrapBackendIdentity,
    BubblewrapCapability,
    _Mount,
    _validate_mount_allowlist,
    build_bubblewrap_process_launch,
    preflight_bubblewrap_isolation,
)
from research_automation_supervisor.live_shadow_models import LiveShadowResult
from tests.live_shadow_helpers import (
    create_live_shadow_tree,
    live_supervisor_response,
)
from tests.shadow_helpers import (
    SOURCE_AUDITOR_UUID,
    SOURCE_WORKER_UUID,
    SUPERVISOR_UUID,
    supervisor_proposal,
    write_review,
)
from tests.workflow_helpers import (
    auditor_result,
    codex_response,
    git,
    worker_result,
)


def _prepared_supervisor(
    tmp_path: Path,
) -> tuple[
    PreparedCodexRequest,
    Path,
    Path,
    Path,
    Path,
    BubblewrapCapability,
]:
    run_root = tmp_path / "live-run"
    workspace = run_root / "quarantine" / "workspace"
    runtime_home = run_root / "quarantine" / "codex-home"
    schema = run_root / "decisions" / "worker_initial-r000-a001" / "output-schema.json"
    action = tmp_path / "action"
    codex = tmp_path / "codex"
    auth = tmp_path / "auth.json"
    prompt = tmp_path / "policy.md"
    for directory in (workspace, runtime_home, schema.parent, action):
        directory.mkdir(parents=True, exist_ok=True)
    schema.write_text('{"type":"object"}\n', encoding="ascii")
    codex.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
    codex.chmod(codex.stat().st_mode | stat.S_IXUSR)
    auth.write_text('{"subscription":"unit-test-secret"}\n', encoding="ascii")
    prompt.write_text("Supervisor policy.\n", encoding="utf-8")
    request = CodexRunRequest(
        schema_version=1,
        run_id="stage1-run",
        role="supervisor",
        workspace=str(workspace),
        prompt_path=str(prompt),
        model="gpt-5.6-sol",
        reasoning_effort="high",
        timeout_seconds=60,
    )
    prepared = PreparedCodexRequest(
        request_path=tmp_path / "live-shadow.yaml",
        request=request,
        workspace=workspace,
        prompt_path=prompt,
        prompt_bytes=b"blind supervisor input\n",
        prompt_sha256=hashlib.sha256(b"blind supervisor input\n").hexdigest(),
        policy=ROLE_POLICIES["supervisor"],
    )
    capability = BubblewrapCapability(
        identity=BubblewrapBackendIdentity(
            schema_version=1,
            isolation_schema_version=1,
            backend="bubblewrap",
            canonical_bubblewrap_path="/usr/bin/bwrap",
            bubblewrap_version="bubblewrap 0.11.1",
            capability_result="passed",
        ),
        authentication_file=auth,
    )
    return prepared, run_root, runtime_home, schema, action, capability


def _production_launch(
    tmp_path: Path,
    *,
    resume: bool = False,
) -> tuple[CodexProcessLaunch, PreparedCodexRequest, Path, Path, Path, Path]:
    prepared, run_root, runtime_home, schema, action, capability = (
        _prepared_supervisor(tmp_path)
    )
    final_message = action / "last-message.md"
    semantic = build_codex_command(
        prepared,
        str(tmp_path / "codex"),
        final_message,
        output_schema=schema,
        resume_thread_id=SUPERVISOR_UUID if resume else None,
    )
    repository = tmp_path / "authoritative-repository"
    stage2_run = tmp_path / "authoritative-stage2-run"
    repository.mkdir()
    stage2_run.mkdir()
    launch = build_bubblewrap_process_launch(
        semantic,
        prepared,
        {"PATH": os.environ.get("PATH", ""), "LANG": "C.UTF-8"},
        final_message,
        schema,
        capability=capability,
        stage4_run_root=run_root,
        runtime_home=runtime_home,
        forbidden_roots=(repository, stage2_run),
    )
    return launch, prepared, run_root, schema, action, runtime_home


def _mount_arguments(command: tuple[str, ...]) -> list[tuple[str, str, str]]:
    result: list[tuple[str, str, str]] = []
    index = 0
    while index < command.index("--"):
        if command[index] in {"--bind", "--ro-bind"}:
            result.append(
                (command[index], command[index + 1], command[index + 2])
            )
            index += 3
        else:
            index += 1
    return result


def test_production_bubblewrap_argv_is_a_synthetic_allowlist(
    tmp_path: Path,
) -> None:
    launch, prepared, _, schema, action, runtime_home = _production_launch(
        tmp_path
    )
    command = launch.command
    separator = command.index("--")
    prefix = command[:separator]
    nested = command[separator + 1 :]

    assert command[0] == "/usr/bin/bwrap"
    assert prefix[1:15] == (
        "--die-with-parent",
        "--new-session",
        "--unshare-user",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--unshare-cgroup-try",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
        "--dir",
    )
    assert "--unshare-net" not in command
    assert nested[0] == ISOLATED_CODEX_PATH
    assert nested[nested.index("--cd") + 1] == ISOLATED_WORKSPACE_PATH
    assert (
        nested[nested.index("--output-last-message") + 1]
        == f"{ISOLATED_ACTION_DIRECTORY}/last-message.md"
    )
    assert (
        nested[nested.index("--output-schema") + 1]
        == ISOLATED_OUTPUT_SCHEMA_PATH
    )
    assert str(prepared.workspace) not in nested
    assert str(schema) not in nested
    assert str(action) not in nested
    mounts = _mount_arguments(command)
    assert ("--ro-bind", str(prepared.workspace), "/workspace") in mounts
    assert ("--bind", str(action), "/action") in mounts
    assert ("--ro-bind", str(schema), "/control/output-schema.json") in mounts
    assert ("--bind", str(runtime_home), "/home/supervisor") in mounts


def test_mounts_never_bind_host_root_home_mnt_or_authority(
    tmp_path: Path,
) -> None:
    launch, _, _, _, _, _ = _production_launch(tmp_path)
    mounts = _mount_arguments(launch.command)
    sources = {source for _, source, _ in mounts}
    destinations = {destination for _, _, destination in mounts}
    forbidden_sources = {
        "/",
        "/home",
        "/mnt",
        str(tmp_path / "authoritative-repository"),
        str(tmp_path / "authoritative-stage2-run"),
    }
    assert not sources.intersection(forbidden_sources)
    assert "/" not in destinations
    assert "/home" not in destinations
    assert "/mnt" not in destinations
    assert "--ro-bind" in launch.command
    assert ("--ro-bind", "/", "/") not in mounts


def test_isolated_environment_and_only_two_writable_mounts(
    tmp_path: Path,
) -> None:
    launch, _, _, _, action, runtime_home = _production_launch(tmp_path)
    assert launch.environment["HOME"] == ISOLATED_HOME
    assert launch.environment["CODEX_HOME"] == ISOLATED_HOME
    assert launch.environment["TMPDIR"] == ISOLATED_TMPDIR
    assert launch.environment["LANG"] == "C.UTF-8"
    assert {
        (source, destination)
        for option, source, destination in _mount_arguments(launch.command)
        if option == "--bind"
    } == {
        (str(action), ISOLATED_ACTION_DIRECTORY),
        (str(runtime_home), ISOLATED_HOME),
    }


def test_initial_and_exact_uuid_resume_use_the_same_isolation(
    tmp_path: Path,
) -> None:
    initial, *_ = _production_launch(tmp_path / "initial")
    resumed, *_ = _production_launch(tmp_path / "resumed", resume=True)
    for command in (initial.command, resumed.command):
        assert command[0] == "/usr/bin/bwrap"
        assert "--proc" in command
        assert "--dev" in command
        assert "--tmpfs" in command
        assert "--unshare-net" not in command
    nested = resumed.command[resumed.command.index("--") + 1 :]
    resume_index = nested.index("resume")
    assert nested[resume_index + 1] == SUPERVISOR_UUID
    assert "--last" not in nested
    assert "--all" not in nested


def test_mount_rejects_symlink_into_authoritative_repository(
    tmp_path: Path,
) -> None:
    prepared, run_root, runtime_home, schema, action, capability = (
        _prepared_supervisor(tmp_path)
    )
    repository = tmp_path / "repository"
    repository.mkdir()
    real_workspace = repository / "stolen"
    real_workspace.mkdir()
    prepared.workspace.rmdir()
    prepared.workspace.symlink_to(real_workspace, target_is_directory=True)
    semantic = build_codex_command(
        prepared,
        str(tmp_path / "codex"),
        action / "last-message.md",
        output_schema=schema,
    )
    with pytest.raises(LiveShadowIntegrityError, match="symlink"):
        build_bubblewrap_process_launch(
            semantic,
            prepared,
            {},
            action / "last-message.md",
            schema,
            capability=capability,
            stage4_run_root=run_root,
            runtime_home=runtime_home,
            forbidden_roots=(repository, tmp_path / "stage2-run"),
        )


def test_mount_rejects_duplicate_and_overlapping_destinations(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    workspace = tmp_path / "workspace"
    action = tmp_path / "action"
    schema = tmp_path / "schema.json"
    runtime = tmp_path / "runtime"
    forbidden_a = tmp_path / "repository"
    forbidden_b = tmp_path / "stage2-run"
    for directory in (
        source,
        workspace,
        action,
        runtime,
        forbidden_a,
        forbidden_b,
    ):
        directory.mkdir()
    schema.write_text("{}\n", encoding="ascii")
    duplicate = (
        _Mount("--bind", action, "/action", "action-output"),
        _Mount("--bind", runtime, "/home/supervisor", "codex-runtime-home"),
        _Mount("--ro-bind", source, "/workspace", "one"),
        _Mount("--ro-bind", source, "/workspace", "two"),
    )
    with pytest.raises(LiveShadowIntegrityError, match="duplicate"):
        _validate_mount_allowlist(
            duplicate,
            forbidden_roots=(forbidden_a, forbidden_b),
            stage4_run_root=None,
            workspace=workspace,
            action_directory=action,
            output_schema=schema,
            runtime_home=runtime,
        )


def test_real_preflight_runs_without_invoking_codex(
    tmp_path: Path,
) -> None:
    codex = tmp_path / "must-not-run"
    codex.write_text("#!/bin/sh\nexit 99\n", encoding="ascii")
    codex.chmod(codex.stat().st_mode | stat.S_IXUSR)
    auth = tmp_path / "auth.json"
    auth.write_text("{}\n", encoding="ascii")
    repository = tmp_path / "repository"
    stage2 = tmp_path / "stage2-run"
    repository.mkdir()
    stage2.mkdir()
    capability = preflight_bubblewrap_isolation(
        bubblewrap_executable="/usr/bin/bwrap",
        codex_executable=str(codex),
        authentication_file=auth,
        environ={},
        forbidden_roots=(repository, stage2),
    )
    assert capability.identity.capability_result == "passed"
    assert capability.identity.bubblewrap_version.startswith("bubblewrap ")


def test_authentication_material_is_absent_from_dependency_errors(
    tmp_path: Path,
) -> None:
    secret = "AUTH-CONTENTS-MUST-NEVER-BE-REPORTED"
    missing_auth = tmp_path / secret / "auth.json"
    codex = tmp_path / "codex"
    codex.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
    codex.chmod(codex.stat().st_mode | stat.S_IXUSR)
    repository = tmp_path / "repository"
    stage2 = tmp_path / "stage2"
    repository.mkdir()
    stage2.mkdir()
    with pytest.raises(LiveShadowDependencyError) as captured:
        preflight_bubblewrap_isolation(
            bubblewrap_executable="/usr/bin/bwrap",
            codex_executable=str(codex),
            authentication_file=missing_auth,
            environ={},
            forbidden_roots=(repository, stage2),
        )
    assert secret not in str(captured.value)


def test_missing_bubblewrap_fails_before_stage2_or_supervisor_launch(
    tmp_path: Path,
) -> None:
    spec, _, _, _, base_services = create_live_shadow_tree(tmp_path)
    supervisor_launches = 0

    def forbidden_supervisor(*_: object, **__: object) -> Any:
        nonlocal supervisor_launches
        supervisor_launches += 1
        raise AssertionError("unisolated fallback launched")

    services = replace(
        base_services,
        supervisor_invoker=forbidden_supervisor,  # type: ignore[arg-type]
        isolation_preflight=preflight_bubblewrap_isolation,
        bubblewrap_executable="/usr/bin/definitely-missing-bwrap",
    )
    with pytest.raises(LiveShadowDependencyError, match="Bubblewrap"):
        run_live_shadow(
            spec,
            runs_dir=tmp_path / "live-runs",
            stage2_runs_dir=tmp_path / "stage2-runs",
            services=services,
        )
    assert not (tmp_path / "live-runs").exists()
    assert not (tmp_path / "stage2-runs").exists()
    assert supervisor_launches == 0


def test_failed_capability_probe_fails_before_stage2_launch(
    tmp_path: Path,
) -> None:
    spec, _, _, _, base_services = create_live_shadow_tree(tmp_path)

    def failed_preflight(**_: object) -> BubblewrapCapability:
        raise LiveShadowDependencyError(
            "Bubblewrap synthetic-root capability probe failed"
        )

    services = replace(
        base_services,
        isolation_preflight=failed_preflight,
    )
    with pytest.raises(LiveShadowDependencyError, match="capability"):
        run_live_shadow(
            spec,
            runs_dir=tmp_path / "live-runs",
            stage2_runs_dir=tmp_path / "stage2-runs",
            services=services,
        )
    assert not (tmp_path / "stage2-runs").exists()


def test_explicit_run_root_cannot_weaken_separation(
    tmp_path: Path,
) -> None:
    spec, _, project, _, services = create_live_shadow_tree(tmp_path)
    with pytest.raises(LiveShadowInputError, match="must be separate"):
        run_live_shadow(
            spec,
            runs_dir=project / "shadow-runs",
            stage2_runs_dir=tmp_path / "stage2-runs",
            services=services,
        )
    assert not (project / "shadow-runs").exists()


def test_resume_with_missing_backend_degrades_shadow_without_relaunching_stage2(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import research_automation_supervisor.live_shadow_engine as engine

    spec, _, _, _, services = create_live_shadow_tree(
        tmp_path,
        stage2_responses=[
            codex_response(
                "worker",
                SOURCE_WORKER_UUID,
                worker_result(),
                sleep_seconds=2.0,
            ),
            codex_response(
                "auditor",
                SOURCE_AUDITOR_UUID,
                auditor_result(),
            ),
        ],
    )
    interrupted = False

    def crash_after_first_envelope(point: str) -> None:
        nonlocal interrupted
        if interrupted or point != "after_state_replacement":
            return
        journals = tuple((tmp_path / "live-runs").glob("*/journal.jsonl"))
        if not journals:
            return
        lines = journals[0].read_text(encoding="ascii").splitlines()
        if lines and json.loads(lines[-1])["reason"] == (
            "live_decision_envelope_frozen"
        ):
            interrupted = True
            raise RuntimeError("crash before isolated supervisor launch")

    monkeypatch.setattr(engine, "_snapshot_checkpoint", crash_after_first_envelope)
    with pytest.raises(RuntimeError, match="before isolated"):
        run_live_shadow(
            spec,
            runs_dir=tmp_path / "live-runs",
            stage2_runs_dir=tmp_path / "stage2-runs",
            services=services,
        )
    assert interrupted
    run_directory = next((tmp_path / "live-runs").iterdir())
    authoritative_result_path = next(
        (tmp_path / "stage2-runs").glob("*/result.json")
    )
    assert (
        json.loads(authoritative_result_path.read_text(encoding="utf-8"))[
            "status"
        ]
        == "worker_running"
    )

    clock = [datetime(2040, 1, 1, tzinfo=UTC)]

    def unavailable(**_: object) -> BubblewrapCapability:
        raise LiveShadowDependencyError("Bubblewrap disappeared")

    def advance(_: float) -> None:
        clock[0] += timedelta(seconds=31)
        time.sleep(0.001)

    monkeypatch.setattr(engine, "_snapshot_checkpoint", lambda _: None)
    recovered = resume_live_shadow(
        run_directory,
        services=replace(
            services,
            isolation_preflight=unavailable,
            utc_now=lambda: clock[0],
            sleep=advance,
        ),
    )
    assert recovered.status == "shadow_degraded"
    assert recovered.authoritative_stage2_status == "completed"
    assert recovered.observed_decision_count == 2
    assert recovered.proposal_count == 2
    journal = [
        json.loads(line)
        for line in (run_directory / "journal.jsonl")
        .read_text(encoding="ascii")
        .splitlines()
    ]
    assert sum(
        entry["reason"] == "authoritative_stage2_launched"
        for entry in journal
    ) == 1
    state = json.loads(
        (run_directory / "state.json").read_text(encoding="utf-8")
    )
    assert any(
        failure["reason"] == "isolation_dependency_failure"
        for failure in state["shadow_failures"]
    )
    assert not (tmp_path / "live-shadow-counter").exists()


def test_resumed_blind_stdin_excludes_prior_comparison_and_candidate(
    tmp_path: Path,
) -> None:
    candidate_sentinel = "PRIOR-CANDIDATE-MUST-NOT-ENTER-RESUMED-STDIN"
    first = live_supervisor_response("worker_initial")
    first_proposal = json.loads(str(first["final"]))
    first_proposal["prompt"] = candidate_sentinel
    first["final"] = json.dumps(first_proposal, sort_keys=True)
    first["observation_path"] = str(tmp_path / "first-observation.json")
    second = live_supervisor_response("auditor", resume=True)
    second["observation_path"] = str(tmp_path / "second-observation.json")
    spec, _, _, _, services = create_live_shadow_tree(
        tmp_path,
        supervisor_responses=[first, second],
    )
    result = run_live_shadow(
        spec,
        runs_dir=tmp_path / "live-runs",
        stage2_runs_dir=tmp_path / "stage2-runs",
        services=services,
    )
    run_directory = Path(result.artifact_directory)
    resumed_prompt = base64.b64decode(
        json.loads(
            (tmp_path / "second-observation.json").read_text(encoding="utf-8")
        )["prompt_base64"]
    )
    comparison = (
        run_directory
        / "comparisons/worker_initial-r000-a001/comparison.json"
    ).read_bytes()
    assert candidate_sentinel.encode("utf-8") not in resumed_prompt
    assert comparison not in resumed_prompt
    assert b'"review_status":"reviewed"' not in resumed_prompt


def _write_isolation_fake(
    path: Path,
    *,
    authoritative_workspace: Path,
    worker_release: Path,
    future_sentinel: str,
) -> None:
    workspace_b64 = base64.b64encode(
        str(authoritative_workspace).encode("utf-8")
    ).decode("ascii")
    release_b64 = base64.b64encode(
        str(worker_release).encode("utf-8")
    ).decode("ascii")
    sentinel_b64 = base64.b64encode(future_sentinel.encode("utf-8")).decode(
        "ascii"
    )
    worker_final = worker_result()
    auditor_final = auditor_result()
    worker_proposal = supervisor_proposal("worker_initial")
    auditor_proposal = supervisor_proposal("auditor")
    source = f"""#!/usr/bin/python3
import base64
import json
import os
import sys
import time
from pathlib import Path

WORKSPACE = base64.b64decode({workspace_b64!r}).decode()
WORKER_RELEASE = base64.b64decode({release_b64!r}).decode()
SENTINEL = base64.b64decode({sentinel_b64!r}).decode()
SUPERVISOR_UUID = {SUPERVISOR_UUID!r}
WORKER_UUID = {SOURCE_WORKER_UUID!r}
AUDITOR_UUID = {SOURCE_AUDITOR_UUID!r}

def option(name):
    index = sys.argv.index(name)
    return sys.argv[index + 1]

def direct_read(target):
    try:
        if os.path.isdir(target):
            os.listdir(target)
            return False
        with open(target, "rb") as handle:
            handle.read(1)
        return False
    except OSError:
        return True

def supervisor():
    home = Path("/home/supervisor")
    release = home / "probe-release"
    targets = home / "targets.json"
    deadline = time.monotonic() + 15
    while (not release.is_file() or not targets.is_file()) and time.monotonic() < deadline:
        time.sleep(0.01)
    target_data = json.loads(targets.read_text(encoding="utf-8"))
    stage2 = target_data["stage2_run"]
    turn_path = home / "turn-count"
    turn = int(turn_path.read_text(encoding="ascii")) + 1 if turn_path.exists() else 1
    turn_path.write_text(str(turn), encoding="ascii")
    session_path = home / "session-id"
    if turn == 1:
        session_path.write_text(SUPERVISOR_UUID, encoding="ascii")
    session_ok = session_path.read_text(encoding="ascii") == SUPERVISOR_UUID
    resumed = "resume" in sys.argv
    exact_resume = (not resumed and turn == 1) or (
        resumed and sys.argv[sys.argv.index("resume") + 1] == SUPERVISOR_UUID
    )
    root_found = False
    for current, directories, files in os.walk("/", followlinks=False):
        directories[:] = [
            name for name in directories
            if str(Path(current) / name) not in {{"/proc", "/dev"}}
        ]
        if SENTINEL in files or SENTINEL in directories:
            root_found = True
            break
    inherited = []
    for item in Path("/proc/self/fd").iterdir():
        try:
            number = int(item.name)
            target = os.readlink(item)
        except (OSError, ValueError):
            continue
        if number > 2:
            inherited.append({{"fd": number, "target": target}})
    try:
        home_entries = sorted(item.name for item in Path("/home").iterdir())
    except OSError:
        home_entries = ["<unreadable>"]
    try:
        mnt_entries = sorted(item.name for item in Path("/mnt").iterdir())
    except OSError:
        mnt_entries = []
    try:
        Path("/workspace/write-probe").write_text("forbidden")
        workspace_read_only = False
    except OSError:
        workspace_read_only = True
    try:
        Path("/home/supervisor/auth.json").write_text("forbidden")
        auth_read_only = False
    except OSError:
        auth_read_only = True
    report = {{
        "turn": turn,
        "session_ok": session_ok,
        "exact_resume": exact_resume,
        "workspace_absent": not os.path.exists(WORKSPACE),
        "workspace_read_denied": direct_read(WORKSPACE),
        "stage2_absent": not os.path.exists(stage2),
        "stage2_read_denied": direct_read(stage2),
        "proc_self_denied": direct_read("/proc/self/root" + WORKSPACE),
        "proc_1_denied": direct_read("/proc/1/root" + WORKSPACE),
        "proc_self_root": os.readlink("/proc/self/root"),
        "proc_1_root": os.readlink("/proc/1/root"),
        "home_entries": home_entries,
        "mnt_entries": mnt_entries,
        "future_sentinel_found": root_found,
        "unexpected_fds": inherited,
        "workspace_read_only": workspace_read_only,
        "schema_readable": Path("/control/output-schema.json").is_file(),
        "auth_readable": Path("/home/supervisor/auth.json").is_file(),
        "auth_read_only": auth_read_only,
        "home": os.environ.get("HOME"),
        "codex_home": os.environ.get("CODEX_HOME"),
        "tmpdir": os.environ.get("TMPDIR"),
    }}
    (home / f"probe-{{turn}}.json").write_text(
        json.dumps(report, sort_keys=True), encoding="utf-8"
    )
    proposal = {worker_proposal!r} if turn == 1 else {auditor_proposal!r}
    Path(option("--output-last-message")).write_text(proposal, encoding="utf-8")
    print(json.dumps({{"type": "thread.started", "thread_id": SUPERVISOR_UUID}}))
    return 0

def main():
    if sys.argv[1:] == ["--version"]:
        print("codex-cli 0.200.0")
        return 0
    sandbox = option("--sandbox")
    ephemeral = "--ephemeral" in sys.argv
    if sandbox == "read-only" and not ephemeral:
        sys.stdin.buffer.read()
        return supervisor()
    if sandbox == "workspace-write":
        Path({str(worker_release.parent / "worker-ready")!r}).write_text("ready")
        deadline = time.monotonic() + 15
        while not Path(WORKER_RELEASE).is_file() and time.monotonic() < deadline:
            time.sleep(0.01)
        final = {worker_final!r}
        thread = WORKER_UUID
    else:
        final = {auditor_final!r}
        thread = AUDITOR_UUID
    sys.stdin.buffer.read()
    Path(option("--output-last-message")).write_text(final, encoding="utf-8")
    print(json.dumps({{"type": "thread.started", "thread_id": thread}}))
    return 0

raise SystemExit(main())
"""
    path.write_text(source, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def test_real_bubblewrap_denies_authority_proc_roots_and_future_sentinel(
    tmp_path: Path,
) -> None:
    if not Path("/usr/bin/bwrap").is_file():
        pytest.skip("Bubblewrap executable genuinely does not exist")
    user_namespace_probe = subprocess.run(
        [
            "/usr/bin/bwrap",
            "--unshare-user",
            "--unshare-pid",
            "--proc",
            "/proc",
            "--ro-bind",
            "/usr",
            "/usr",
            "--ro-bind",
            "/lib",
            "/lib",
            "--ro-bind",
            "/lib64",
            "/lib64",
            "--",
            "/usr/bin/true",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        close_fds=True,
    )
    if user_namespace_probe.returncode != 0:
        pytest.skip("Bubblewrap user namespaces are genuinely unavailable")
    spec, _, project, _, base_services = create_live_shadow_tree(tmp_path)
    (project / ".gitignore").write_text(".future-sentinel-*\n", encoding="ascii")
    git(project, "add", ".gitignore")
    git(project, "commit", "-q", "-m", "ignore isolation sentinel")
    future_sentinel = f".future-sentinel-{hashlib.sha256(os.urandom(32)).hexdigest()}"
    worker_release = tmp_path / "coordination" / "worker-release"
    worker_release.parent.mkdir()
    fake = tmp_path / "adversarial-fake-codex"
    _write_isolation_fake(
        fake,
        authoritative_workspace=project,
        worker_release=worker_release,
        future_sentinel=future_sentinel,
    )
    auth_secret = f"AUTH-MATERIAL-{hashlib.sha256(os.urandom(32)).hexdigest()}"
    auth = tmp_path / "subscription-auth.json"
    auth.write_text(json.dumps({"token": auth_secret}), encoding="ascii")
    environment = dict(os.environ)
    environment.pop("FAKE_CODEX_CONFIG", None)
    services = replace(
        base_services,
        codex_executable=str(fake),
        supervisor_invoker=None,
        isolation_preflight=preflight_bubblewrap_isolation,
        bubblewrap_executable="/usr/bin/bwrap",
        codex_authentication_file=auth,
        environ=environment,
    )
    outcome: dict[str, Any] = {}

    def run() -> None:
        try:
            outcome["result"] = run_live_shadow(
                spec,
                runs_dir=tmp_path / "live-runs",
                stage2_runs_dir=tmp_path / "stage2-runs",
                services=services,
            )
        except BaseException as exc:
            outcome["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    run_directory: Path | None = None
    stage2_run: Path | None = None
    while time.monotonic() < deadline:
        candidates = tuple((tmp_path / "live-runs").glob("*"))
        if candidates:
            run_directory = candidates[0]
            journal = run_directory / "journal.jsonl"
            run_record = run_directory / "authoritative" / "stage2-run.json"
            if journal.is_file() and run_record.is_file():
                reasons = {
                    json.loads(line)["reason"]
                    for line in journal.read_text(encoding="ascii").splitlines()
                }
                if "live_decision_envelope_frozen" in reasons:
                    stage2_run = Path(
                        json.loads(run_record.read_text(encoding="utf-8"))[
                            "run_directory"
                        ]
                    )
                    break
        time.sleep(0.01)
    assert run_directory is not None
    assert stage2_run is not None

    (project / future_sentinel).write_text(
        future_sentinel,
        encoding="ascii",
    )
    runtime_home = run_directory / "quarantine" / "codex-home"
    (runtime_home / "targets.json").write_text(
        json.dumps({"stage2_run": str(stage2_run)}),
        encoding="utf-8",
    )
    (runtime_home / "probe-release").write_text("release", encoding="ascii")
    worker_release.write_text("release", encoding="ascii")
    thread.join(timeout=20)
    assert not thread.is_alive()
    assert "error" not in outcome, repr(outcome.get("error"))
    result = outcome["result"]
    assert isinstance(result, LiveShadowResult)
    assert result.status == "awaiting_reviews"
    assert result.supervisor_session_id == SUPERVISOR_UUID

    reports = [
        json.loads((runtime_home / f"probe-{turn}.json").read_text(encoding="utf-8"))
        for turn in (1, 2)
    ]
    for turn, report in enumerate(reports, start=1):
        assert report["turn"] == turn
        assert report["session_ok"] is True
        assert report["exact_resume"] is True
        assert report["workspace_absent"] is True
        assert report["workspace_read_denied"] is True
        assert report["stage2_absent"] is True
        assert report["stage2_read_denied"] is True
        assert report["proc_self_denied"] is True
        assert report["proc_1_denied"] is True
        assert report["proc_self_root"] == "/"
        assert report["proc_1_root"] == "/"
        assert report["home_entries"] == ["supervisor"]
        assert report["mnt_entries"] == []
        assert report["future_sentinel_found"] is False
        assert report["unexpected_fds"] == []
        assert report["workspace_read_only"] is True
        assert report["schema_readable"] is True
        assert report["auth_readable"] is True
        assert report["auth_read_only"] is True
        assert report["home"] == ISOLATED_HOME
        assert report["codex_home"] == ISOLATED_HOME
        assert report["tmpdir"] == ISOLATED_TMPDIR

    second_metadata = json.loads(
        (
            run_directory
            / "proposals/auditor-r000-a002/stage1-run/metadata.json"
        ).read_text(encoding="utf-8")
    )
    command = second_metadata["command"]
    resume_index = command.index("resume")
    assert command[resume_index + 1] == SUPERVISOR_UUID
    assert command[0] == "/usr/bin/bwrap"
    assert "--last" not in command
    assert "--all" not in command

    for index, proposal_id in enumerate(
        ("worker_initial-r000-a001", "auditor-r000-a002"),
        start=1,
    ):
        record_live_shadow_review(
            run_directory,
            proposal_id,
            write_review(
                tmp_path / f"integration-review-{index}.yaml",
                proposal_id,
            ),
            services=services,
        )
    report_bytes = json.dumps(
        live_shadow_report(run_directory),
        sort_keys=True,
    ).encode("utf-8")

    sentinel_bytes = future_sentinel.encode("utf-8")
    auth_bytes = auth_secret.encode("utf-8")
    assert sentinel_bytes not in report_bytes
    assert auth_bytes not in report_bytes
    for artifact in run_directory.rglob("*"):
        if artifact.is_file():
            content = artifact.read_bytes()
            assert sentinel_bytes not in content, artifact
            assert auth_bytes not in content, artifact
