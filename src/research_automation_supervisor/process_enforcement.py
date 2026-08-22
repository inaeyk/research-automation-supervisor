"""Durable systemd user/cgroup-v2 containment for one Codex action."""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from decimal import Decimal
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal, NamedTuple, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from research_automation_supervisor.durable_state import atomic_write_json
from research_automation_supervisor.execution_budget import HardLimitReasonTuple, Sha256Digest

DEFAULT_WALL_CLOCK_LIMIT_SECONDS = 14_400.0
DEFAULT_CONTROL_PLANE_TIMEOUT_SECONDS = 5.0
PROCESS_TERMINATION_EVIDENCE_FILENAME = "process-termination.json"
CONTAINMENT_BACKEND: Literal["systemd_user_cgroup_v2"] = "systemd_user_cgroup_v2"
_UNIT_PREFIX = "ras-codex-"
_UNIT_PATTERN = re.compile(r"^ras-codex-[0-9a-f]{32}\.service$")
_INVOCATION_PATTERN = re.compile(r"^[0-9a-f]{32}$")

ProcessTerminationReasonV1 = Literal[
    "execution_budget_exhausted",
    "execution_budget_accounting_integrity_failure",
    "wall_clock_limit_exceeded",
]
ProcessTerminationPhaseV1 = Literal[
    "never_launched",
    "launch_intent_persisted",
    "running",
    "termination_intent_persisted",
    "graceful_termination_sent",
    "hard_kill_sent",
    "containment_closed",
    "termination_failed",
    "reaped",
]
ProcessTerminationRecoveryDispositionV1 = Literal[
    "never_launched",
    "launch_outcome_unknown",
    "exact_unit_requires_stop",
    "already_closed",
    "containment_gone_after_bound_identity",
    "termination_failed_unit_present",
    "identity_unproven_or_reused",
]
SystemdInspectionStateV1 = Literal[
    "proven_live",
    "proven_closed",
    "absent",
    "identity_mismatch",
    "ambiguous",
]
SystemdStopStatusV1 = Literal["closed", "failed"]
OwnedProcessGroupStateV1 = Literal[
    "verified_owned_group_present",
    "owned_group_empty",
    "ambiguous",
]
ProcessGroupSignalStatusV1 = Literal["sent", "group_already_empty", "failed"]


class ProcessEnforcementPolicyV1(BaseModel):
    """Bounded wall-clock and systemd control-plane policy for one action."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    max_wall_clock_seconds: Annotated[float, Field(gt=0)] = (
        DEFAULT_WALL_CLOCK_LIMIT_SECONDS
    )
    control_plane_timeout_seconds: Annotated[float, Field(gt=0, le=60)] = (
        DEFAULT_CONTROL_PLANE_TIMEOUT_SECONDS
    )


class OwnedProcessIdentityV1(BaseModel):
    """Diagnostic Linux wrapper identity; never a safe-closure authority."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    pid: Annotated[int, Field(gt=0)]
    process_group_id: Annotated[int, Field(gt=0)]
    session_id: Annotated[int, Field(gt=0)]
    start_ticks: Annotated[int, Field(gt=0)]


class OwnedProcessGroupInspectionV1(BaseModel):
    """Diagnostic-only inspection of the wrapper's original process group."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    state: OwnedProcessGroupStateV1
    live_member_pids: tuple[int, ...] = ()


class ProcessGroupSignalResultV1(BaseModel):
    """Compatibility result for diagnostic/graceful local wrapper cleanup."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    status: ProcessGroupSignalStatusV1
    error: str | None = None


class SystemdUnitInspectionV1(BaseModel):
    """Bounded inspection of one exact transient service and its cgroup."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    state: SystemdInspectionStateV1
    unit_name: str
    invocation_id: str | None = None
    control_group: str | None = None
    active_state: str | None = None
    sub_state: str | None = None
    unit_result: str | None = None
    exec_main_code: str | None = None
    exec_main_status: int | None = None
    cgroup_empty: bool | None = None
    error: str | None = None


class SystemdStopResultV1(BaseModel):
    """Result of one bounded, identity-checked whole-unit stop request."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    status: SystemdStopStatusV1
    inspection: SystemdUnitInspectionV1
    stop_requested: bool = False
    final_kill_observed: bool = False
    error: str | None = None


class ProcessTerminationEvidenceV1(BaseModel):
    """Atomic lifecycle and authoritative containment-closure evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[2] = 2
    evidence_kind: Literal["codex_process_termination"] = "codex_process_termination"
    task_id: Annotated[str, Field(min_length=1)]
    action_id: Annotated[str, Field(min_length=1)]
    codex_thread_id: str | None = None
    containment_backend: Literal["systemd_user_cgroup_v2"]
    unit_name: Annotated[str, Field(min_length=1, max_length=63)]
    invocation_id: str | None = None
    control_group: str | None = None
    phase: ProcessTerminationPhaseV1 = "never_launched"
    process_identity: OwnedProcessIdentityV1 | None = None
    termination_reason: ProcessTerminationReasonV1 | None = None
    containment_stop_reason: str | None = None
    reached_hard_limits: HardLimitReasonTuple = ()
    decision_elapsed_seconds: Annotated[float, Field(ge=0)] | None = None
    systemd_stop_requested: bool = False
    graceful_termination_sent: bool = False
    hard_kill_sent: bool = False
    signal_error: str | None = None
    unit_active_state: str | None = None
    unit_sub_state: str | None = None
    unit_result: str | None = None
    containment_closed: bool | None = None
    cgroup_empty: bool | None = None
    final_return_code: int | None = None
    process_reaped: bool = False
    owned_process_group_empty: bool | None = None
    budget_checkpoint_path: str | None = None
    budget_checkpoint_sha256: Sha256Digest | None = None
    native_source_cursor_path: str | None = None
    native_source_cursor_offset_at_stop: Annotated[int, Field(ge=0)] | None = None
    rollout_relative_path: str | None = None
    final_rollout_size_bytes: Annotated[int, Field(ge=0)] | None = None
    unconsumed_tail_bytes: Annotated[int, Field(ge=0)] | None = None
    unconsumed_tail_present: bool | None = None
    task_failure: Literal[False] = False
    automatic_retry_or_repair: Literal[False] = False
    continuation_authorized: Literal[False] = False

    @model_validator(mode="after")
    def validate_lifecycle(self) -> ProcessTerminationEvidenceV1:
        if not _UNIT_PATTERN.fullmatch(self.unit_name):
            raise ValueError("invalid action-local systemd unit name")
        if self.invocation_id is not None and not _INVOCATION_PATTERN.fullmatch(
            self.invocation_id
        ):
            raise ValueError("invalid systemd invocation identity")
        if self.control_group is not None and not _valid_control_group(
            self.control_group, self.unit_name, os.getuid()
        ):
            raise ValueError("unexpected systemd control group")
        identity_bound = self.invocation_id is not None and self.control_group is not None
        if (self.invocation_id is None) != (self.control_group is None):
            raise ValueError("InvocationID and ControlGroup must be bound together")
        if self.phase in {
            "running",
            "termination_intent_persisted",
            "graceful_termination_sent",
            "hard_kill_sent",
            "containment_closed",
            "reaped",
        } and not identity_bound:
            raise ValueError("launched containment evidence requires exact unit identity")
        if self.phase in {
            "termination_intent_persisted",
            "graceful_termination_sent",
            "hard_kill_sent",
        } and self.termination_reason is None and self.containment_stop_reason is None:
            raise ValueError("termination lifecycle requires an explicit stop reason")
        if self.graceful_termination_sent and not self.systemd_stop_requested:
            raise ValueError("graceful termination requires a systemd stop request")
        if self.hard_kill_sent and not self.systemd_stop_requested:
            raise ValueError("final kill requires a systemd stop request")
        if self.containment_closed is True:
            if self.phase not in {"containment_closed", "reaped", "termination_failed"}:
                raise ValueError("containment closure contradicts lifecycle phase")
            if not identity_bound or self.cgroup_empty is not True:
                raise ValueError("containment closure requires exact identity and empty cgroup")
        if self.phase in {"containment_closed", "reaped"} and (
            self.containment_closed is not True or self.cgroup_empty is not True
        ):
            raise ValueError("safe closure requires authoritative containment evidence")
        if self.phase == "reaped" and not self.process_reaped:
            raise ValueError("reaped phase requires the local wrapper to be reaped")
        if self.process_reaped and self.phase not in {"reaped", "termination_failed"}:
            raise ValueError("process_reaped contradicts lifecycle phase")
        if (self.budget_checkpoint_path is None) != (
            self.budget_checkpoint_sha256 is None
        ):
            raise ValueError("budget checkpoint path and digest must be paired")
        if self.unconsumed_tail_bytes is not None:
            if self.final_rollout_size_bytes is None:
                raise ValueError("tail bytes require a final rollout size")
            if self.unconsumed_tail_present != (self.unconsumed_tail_bytes > 0):
                raise ValueError("tail presence must match tail byte count")
        return self


class ProcessTerminationRecoveryAssessmentV1(BaseModel):
    """Fail-closed recovery classification; it never signals by itself."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[2] = 2
    disposition: ProcessTerminationRecoveryDispositionV1
    may_signal_containment_unit: bool
    may_signal_owned_process_group: Literal[False] = False
    automatic_relaunch_authorized: Literal[False] = False


class ProcessTerminationEvidenceError(ValueError):
    """Termination evidence is unavailable, malformed, or not durable."""


class ContainmentControlError(RuntimeError):
    """A bounded systemd control-plane operation could not be proven."""


class ContainmentBackend(Protocol):
    """Small injectable boundary used by deterministic adapter tests."""

    control_plane_timeout_seconds: float

    def preflight(self, unit_name: str) -> None: ...

    def build_launch_command(
        self,
        unit_name: str,
        command: Sequence[str],
        cwd: Path,
        stop_grace_seconds: float,
        runtime_max_seconds: float,
    ) -> tuple[str, ...]: ...

    def bind_identity(self, unit_name: str, wrapper_pid: int) -> SystemdUnitInspectionV1: ...

    def inspect(
        self,
        unit_name: str,
        invocation_id: str | None,
        control_group: str | None,
    ) -> SystemdUnitInspectionV1: ...

    def stop(
        self,
        unit_name: str,
        invocation_id: str | None,
        control_group: str | None,
        stop_grace_seconds: float,
    ) -> SystemdStopResultV1: ...


class _CommandResult(NamedTuple):
    returncode: int
    stdout: str


class SystemdUserCgroupV2Backend:
    """Bounded supervisor-side controller for systemd user transient services."""

    def __init__(
        self,
        environment: Mapping[str, str],
        *,
        control_plane_timeout_seconds: float = DEFAULT_CONTROL_PLANE_TIMEOUT_SECONDS,
        systemctl_executable: str | None = None,
        systemd_run_executable: str | None = None,
        cgroup_root: Path = Path("/sys/fs/cgroup"),
    ) -> None:
        self.environment = dict(environment)
        self.control_plane_timeout_seconds = control_plane_timeout_seconds
        path = self.environment.get("PATH")
        self.systemctl_executable = systemctl_executable or shutil.which(
            "systemctl", path=path
        ) or "systemctl"
        self.systemd_run_executable = systemd_run_executable or shutil.which(
            "systemd-run", path=path
        ) or "systemd-run"
        self.cgroup_root = cgroup_root

    def preflight(self, unit_name: str) -> None:
        _require_unit_name(unit_name)
        if not (self.cgroup_root / "cgroup.controllers").is_file():
            raise ContainmentControlError("host cgroup v2 is unavailable")
        manager = self._run_systemctl(
            ("show", "--property=Version", "--value"),
            timeout=self.control_plane_timeout_seconds,
        )
        if manager.returncode != 0 or not manager.stdout.strip():
            raise ContainmentControlError("systemd user manager is unavailable")
        inspection = self.inspect(unit_name, None, None)
        if inspection.state != "absent":
            raise ContainmentControlError("fresh action unit name is not absent")

    def build_launch_command(
        self,
        unit_name: str,
        command: Sequence[str],
        cwd: Path,
        stop_grace_seconds: float,
        runtime_max_seconds: float,
    ) -> tuple[str, ...]:
        _require_unit_name(unit_name)
        helper = Path(__file__).with_name("systemd_launch_helper.py").resolve()
        runtime_directory = f"/run/user/{os.getuid()}"
        inaccessible = f"{runtime_directory}/bus {runtime_directory}/systemd"
        runtime_max_value = Decimal(str(runtime_max_seconds))
        if not runtime_max_value.is_finite() or runtime_max_value <= 0:
            raise ValueError("systemd runtime maximum must be finite and positive")
        runtime_max = format(runtime_max_value, "f")
        return (
            self.systemd_run_executable,
            "--user",
            "--quiet",
            "--pipe",
            f"--unit={unit_name}",
            "--property=Type=exec",
            "--property=KillMode=control-group",
            f"--property=TimeoutStopSec={max(0.001, stop_grace_seconds):.3f}s",
            f"--property=RuntimeMaxSec={runtime_max}s",
            "--property=KillSignal=SIGTERM",
            "--property=FinalKillSignal=SIGKILL",
            "--property=ProtectControlGroups=yes",
            f"--property=InaccessiblePaths={inaccessible}",
            f"--working-directory={cwd}",
            os.path.realpath(sys.executable),
            "-I",
            "-S",
            str(helper),
            *command,
        )

    def bind_identity(self, unit_name: str, wrapper_pid: int) -> SystemdUnitInspectionV1:
        del wrapper_pid
        deadline = time.monotonic() + self.control_plane_timeout_seconds
        latest = self.inspect(unit_name, None, None)
        while latest.state == "absent" and time.monotonic() < deadline:
            time.sleep(0.01)
            latest = self.inspect(unit_name, None, None)
        return latest

    def inspect(
        self,
        unit_name: str,
        invocation_id: str | None,
        control_group: str | None,
    ) -> SystemdUnitInspectionV1:
        _require_unit_name(unit_name)
        result = self._run_systemctl(
            (
                "show",
                unit_name,
                "--no-pager",
                "--property=Id",
                "--property=LoadState",
                "--property=ActiveState",
                "--property=SubState",
                "--property=InvocationID",
                "--property=ControlGroup",
                "--property=Result",
                "--property=ExecMainCode",
                "--property=ExecMainStatus",
            ),
            timeout=self.control_plane_timeout_seconds,
        )
        if result.returncode != 0:
            return SystemdUnitInspectionV1(
                state="ambiguous",
                unit_name=unit_name,
                error="bounded systemd unit inspection failed",
            )
        properties = _parse_properties(result.stdout)
        if properties.get("LoadState") == "not-found" or not properties.get("Id"):
            return SystemdUnitInspectionV1(
                state="absent",
                unit_name=unit_name,
                control_group=control_group,
                cgroup_empty=(
                    self._cgroup_empty(control_group) if control_group is not None else None
                ),
            )
        observed_unit = properties.get("Id")
        observed_invocation = properties.get("InvocationID", "").lower() or None
        observed_group = properties.get("ControlGroup") or None
        active_state = properties.get("ActiveState") or None
        sub_state = properties.get("SubState") or None
        effective_group = (
            observed_group
            if observed_group is not None
            else control_group
            if active_state in {"inactive", "failed"}
            else None
        )
        common: dict[str, Any] = dict(
            unit_name=unit_name,
            invocation_id=observed_invocation,
            control_group=effective_group,
            active_state=active_state,
            sub_state=sub_state,
            unit_result=properties.get("Result") or None,
            exec_main_code=properties.get("ExecMainCode") or None,
            exec_main_status=_optional_int(properties.get("ExecMainStatus")),
        )
        if (
            observed_unit != unit_name
            or observed_invocation is None
            or not _INVOCATION_PATTERN.fullmatch(observed_invocation)
            or effective_group is None
            or not _valid_control_group(effective_group, unit_name, os.getuid())
            or (invocation_id is not None and observed_invocation != invocation_id)
            or (control_group is not None and effective_group != control_group)
        ):
            return SystemdUnitInspectionV1(
                state="identity_mismatch",
                error="systemd unit identity is unproven or replaced",
                **common,
            )
        cgroup_empty = self._cgroup_empty(effective_group)
        if cgroup_empty is None:
            return SystemdUnitInspectionV1(
                state="ambiguous",
                cgroup_empty=None,
                error="action cgroup population could not be inspected",
                **common,
            )
        if active_state in {"active", "activating", "reloading", "deactivating"}:
            state: SystemdInspectionStateV1 = "proven_live"
        elif active_state in {"inactive", "failed"} and cgroup_empty:
            state = "proven_closed"
        else:
            state = "ambiguous"
        return SystemdUnitInspectionV1(state=state, cgroup_empty=cgroup_empty, **common)

    def stop(
        self,
        unit_name: str,
        invocation_id: str | None,
        control_group: str | None,
        stop_grace_seconds: float,
    ) -> SystemdStopResultV1:
        before = self.inspect(unit_name, invocation_id, control_group)
        if before.state == "proven_closed":
            return SystemdStopResultV1(status="closed", inspection=before)
        if before.state != "proven_live":
            return SystemdStopResultV1(
                status="failed",
                inspection=before,
                error="exact live containment unit was not proven before stop",
            )
        result = self._run_systemctl(
            ("stop", unit_name),
            timeout=stop_grace_seconds + self.control_plane_timeout_seconds,
        )
        if result.returncode != 0:
            return SystemdStopResultV1(
                status="failed",
                inspection=before,
                stop_requested=True,
                error="bounded systemd control-group stop failed",
            )
        after = self.inspect(unit_name, before.invocation_id, before.control_group)
        if after.state == "absent" and before.control_group is not None:
            empty = self._cgroup_empty(before.control_group)
            if empty is True:
                after = before.model_copy(
                    update={
                        "state": "proven_closed",
                        "active_state": "inactive",
                        "sub_state": "dead",
                        "cgroup_empty": True,
                    }
                )
        if after.state != "proven_closed" or after.cgroup_empty is not True:
            return SystemdStopResultV1(
                status="failed",
                inspection=after,
                stop_requested=True,
                error="systemd stop did not prove the exact cgroup closed",
            )
        final_kill = (
            after.exec_main_status == 9
            or after.unit_result in {"timeout", "signal", "watchdog"}
        )
        return SystemdStopResultV1(
            status="closed",
            inspection=after,
            stop_requested=True,
            final_kill_observed=final_kill,
        )

    def _run_systemctl(self, arguments: Sequence[str], *, timeout: float) -> _CommandResult:
        try:
            completed = subprocess.run(
                (self.systemctl_executable, "--user", *arguments),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=self.environment,
                timeout=max(0.001, timeout),
            )
        except (OSError, subprocess.SubprocessError):
            return _CommandResult(125, "")
        return _CommandResult(completed.returncode, completed.stdout)

    def _cgroup_empty(self, control_group: str) -> bool | None:
        if not _valid_control_group(control_group, None, os.getuid()):
            return None
        root = self.cgroup_root.joinpath(*PurePosixPath(control_group).parts[1:])
        if not root.exists():
            return True
        try:
            populated = parse_cgroup_events_populated(
                (root / "cgroup.events").read_text(encoding="ascii")
            )
        except (OSError, UnicodeDecodeError):
            return None
        return None if populated is None else not populated


def new_action_unit_name() -> str:
    """Return a bounded, content-free action unit name with 128 bits of entropy."""
    return f"{_UNIT_PREFIX}{secrets.token_hex(16)}.service"


def write_process_termination_evidence(
    path: Path,
    evidence: ProcessTerminationEvidenceV1,
) -> None:
    """Atomically replace the exact action-local termination evidence."""
    atomic_write_json(
        path,
        evidence.model_dump(mode="json"),
        error_factory=ProcessTerminationEvidenceError,
        error_message="process termination evidence could not be written",
    )


def load_process_termination_evidence(path: Path) -> ProcessTerminationEvidenceV1:
    """Load strict evidence without accepting symlinks or non-regular files."""
    try:
        if path.is_symlink() or not path.is_file():
            raise ValueError("termination evidence is not a regular file")
        return ProcessTerminationEvidenceV1.model_validate_json(path.read_bytes())
    except (OSError, ValidationError, ValueError) as exc:
        raise ProcessTerminationEvidenceError(
            "process termination evidence could not be loaded"
        ) from exc


def assess_process_termination_recovery(
    evidence: ProcessTerminationEvidenceV1,
    *,
    inspection: SystemdUnitInspectionV1 | None = None,
) -> ProcessTerminationRecoveryAssessmentV1:
    """Classify recovery from exact persisted unit identity and bounded inspection."""
    if evidence.phase == "never_launched":
        disposition: ProcessTerminationRecoveryDispositionV1 = (
            "never_launched"
            if inspection is None or inspection.state == "absent"
            else "identity_unproven_or_reused"
        )
        may_signal = False
    elif (
        evidence.phase == "termination_failed"
        and evidence.containment_closed is True
        and evidence.cgroup_empty is True
    ):
        disposition = "termination_failed_unit_present"
        may_signal = False
    elif evidence.containment_closed is True and evidence.cgroup_empty is True:
        disposition = "already_closed"
        may_signal = False
    elif inspection is None:
        disposition = (
            "launch_outcome_unknown"
            if evidence.phase == "launch_intent_persisted"
            else "identity_unproven_or_reused"
        )
        may_signal = False
    elif (
        inspection.unit_name != evidence.unit_name
        or inspection.state in {"identity_mismatch", "ambiguous"}
        or inspection.state != "absent"
        and evidence.invocation_id is not None
        and (
            inspection.invocation_id != evidence.invocation_id
            or inspection.control_group != evidence.control_group
        )
    ):
        disposition = "identity_unproven_or_reused"
        may_signal = False
    elif evidence.phase == "termination_failed":
        disposition = "termination_failed_unit_present"
        may_signal = False
    elif inspection.state == "proven_live":
        disposition = "exact_unit_requires_stop"
        may_signal = True
    elif inspection.state == "proven_closed":
        disposition = "containment_gone_after_bound_identity"
        may_signal = False
    elif inspection.state == "absent":
        disposition = (
            "containment_gone_after_bound_identity"
            if evidence.invocation_id is not None
            and inspection.cgroup_empty is True
            else "launch_outcome_unknown"
        )
        may_signal = False
    else:
        disposition = "identity_unproven_or_reused"
        may_signal = False
    return ProcessTerminationRecoveryAssessmentV1(
        disposition=disposition,
        may_signal_containment_unit=may_signal,
    )


def reconcile_process_termination_recovery(
    path: Path,
    backend: ContainmentBackend,
    *,
    stop_grace_seconds: float,
) -> ProcessTerminationRecoveryAssessmentV1:
    """Boundedly reconcile one durable action unit without authorizing relaunch."""
    evidence = load_process_termination_evidence(path)
    inspection = backend.inspect(
        evidence.unit_name,
        evidence.invocation_id,
        evidence.control_group,
    )
    assessment = assess_process_termination_recovery(evidence, inspection=inspection)
    if assessment.disposition in {"already_closed", "never_launched"}:
        return assessment
    if assessment.disposition == "containment_gone_after_bound_identity":
        invocation_id = evidence.invocation_id or inspection.invocation_id
        control_group = evidence.control_group or inspection.control_group
        if invocation_id is None or control_group is None or inspection.cgroup_empty is not True:
            return _persist_unresolved_recovery(
                path,
                evidence,
                "closed containment identity could not be proven during recovery",
                inspection,
            )
        phase: ProcessTerminationPhaseV1 = (
            "termination_failed"
            if evidence.phase == "termination_failed"
            else "containment_closed"
        )
        reconciled = evidence.model_copy(
            update={
                "phase": phase,
                "invocation_id": invocation_id,
                "control_group": control_group,
                "unit_active_state": inspection.active_state,
                "unit_sub_state": inspection.sub_state,
                "unit_result": inspection.unit_result,
                "containment_closed": True,
                "cgroup_empty": True,
            }
        )
        write_process_termination_evidence(path, reconciled)
        return assess_process_termination_recovery(reconciled)
    if not assessment.may_signal_containment_unit:
        return _persist_unresolved_recovery(
            path,
            evidence,
            inspection.error or f"containment recovery is {assessment.disposition}",
            inspection,
        )
    if (
        inspection.state != "proven_live"
        or inspection.invocation_id is None
        or inspection.control_group is None
    ):
        return _persist_unresolved_recovery(
            path,
            evidence,
            "exact live containment identity could not be bound during recovery",
            inspection,
        )
    intent = evidence.model_copy(
        update={
            "phase": "termination_intent_persisted",
            "invocation_id": inspection.invocation_id,
            "control_group": inspection.control_group,
            "containment_stop_reason": (
                evidence.containment_stop_reason or "workflow_recovery_reconciliation"
            ),
            "unit_active_state": inspection.active_state,
            "unit_sub_state": inspection.sub_state,
            "unit_result": inspection.unit_result,
            "containment_closed": False,
            "cgroup_empty": False,
        }
    )
    write_process_termination_evidence(path, intent)
    stopped = backend.stop(
        intent.unit_name,
        intent.invocation_id,
        intent.control_group,
        stop_grace_seconds,
    )
    if stopped.status != "closed":
        failed = intent.model_copy(
            update={
                "phase": "termination_failed",
                "systemd_stop_requested": stopped.stop_requested,
                "signal_error": stopped.error or "containment recovery stop failed",
                "unit_active_state": stopped.inspection.active_state,
                "unit_sub_state": stopped.inspection.sub_state,
                "unit_result": stopped.inspection.unit_result,
                "containment_closed": False,
                "cgroup_empty": stopped.inspection.cgroup_empty,
            }
        )
        write_process_termination_evidence(path, failed)
        return assess_process_termination_recovery(failed, inspection=stopped.inspection)
    closed = stopped.inspection
    if (
        closed.state != "proven_closed"
        or closed.unit_name != intent.unit_name
        or closed.invocation_id != intent.invocation_id
        or closed.control_group != intent.control_group
        or closed.cgroup_empty is not True
    ):
        return _persist_unresolved_recovery(
            path,
            intent,
            "containment recovery stop did not prove exact closure",
            closed,
        )
    reconciled = intent.model_copy(
        update={
            "phase": "containment_closed",
            "systemd_stop_requested": stopped.stop_requested,
            "graceful_termination_sent": stopped.stop_requested,
            "hard_kill_sent": stopped.final_kill_observed,
            "unit_active_state": closed.active_state,
            "unit_sub_state": closed.sub_state,
            "unit_result": closed.unit_result,
            "containment_closed": True,
            "cgroup_empty": True,
        }
    )
    write_process_termination_evidence(path, reconciled)
    return assess_process_termination_recovery(reconciled)


def _persist_unresolved_recovery(
    path: Path,
    evidence: ProcessTerminationEvidenceV1,
    error: str,
    inspection: SystemdUnitInspectionV1,
) -> ProcessTerminationRecoveryAssessmentV1:
    if evidence.phase == "never_launched":
        unresolved = evidence.model_copy(
            update={"phase": "termination_failed", "signal_error": error}
        )
    elif evidence.phase in {"containment_closed", "reaped"}:
        unresolved = evidence
    else:
        unresolved = evidence.model_copy(
            update={"phase": "termination_failed", "signal_error": error}
        )
    if unresolved != evidence:
        write_process_termination_evidence(path, unresolved)
    return assess_process_termination_recovery(unresolved, inspection=inspection)


def parse_cgroup_events_populated(source: str) -> bool | None:
    """Return the kernel hierarchical populated bit, rejecting malformed events."""
    values: dict[str, int] = {}
    for line in source.splitlines():
        fields = line.split()
        if len(fields) != 2 or not fields[0] or not fields[1].isdecimal():
            return None
        if fields[0] in values:
            return None
        values[fields[0]] = int(fields[1])
    populated = values.get("populated")
    return bool(populated) if populated in {0, 1} else None


def file_sha256(path: Path) -> str:
    """Return an exact digest for a durable referenced checkpoint."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def process_start_ticks(pid: int) -> int | None:
    """Return Linux /proc start ticks for diagnostic wrapper identity."""
    if pid <= 0:
        return None
    try:
        content = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        close = content.rfind(")")
        if close < 0:
            return None
        fields = content[close + 2 :].split()
        value = int(fields[19])
    except (OSError, ValueError, IndexError):
        return None
    return value if value > 0 else None


class _ProcStat(NamedTuple):
    pid: int
    state: str
    process_group_id: int
    session_id: int
    start_ticks: int


def _read_proc_stat(pid: int) -> _ProcStat | None:
    try:
        content = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        close = content.rfind(")")
        fields = content[close + 2 :].split()
        return _ProcStat(
            pid=pid,
            state=fields[0],
            process_group_id=int(fields[2]),
            session_id=int(fields[3]),
            start_ticks=int(fields[19]),
        )
    except (OSError, ValueError, IndexError):
        return None


def inspect_owned_process_group(
    identity: OwnedProcessIdentityV1,
) -> OwnedProcessGroupInspectionV1:
    """Return diagnostic PGID evidence that never authorizes safe closure."""
    try:
        entries = tuple(Path("/proc").iterdir())
    except OSError:
        return OwnedProcessGroupInspectionV1(state="ambiguous")
    leader: _ProcStat | None = None
    members: list[_ProcStat] = []
    for entry in entries:
        if not entry.name.isdecimal():
            continue
        stat = _read_proc_stat(int(entry.name))
        if stat is None:
            continue
        if stat.pid == identity.pid:
            leader = stat
        if stat.process_group_id == identity.process_group_id and stat.state != "Z":
            members.append(stat)
    if leader is not None and leader.state != "Z" and (
        leader.start_ticks != identity.start_ticks
        or leader.process_group_id != identity.process_group_id
        or leader.session_id != identity.session_id
    ):
        return OwnedProcessGroupInspectionV1(state="ambiguous")
    if not members:
        return OwnedProcessGroupInspectionV1(state="owned_group_empty")
    if any(member.session_id != identity.session_id for member in members):
        return OwnedProcessGroupInspectionV1(state="ambiguous")
    return OwnedProcessGroupInspectionV1(
        state="verified_owned_group_present",
        live_member_pids=tuple(sorted(member.pid for member in members)),
    )


def _require_unit_name(unit_name: str) -> None:
    if not _UNIT_PATTERN.fullmatch(unit_name):
        raise ContainmentControlError("invalid action-local systemd unit name")


def _valid_control_group(
    control_group: str,
    unit_name: str | None,
    uid: int,
) -> bool:
    if not control_group.startswith(f"/user.slice/user-{uid}.slice/"):
        return False
    path = PurePosixPath(control_group)
    if not path.is_absolute() or ".." in path.parts:
        return False
    return unit_name is None or path.name == unit_name


def _parse_properties(source: str) -> dict[str, str]:
    properties: dict[str, str] = {}
    for line in source.splitlines():
        name, separator, value = line.partition("=")
        if separator and name:
            properties[name] = value
    return properties


def _optional_int(value: str | None) -> int | None:
    try:
        return int(value) if value else None
    except ValueError:
        return None
