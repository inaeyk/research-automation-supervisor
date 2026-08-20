"""Human-launched, semantically decomposed replay harness.

The command is deliberately a dry run unless an operator supplies the exact execution
acknowledgement.  Semantic task boundaries remain durable while bounded session epochs may
continue the same verified Codex thread with ``codex-task resume``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)

from research_automation_supervisor.codex_models import ModelName, ReasoningEffort
from research_automation_supervisor.context_economy import (
    CONTEXT_ECONOMY_PROFILES,
    context_economy_receipt_from_events,
)
from research_automation_supervisor.semantic_decomposition import (
    AgentHandoffV1,
    ArtifactReferenceV1,
    AuthorityReferenceV1,
    HandoffSizeV1,
    RepositoryIdentityV1,
    SemanticStageTelemetryV1,
    SemanticSubtaskTelemetryV1,
    SemanticSubtaskV1,
    SemanticTaskPlanV1,
    SessionEpochPlanV1,
    SessionEpochV1,
    aggregate_semantic_telemetry,
    continued_epoch_launch,
    fresh_session_launch,
    measure_agent_handoff,
    semantic_subtask_telemetry,
    write_agent_handoff,
)
from research_automation_supervisor.token_accounting import (
    CodexTurnUsageV1,
    CodexUsageBindingV1,
    CodexUsageReceiptV1,
    UsageRole,
    cumulative_usage_from_jsonl,
    load_verified_receipt,
    receipt_from_jsonl,
    write_receipt,
)

EXECUTION_ACKNOWLEDGEMENT = "I_ACKNOWLEDGE_EXTERNAL_REPLAY"
DEFAULT_CODEX_HOME = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).resolve()
DEFAULT_CODEX_TASK = DEFAULT_CODEX_HOME / "bin" / "codex-task"
DEFAULT_LEDGER_ROOT = DEFAULT_CODEX_HOME / "task-ledgers"


def _freeze_sequence(value: object) -> object:
    return tuple(value) if isinstance(value, list) else value


DraftStrings = Annotated[tuple[str, ...], BeforeValidator(_freeze_sequence)]
DraftArtifacts = Annotated[tuple[ArtifactReferenceV1, ...], BeforeValidator(_freeze_sequence)]
WriterIds = Annotated[tuple[str, ...], BeforeValidator(_freeze_sequence)]


class ReplayCommandV1(BaseModel):
    """One exact epoch turn command and its durable inputs."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    epoch_id: str
    epoch_turn_index: Annotated[int, Field(ge=1)]
    subtask_id: str
    task_id: str
    mode: Literal["run", "resume"]
    prompt_path: str
    handoff_path: str
    launch_path: str
    argv: tuple[str, ...]


class ControlledReplayV1(BaseModel):
    """Frozen authority, runtime controls, and semantic plan for one replay."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, str_strip_whitespace=True)

    schema_version: Literal[1] = 1
    control_id: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,127}$")]
    implementation_authority_commit: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    replay_start_commit: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    model: ModelName
    reasoning_effort: ReasoningEffort
    workload_authority: AuthorityReferenceV1
    global_target_writer_subtask_ids: WriterIds
    plan: SemanticTaskPlanV1
    epoch_plan: SessionEpochPlanV1

    @model_validator(mode="after")
    def bind_workload_authority(self) -> ControlledReplayV1:
        if self.workload_authority not in self.plan.authority_references:
            raise ValueError("workload authority must be included in plan authority references")
        writers = self.global_target_writer_subtask_ids
        if not writers or len(writers) != len(set(writers)):
            raise ValueError("global target writer subtask IDs must be non-empty and unique")
        tasks = {subtask.subtask_id: subtask for subtask in self.plan.subtasks}
        if any(writer not in tasks for writer in writers):
            raise ValueError("global target writer subtask ID is absent from the plan")
        if any(tasks[writer].role.endswith("auditor") for writer in writers):
            raise ValueError("auditors cannot write the controlled global target")
        if self.epoch_plan.semantic_plan_id != self.plan.plan_id:
            raise ValueError("session epoch plan identifies the wrong semantic plan")
        ordered = tuple(subtask.subtask_id for subtask in self.plan.subtasks)
        if self.epoch_plan.ordered_subtask_ids != ordered:
            raise ValueError("session epoch plan must preserve exact semantic subtask order")
        for epoch in self.epoch_plan.epochs:
            epoch_tasks = tuple(tasks[subtask_id] for subtask_id in epoch.subtask_ids)
            if any(subtask.role != epoch.role for subtask in epoch_tasks):
                raise ValueError("session epoch cannot cross semantic subtask roles")
            if epoch.configuration.model != self.model:
                raise ValueError("session epoch model differs from frozen replay model")
            if epoch.configuration.reasoning_effort != self.reasoning_effort:
                raise ValueError("session epoch reasoning differs from frozen replay reasoning")
            expected_sandbox = (
                "read-only"
                if epoch.role in {"coding_auditor", "physics_auditor"}
                else "workspace-write"
            )
            if epoch.configuration.sandbox != expected_sandbox:
                raise ValueError("session epoch sandbox differs from role policy")
        return self


class AgentHandoffDraftV1(BaseModel):
    """Model-authored semantics; the Supervisor binds repository and authority facts."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, str_strip_whitespace=True)

    schema_version: Literal[1] = 1
    completed_objective: Annotated[str, Field(min_length=1, max_length=2_000)]
    changed_paths: DraftStrings
    changed_interfaces: DraftStrings
    valid_evidence_receipts: DraftArtifacts
    decisions_and_invariants: DraftStrings
    unresolved_findings: DraftStrings
    remaining_work: DraftStrings
    next_subtask_requirements: Annotated[DraftStrings, Field(min_length=1)]
    do_not_rediscover_or_retest: DraftStrings
    oversize_justification: Annotated[str, Field(min_length=12, max_length=1_000)] | None = None


class ReplayPreparationV1(BaseModel):
    """Complete deterministic command manifest emitted without launching Codex."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    control_id: str
    plan_id: str
    epoch_plan_id: str
    control_sha256: str
    implementation_authority_commit: str
    replay_start_commit: str
    model: str
    reasoning_effort: str
    authority_root: str
    workspace: str
    artifact_root: str
    global_target: str
    ledger_root: str
    commands: tuple[ReplayCommandV1, ...]


def load_control(path: Path) -> ControlledReplayV1:
    """Load one strict control record; unknown or non-finite fields fail closed."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"), parse_constant=_reject_constant)
        return ControlledReplayV1.model_validate(value)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError, ValidationError) as exc:
        raise ValueError(f"semantic replay control is invalid: {path}") from exc


def verify_authority(plan: SemanticTaskPlanV1, authority_root: Path) -> None:
    """Verify plan and subtask authority against current exact bytes."""
    authority_root = authority_root.resolve(strict=True)
    references = {
        (reference.path, reference.sha256)
        for reference in (
            *plan.authority_references,
            *(reference for item in plan.subtasks for reference in item.authority_references),
        )
    }
    for name, expected in references:
        path = Path(name)
        if path.is_absolute():
            raise ValueError(f"authority path must be relative to authority_root: {name}")
        candidate = (authority_root / path).resolve(strict=False)
        if not candidate.is_relative_to(authority_root):
            raise ValueError(f"authority escapes authority_root: {name}")
        if not candidate.is_file():
            raise ValueError(f"authority is unavailable: {name}")
        if hashlib.sha256(candidate.read_bytes()).hexdigest() != expected:
            raise ValueError(f"authority hash changed: {name}")


def repository_identity(workspace: Path, base_commit: str) -> RepositoryIdentityV1:
    """Bind a handoff to HEAD plus tracked/untracked candidate bytes."""
    head = _git(workspace, "rev-parse", "HEAD").decode("ascii").strip()
    status = _git(
        workspace,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "-z",
    )
    diff = _git(workspace, "diff", "--binary", base_commit, "--")
    digest = hashlib.sha256()
    digest.update(b"status\0")
    digest.update(status)
    digest.update(b"diff\0")
    digest.update(diff)
    for entry in status.split(b"\0"):
        if not entry.startswith(b"?? "):
            continue
        relative = os.fsdecode(entry[3:])
        candidate = workspace / relative
        if candidate.is_symlink():
            digest.update(b"untracked-symlink\0")
            digest.update(os.fsencode(relative))
            digest.update(b"\0")
            digest.update(os.fsencode(os.readlink(candidate)))
        elif candidate.is_file():
            digest.update(b"untracked\0")
            digest.update(os.fsencode(relative))
            digest.update(b"\0")
            digest.update(candidate.read_bytes())
    return RepositoryIdentityV1(
        repository=str(workspace.resolve()),
        base_commit=base_commit,
        head_commit=head,
        diff_sha256=digest.hexdigest(),
        dirty=bool(status),
    )


def render_subtask_prompt(
    subtask: SemanticSubtaskV1,
    *,
    epoch: SessionEpochV1,
    epoch_turn_index: int,
    predecessor_artifacts: Sequence[ArtifactReferenceV1],
    authority_root: Path,
    expected_repository: RepositoryIdentityV1,
    global_target: Path,
) -> str:
    """Render a fresh epoch handoff or a compact in-epoch semantic delta."""
    profile = CONTEXT_ECONOMY_PROFILES["B4"]
    assert profile.model_auto_compact_token_limit == 64_000
    assert profile.tool_output_token_limit == 2_048
    if subtask.subtask_id != epoch.subtask_ids[epoch_turn_index - 1]:
        raise ValueError("prompt subtask does not match the epoch turn order")
    continued = epoch_turn_index > 1
    semantic_payload: dict[str, object]
    if continued:
        semantic_payload = {
            "subtask_id": subtask.subtask_id,
            "sequence": subtask.sequence,
            "next_objective": subtask.objective,
            "scope": subtask.scope,
            "deliverables": subtask.deliverables,
            "completion_conditions": subtask.completion_conditions,
            "validation_requirements": subtask.validation_requirements,
            "stop_conditions": subtask.stop_conditions,
        }
    else:
        semantic_payload = subtask.model_dump(mode="json")
    payload = {
        "epoch": {
            "epoch_id": epoch.epoch_id,
            "epoch_turn_index": epoch_turn_index,
            "continuation_policy": epoch.continuation_policy,
        },
        "semantic_subtask" if not continued else "semantic_delta": semantic_payload,
        "authority_locations": [
            {
                "path": str(
                    Path(item.path) if Path(item.path).is_absolute() else authority_root / item.path
                ),
                "sha256": item.sha256,
            }
            for item in subtask.authority_references
        ],
        "predecessor_artifacts": [item.model_dump(mode="json") for item in predecessor_artifacts],
        "repository_identity_at_launch": expected_repository.model_dump(mode="json"),
        "controlled_global_target": str(global_target),
    }
    session_statement = (
        "This is a compact semantic delta inside the current epoch. Continue from the source, "
        "test architecture, and interfaces already in this same bounded session. No full "
        "AgentHandoffV1 is injected back into this session; durable predecessor results exist "
        "only for crash recovery."
        if continued
        else "This begins a fresh session epoch. No conversation transcript was transferred; "
        "the compact predecessor artifacts are the only cross-epoch handoff."
    )
    return (
        "Semantic Context Economy V1: use B4 with a 64000-token auto-compaction limit "
        "and a 2048-token tool-output limit. Inspect and plan once; batch related searches, "
        "reads, coherent edits, and tests. Tool-call counts are telemetry and batching guidance, "
        "not hard denial thresholds. Do not rerun a valid PASS unless its recorded validity "
        "inputs changed. Preserve verbose output in durable files and expose bounded summaries. "
        "Do not repeat status, diff, or tests without a new reason."
        f"\n\n{session_statement} Repository state, authority files, "
        "hashes, receipts, and handoffs are durable memory. "
        "Read predecessor artifacts only as needed. Follow the bounded scope, validation, and "
        "stop conditions. Do not launch PA-5D scientific work or the replay harness. Preserve "
        "qualified recovery and exactly-once behavior. The controlled_global_target is the "
        "disposable stand-in for global Codex files required by the original workload; never "
        "write the live $CODEX_HOME, credentials, or authoritative launch ledger.\n\n"
        + json.dumps(payload, indent=2, sort_keys=True)
        + "\n\nReturn only canonical JSON matching AgentHandoffDraftV1 in "
        f"{Path(__file__).resolve()}. The Supervisor—not the model—binds repository identity, "
        "authority hashes, handoff ID, and transcript_included=false. Include no Markdown, "
        "transcript, hidden reasoning, or shell logs. Keep the resulting handoff at or below "
        "1000 tokens when possible and at or below 2000; a larger handoff needs explicit "
        "justification."
    )


def prepare_replay(
    *,
    control_path: Path,
    authority_root: Path,
    workspace: Path,
    artifact_root: Path,
    global_target: Path,
    codex_task: Path = DEFAULT_CODEX_TASK,
) -> ReplayPreparationV1:
    """Validate authority and return exact commands without creating files or sessions."""
    control = load_control(control_path)
    plan = control.plan
    authority_root = authority_root.resolve(strict=True)
    workspace = workspace.resolve(strict=True)
    artifact_root = artifact_root.resolve()
    global_target = global_target.resolve()
    if artifact_root == workspace or workspace in artifact_root.parents:
        raise ValueError("artifact_root must be outside the replay workspace")
    codex_task = codex_task.resolve()
    protected_roots = (
        workspace,
        authority_root,
        artifact_root,
        DEFAULT_CODEX_HOME,
        codex_task,
    )
    if any(_paths_overlap(global_target, protected) for protected in protected_roots):
        raise ValueError(
            "global_target must be isolated from workspace, authority, artifacts, and live "
            "$CODEX_HOME"
        )
    verify_authority(plan, authority_root)
    _git(
        authority_root,
        "cat-file",
        "-e",
        f"{control.implementation_authority_commit}^{{commit}}",
    )
    head = _git(workspace, "rev-parse", "HEAD").decode("ascii").strip()
    if head != control.replay_start_commit:
        raise ValueError("replay workspace HEAD does not match frozen replay_start_commit")
    dirty = bool(_git(workspace, "status", "--porcelain=v1", "-z"))
    completed_prefix = _completed_subtask_prefix(plan, artifact_root, control.epoch_plan)
    recovery_subtask = (
        plan.subtasks[len(completed_prefix)] if len(completed_prefix) < len(plan.subtasks) else None
    )
    recovery_inputs_exist = recovery_subtask is not None and _recovery_inputs_exist(
        artifact_root, recovery_subtask
    )
    if (
        not completed_prefix
        and not recovery_inputs_exist
        and global_target.exists()
        and (not global_target.is_dir() or any(global_target.iterdir()))
    ):
        raise ValueError("global_target must be absent or empty before its first launch")
    if dirty and not completed_prefix and not recovery_inputs_exist:
        raise ValueError("replay workspace must be clean before its first launch")
    if completed_prefix and not recovery_inputs_exist:
        last_handoff = _parse_stored_handoff(
            artifact_root / "handoffs" / f"{completed_prefix[-1]}.json"
        )
        if last_handoff.repository_identity != repository_identity(
            workspace, control.replay_start_commit
        ):
            raise ValueError("replay workspace no longer matches the last completed handoff")
    profile = CONTEXT_ECONOMY_PROFILES["B4"]
    assert profile.model_auto_compact_token_limit == 64_000
    commands: list[ReplayCommandV1] = []
    epoch_by_subtask = {
        subtask_id: (epoch, turn_index)
        for epoch in control.epoch_plan.epochs
        for turn_index, subtask_id in enumerate(epoch.subtask_ids, start=1)
    }
    for subtask in plan.subtasks:
        epoch, epoch_turn_index = epoch_by_subtask[subtask.subtask_id]
        task_id = f"{plan.plan_id}.{epoch.epoch_id}"
        if len(task_id) > 128:
            raise ValueError("composed replay task_id exceeds codex-task's 128-character limit")
        prompt_path = artifact_root / "prompts" / f"{subtask.subtask_id}.md"
        handoff_path = artifact_root / "handoffs" / f"{subtask.subtask_id}.json"
        launch_path = artifact_root / "launches" / f"{subtask.subtask_id}.json"
        if epoch_turn_index == 1:
            argv = [
                str(codex_task),
                "run",
                task_id,
                str(workspace),
                str(prompt_path),
                "--sandbox",
                epoch.configuration.sandbox,
                "-c",
                "model_auto_compact_token_limit=64000",
                "-c",
                "tool_output_token_limit=2048",
            ]
            if any(item in control.global_target_writer_subtask_ids for item in epoch.subtask_ids):
                argv.extend(("--add-dir", str(global_target)))
            argv.extend(
                (
                    "--model",
                    epoch.configuration.model,
                    "-c",
                    f"model_reasoning_effort={epoch.configuration.reasoning_effort}",
                )
            )
            mode: Literal["run", "resume"] = "run"
        else:
            argv = [str(codex_task), "resume", task_id, str(prompt_path)]
            mode = "resume"
        commands.append(
            ReplayCommandV1(
                epoch_id=epoch.epoch_id,
                epoch_turn_index=epoch_turn_index,
                subtask_id=subtask.subtask_id,
                task_id=task_id,
                mode=mode,
                prompt_path=str(prompt_path),
                handoff_path=str(handoff_path),
                launch_path=str(launch_path),
                argv=tuple(argv),
            )
        )
    return ReplayPreparationV1(
        control_id=control.control_id,
        plan_id=plan.plan_id,
        epoch_plan_id=control.epoch_plan.epoch_plan_id,
        control_sha256=hashlib.sha256(control_path.read_bytes()).hexdigest(),
        implementation_authority_commit=control.implementation_authority_commit,
        replay_start_commit=control.replay_start_commit,
        model=control.model,
        reasoning_effort=control.reasoning_effort,
        authority_root=str(authority_root),
        workspace=str(workspace),
        artifact_root=str(artifact_root),
        global_target=str(global_target),
        ledger_root=str(DEFAULT_LEDGER_ROOT),
        commands=tuple(commands),
    )


def execute_replay(
    *,
    control_path: Path,
    authority_root: Path,
    workspace: Path,
    artifact_root: Path,
    global_target: Path,
    codex_task: Path = DEFAULT_CODEX_TASK,
) -> SemanticStageTelemetryV1:
    """Execute semantic turns in bounded epochs with qualified run/resume recovery."""
    preparation = prepare_replay(
        control_path=control_path,
        authority_root=authority_root,
        workspace=workspace,
        artifact_root=artifact_root,
        global_target=global_target,
        codex_task=codex_task,
    )
    control = load_control(control_path)
    plan = control.plan
    base_commit = control.replay_start_commit
    artifact_root.mkdir(parents=True, exist_ok=True)
    artifact_root = artifact_root.resolve(strict=True)
    if str(artifact_root) != preparation.artifact_root:
        raise ValueError("artifact_root changed between preparation and execution")
    global_target.mkdir(parents=True, exist_ok=True)
    global_target = global_target.resolve(strict=True)
    if str(global_target) != preparation.global_target:
        raise ValueError("global_target changed between preparation and execution")
    ledger_root = DEFAULT_LEDGER_ROOT
    if str(ledger_root) != preparation.ledger_root:
        raise ValueError("authoritative ledger root changed between preparation and execution")
    codex_task = codex_task.resolve()
    if _paths_overlap(global_target, ledger_root) or _paths_overlap(global_target, codex_task):
        raise ValueError("global_target overlaps the trusted launcher or authoritative ledger")
    epochs = {epoch.epoch_id: epoch for epoch in control.epoch_plan.epochs}
    first_commands = {
        command.epoch_id: command
        for command in preparation.commands
        if command.epoch_turn_index == 1
    }
    telemetry: list[SemanticSubtaskTelemetryV1] = []
    for subtask, command in zip(plan.subtasks, preparation.commands, strict=True):
        epoch = epochs[command.epoch_id]
        handoff_path = Path(command.handoff_path)
        task_root = ledger_root / command.task_id
        if handoff_path.exists():
            telemetry_path = artifact_root / "telemetry" / f"{subtask.subtask_id}.json"
            if telemetry_path.exists():
                record = _load_existing_telemetry(
                    artifact_root,
                    subtask,
                    epoch=epoch,
                    epoch_turn_index=command.epoch_turn_index,
                )
                event_log, _, completed_state = _verified_completed_task(
                    task_root=task_root,
                    command=command,
                    epoch_run_command=first_commands[epoch.epoch_id],
                    workspace=workspace,
                    expected_model=control.model,
                    allow_later_turns=True,
                )
                if _required_string(completed_state, "thread_id") != record.codex_thread_id:
                    raise ValueError("completed epoch telemetry has the wrong Codex thread")
                expected_usage = _usage_for_epoch_turn(
                    event_log=event_log,
                    task_root=task_root,
                    command=command,
                    plan_id=plan.plan_id,
                    role=_usage_role(subtask.role),
                    repair_or_retry=subtask.role == "repair",
                    task_state=completed_state,
                )
                persisted_usage = load_verified_receipt(
                    artifact_root / "usage" / f"{subtask.subtask_id}.json",
                    event_log=event_log,
                )
                if persisted_usage != expected_usage:
                    raise ValueError("persisted resumed-turn usage is not its exact delta")
                _verify_telemetry_usage(record, persisted_usage)
                telemetry.append(record)
                continue
        predecessors = _resolve_replay_predecessors(
            subtask,
            artifact_root,
            epoch_plan=control.epoch_plan,
            include_usage_receipts=subtask.role in {"coding_auditor", "physics_auditor"},
        )
        prompt_path = Path(command.prompt_path)
        launch_path = Path(command.launch_path)
        identity = repository_identity(workspace, base_commit)
        if subtask.sequence == 1:
            if identity.dirty:
                raise ValueError(
                    "first-subtask pre-launch recovery no longer has a clean replay tree"
                )
        else:
            previous_id = plan.subtasks[subtask.sequence - 2].subtask_id
            previous = _parse_stored_handoff(artifact_root / "handoffs" / f"{previous_id}.json")
            if previous.repository_identity != identity:
                raise ValueError("pre-launch replay tree differs from the predecessor handoff")
        state = _read_task_state(task_root) if task_root.exists() else None
        if state is not None:
            _verify_task_execution_policy(
                state,
                epoch_run_command=first_commands[epoch.epoch_id],
                expected_model=control.model,
            )
        if command.epoch_turn_index == 1:
            launch = fresh_session_launch(subtask, epoch_id=epoch.epoch_id)
        else:
            if state is None:
                raise ValueError("continued epoch has no existing task ledger")
            state_thread_id = _required_string(state, "thread_id")
            if any(
                record.epoch_id == epoch.epoch_id and record.codex_thread_id != state_thread_id
                for record in telemetry
            ):
                raise ValueError("continued epoch task state changed Codex thread identity")
            launch = continued_epoch_launch(
                subtask,
                epoch=epoch,
                epoch_turn_index=command.epoch_turn_index,
                thread_id=state_thread_id,
            )
        prompt = render_subtask_prompt(
            subtask,
            epoch=epoch,
            epoch_turn_index=command.epoch_turn_index,
            predecessor_artifacts=predecessors,
            authority_root=authority_root,
            expected_repository=identity,
            global_target=global_target,
        )
        _write_or_verify(prompt_path, prompt.encode("utf-8"))
        _json_or_verify(launch_path, launch.model_dump(mode="json"))
        if task_root.exists():
            _verify_launch_record(launch_path, launch)
        completed_turns = _state_turn_count(state) if state is not None else 0
        if completed_turns == command.epoch_turn_index:
            pass
        elif completed_turns == command.epoch_turn_index - 1:
            _refuse_unqualified_turn_relaunch(task_root, command.epoch_turn_index)
            if task_root.exists() and command.mode == "run":
                raise ValueError("fresh epoch task state exists without a completed turn")
            completed = subprocess.run(command.argv, check=False)
            if completed.returncode != 0:
                raise RuntimeError(
                    f"subtask {subtask.subtask_id} failed; preserve {task_root} for qualified "
                    "recovery"
                )
        else:
            raise ValueError("epoch ledger turn count does not match the next semantic turn")
        event_log, final_message, task_state = _verified_completed_task(
            task_root=task_root,
            command=command,
            epoch_run_command=first_commands[epoch.epoch_id],
            workspace=workspace,
            expected_model=control.model,
        )
        if (
            launch.prior_thread_id is not None
            and _required_string(task_state, "thread_id") != launch.prior_thread_id
        ):
            raise ValueError("continued epoch changed Codex thread identity")
        actual_identity = repository_identity(workspace, base_commit)
        handoff = _parse_final_handoff(final_message, subtask, actual_identity)
        handoff_size = _write_or_verify_handoff(handoff, handoff_path)
        usage_role = _usage_role(subtask.role)
        usage = _usage_for_epoch_turn(
            event_log=event_log,
            task_root=task_root,
            command=command,
            plan_id=plan.plan_id,
            role=usage_role,
            repair_or_retry=subtask.role == "repair",
            task_state=task_state,
        )
        if not usage.complete:
            raise ValueError(
                f"subtask {subtask.subtask_id} lacks complete authoritative runtime usage"
            )
        receipt_path = artifact_root / "usage" / f"{subtask.subtask_id}.json"
        write_receipt(receipt_path, usage)
        context = context_economy_receipt_from_events(
            event_log,
            prompt_bytes=len(prompt.encode("utf-8")),
            profile="B4",
            usage_receipt=usage,
        )
        _json_or_verify(
            artifact_root / "context" / f"{subtask.subtask_id}.json",
            context.model_dump(mode="json"),
        )
        record = semantic_subtask_telemetry(
            subtask=subtask,
            usage=usage,
            context=context,
            launch=launch,
            handoff_size=handoff_size,
            codex_thread_id=_required_string(task_state, "thread_id"),
        )
        _json_or_verify(
            artifact_root / "telemetry" / f"{subtask.subtask_id}.json",
            record.model_dump(mode="json"),
        )
        telemetry.append(record)
    aggregate = aggregate_semantic_telemetry(telemetry)
    _json_or_verify(
        artifact_root / "semantic-stage-telemetry.json",
        aggregate.model_dump(mode="json"),
    )
    return aggregate


def _resolve_replay_predecessors(
    subtask: SemanticSubtaskV1,
    artifact_root: Path,
    *,
    epoch_plan: SessionEpochPlanV1,
    include_usage_receipts: bool,
) -> tuple[ArtifactReferenceV1, ...]:
    """Verify all semantic dependencies, but transfer only compact cross-epoch state."""
    resolved: list[ArtifactReferenceV1] = []
    resolved_root = artifact_root.resolve(strict=True)
    membership = {
        subtask_id: epoch for epoch in epoch_plan.epochs for subtask_id in epoch.subtask_ids
    }
    current_epoch = membership[subtask.subtask_id]
    terminal_subtasks = {epoch.subtask_ids[-1] for epoch in epoch_plan.epochs}
    for requirement in subtask.required_predecessor_artifacts:
        relative = Path(requirement.path)
        if relative.is_absolute():
            raise ValueError("predecessor artifact path must be relative to artifact_root")
        candidate = (resolved_root / relative).resolve(strict=False)
        if not candidate.is_relative_to(resolved_root):
            raise ValueError(f"predecessor escapes artifact_root: {requirement.path}")
        if not candidate.is_file():
            raise ValueError(f"predecessor handoff unavailable: {requirement.path}")
        digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
        if requirement.sha256 is not None and digest != requirement.sha256:
            raise ValueError(f"predecessor hash changed: {requirement.path}")
        producer = requirement.produced_by_subtask_id
        if (
            current_epoch.subtask_ids[0] == subtask.subtask_id
            and producer in terminal_subtasks
            and producer is not None
            and membership[producer] != current_epoch
        ):
            resolved.append(
                ArtifactReferenceV1(
                    path=str(candidate),
                    sha256=digest,
                    kind=requirement.kind,
                )
            )
    if include_usage_receipts:
        current_position = epoch_plan.ordered_subtask_ids.index(subtask.subtask_id)
        for prior_id in epoch_plan.ordered_subtask_ids[:current_position]:
            candidate = resolved_root / "usage" / f"{prior_id}.json"
            if not candidate.is_file():
                raise ValueError(f"auditor usage receipt unavailable: {prior_id}")
            resolved.append(
                ArtifactReferenceV1(
                    path=str(candidate),
                    sha256=hashlib.sha256(candidate.read_bytes()).hexdigest(),
                    kind="evidence",
                )
            )
    return tuple(resolved)


def _load_existing_telemetry(
    artifact_root: Path,
    subtask: SemanticSubtaskV1,
    *,
    epoch: SessionEpochV1,
    epoch_turn_index: int,
) -> SemanticSubtaskTelemetryV1:
    path = artifact_root / "telemetry" / f"{subtask.subtask_id}.json"
    try:
        record = SemanticSubtaskTelemetryV1.model_validate_json(path.read_bytes())
    except (OSError, ValidationError) as exc:
        raise ValueError("existing handoff lacks matching valid telemetry") from exc
    if (
        record.subtask_id != subtask.subtask_id
        or record.role != subtask.role
        or record.epoch_id != epoch.epoch_id
        or record.epoch_turn_index != epoch_turn_index
    ):
        raise ValueError("existing telemetry identifies the wrong semantic subtask")
    if not record.accounting_complete:
        raise ValueError("existing telemetry has incomplete authoritative token accounting")
    expected_fresh = epoch_turn_index == 1
    if record.session_fresh != expected_fresh:
        raise ValueError("existing telemetry contradicts the epoch session boundary")
    if not expected_fresh and record.continuation_reason != "epoch_continuation":
        raise ValueError("continued epoch telemetry lacks its continuation reason")
    return record


def _completed_subtask_prefix(
    plan: SemanticTaskPlanV1,
    artifact_root: Path,
    epoch_plan: SessionEpochPlanV1 | None = None,
) -> tuple[str, ...]:
    """Accept only a contiguous, fully evidenced prefix during external recovery."""
    completed: list[str] = []
    gap_seen = False
    for subtask in plan.subtasks:
        handoff = artifact_root / "handoffs" / f"{subtask.subtask_id}.json"
        telemetry = artifact_root / "telemetry" / f"{subtask.subtask_id}.json"
        present = handoff.exists() or telemetry.exists()
        if not present:
            gap_seen = True
            continue
        if gap_seen:
            raise ValueError("replay progress is incomplete or not a contiguous subtask prefix")
        if not handoff.is_file() or not telemetry.is_file():
            gap_seen = True
            continue
        stored = _parse_stored_handoff(handoff)
        if stored.handoff_id != f"{subtask.subtask_id}-handoff":
            raise ValueError("stored replay handoff identifies the wrong subtask")
        if stored.authority_references != subtask.authority_references:
            raise ValueError("stored replay handoff has mismatched authority")
        if epoch_plan is not None:
            epoch = next(
                item for item in epoch_plan.epochs if subtask.subtask_id in item.subtask_ids
            )
            _load_existing_telemetry(
                artifact_root,
                subtask,
                epoch=epoch,
                epoch_turn_index=epoch.subtask_ids.index(subtask.subtask_id) + 1,
            )
        else:
            path = artifact_root / "telemetry" / f"{subtask.subtask_id}.json"
            try:
                record = SemanticSubtaskTelemetryV1.model_validate_json(path.read_bytes())
            except (OSError, ValidationError) as exc:
                raise ValueError("existing handoff lacks matching valid telemetry") from exc
            if (
                record.subtask_id != subtask.subtask_id
                or record.role != subtask.role
                or not record.accounting_complete
            ):
                raise ValueError("existing telemetry identifies the wrong semantic subtask")
        completed.append(subtask.subtask_id)
    return tuple(completed)


def _recovery_inputs_exist(artifact_root: Path, subtask: SemanticSubtaskV1) -> bool:
    """Identify a durable current-turn recovery marker without trusting its content."""
    prompt = artifact_root / "prompts" / f"{subtask.subtask_id}.md"
    launch = artifact_root / "launches" / f"{subtask.subtask_id}.json"
    return prompt.is_file() and launch.is_file()


def _read_task_state(task_root: Path) -> dict[str, Any]:
    state_path = task_root / "task.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"), parse_constant=_reject_constant)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("global task state is invalid") from exc
    if not isinstance(state, dict):
        raise ValueError("global task state is invalid")
    return state


def _state_turn_count(state: dict[str, Any]) -> int:
    value = state.get("turn_count", 0)
    if type(value) is not int or value < 0:
        raise ValueError("global task state has an invalid turn count")
    return value


def _refuse_unqualified_turn_relaunch(task_root: Path, turn_index: int) -> None:
    """Fail closed when durable turn files exist beyond the qualified task state.

    The global wrapper owns task-state recovery.  The replay supervisor must not infer a
    completed turn from partial files, but their presence proves that launching the same turn
    again could duplicate external actions.
    """
    turns = task_root / "turns"
    stem = f"{turn_index:06d}"
    candidates = (
        turns / f"{stem}.events.jsonl",
        turns / f"{stem}.final-message.md",
        turns / f"{stem}.prompt.sha256",
    )
    if any(path.exists() for path in candidates):
        raise ValueError(
            "epoch turn artifacts exist beyond qualified task state; refusing duplicate launch"
        )


def _verified_completed_task(
    *,
    task_root: Path,
    command: ReplayCommandV1,
    epoch_run_command: ReplayCommandV1,
    workspace: Path,
    expected_model: str,
    allow_later_turns: bool = False,
) -> tuple[Path, Path, dict[str, Any]]:
    """Qualify exactly one completed epoch turn for consumption without another launch."""
    state = _read_task_state(task_root)
    if state.get("usage_complete") is not True:
        raise ValueError("global task receipt is incomplete")
    _verify_task_execution_policy(
        state,
        epoch_run_command=epoch_run_command,
        expected_model=expected_model,
    )
    if state.get("task_id") != command.task_id:
        raise ValueError("global task state identifies the wrong epoch")
    if state.get("working_directory") != str(workspace.resolve(strict=True)):
        raise ValueError("global task state identifies the wrong replay workspace")
    state_turn_count = _state_turn_count(state)
    turn_count_matches = (
        state_turn_count >= command.epoch_turn_index
        if allow_later_turns
        else state_turn_count == command.epoch_turn_index
    )
    if not turn_count_matches:
        raise ValueError("global epoch ledger does not contain the expected completed turn")
    stem = f"{command.epoch_turn_index:06d}"
    event_log = task_root / "turns" / f"{stem}.events.jsonl"
    final_message = task_root / "turns" / f"{stem}.final-message.md"
    if not event_log.is_file() or not final_message.is_file():
        raise ValueError("global epoch ledger lacks the expected completed turn files")
    prompt_hashes = sorted((task_root / "turns").glob("*.prompt.sha256"))
    if len(prompt_hashes) != state_turn_count:
        raise ValueError("global epoch ledger does not retain every completed prompt hash")
    expected_prompt_hash = hashlib.sha256(Path(command.prompt_path).read_bytes()).hexdigest()
    prompt_hash = task_root / "turns" / f"{stem}.prompt.sha256"
    if prompt_hash.read_text(encoding="ascii").strip() != expected_prompt_hash:
        raise ValueError("global task prompt hash differs from the durable replay prompt")
    return event_log, final_message, state


def _usage_for_epoch_turn(
    *,
    event_log: Path,
    task_root: Path,
    command: ReplayCommandV1,
    plan_id: str,
    role: UsageRole,
    repair_or_retry: bool,
    task_state: dict[str, Any],
) -> CodexUsageReceiptV1:
    prior: CodexTurnUsageV1 | None = None
    if command.epoch_turn_index > 1:
        previous = task_root / "turns" / f"{command.epoch_turn_index - 1:06d}.events.jsonl"
        prior_thread, prior = cumulative_usage_from_jsonl(previous)
        if prior_thread != _required_string(task_state, "thread_id"):
            raise ValueError("prior cumulative usage identifies a different Codex thread")
    return receipt_from_jsonl(
        event_log,
        binding=CodexUsageBindingV1(
            campaign_id=plan_id,
            task_id=command.task_id,
            action_id=command.subtask_id,
            role=role,
            repair_or_retry=repair_or_retry,
        ),
        model=_required_string(task_state, "model"),
        codex_cli_version=_optional_string(task_state.get("codex_cli_version")),
        prior_cumulative_usage=prior,
        require_prior_cumulative_usage=command.epoch_turn_index > 1,
    )


def _verify_telemetry_usage(
    telemetry: SemanticSubtaskTelemetryV1,
    usage: CodexUsageReceiptV1,
) -> None:
    expected = {
        "usage_receipt_id": usage.receipt_id,
        "input_tokens": usage.input_tokens,
        "cached_input_tokens": usage.cached_input_tokens,
        "cache_write_input_tokens": usage.cache_write_input_tokens,
        "uncached_input_tokens": usage.input_tokens - usage.cached_input_tokens,
        "output_tokens": usage.output_tokens,
        "reasoning_output_tokens": usage.reasoning_output_tokens,
        "combined_tokens": usage.combined_tokens,
    }
    if any(getattr(telemetry, key) != value for key, value in expected.items()):
        raise ValueError("persisted semantic telemetry differs from exact resumed-turn usage")


def _verify_task_execution_policy(
    state: dict[str, Any],
    *,
    epoch_run_command: ReplayCommandV1,
    expected_model: str,
) -> None:
    if state.get("schema_version") != 2:
        raise ValueError("global task state has an unsupported schema")
    options = list(epoch_run_command.argv[5:])
    if state.get("model") != expected_model:
        raise ValueError("global task state identifies the wrong model")
    if state.get("initial_options") != options:
        raise ValueError("global task state launch options differ from the frozen command")
    core = {
        "schema_version": 1,
        "codex_exec_options": options,
        "model": expected_model,
    }
    expected_policy = {
        **core,
        "sha256": hashlib.sha256(
            json.dumps(
                core,
                allow_nan=False,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("ascii")
        ).hexdigest(),
    }
    if state.get("frozen_execution_policy") != expected_policy:
        raise ValueError("global task state does not retain the frozen execution policy")


def _verify_launch_record(path: Path, expected: BaseModel) -> None:
    try:
        actual = json.loads(path.read_text(encoding="utf-8"), parse_constant=_reject_constant)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("durable session launch record is invalid") from exc
    if actual != expected.model_dump(mode="json"):
        raise ValueError("durable session launch record differs from the semantic plan")


def _write_or_verify_handoff(handoff: AgentHandoffV1, destination: Path) -> HandoffSizeV1:
    """Persist once, or verify an interruption-left handoff before rebuilding telemetry."""
    size = measure_agent_handoff(handoff)
    if destination.exists():
        if _parse_stored_handoff(destination) != handoff:
            raise ValueError("existing handoff differs from the completed model result")
        return size
    return write_agent_handoff(handoff, destination)


def _parse_final_handoff(
    path: Path,
    subtask: SemanticSubtaskV1,
    repository: RepositoryIdentityV1,
) -> AgentHandoffV1:
    """Validate model-authored semantics and bind Supervisor-owned durable facts."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"), parse_constant=_reject_constant)
        draft = AgentHandoffDraftV1.model_validate(value)
        handoff = AgentHandoffV1(
            handoff_id=f"{subtask.subtask_id}-handoff",
            completed_objective=draft.completed_objective,
            changed_paths=draft.changed_paths,
            changed_interfaces=draft.changed_interfaces,
            repository_identity=repository,
            authority_references=subtask.authority_references,
            valid_evidence_receipts=draft.valid_evidence_receipts,
            decisions_and_invariants=draft.decisions_and_invariants,
            unresolved_findings=draft.unresolved_findings,
            remaining_work=draft.remaining_work,
            next_subtask_requirements=draft.next_subtask_requirements,
            do_not_rediscover_or_retest=draft.do_not_rediscover_or_retest,
            oversize_justification=draft.oversize_justification,
        )
        measure_agent_handoff(handoff)
        return handoff
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError, ValidationError) as exc:
        raise ValueError("subtask final message is not a valid compact AgentHandoffV1") from exc


def _parse_stored_handoff(path: Path) -> AgentHandoffV1:
    try:
        handoff = AgentHandoffV1.model_validate_json(path.read_bytes())
        measure_agent_handoff(handoff)
        return handoff
    except (OSError, ValueError, ValidationError) as exc:
        raise ValueError("stored replay handoff is invalid") from exc


def _usage_role(role: str) -> UsageRole:
    if role in {"worker", "repair"}:
        return "worker"
    if role == "coding_auditor":
        return "coding_auditor"
    if role == "physics_auditor":
        return "physics_auditor"
    if role == "supervisor":
        return "supervisor"
    return "other"


def _git(workspace: Path, *arguments: str) -> bytes:
    completed = subprocess.run(
        ("git", *arguments),
        cwd=workspace,
        check=False,
        capture_output=True,
    )
    if completed.returncode != 0:
        raise ValueError(f"git {' '.join(arguments)} failed")
    return completed.stdout


def _exclusive_write(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())


def _exclusive_json(path: Path, value: object) -> None:
    _exclusive_write(
        path,
        json.dumps(value, indent=2, sort_keys=True).encode("utf-8") + b"\n",
    )


def _write_or_verify(path: Path, value: bytes) -> None:
    """Create a launch input once, or verify exact bytes after a pre-launch interruption."""
    try:
        _exclusive_write(path, value)
        return
    except FileExistsError:
        pass
    if path.is_symlink() or not path.is_file() or path.read_bytes() != value:
        raise ValueError(f"existing launch input does not match the replay plan: {path}")


def _json_or_verify(path: Path, value: object) -> None:
    encoded = json.dumps(value, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    _write_or_verify(path, encoded)


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(json.dumps(value, indent=2, sort_keys=True).encode("utf-8") + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _required_string(value: dict[str, Any], key: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result:
        raise ValueError(f"task state has no valid {key}")
    return result


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _reject_constant(value: str) -> Any:
    raise ValueError(f"non-finite JSON constant is forbidden: {value}")


def _paths_overlap(left: Path, right: Path) -> bool:
    """Return whether either resolved path contains the other."""
    left = left.resolve()
    right = right.resolve()
    return left == right or left in right.parents or right in left.parents


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--control", type=Path, required=True)
    parser.add_argument("--authority-root", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--global-target", type=Path, required=True)
    parser.add_argument("--codex-task", type=Path, default=DEFAULT_CODEX_TASK)
    parser.add_argument(
        "--execute",
        metavar="ACKNOWLEDGEMENT",
        help=f"launch only when exactly {EXECUTION_ACKNOWLEDGEMENT!r}",
    )
    return parser


def main() -> None:
    args = _parser().parse_args()
    preparation = prepare_replay(
        control_path=args.control,
        authority_root=args.authority_root,
        workspace=args.workspace,
        artifact_root=args.artifact_root,
        global_target=args.global_target,
        codex_task=args.codex_task,
    )
    if args.execute is None:
        print(json.dumps(preparation.model_dump(mode="json"), indent=2, sort_keys=True))
        print("\nDry run only. Exact shell commands:")
        for item in preparation.commands:
            print(shlex.join(item.argv))
        return
    if args.execute != EXECUTION_ACKNOWLEDGEMENT:
        raise SystemExit("invalid execution acknowledgement; no sessions launched")
    aggregate = execute_replay(
        control_path=args.control,
        authority_root=args.authority_root,
        workspace=args.workspace,
        artifact_root=args.artifact_root,
        global_target=args.global_target,
        codex_task=args.codex_task,
    )
    _atomic_json(
        args.artifact_root / "replay-preparation.json",
        preparation.model_dump(mode="json"),
    )
    print(json.dumps(aggregate.model_dump(mode="json"), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
