from __future__ import annotations

import contextlib
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
import yaml

import research_automation_supervisor.codex_adapter as adapter_module
from research_automation_supervisor.codex_adapter import AdapterLimits, run_prepared_codex
from research_automation_supervisor.codex_models import PreparedCodexRequest, load_codex_request
from research_automation_supervisor.execution_budget import ExecutionBudgetPolicyV1
from research_automation_supervisor.execution_budget_enforcement import (
    LiveExecutionBudgetControllerV1,
)
from research_automation_supervisor.process_enforcement import (
    PROCESS_TERMINATION_EVIDENCE_FILENAME,
    ContainmentControlError,
    OwnedProcessIdentityV1,
    ProcessEnforcementPolicyV1,
    ProcessTerminationEvidenceV1,
    SystemdStopResultV1,
    SystemdUnitInspectionV1,
    SystemdUserCgroupV2Backend,
    assess_process_termination_recovery,
    inspect_owned_process_group,
    load_process_termination_evidence,
    new_action_unit_name,
    parse_cgroup_events_populated,
    process_start_ticks,
    write_process_termination_evidence,
)
from research_automation_supervisor.systemd_launch_helper import __file__ as HELPER_FILE
from research_automation_supervisor.systemd_launch_helper import encode_environment_frame
from research_automation_supervisor.workflow_engine import (
    WorkflowServices,
    resume_substage,
    run_substage,
)
from tests.workflow_helpers import codex_response, create_workflow_tree, worker_result

FAKE_CODEX = (Path(__file__).parent / "fixtures" / "fake_codex.py").resolve()
THREAD_ID = "01a00000-0000-7000-8000-000000000003"


class FakeContainmentBackend:
    """Deterministic unit/cgroup facade; real hostile escapes use the host slice."""

    control_plane_timeout_seconds = 0.2

    def __init__(self) -> None:
        self.units: dict[str, tuple[int, str, str, OwnedProcessIdentityV1]] = {}
        self.launch_commands: list[tuple[str, ...]] = []
        self.runtime_max_seconds: float | None = None
        self.stop_count = 0
        self.fail_preflight = False
        self.fail_stop = False
        self.force_mismatch: str | None = None

    def preflight(self, unit_name: str) -> None:
        if self.fail_preflight:
            raise ContainmentControlError("injected unavailable user manager")
        assert unit_name not in self.units

    def build_launch_command(
        self,
        unit_name: str,
        command: Sequence[str],
        cwd: Path,
        stop_grace_seconds: float,
        runtime_max_seconds: float,
    ) -> tuple[str, ...]:
        del unit_name, cwd, stop_grace_seconds
        self.runtime_max_seconds = runtime_max_seconds
        built = (sys.executable, "-I", "-S", str(Path(HELPER_FILE).resolve()), *command)
        self.launch_commands.append(built)
        return built

    def bind_identity(self, unit_name: str, wrapper_pid: int) -> SystemdUnitInspectionV1:
        ticks = process_start_ticks(wrapper_pid)
        assert ticks is not None
        invocation = hashlib.sha256(unit_name.encode()).hexdigest()[:32]
        group = (
            f"/user.slice/user-{os.getuid()}.slice/user@{os.getuid()}.service/"
            f"app.slice/{unit_name}"
        )
        diagnostic = OwnedProcessIdentityV1(
            pid=wrapper_pid,
            process_group_id=os.getpgid(wrapper_pid),
            session_id=os.getsid(wrapper_pid),
            start_ticks=ticks,
        )
        self.units[unit_name] = (wrapper_pid, invocation, group, diagnostic)
        return self.inspect(unit_name, None, None)

    def inspect(
        self,
        unit_name: str,
        invocation_id: str | None,
        control_group: str | None,
    ) -> SystemdUnitInspectionV1:
        stored = self.units.get(unit_name)
        if stored is None:
            return SystemdUnitInspectionV1(state="absent", unit_name=unit_name)
        _, invocation, group, diagnostic = stored
        if self.force_mismatch == "invocation" or (
            invocation_id is not None and invocation != invocation_id
        ):
            return SystemdUnitInspectionV1(
                state="identity_mismatch",
                unit_name=unit_name,
                invocation_id="f" * 32,
                control_group=group,
                error="injected invocation mismatch",
            )
        if self.force_mismatch == "control_group" or (
            control_group is not None and group != control_group
        ):
            return SystemdUnitInspectionV1(
                state="identity_mismatch",
                unit_name=unit_name,
                invocation_id=invocation,
                control_group=group + "-replaced",
                error="injected cgroup mismatch",
            )
        live = inspect_owned_process_group(diagnostic).state == "verified_owned_group_present"
        return SystemdUnitInspectionV1(
            state="proven_live" if live else "proven_closed",
            unit_name=unit_name,
            invocation_id=invocation,
            control_group=group,
            active_state="active" if live else "inactive",
            sub_state="running" if live else "dead",
            unit_result="success",
            cgroup_empty=not live,
        )

    def stop(
        self,
        unit_name: str,
        invocation_id: str | None,
        control_group: str | None,
        stop_grace_seconds: float,
    ) -> SystemdStopResultV1:
        before = self.inspect(unit_name, invocation_id, control_group)
        self.stop_count += 1
        if self.fail_stop or before.state != "proven_live":
            return SystemdStopResultV1(
                status="failed",
                inspection=before,
                stop_requested=before.state == "proven_live",
                error="injected systemctl stop failure",
            )
        pid = self.units[unit_name][0]
        with contextlib.suppress(ProcessLookupError):
            os.killpg(pid, signal.SIGTERM)
        deadline = time.monotonic() + stop_grace_seconds
        while self.inspect(unit_name, invocation_id, control_group).state == "proven_live":
            if time.monotonic() >= deadline:
                break
            time.sleep(0.002)
        final_kill = self.inspect(unit_name, invocation_id, control_group).state == "proven_live"
        if final_kill:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(pid, signal.SIGKILL)
            deadline = time.monotonic() + self.control_plane_timeout_seconds
            while self.inspect(unit_name, invocation_id, control_group).state == "proven_live":
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.002)
        after = self.inspect(unit_name, invocation_id, control_group)
        return SystemdStopResultV1(
            status="closed" if after.state == "proven_closed" else "failed",
            inspection=after,
            stop_requested=True,
            final_kill_observed=final_kill,
            error=None if after.state == "proven_closed" else "fake cgroup remained live",
        )


def _prepared_request(
    tmp_path: Path,
    *,
    run_id: str = "worker-run",
    timeout_seconds: int = 30,
) -> PreparedCodexRequest:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    (tmp_path / "prompt.md").write_text("Bounded process test.\n", encoding="utf-8")
    request_path = tmp_path / "request.yaml"
    request_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "run_id": run_id,
                "role": "worker",
                "workspace": "workspace",
                "prompt_path": "prompt.md",
                "model": "gpt-5.6-sol",
                "reasoning_effort": "xhigh",
                "timeout_seconds": timeout_seconds,
            }
        ),
        encoding="utf-8",
    )
    return load_codex_request(request_path, git_worktree_checker=lambda _: True)


def _configure(prepared: PreparedCodexRequest, **configuration: object) -> None:
    (prepared.workspace / ".fake-codex.json").write_text(
        json.dumps(configuration),
        encoding="utf-8",
    )


def _jsonl(event: dict[str, object]) -> bytes:
    return json.dumps(event, separators=(",", ":"), sort_keys=True).encode() + b"\n"


def _session_meta() -> dict[str, object]:
    return {"type": "session_meta", "payload": {"id": THREAD_ID}}


def _usage(input_tokens: int) -> dict[str, int]:
    return {
        "input_tokens": input_tokens,
        "cached_input_tokens": 0,
        "cache_write_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_output_tokens": 0,
    }


def _token_count(input_tokens: int) -> dict[str, object]:
    return {
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {"total_token_usage": _usage(input_tokens)},
        },
    }


def _task_complete() -> dict[str, object]:
    return {"type": "event_msg", "payload": {"type": "task_complete"}}


def _tool_call() -> dict[str, object]:
    return {
        "type": "response_item",
        "payload": {
            "type": "custom_tool_call",
            "name": "exec",
            "input": 'text(await tools.exec_command({"cmd":"pwd"}));',
        },
    }


def _rollout_path(codex_home: Path) -> Path:
    path = (
        codex_home
        / "sessions"
        / "2026"
        / "08"
        / "20"
        / f"rollout-2026-08-20T11-20-30-{THREAD_ID}.jsonl"
    )
    path.parent.mkdir(parents=True)
    return path


def _write_rollout(codex_home: Path, events: list[dict[str, object]]) -> Path:
    path = _rollout_path(codex_home)
    path.write_bytes(b"".join(_jsonl(event) for event in events))
    return path


def _policy(**changes: int) -> ExecutionBudgetPolicyV1:
    return ExecutionBudgetPolicyV1(
        max_inference_samples=changes.get("max_inference_samples", 100),
        max_tool_calls=changes.get("max_tool_calls", 100),
        max_patch_calls=changes.get("max_patch_calls", 100),
        max_compactions=changes.get("max_compactions", 100),
        max_input_token_delta=changes.get("max_input_token_delta", 100_000_000),
    )


def _controller(
    tmp_path: Path,
    *,
    policy: ExecutionBudgetPolicyV1,
) -> LiveExecutionBudgetControllerV1:
    return LiveExecutionBudgetControllerV1.start_new_turn(
        checkpoint_path=tmp_path / "execution-budget.json",
        normalized_event_directory=tmp_path / "execution-budget-events",
        task_id="task-1",
        policy=policy,
    )


def _stdout_thread_started() -> str:
    return json.dumps({"thread_id": THREAD_ID, "type": "thread.started"})


def _stdout_completed(input_tokens: int = 1) -> str:
    return json.dumps(
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": input_tokens,
                "cached_input_tokens": 0,
                "output_tokens": 0,
                "reasoning_output_tokens": 0,
            },
        }
    )


def _environment(codex_home: Path) -> dict[str, str]:
    return {
        "PATH": os.environ["PATH"],
        "HOME": str(codex_home.parent / "home"),
        "CODEX_HOME": str(codex_home),
        "LANG": "C.UTF-8",
    }


def _run(
    prepared: PreparedCodexRequest,
    codex_home: Path,
    *,
    controller: LiveExecutionBudgetControllerV1 | None = None,
    process_policy: ProcessEnforcementPolicyV1 | None = None,
    monotonic: Any = time.monotonic,
    limits: AdapterLimits | None = None,
    process_finished: Any = None,
    process_started: Any = None,
    version_probe: Any = None,
    containment_backend: Any = None,
):
    enforcement_enabled = controller is not None or process_policy is not None
    return run_prepared_codex(
        prepared,
        runs_dir=prepared.request_path.parent / "runs",
        codex_executable=str(FAKE_CODEX),
        environ=_environment(codex_home),
        limits=limits
        or AdapterLimits(termination_grace_seconds=0.05, io_poll_seconds=0.005),
        monotonic=monotonic,
        execution_budget_controller=controller,
        process_enforcement_policy=process_policy,
        process_finished=process_finished,
        process_started=process_started,
        version_probe=version_probe,
        containment_backend=(
            containment_backend
            if containment_backend is not None
            else FakeContainmentBackend()
            if enforcement_enabled
            else None
        ),
    )


def _evidence(result: Any) -> ProcessTerminationEvidenceV1:
    return load_process_termination_evidence(
        Path(result.artifact_directory) / PROCESS_TERMINATION_EVIDENCE_FILENAME
    )


def _process_is_live(pid: int) -> bool:
    try:
        content = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    except OSError:
        return False
    close = content.rfind(")")
    return close >= 0 and content[close + 2 :].split()[0] != "Z"


def test_under_budget_process_exits_naturally_and_is_reaped_before_callback(
    tmp_path: Path,
) -> None:
    prepared = _prepared_request(tmp_path)
    codex_home = tmp_path / "codex-home"
    _write_rollout(codex_home, [_session_meta(), _token_count(1), _task_complete()])
    _configure(
        prepared,
        stdout_lines=[_stdout_thread_started(), _stdout_completed()],
        final="complete",
    )
    controller = _controller(tmp_path, policy=_policy())
    callback_phases: list[str] = []
    evidence_path = (
        tmp_path
        / "runs"
        / prepared.request.run_id
        / PROCESS_TERMINATION_EVIDENCE_FILENAME
    )

    result = _run(
        prepared,
        codex_home,
        controller=controller,
        process_finished=lambda _: callback_phases.append(
            load_process_termination_evidence(evidence_path).phase
        ),
    )
    evidence = _evidence(result)

    assert result.status == "succeeded"
    assert evidence.phase == "reaped"
    assert evidence.process_reaped is True
    assert evidence.containment_backend == "systemd_user_cgroup_v2"
    assert evidence.containment_closed is True
    assert evidence.cgroup_empty is True
    assert evidence.termination_reason is None
    assert evidence.graceful_termination_sent is False
    assert evidence.hard_kill_sent is False
    assert evidence.continuation_authorized is False
    assert callback_phases == ["reaped"]


def test_sample_64_stops_group_and_preserves_sample_65_as_explicit_tail(
    tmp_path: Path,
) -> None:
    prepared = _prepared_request(tmp_path)
    codex_home = tmp_path / "codex-home"
    meta = _jsonl(_session_meta())
    samples = [_jsonl(_token_count(index)) for index in range(1, 66)]
    rollout = _rollout_path(codex_home)
    rollout.write_bytes(meta + b"".join(samples))
    _configure(
        prepared,
        stdout_lines_before_sleep=[_stdout_thread_started()],
        sleep_seconds=30,
        final="unreachable",
    )
    controller = _controller(tmp_path, policy=_policy(max_inference_samples=64))
    callback_phases: list[str] = []
    evidence_path = (
        tmp_path
        / "runs"
        / prepared.request.run_id
        / PROCESS_TERMINATION_EVIDENCE_FILENAME
    )

    class StepClock:
        value = 0.0

        def __call__(self) -> float:
            self.value += 0.01
            return self.value

    unrelated = subprocess.Popen(["sleep", "10"])
    try:
        result = _run(
            prepared,
            codex_home,
            controller=controller,
            monotonic=StepClock(),
            process_finished=lambda _: callback_phases.append(
                load_process_termination_evidence(evidence_path).phase
            ),
        )
        evidence = _evidence(result)

        assert result.status == "bounded_continuation_required"
        assert controller.checkpoint.state.inference_sample_count == 64
        assert evidence.termination_reason == "execution_budget_exhausted"
        assert evidence.reached_hard_limits == ("max_inference_samples",)
        assert evidence.graceful_termination_sent is True
        assert evidence.process_reaped is True
        assert evidence.task_failure is False
        assert evidence.automatic_retry_or_repair is False
        expected_offset = len(meta + b"".join(samples[:64]))
        assert evidence.native_source_cursor_offset_at_stop == expected_offset
        assert evidence.final_rollout_size_bytes == rollout.stat().st_size
        assert evidence.unconsumed_tail_bytes == len(samples[64])
        assert evidence.unconsumed_tail_present is True
        assert evidence.decision_elapsed_seconds is not None
        assert evidence.decision_elapsed_seconds <= 0.12
        assert callback_phases == ["reaped"]
        assert unrelated.poll() is None
    finally:
        unrelated.terminate()
        unrelated.wait(timeout=2)


def test_accounting_integrity_failure_stops_without_relaunch(tmp_path: Path) -> None:
    prepared = _prepared_request(tmp_path)
    codex_home = tmp_path / "codex-home"
    malformed_usage = _token_count(1)
    payload = malformed_usage["payload"]
    assert isinstance(payload, dict)
    payload["info"] = {"total_token_usage": {"input_tokens": "invalid"}}
    _write_rollout(codex_home, [_session_meta(), malformed_usage])
    _configure(
        prepared,
        stdout_lines_before_sleep=[_stdout_thread_started()],
        sleep_seconds=30,
    )
    controller = _controller(tmp_path, policy=_policy())

    result = _run(prepared, codex_home, controller=controller)
    evidence = _evidence(result)

    assert result.status == "accounting_integrity_failure"
    assert evidence.termination_reason == (
        "execution_budget_accounting_integrity_failure"
    )
    assert evidence.graceful_termination_sent is True
    assert evidence.process_reaped is True
    assert evidence.containment_closed is True
    assert evidence.cgroup_empty is True
    assert evidence.automatic_retry_or_repair is False
    assert evidence.continuation_authorized is False


def test_sigterm_ignoring_process_group_is_hard_killed_with_descendant(
    tmp_path: Path,
) -> None:
    prepared = _prepared_request(tmp_path)
    codex_home = tmp_path / "codex-home"
    _configure(
        prepared,
        ignore_term=True,
        spawn_child_sleep=30,
        child_ignore_term=True,
        sleep_seconds=30,
    )

    result = _run(
        prepared,
        codex_home,
        process_policy=ProcessEnforcementPolicyV1(max_wall_clock_seconds=0.1),
    )
    evidence = _evidence(result)

    assert result.status == "wall_clock_limit_exceeded"
    assert evidence.graceful_termination_sent is True
    assert evidence.hard_kill_sent is True
    assert evidence.final_return_code is not None
    assert evidence.final_return_code < 0
    assert evidence.process_reaped is True
    assert evidence.owned_process_group_empty is True
    child_pid = int((prepared.workspace / ".fake-codex-child.pid").read_text())
    deadline = time.monotonic() + 2
    while _process_is_live(child_pid) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not _process_is_live(child_pid)


def test_leader_exit_does_not_close_action_until_surviving_group_is_killed(
    tmp_path: Path,
) -> None:
    prepared = _prepared_request(tmp_path)
    codex_home = tmp_path / "codex-home"
    _configure(
        prepared,
        spawn_child_sleep=30,
        child_ignore_term=True,
        leader_exit_after_child_ready=True,
    )
    callback_closed: list[bool | None] = []
    backend = FakeContainmentBackend()
    evidence_path = (
        tmp_path
        / "runs"
        / prepared.request.run_id
        / PROCESS_TERMINATION_EVIDENCE_FILENAME
    )

    result = _run(
        prepared,
        codex_home,
        process_policy=ProcessEnforcementPolicyV1(max_wall_clock_seconds=10.0),
        process_finished=lambda _: callback_closed.append(
            load_process_termination_evidence(
                evidence_path
            ).containment_closed
        ),
        containment_backend=backend,
    )
    evidence = _evidence(result)
    child_pid = int((prepared.workspace / ".fake-codex-child.pid").read_text())

    assert result.status == "missing_final_message"
    assert evidence.graceful_termination_sent is True
    assert evidence.hard_kill_sent is True
    assert evidence.process_reaped is True
    assert evidence.containment_closed is True
    assert evidence.cgroup_empty is True
    assert not _process_is_live(child_pid)
    assert callback_closed == [True]
    assert backend.stop_count == 1


def test_wall_clock_uses_injected_monotonic_and_never_estimates_usage(
    tmp_path: Path,
) -> None:
    prepared = _prepared_request(tmp_path)
    codex_home = tmp_path / "codex-home"
    _configure(prepared, sleep_seconds=30)

    class StepClock:
        value = 0.0

        def __call__(self) -> float:
            self.value += 0.01
            return self.value

    clock = StepClock()
    policy = ProcessEnforcementPolicyV1(max_wall_clock_seconds=0.03)
    result = _run(prepared, codex_home, process_policy=policy, monotonic=clock)
    evidence = _evidence(result)
    receipt = json.loads(
        (Path(result.artifact_directory) / "usage-receipt.json").read_text()
    )

    assert result.status == "wall_clock_limit_exceeded"
    assert evidence.termination_reason == "wall_clock_limit_exceeded"
    assert evidence.decision_elapsed_seconds is not None
    assert policy.max_wall_clock_seconds <= evidence.decision_elapsed_seconds <= 0.05
    assert evidence.budget_checkpoint_path is None
    assert receipt["complete"] is False
    assert receipt["incomplete_reasons"]
    assert receipt["input_tokens"] == 0
    assert receipt["output_tokens"] == 0
    assert receipt["combined_tokens"] == 0


def test_completed_token_budget_race_does_not_signal_but_retains_limit(
    tmp_path: Path,
) -> None:
    prepared = _prepared_request(tmp_path)
    codex_home = tmp_path / "codex-home"
    _write_rollout(codex_home, [_session_meta(), _token_count(11), _task_complete()])
    _configure(
        prepared,
        stdout_lines=[_stdout_thread_started(), _stdout_completed(11)],
        final="complete",
    )
    controller = _controller(tmp_path, policy=_policy(max_input_token_delta=10))

    result = _run(prepared, codex_home, controller=controller)
    evidence = _evidence(result)

    assert result.status == "succeeded"
    assert controller.outcome.decision == "completed"
    assert evidence.reached_hard_limits == ("max_input_token_delta",)
    assert evidence.termination_reason is None
    assert evidence.graceful_termination_sent is False
    assert evidence.hard_kill_sent is False


def test_environment_and_streaming_fidelity_without_metadata_secret(
    tmp_path: Path,
) -> None:
    prepared = _prepared_request(tmp_path)
    codex_home = tmp_path / "codex-home"
    _configure(
        prepared,
        stdout_lines=[_stdout_thread_started(), _stdout_completed()],
        stderr="stderr-remains-separate",
        final="complete",
    )
    secret = "sensitive-systemd-sentinel-03b"
    environment = _environment(codex_home)
    environment.update(
        {
            "OPENAI_API_KEY": secret,
            "SAFE_ENVIRONMENT_SENTINEL": "expected-safe-value",
        }
    )
    backend = FakeContainmentBackend()

    result = run_prepared_codex(
        prepared,
        runs_dir=prepared.request_path.parent / "runs",
        codex_executable=str(FAKE_CODEX),
        environ=environment,
        limits=AdapterLimits(termination_grace_seconds=0.05, io_poll_seconds=0.005),
        process_enforcement_policy=ProcessEnforcementPolicyV1(),
        containment_backend=backend,
    )
    evidence = _evidence(result)
    observed = json.loads(
        (prepared.workspace / ".fake-codex-observation.json").read_text()
    )

    assert result.status == "succeeded"
    assert observed["prompt_base64"] == "Qm91bmRlZCBwcm9jZXNzIHRlc3QuCg=="
    assert observed["environment"]["SAFE_ENVIRONMENT_SENTINEL"] == "expected-safe-value"
    assert "OPENAI_API_KEY" not in observed["environment"]
    assert result.event_count == 2
    assert (Path(result.artifact_directory) / "stderr.log").read_text() == (
        "stderr-remains-separate"
    )
    assert evidence.containment_closed is True
    assert all(secret not in item for command in backend.launch_commands for item in command)
    durable = b"".join(
        path.read_bytes()
        for path in Path(result.artifact_directory).iterdir()
        if path.is_file()
    )
    assert secret.encode() not in durable


def test_required_systemd_run_shape_contains_no_environment_values(tmp_path: Path) -> None:
    unit = "ras-codex-66666666666666666666666666666666.service"
    backend = SystemdUserCgroupV2Backend(
        {"PATH": os.environ["PATH"], "SAFE_ENV": "must-not-be-in-argv"},
        systemd_run_executable="/usr/bin/systemd-run",
    )
    command = backend.build_launch_command(
        unit,
        ("/usr/bin/printf", "hello"),
        tmp_path,
        0.25,
        12.5,
    )

    assert command[:4] == ("/usr/bin/systemd-run", "--user", "--quiet", "--pipe")
    assert f"--unit={unit}" in command
    assert "--property=Type=exec" in command
    assert "--property=KillMode=control-group" in command
    assert "--property=TimeoutStopSec=0.250s" in command
    assert "--property=RuntimeMaxSec=12.5s" in command
    assert "--property=KillSignal=SIGTERM" in command
    assert "--property=FinalKillSignal=SIGKILL" in command
    assert "--property=ProtectControlGroups=yes" in command
    assert (
        f"--property=InaccessiblePaths=/run/user/{os.getuid()}/bus "
        f"/run/user/{os.getuid()}/systemd"
    ) in command
    assert "--collect" not in command
    assert not any("setenv" in item for item in command)
    assert not any("must-not-be-in-argv" in item for item in command)


@pytest.mark.parametrize(
    ("request_timeout", "wall_clock_limit", "expected"),
    [(30, 60.0, 30.0), (30, 12.5, 12.5)],
)
def test_runtime_max_is_minimum_of_request_and_wall_clock(
    tmp_path: Path,
    request_timeout: int,
    wall_clock_limit: float,
    expected: float,
) -> None:
    prepared = _prepared_request(tmp_path, timeout_seconds=request_timeout)
    _configure(
        prepared,
        stdout_lines=[_stdout_thread_started(), _stdout_completed()],
        final="complete",
    )
    backend = FakeContainmentBackend()

    result = _run(
        prepared,
        tmp_path / "codex-home",
        process_policy=ProcessEnforcementPolicyV1(
            max_wall_clock_seconds=wall_clock_limit
        ),
        containment_backend=backend,
    )

    assert result.status == "succeeded"
    assert backend.runtime_max_seconds == expected


def test_user_manager_unavailable_fails_before_codex_launch(tmp_path: Path) -> None:
    prepared = _prepared_request(tmp_path)
    backend = FakeContainmentBackend()
    backend.fail_preflight = True
    started: list[int] = []

    result = _run(
        prepared,
        tmp_path / "codex-home",
        process_policy=ProcessEnforcementPolicyV1(),
        containment_backend=backend,
        process_started=started.append,
    )
    evidence = _evidence(result)

    assert result.status == "launch_failed"
    assert evidence.phase == "termination_failed"
    assert evidence.invocation_id is None
    assert evidence.containment_closed is None
    assert backend.launch_commands == []
    assert started == []
    assert not (prepared.workspace / ".fake-codex-observation.json").exists()


def test_ambiguous_initial_binding_reconciles_and_stops_exact_live_unit(
    tmp_path: Path,
) -> None:
    class AmbiguousInitialBindingBackend(FakeContainmentBackend):
        def bind_identity(
            self, unit_name: str, wrapper_pid: int
        ) -> SystemdUnitInspectionV1:
            super().bind_identity(unit_name, wrapper_pid)
            return SystemdUnitInspectionV1(
                state="ambiguous",
                unit_name=unit_name,
                error="injected initial identity timeout",
            )

    prepared = _prepared_request(tmp_path)
    _configure(prepared, sleep_seconds=30)
    backend = AmbiguousInitialBindingBackend()
    finished: list[int] = []

    result = _run(
        prepared,
        tmp_path / "codex-home",
        process_policy=ProcessEnforcementPolicyV1(),
        containment_backend=backend,
        process_finished=finished.append,
    )
    evidence = _evidence(result)

    assert result.status == "launch_failed"
    assert backend.stop_count == 1
    assert evidence.phase == "termination_failed"
    assert evidence.containment_stop_reason == "post_launch_identity_uncertainty"
    assert evidence.containment_closed is True
    assert evidence.cgroup_empty is True
    assert evidence.process_reaped is True
    assert finished == []


@pytest.mark.parametrize("mismatch", ["invocation", "control_group"])
def test_identity_mismatch_never_signals_or_authorizes_relaunch(
    tmp_path: Path,
    mismatch: str,
) -> None:
    prepared = _prepared_request(tmp_path)
    _configure(prepared, sleep_seconds=30)
    backend = FakeContainmentBackend()
    backend.force_mismatch = mismatch

    result = _run(
        prepared,
        tmp_path / "codex-home",
        process_policy=ProcessEnforcementPolicyV1(),
        containment_backend=backend,
    )
    evidence = _evidence(result)
    pid = next(iter(backend.units.values()))[0]
    try:
        assert result.status == "launch_failed"
        assert evidence.phase == "termination_failed"
        assert evidence.invocation_id is None
        assert backend.stop_count == 0
        assert _process_is_live(pid)
        assessment = assess_process_termination_recovery(evidence)
        assert assessment.disposition == "identity_unproven_or_reused"
        assert assessment.may_signal_containment_unit is False
        assert assessment.automatic_relaunch_authorized is False
        assert evidence.automatic_retry_or_repair is False
        assert evidence.continuation_authorized is False
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(pid, signal.SIGKILL)
        with contextlib.suppress(ChildProcessError):
            os.waitpid(pid, 0)


def test_signal_errors_remain_terminal_and_are_durable(
    tmp_path: Path,
) -> None:
    prepared = _prepared_request(tmp_path)
    codex_home = tmp_path / "codex-home"
    _write_rollout(codex_home, [_session_meta(), _tool_call()])
    _configure(
        prepared,
        ignore_term=True,
        stdout_lines_before_sleep=[_stdout_thread_started()],
        sleep_seconds=30,
    )
    controller = _controller(tmp_path, policy=_policy(max_tool_calls=1))
    backend = FakeContainmentBackend()
    backend.fail_stop = True
    started: list[int] = []
    finished: list[int] = []

    try:
        result = _run(
            prepared,
            codex_home,
            controller=controller,
            process_started=started.append,
            process_finished=finished.append,
            containment_backend=backend,
        )
        evidence = _evidence(result)

        assert result.status == "bounded_continuation_required"
        assert evidence.phase == "termination_failed"
        assert evidence.signal_error == "injected systemctl stop failure"
        assert evidence.systemd_stop_requested is True
        assert evidence.graceful_termination_sent is False
        assert evidence.hard_kill_sent is False
        assert evidence.process_reaped is False
        assert evidence.containment_closed is False
        assert evidence.cgroup_empty is False
        assert evidence.automatic_retry_or_repair is False
        assert evidence.continuation_authorized is False
        assert finished == []
        assert started and _process_is_live(started[0])
    finally:
        if started:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(started[0], signal.SIGKILL)
            with contextlib.suppress(ChildProcessError):
                os.waitpid(started[0], 0)


def test_failed_closure_inspection_cannot_report_success_or_fire_callback(
    tmp_path: Path,
) -> None:
    class InspectionFailureBackend(FakeContainmentBackend):
        def inspect(
            self,
            unit_name: str,
            invocation_id: str | None,
            control_group: str | None,
        ) -> SystemdUnitInspectionV1:
            inspected = super().inspect(unit_name, invocation_id, control_group)
            if inspected.state == "proven_closed":
                return inspected.model_copy(
                    update={
                        "state": "ambiguous",
                        "cgroup_empty": None,
                        "error": "injected systemctl inspection timeout",
                    }
                )
            return inspected

    prepared = _prepared_request(tmp_path)
    _configure(
        prepared,
        stdout_lines=[_stdout_thread_started(), _stdout_completed()],
        final="complete",
    )
    finished: list[int] = []
    result = _run(
        prepared,
        tmp_path / "codex-home",
        process_policy=ProcessEnforcementPolicyV1(),
        containment_backend=InspectionFailureBackend(),
        process_finished=finished.append,
    )
    evidence = _evidence(result)

    assert result.status == "process_failed"
    assert evidence.phase == "termination_failed"
    assert evidence.containment_closed is None
    assert evidence.process_reaped is False
    assert finished == []


def test_recovery_classifies_crash_windows_and_refuses_reused_pid() -> None:
    unit = "ras-codex-11111111111111111111111111111111.service"
    invocation = "2" * 32
    group = (
        f"/user.slice/user-{os.getuid()}.slice/user@{os.getuid()}.service/"
        f"app.slice/{unit}"
    )
    live = SystemdUnitInspectionV1(
        state="proven_live",
        unit_name=unit,
        invocation_id=invocation,
        control_group=group,
        active_state="active",
        sub_state="running",
        cgroup_empty=False,
    )
    bound = ProcessTerminationEvidenceV1(
        task_id="task-1",
        action_id="action-1",
        containment_backend="systemd_user_cgroup_v2",
        unit_name=unit,
        invocation_id=invocation,
        control_group=group,
        phase="running",
    )
    requires_stop = assess_process_termination_recovery(bound, inspection=live)
    assert requires_stop.disposition == "exact_unit_requires_stop"
    assert requires_stop.may_signal_containment_unit is True
    assert requires_stop.may_signal_owned_process_group is False

    never_launched = assess_process_termination_recovery(
        ProcessTerminationEvidenceV1(
            task_id="task-1",
            action_id="action-1",
            containment_backend="systemd_user_cgroup_v2",
            unit_name=unit,
        )
    )
    assert never_launched.disposition == "never_launched"
    unexpected_never_launched_unit = assess_process_termination_recovery(
        ProcessTerminationEvidenceV1(
            task_id="task-1",
            action_id="action-1",
            containment_backend="systemd_user_cgroup_v2",
            unit_name=unit,
        ),
        inspection=live,
    )
    assert unexpected_never_launched_unit.disposition == "identity_unproven_or_reused"
    assert unexpected_never_launched_unit.may_signal_containment_unit is False

    launch_evidence = ProcessTerminationEvidenceV1(
        task_id="task-1",
        action_id="action-1",
        containment_backend="systemd_user_cgroup_v2",
        unit_name=unit,
        phase="launch_intent_persisted",
    )
    launch_unknown = assess_process_termination_recovery(
        launch_evidence,
        inspection=SystemdUnitInspectionV1(state="absent", unit_name=unit),
    )
    assert launch_unknown.disposition == "launch_outcome_unknown"
    assert launch_unknown.automatic_relaunch_authorized is False

    mismatch = live.model_copy(update={"invocation_id": "3" * 32})
    reused = assess_process_termination_recovery(bound, inspection=mismatch)
    assert reused.disposition == "identity_unproven_or_reused"
    assert reused.may_signal_containment_unit is False

    bound_unit_disappeared = assess_process_termination_recovery(
        bound,
        inspection=SystemdUnitInspectionV1(
            state="absent",
            unit_name=unit,
            control_group=group,
            cgroup_empty=True,
        ),
    )
    assert bound_unit_disappeared.disposition == "containment_gone_after_bound_identity"
    assert bound_unit_disappeared.may_signal_containment_unit is False

    failed_without_reason = bound.model_copy(update={"phase": "termination_failed"})
    failed = assess_process_termination_recovery(
        failed_without_reason,
        inspection=live,
    )
    assert failed.disposition == "termination_failed_unit_present"
    assert failed.may_signal_containment_unit is False

    failed_after_proven_closure = assess_process_termination_recovery(
        failed_without_reason.model_copy(
            update={"containment_closed": True, "cgroup_empty": True}
        ),
        inspection=live.model_copy(
            update={
                "state": "proven_closed",
                "active_state": "inactive",
                "sub_state": "dead",
                "cgroup_empty": True,
            }
        ),
    )
    assert failed_after_proven_closure.disposition == "termination_failed_unit_present"
    assert failed_after_proven_closure.may_signal_containment_unit is False

    closed = bound.model_copy(
        update={
            "phase": "reaped",
            "containment_closed": True,
            "cgroup_empty": True,
            "process_reaped": True,
            "final_return_code": 0,
        }
    )
    reaped = assess_process_termination_recovery(closed)
    assert reaped.disposition == "already_closed"
    assert reaped.may_signal_owned_process_group is False

    closed_before_wrapper_reap = closed.model_copy(
        update={"phase": "containment_closed", "process_reaped": False}
    )
    closure_recovery = assess_process_termination_recovery(closed_before_wrapper_reap)
    assert closure_recovery.disposition == "already_closed"
    assert closure_recovery.may_signal_containment_unit is False


@pytest.mark.parametrize(
    ("case", "expected_stop", "expected_closed"),
    [
        ("launch_outcome_unknown", 0, False),
        ("exact_live", 1, True),
        ("identity_mismatch", 0, False),
        ("ambiguous_inspection", 0, False),
        ("bound_unit_absent", 0, True),
        ("termination_failed_live", 0, False),
        ("termination_failed_closed", 0, True),
        ("already_closed", 0, True),
    ],
)
def test_workflow_resume_reconciles_containment_without_duplicate_launch(
    tmp_path: Path,
    case: str,
    expected_stop: int,
    expected_closed: bool,
) -> None:
    unit = "ras-codex-77777777777777777777777777777777.service"
    invocation = "8" * 32
    group = (
        f"/user.slice/user-{os.getuid()}.slice/user@{os.getuid()}.service/"
        f"app.slice/{unit}"
    )
    phase = {
        "launch_outcome_unknown": "launch_intent_persisted",
        "exact_live": "running",
        "identity_mismatch": "running",
        "ambiguous_inspection": "running",
        "bound_unit_absent": "running",
        "termination_failed_live": "termination_failed",
        "termination_failed_closed": "termination_failed",
        "already_closed": "containment_closed",
    }[case]
    bound = phase not in {"launch_intent_persisted"}
    initially_closed = case in {"termination_failed_closed", "already_closed"}
    evidence = ProcessTerminationEvidenceV1(
        task_id="minimal-substage",
        action_id="worker-r000",
        containment_backend="systemd_user_cgroup_v2",
        unit_name=unit,
        invocation_id=invocation if bound else None,
        control_group=group if bound else None,
        phase=phase,  # type: ignore[arg-type]
        containment_closed=True if initially_closed else None,
        cgroup_empty=True if initially_closed else None,
    )

    class RecoveryBackend(FakeContainmentBackend):
        def inspect(
            self,
            unit_name: str,
            invocation_id: str | None,
            control_group: str | None,
        ) -> SystemdUnitInspectionV1:
            del invocation_id, control_group
            if case == "launch_outcome_unknown":
                return SystemdUnitInspectionV1(state="absent", unit_name=unit_name)
            if case == "identity_mismatch":
                return SystemdUnitInspectionV1(
                    state="identity_mismatch",
                    unit_name=unit_name,
                    invocation_id="9" * 32,
                    control_group=group,
                    error="injected reused unit",
                )
            if case == "ambiguous_inspection":
                return SystemdUnitInspectionV1(
                    state="ambiguous",
                    unit_name=unit_name,
                    error="injected inspection timeout",
                )
            if case == "bound_unit_absent":
                return SystemdUnitInspectionV1(
                    state="absent",
                    unit_name=unit_name,
                    control_group=group,
                    cgroup_empty=True,
                )
            return SystemdUnitInspectionV1(
                state="proven_closed" if initially_closed else "proven_live",
                unit_name=unit_name,
                invocation_id=invocation,
                control_group=group,
                active_state="inactive" if initially_closed else "active",
                sub_state="dead" if initially_closed else "running",
                cgroup_empty=initially_closed,
            )

        def stop(
            self,
            unit_name: str,
            invocation_id: str | None,
            control_group: str | None,
            stop_grace_seconds: float,
        ) -> SystemdStopResultV1:
            del stop_grace_seconds
            self.stop_count += 1
            assert (unit_name, invocation_id, control_group) == (unit, invocation, group)
            return SystemdStopResultV1(
                status="closed",
                stop_requested=True,
                inspection=SystemdUnitInspectionV1(
                    state="proven_closed",
                    unit_name=unit,
                    invocation_id=invocation,
                    control_group=group,
                    active_state="inactive",
                    sub_state="dead",
                    cgroup_empty=True,
                ),
            )

    spec, _, fake = create_workflow_tree(tmp_path)

    def interrupt_after_evidence(prepared: Any, **kwargs: Any) -> Any:
        artifact = Path(kwargs["runs_dir"]) / prepared.request.run_id
        artifact.mkdir(parents=True)
        write_process_termination_evidence(
            artifact / PROCESS_TERMINATION_EVIDENCE_FILENAME,
            evidence,
        )
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_substage(
            spec,
            runs_dir=tmp_path / "workflow-runs",
            services=WorkflowServices(
                codex_executable=str(fake),
                codex_invoker=interrupt_after_evidence,
            ),
        )
    run_directory = next((tmp_path / "workflow-runs").iterdir())
    duplicate_launches: list[str] = []

    def reject_duplicate(prepared: Any, **_kwargs: Any) -> Any:
        duplicate_launches.append(prepared.request.run_id)
        raise AssertionError("workflow attempted a duplicate Codex launch")

    backend = RecoveryBackend()
    result = resume_substage(
        run_directory,
        services=WorkflowServices(
            codex_executable=str(fake),
            codex_invoker=reject_duplicate,
            containment_backend=backend,
        ),
    )
    recovered = load_process_termination_evidence(
        run_directory
        / "worker/codex/worker-r000"
        / PROCESS_TERMINATION_EVIDENCE_FILENAME
    )

    assert result.status == "human_paused"
    assert duplicate_launches == []
    assert backend.stop_count == expected_stop
    assert recovered.containment_closed is (True if expected_closed else None)
    if not expected_closed or case == "termination_failed_closed":
        assert recovered.phase == "termination_failed"


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("populated 0\nfrozen 0\n", False),
        ("populated 1\nfrozen 0\n", True),
        ("frozen 0\n", None),
        ("populated 2\n", None),
        ("populated 0\npopulated 1\n", None),
        ("populated nope\n", None),
        ("populated\n", None),
    ],
)
def test_cgroup_events_populated_parser_is_strict(
    source: str,
    expected: bool | None,
) -> None:
    assert parse_cgroup_events_populated(source) is expected


@pytest.mark.parametrize(
    ("source", "expected_empty"),
    [
        ("populated 0\nfrozen 0\n", True),
        ("populated 1\nfrozen 0\n", False),
        ("populated invalid\n", None),
    ],
)
def test_existing_cgroup_closure_uses_hierarchical_cgroup_events_only(
    tmp_path: Path,
    source: str,
    expected_empty: bool | None,
) -> None:
    unit = "ras-codex-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.service"
    group = (
        f"/user.slice/user-{os.getuid()}.slice/user@{os.getuid()}.service/"
        f"app.slice/{unit}"
    )
    cgroup = tmp_path.joinpath(*Path(group).parts[1:])
    cgroup.mkdir(parents=True)
    (cgroup / "cgroup.events").write_text(source, encoding="ascii")
    (cgroup / "cgroup.procs").write_text("999999\n", encoding="ascii")
    child = cgroup / "child"
    child.mkdir()
    (child / "cgroup.procs").write_text("888888\n", encoding="ascii")
    backend = SystemdUserCgroupV2Backend({}, cgroup_root=tmp_path)

    assert backend._cgroup_empty(group) is expected_empty


def test_missing_or_unreadable_cgroup_events_fails_closed_but_disappearance_is_empty(
    tmp_path: Path,
) -> None:
    unit = "ras-codex-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.service"
    group = (
        f"/user.slice/user-{os.getuid()}.slice/user@{os.getuid()}.service/"
        f"app.slice/{unit}"
    )
    cgroup = tmp_path.joinpath(*Path(group).parts[1:])
    cgroup.mkdir(parents=True)
    backend = SystemdUserCgroupV2Backend({}, cgroup_root=tmp_path)

    assert backend._cgroup_empty(group) is None
    (cgroup / "cgroup.events").mkdir()
    assert backend._cgroup_empty(group) is None
    assert backend._cgroup_empty(group.replace(unit, "ras-codex-" + "c" * 32 + ".service")) is True


def test_pgid_diagnostic_cannot_authorize_containment_closure() -> None:
    unit = "ras-codex-44444444444444444444444444444444.service"
    group = (
        f"/user.slice/user-{os.getuid()}.slice/user@{os.getuid()}.service/"
        f"app.slice/{unit}"
    )
    evidence = ProcessTerminationEvidenceV1(
        task_id="task-1",
        action_id="action-1",
        containment_backend="systemd_user_cgroup_v2",
        unit_name=unit,
        invocation_id="5" * 32,
        control_group=group,
        phase="termination_failed",
        owned_process_group_empty=True,
    )
    assessment = assess_process_termination_recovery(
        evidence,
        inspection=SystemdUnitInspectionV1(
            state="proven_live",
            unit_name=unit,
            invocation_id="5" * 32,
            control_group=group,
            active_state="active",
            cgroup_empty=False,
        ),
    )
    assert assessment.disposition == "termination_failed_unit_present"
    assert assessment.may_signal_owned_process_group is False


def test_launch_intent_is_durable_before_popen_and_recovery_fails_closed(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    prepared = _prepared_request(tmp_path)
    codex_home = tmp_path / "codex-home"
    evidence_path = (
        tmp_path
        / "runs"
        / prepared.request.run_id
        / PROCESS_TERMINATION_EVIDENCE_FILENAME
    )
    observed_phases: list[str] = []

    def fail_after_observing_launch_intent(*_args: Any, **_kwargs: Any) -> Any:
        observed_phases.append(load_process_termination_evidence(evidence_path).phase)
        raise OSError("injected Popen boundary failure")

    monkeypatch.setattr(adapter_module.subprocess, "Popen", fail_after_observing_launch_intent)

    result = _run(
        prepared,
        codex_home,
        process_policy=ProcessEnforcementPolicyV1(),
        version_probe=lambda *_args: "0.200.0",
    )
    evidence = _evidence(result)
    assessment = assess_process_termination_recovery(evidence)

    assert result.status == "launch_failed"
    assert observed_phases == ["launch_intent_persisted"]
    assert evidence.phase == "launch_intent_persisted"
    assert evidence.process_identity is None
    assert assessment.disposition == "launch_outcome_unknown"
    assert assessment.automatic_relaunch_authorized is False


def test_workflow_budget_boundary_pauses_without_repair_or_duplicate_launch(
    tmp_path: Path,
) -> None:
    response = codex_response(
        "worker",
        THREAD_ID,
        worker_result(),
        stdout_lines_before_sleep=[_stdout_thread_started()],
        stdout_lines=[],
        sleep_seconds=30,
    )
    spec, _, fake = create_workflow_tree(tmp_path, responses=[response])
    codex_home = tmp_path / "codex-home"
    _write_rollout(
        codex_home,
        [_session_meta(), *(_token_count(index) for index in range(1, 66))],
    )
    controller = _controller(tmp_path, policy=_policy(max_inference_samples=64))
    backend = FakeContainmentBackend()
    calls: list[str] = []

    def invoke(request: PreparedCodexRequest, **kwargs: Any) -> Any:
        calls.append(request.request.role)
        return run_prepared_codex(
            request,
            execution_budget_controller=controller,
            containment_backend=backend,
            **kwargs,
        )

    result = run_substage(
        spec,
        runs_dir=tmp_path / "runs",
        services=WorkflowServices(
            codex_executable=str(fake),
            codex_invoker=invoke,
            environ=_environment(codex_home),
        ),
    )
    run_directory = Path(result.artifact_directory)
    evidence = load_process_termination_evidence(
        run_directory
        / "worker"
        / "codex"
        / "worker-r000"
        / PROCESS_TERMINATION_EVIDENCE_FILENAME
    )

    assert result.status == "human_paused"
    assert result.pause_reason == "worker_bounded_continuation_required"
    assert result.repair_round == 0
    assert result.latest_audit_action_id is None
    assert calls == ["worker"]
    assert backend.stop_count == 1
    assert evidence.termination_reason == "execution_budget_exhausted"
    assert evidence.containment_closed is True
    assert evidence.automatic_retry_or_repair is False
    assert evidence.continuation_authorized is False


@pytest.mark.systemd_host
def test_real_host_runtime_max_kills_term_ignoring_setsid_descendant(
    tmp_path: Path,
) -> None:
    assert Path("/sys/fs/cgroup/cgroup.controllers").is_file()
    environment = dict(os.environ)
    backend = SystemdUserCgroupV2Backend(environment)
    unit = new_action_unit_name()
    child_pid_path = tmp_path / "setsid-child.pid"
    child_code = (
        "import signal,time;"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
        "time.sleep(30)"
    )
    service_code = (
        "import os,signal,subprocess,sys,time;"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
        "child=subprocess.Popen([sys.executable,'-c',sys.argv[2]],start_new_session=True);"
        "open(sys.argv[1],'w',encoding='ascii').write(str(child.pid));"
        "time.sleep(30)"
    )
    process: subprocess.Popen[bytes] | None = None
    child_pid: int | None = None

    try:
        backend.preflight(unit)
        launch = backend.build_launch_command(
            unit,
            (sys.executable, "-c", service_code, str(child_pid_path), child_code),
            tmp_path,
            0.1,
            0.5,
        )
        started = time.monotonic()
        process = subprocess.Popen(
            launch,
            cwd=tmp_path,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            close_fds=True,
        )
        assert process.stdin is not None
        process.stdin.write(encode_environment_frame(environment))
        process.stdin.close()
        identity = backend.bind_identity(unit, process.pid)
        assert identity.state == "proven_live"
        deadline = time.monotonic() + 2.0
        while not child_pid_path.is_file() and time.monotonic() < deadline:
            time.sleep(0.01)
        child_pid = int(child_pid_path.read_text(encoding="ascii"))
        assert os.getsid(child_pid) == child_pid

        process.wait(timeout=5)
        elapsed = time.monotonic() - started
        closed = backend.inspect(
            unit,
            identity.invocation_id,
            identity.control_group,
        )

        assert 0.4 <= elapsed < 5
        assert closed.state == "proven_closed"
        assert closed.cgroup_empty is True
        assert closed.unit_result == "timeout"
        assert not _process_is_live(child_pid)
    finally:
        subprocess.run(
            ["systemctl", "--user", "stop", unit],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5,
        )
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        if child_pid is not None and _process_is_live(child_pid):
            with contextlib.suppress(ProcessLookupError):
                os.kill(child_pid, signal.SIGKILL)


@pytest.mark.systemd_host
def test_real_host_cgroup_v2_contains_detached_and_blocks_escape(
    tmp_path: Path,
) -> None:
    if not Path("/sys/fs/cgroup/cgroup.controllers").is_file():
        pytest.skip("host does not expose cgroup v2")
    manager = subprocess.run(
        ["systemctl", "--user", "show", "--property=Version", "--value"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=5,
        text=True,
    )
    if manager.returncode != 0 or not manager.stdout.strip():
        pytest.skip("systemd user manager is unavailable")

    prepared = _prepared_request(tmp_path, run_id="host-containment")
    codex_home = tmp_path / "codex-home"
    sibling_unit = f"ras-escape-{os.getpid()}-{time.monotonic_ns()}.service"
    proc_root_unit = f"ras-proc-escape-{os.getpid()}-{time.monotonic_ns()}.service"
    _configure(
        prepared,
        stdout_lines_before_sleep=[_stdout_thread_started()],
        spawn_child_sleep=30,
        child_setsid=True,
        child_ignore_term=True,
        ignore_term=True,
        attempt_sibling_systemd_unit=sibling_unit,
        attempt_direct_cgroup_migration=True,
        attempt_proc_root_escape_pid=os.getpid(),
        proc_root_sibling_systemd_unit=proc_root_unit,
        sleep_seconds=30,
    )
    environment = dict(os.environ)
    environment["CODEX_HOME"] = str(codex_home)
    environment["HOME"] = str(tmp_path / "home")

    try:
        result = run_prepared_codex(
            prepared,
            runs_dir=tmp_path / "runs",
            codex_executable=str(FAKE_CODEX),
            environ=environment,
            limits=AdapterLimits(
                termination_grace_seconds=0.15,
                io_poll_seconds=0.01,
            ),
            process_enforcement_policy=ProcessEnforcementPolicyV1(
                max_wall_clock_seconds=0.5,
                control_plane_timeout_seconds=5.0,
            ),
        )
        evidence = _evidence(result)
        observed = json.loads(
            (prepared.workspace / ".fake-codex-observation.json").read_text()
        )
        child_pid = int((prepared.workspace / ".fake-codex-child.pid").read_text())
        active = subprocess.run(
            ["systemctl", "--user", "is-active", sibling_unit],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5,
            text=True,
        )

        assert result.status == "wall_clock_limit_exceeded"
        assert result.event_count == 1
        assert observed["prompt_base64"] == "Qm91bmRlZCBwcm9jZXNzIHRlc3QuCg=="
        assert observed["sibling_systemd_escape"]["returncode"] != 0
        assert observed["direct_cgroup_migration"]["succeeded"] is False
        assert all(
            item["accessible"] is False
            for item in observed["proc_root_escape"]["path_access"].values()
        )
        assert (
            observed["proc_root_escape"]["sibling_systemd_escape"]["returncode"]
            != 0
        )
        assert observed["proc_root_escape"]["direct_cgroup_migration"]["succeeded"] is False
        assert active.stdout.strip() != "active"
        assert evidence.containment_closed is True
        assert evidence.cgroup_empty is True
        assert evidence.process_reaped is True
        assert evidence.graceful_termination_sent is True
        assert evidence.hard_kill_sent is True
        assert not _process_is_live(child_pid)
    finally:
        subprocess.run(
            ["systemctl", "--user", "stop", sibling_unit, proc_root_unit],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5,
        )


def test_disabled_adapter_path_does_not_create_watchdog_artifact(tmp_path: Path) -> None:
    prepared = _prepared_request(tmp_path)
    codex_home = tmp_path / "codex-home"
    _configure(
        prepared,
        stdout_lines=[_stdout_completed()],
        final="legacy complete",
    )

    result = _run(prepared, codex_home)

    assert result.status == "succeeded"
    assert not (
        Path(result.artifact_directory) / PROCESS_TERMINATION_EVIDENCE_FILENAME
    ).exists()
