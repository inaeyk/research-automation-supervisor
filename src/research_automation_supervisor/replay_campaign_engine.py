"""Thin autonomous Stage 5A historical-replay campaign controller."""

from __future__ import annotations

import glob
import hashlib
import json
import secrets
import shlex
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol, cast

from pydantic import ValidationError

from research_automation_supervisor.codex_adapter import (
    CodexProcessLaunch,
    run_prepared_codex,
)
from research_automation_supervisor.codex_models import (
    ROLE_POLICIES,
    CodexRunRequest,
    CodexRunResult,
    PreparedCodexRequest,
)
from research_automation_supervisor.durable_state import (
    append_hashed_journal_entry,
    commit_result_then_state,
    read_hashed_journal,
    reconcile_model_snapshot,
    render_json_bytes,
)
from research_automation_supervisor.errors import (
    LiveShadowDependencyError,
    ReplayCampaignDependencyError,
    ReplayCampaignInputError,
    ReplayCampaignLockError,
    ReplayCampaignStateError,
    ShadowLockError,
    WorkflowInputError,
    WorkflowLockError,
    WorkflowPromptSourceError,
    WorkflowStateError,
)
from research_automation_supervisor.live_shadow_engine import (
    SupervisorInvoker,
    _prepare_artifact_set,
    _snapshot_checkpoint,
)
from research_automation_supervisor.live_shadow_isolation import (
    RECORDED_AUTH_SOURCE,
    BubblewrapCapability,
    IsolationPreflight,
    build_bubblewrap_process_launch,
    preflight_bubblewrap_isolation,
)
from research_automation_supervisor.redaction import redact_json, redact_text
from research_automation_supervisor.replay_campaign_models import (
    PendingHumanDecision,
    ReplayCampaignState,
    SupervisorAction,
)
from research_automation_supervisor.replay_campaign_prompts import (
    build_supervisor_request,
)
from research_automation_supervisor.replay_campaign_sources import (
    PreparedReplayCampaign,
    PreparedReplayTask,
    load_human_replay_decision,
    load_replay_campaign_specification,
)
from research_automation_supervisor.shadow_engine import _ShadowLock, _write_bytes
from research_automation_supervisor.shadow_models import canonical_supervisor_uuid
from research_automation_supervisor.test_runner import (
    TestAttemptResult,
    TestSuiteResult,
    run_test_attempt,
)
from research_automation_supervisor.workflow_engine import (
    WorkflowPromptDecision,
    WorkflowPromptRequest,
    WorkflowServices,
    _resolve_codex_executable,
    _utc_string,
    _write_json,
    continue_substage,
    post_audit_prompt_source_boundary,
    prompt_source_pause_boundary,
    resume_prompt_source_substage,
    resume_substage,
    run_substage,
    substage_status,
)
from research_automation_supervisor.workflow_integrity import sha256_regular_file
from research_automation_supervisor.workflow_models import (
    WorkflowResult,
    normalize_relative_path,
    path_matches_any,
)

ZERO_HASH = "0" * 64
STATE_FILE = "state.json"
JOURNAL_FILE = "journal.jsonl"
REPORT_FILE = "campaign-report.json"
DEFAULT_REPLAY_RUNS_DIRECTORY = (
    Path(tempfile.gettempdir()) / "research-automation-supervisor-replay-campaigns"
)


class GoldTestInvoker(Protocol):
    def __call__(
        self,
        prepared_test: Any,
        artifact_directory: Path,
        action_id: str,
        *,
        environ: Mapping[str, str] | None,
    ) -> TestAttemptResult: ...


class NotificationInvoker(Protocol):
    def __call__(self, payload: Mapping[str, str]) -> None: ...


@dataclass(frozen=True)
class ReplayCampaignServices:
    """Injectable process boundaries for fake-Codex campaign tests."""

    codex_executable: str | None = None
    supervisor_invoker: SupervisorInvoker | None = None
    workflow_services: WorkflowServices | None = None
    gold_test_invoker: GoldTestInvoker = run_test_attempt
    notification_invoker: NotificationInvoker | None = None
    isolation_preflight: IsolationPreflight = preflight_bubblewrap_isolation
    bubblewrap_executable: str | None = None
    codex_authentication_file: Path | None = None
    environ: Mapping[str, str] | None = None
    token_factory: Callable[[], str] = lambda: secrets.token_hex(16)
    utc_now: Callable[[], datetime] = lambda: datetime.now(UTC)


@dataclass(frozen=True)
class _RequiredChecksNormalization:
    action: SupervisorAction
    normalized_acceptance_test_ids: tuple[str, ...] | None
    occurred: bool


@dataclass(frozen=True)
class _ReferencedPathEvidence:
    path: str
    authority: Literal["allowed", "protected"]
    read_only: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "authority": self.authority,
            "read_only": self.read_only,
            "grants_writable_scope": False,
        }


DEFAULT_REPLAY_CAMPAIGN_SERVICES = ReplayCampaignServices()


@dataclass
class _CampaignContext:
    prepared: PreparedReplayCampaign
    run_directory: Path
    state: ReplayCampaignState
    services: ReplayCampaignServices
    codex_executable: str
    isolation_capability: BubblewrapCapability | None


def run_replay_campaign(
    path: Path,
    *,
    runs_dir: Path = DEFAULT_REPLAY_RUNS_DIRECTORY,
    services: ReplayCampaignServices = DEFAULT_REPLAY_CAMPAIGN_SERVICES,
) -> ReplayCampaignState:
    """Create and synchronously drive one ordered historical replay campaign."""
    prepared = load_replay_campaign_specification(
        path,
        environ=None if services.environ is None else dict(services.environ),
    )
    executable = _resolve_campaign_codex(services)
    token = services.token_factory()
    if (
        not token
        or len(token) > 80
        or not token.replace("-", "").replace("_", "").isalnum()
    ):
        raise ReplayCampaignInputError("replay campaign run token is invalid")
    try:
        root = runs_dir.resolve(strict=False)
        root.mkdir(parents=True, exist_ok=True)
        run_directory = root / f"{prepared.specification.campaign_id}-{token}"
        run_directory.mkdir(exist_ok=False)
    except FileExistsError as exc:
        raise ReplayCampaignInputError(
            "exclusive replay campaign run directory already exists"
        ) from exc
    except (OSError, RuntimeError, ValueError) as exc:
        raise ReplayCampaignInputError(
            "replay campaign run directory could not be created"
        ) from exc
    capability = _prepare_isolation(
        prepared,
        run_directory,
        executable,
        services,
    )
    now = _utc_string(services.utc_now())
    state = ReplayCampaignState(
        campaign_id=prepared.specification.campaign_id,
        run_token=token,
        specification_path=str(prepared.specification_path),
        specification_sha256=prepared.specification_sha256,
        current_task_index=0,
        completed_task_ids=(),
        current_task_run=None,
        supervisor_session_id=None,
        task_worker_session_ids={},
        human_assisted_task_ids=(),
        human_decision_count=0,
        pending_human_decision=None,
        continuation_note_path=None,
        paused_boundary=None,
        model_terminal_task_ids=(),
        gold_evaluated_task_ids=(),
        gold_reveal_model_turn_count=None,
        post_gold_model_turn_count=None,
        status="initialized",
        pause_reason=None,
        journal_sequence=0,
        journal_hash=ZERO_HASH,
        started_at=now,
        updated_at=now,
    )
    _write_bytes(run_directory / JOURNAL_FILE, b"")
    _persist_state(run_directory, state)
    evaluator_record = _write_evaluator_record(run_directory, prepared)
    context = _CampaignContext(
        prepared=prepared,
        run_directory=run_directory,
        state=state,
        services=services,
        codex_executable=executable,
        isolation_capability=capability,
    )
    context.state = _event(
        context,
        "campaign_initialized",
        {"status": "running"},
        {
            str(prepared.specification_path): prepared.specification_sha256,
            str(evaluator_record): sha256_regular_file(evaluator_record),
        },
    )
    with _campaign_lock(run_directory, services.utc_now):
        return _drive(context)


def resume_replay_campaign(
    run_directory: Path,
    *,
    decision_path: Path | None = None,
    services: ReplayCampaignServices = DEFAULT_REPLAY_CAMPAIGN_SERVICES,
) -> ReplayCampaignState:
    """Recover a running campaign or apply one immutable pause decision."""
    resolved = _resolve_run(run_directory)
    with _campaign_lock(resolved, services.utc_now):
        state = _reconcile_state_with_journal(resolved, _load_state(resolved))
        _validate_journal(resolved, state)
        if state.status not in {"human_paused", "running"}:
            raise ReplayCampaignInputError(
                "only a running or human-paused campaign can be resumed"
            )
        prepared = load_replay_campaign_specification(
            Path(state.specification_path),
            environ=None if services.environ is None else dict(services.environ),
            require_clean=False,
        )
        if prepared.specification_sha256 != state.specification_sha256:
            raise ReplayCampaignStateError("campaign specification changed before resume")
        executable = _resolve_campaign_codex(services)
        capability = _prepare_isolation(
            prepared,
            resolved,
            executable,
            services,
            recovery=True,
        )
        context = _CampaignContext(
            prepared=prepared,
            run_directory=resolved,
            state=state,
            services=services,
            codex_executable=executable,
            isolation_capability=capability,
        )
        if state.status == "running":
            if decision_path is not None:
                raise ReplayCampaignInputError(
                    "interrupted running-campaign recovery does not accept a human decision"
                )
            return _drive(
                context,
                resume_current=context.state.current_task_run is not None,
                continuation_path=(
                    None
                    if context.state.continuation_note_path is None
                    else Path(context.state.continuation_note_path)
                ),
            )

        task_id = prepared.tasks[state.current_task_index].specification.task_id
        if context.state.pending_human_decision is None:
            if decision_path is None:
                raise ReplayCampaignInputError(
                    "a human-paused campaign requires one exact decision"
                )
            prepared_pending = _prepare_human_decision(
                context,
                decision_path,
                task_id,
            )
            _snapshot_checkpoint("before_human_decision_intent")
            context.state = _event(
                context,
                "human_decision_intent",
                {"pending_human_decision": prepared_pending},
                {
                    prepared_pending.prepared_path: sha256_regular_file(
                        Path(prepared_pending.prepared_path)
                    ),
                    prepared_pending.note_path: sha256_regular_file(
                        Path(prepared_pending.note_path)
                    ),
                },
            )
            _snapshot_checkpoint("after_human_decision_intent")
        pending = context.state.pending_human_decision
        if pending is None:
            raise ReplayCampaignStateError("human decision intent was not preserved")
        decision, note_path = _complete_human_decision(context, pending, task_id)
        _snapshot_checkpoint("after_human_decision_completion")
        if decision == "abort":
            _record_notification(context, "failed_safely", task_id)
            _write_report(context)
            return context.state
        return _drive(
            context,
            resume_current=True,
            continuation_path=note_path,
        )


def replay_campaign_status(run_directory: Path) -> ReplayCampaignState:
    """Read and verify campaign status without mutation or model launches."""
    resolved = _resolve_run(run_directory)
    state = _load_state(resolved)
    _validate_journal(resolved, state)
    try:
        current_hash = sha256_regular_file(Path(state.specification_path))
    except (OSError, WorkflowStateError) as exc:
        raise ReplayCampaignStateError("campaign specification is unavailable") from exc
    if current_hash != state.specification_sha256:
        raise ReplayCampaignStateError("campaign specification changed")
    return state


def _prepare_human_decision(
    context: _CampaignContext,
    decision_path: Path,
    task_id: str,
) -> PendingHumanDecision:
    decision, decision_bytes, _source = load_human_replay_decision(decision_path)
    index = context.state.human_decision_count
    directory = context.run_directory / "human-decisions"
    prepared_path = directory / "prepared" / f"decision-{index:03d}.yaml"
    destination = directory / f"decision-{index:03d}.yaml"
    note_path = directory / f"decision-{index:03d}-note.md"
    if destination.exists():
        raise ReplayCampaignStateError(
            "accepted human decision destination already exists"
        )
    note_bytes = decision.note.encode("utf-8")
    if prepared_path.exists() and note_path.exists():
        if (
            sha256_regular_file(prepared_path)
            != hashlib.sha256(decision_bytes).hexdigest()
            or sha256_regular_file(note_path)
            != hashlib.sha256(note_bytes).hexdigest()
        ):
            _write_bytes(prepared_path, decision_bytes)
            _write_bytes(note_path, note_bytes)
    elif prepared_path.exists() or note_path.exists():
        _write_bytes(prepared_path, decision_bytes)
        _write_bytes(note_path, note_bytes)
    else:
        _write_bytes(prepared_path, decision_bytes)
        _write_bytes(note_path, note_bytes)
    return PendingHumanDecision(
        index=index,
        task_id=task_id,
        decision=decision.decision,
        prepared_path=str(prepared_path),
        destination_path=str(destination),
        note_path=str(note_path),
        sha256=hashlib.sha256(decision_bytes).hexdigest(),
        note_sha256=hashlib.sha256(note_bytes).hexdigest(),
    )


def _complete_human_decision(
    context: _CampaignContext,
    pending: PendingHumanDecision,
    task_id: str,
) -> tuple[str, Path]:
    prepared_path = Path(pending.prepared_path)
    destination = Path(pending.destination_path)
    note_path = Path(pending.note_path)
    if pending.task_id != task_id:
        raise ReplayCampaignStateError(
            "human decision intent contradicts the current campaign task"
        )
    if sha256_regular_file(prepared_path) != pending.sha256:
        raise ReplayCampaignStateError("prepared human decision changed")
    if sha256_regular_file(note_path) != pending.note_sha256:
        raise ReplayCampaignStateError("immutable human decision note changed")
    if destination.exists():
        if sha256_regular_file(destination) != pending.sha256:
            raise ReplayCampaignStateError("accepted human decision changed")
    else:
        _write_bytes(destination, prepared_path.read_bytes())
    decision, decision_bytes, _ = load_human_replay_decision(destination)
    if (
        hashlib.sha256(decision_bytes).hexdigest() != pending.sha256
        or decision.decision != pending.decision
    ):
        raise ReplayCampaignStateError(
            "accepted human decision contradicts its durable intent"
        )
    assisted = tuple(
        dict.fromkeys((*context.state.human_assisted_task_ids, task_id))
    )
    record_path = destination.with_suffix(".json")
    _write_json(
        record_path,
        {
            "schema_version": 1,
            "index": pending.index,
            "task_id": task_id,
            "decision": decision.decision,
            "note": decision.note,
            "decision_path": str(destination),
            "decision_sha256": pending.sha256,
            "note_path": str(note_path),
            "note_sha256": pending.note_sha256,
        },
    )
    _snapshot_checkpoint("after_human_decision_accept_before_completion")
    context.state = _event(
        context,
        "human_decision_completed",
        {
            "status": "aborted" if decision.decision == "abort" else "running",
            "pause_reason": (
                "human_abort" if decision.decision == "abort" else None
            ),
            "human_assisted_task_ids": assisted,
            "human_decision_count": pending.index + 1,
            "pending_human_decision": None,
            "continuation_note_path": (
                None if decision.decision == "abort" else str(note_path)
            ),
            "paused_boundary": (
                None if decision.decision == "abort" else context.state.paused_boundary
            ),
        },
        {
            str(destination): pending.sha256,
            str(note_path): pending.note_sha256,
            str(record_path): sha256_regular_file(record_path),
        },
    )
    return decision.decision, note_path


def replay_campaign_exit_code(status: str) -> int:
    """Map Stage 5A states to the CLI contract."""
    return {
        "completed": 0,
        "human_paused": 5,
        "failed": 4,
        "aborted": 8,
        "initialized": 4,
        "running": 4,
    }[status]


def _drive(
    context: _CampaignContext,
    *,
    resume_current: bool = False,
    continuation_path: Path | None = None,
) -> ReplayCampaignState:
    while context.state.current_task_index < len(context.prepared.tasks):
        task = context.prepared.tasks[context.state.current_task_index]
        task_root = (
            context.run_directory / "tasks" / task.specification.task_id
        )
        continuation_requested = continuation_path is not None
        start_path = task_root / "task-start.json"
        if not start_path.exists():
            _write_json(
                start_path,
                {
                    "schema_version": 1,
                    "task_id": task.specification.task_id,
                    "started_at": _utc_string(context.services.utc_now()),
                },
            )
        note = None if continuation_path is None else continuation_path.read_bytes()
        source = _CampaignPromptSource(context, task, human_note=note)
        workflow_services = _workflow_services(context, source)
        expected_run = _expected_stage2_run(context, task)
        if context.state.current_task_run is None:
            context.state = _event(
                context,
                "campaign_task_started",
                {"current_task_run": str(expected_run)},
                {str(start_path): sha256_regular_file(start_path)},
            )
        elif Path(context.state.current_task_run) != expected_run:
            raise ReplayCampaignStateError(
                "current Stage 2 run contradicts the deterministic campaign task"
            )
        try:
            if resume_current:
                if not expected_run.is_dir():
                    if continuation_path is not None:
                        raise ReplayCampaignStateError(
                            "human continuation has no persistent Stage 2 run"
                        )
                    workflow = run_substage(
                        task.stage2.specification_path,
                        runs_dir=task_root / "stage2",
                        services=workflow_services,
                    )
                elif continuation_path is not None:
                    workflow = _resume_stored_continuation(
                        context,
                        task,
                        expected_run,
                        continuation_path,
                        workflow_services,
                    )
                else:
                    observed = substage_status(expected_run)
                    if observed.status in {
                        "initialized",
                        "worker_running",
                        "scope_checking",
                        "tests_running",
                        "auditor_running",
                        "repair_pending",
                    }:
                        workflow = resume_substage(
                            expected_run,
                            services=workflow_services,
                        )
                    else:
                        workflow = observed
                resume_current = False
                continuation_path = None
            else:
                if expected_run.exists():
                    observed = substage_status(expected_run)
                    workflow = (
                        resume_substage(expected_run, services=workflow_services)
                        if observed.status
                        in {
                            "initialized",
                            "worker_running",
                            "scope_checking",
                            "tests_running",
                            "auditor_running",
                            "repair_pending",
                        }
                        else observed
                    )
                else:
                    workflow = run_substage(
                        task.stage2.specification_path,
                        runs_dir=task_root / "stage2",
                        services=workflow_services,
                    )
        except (WorkflowInputError, WorkflowStateError, WorkflowLockError) as exc:
            return _pause_campaign(
                context,
                "unsafe_workflow_state",
                str(exc),
                task,
                context.state.paused_boundary or "worker_continuation",
            )
        if continuation_requested and context.state.continuation_note_path is not None:
            _snapshot_checkpoint(
                "after_stage2_continuation_accept_before_campaign_cleanup"
            )
            context.state = _event(
                context,
                "human_continuation_delivered",
                {
                    "continuation_note_path": None,
                    "paused_boundary": None,
                },
                {},
            )
        if context.state.current_task_run != workflow.artifact_directory:
            context.state = _event(
                context,
                "stage2_run_recorded",
                {"current_task_run": workflow.artifact_directory},
                {},
            )
        if workflow.worker_thread_id is not None:
            sessions = {
                **context.state.task_worker_session_ids,
                task.specification.task_id: workflow.worker_thread_id,
            }
            context.state = _event(
                context,
                "worker_session_recorded",
                {"task_worker_session_ids": sessions},
                {},
            )
        if workflow.status != "completed":
            _write_paused_task_summary(context, task, workflow)
            category = _workflow_pause_category(workflow)
            try:
                boundary = _paused_boundary(
                    context,
                    task,
                    workflow,
                    workflow_services,
                )
            except (ReplayCampaignStateError, WorkflowStateError) as exc:
                return _pause_campaign(
                    context,
                    "unsafe_workflow_state",
                    str(exc),
                    task,
                    context.state.paused_boundary or "worker_continuation",
                )
            return _pause_campaign(
                context,
                category,
                workflow.pause_reason or workflow.status,
                task,
                boundary,
            )

        terminal_record = _write_model_terminal_record(context, task, workflow)
        model_terminal = (
            *context.state.model_terminal_task_ids,
            task.specification.task_id,
        )
        context.state = _event(
            context,
            "task_model_terminal",
            {
                "model_terminal_task_ids": model_terminal,
                "current_task_index": context.state.current_task_index + 1,
                "current_task_run": None,
                "paused_boundary": None,
            },
            {str(terminal_record): sha256_regular_file(terminal_record)},
        )
        _write_report(context)

    if context.state.gold_reveal_model_turn_count is None:
        frozen_turns = _campaign_model_turn_count(context)
        context.state = _event(
            context,
            "campaign_model_turns_frozen",
            {"gold_reveal_model_turn_count": frozen_turns},
            {},
        )
    for task in context.prepared.tasks:
        task_id = task.specification.task_id
        if task_id in context.state.gold_evaluated_task_ids:
            continue
        workflow = substage_status(_expected_stage2_run(context, task))
        if workflow.status != "completed":
            raise ReplayCampaignStateError(
                "gold evaluation began before every task was model-terminal"
            )
        gold = _run_gold_evaluations(context, task)
        task_report = _write_task_summary(context, task, workflow, gold)
        evaluated = (*context.state.gold_evaluated_task_ids, task_id)
        completed = (*context.state.completed_task_ids, task_id)
        context.state = _event(
            context,
            "task_gold_evaluated",
            {
                "gold_evaluated_task_ids": evaluated,
                "completed_task_ids": completed,
            },
            {str(task_report): sha256_regular_file(task_report)},
        )
    post_gold_turns = _campaign_model_turn_count(context)
    if post_gold_turns != context.state.gold_reveal_model_turn_count:
        raise ReplayCampaignStateError(
            "a model turn occurred after campaign-wide hidden gold reveal"
        )
    if context.state.post_gold_model_turn_count is None:
        context.state = _event(
            context,
            "campaign_gold_turns_verified",
            {"post_gold_model_turn_count": post_gold_turns},
            {},
        )
    context.state = _event(
        context,
        "campaign_completed",
        {"status": "completed", "pause_reason": None},
        {},
    )
    _record_notification(context, "campaign_completed", None)
    _write_report(context)
    return context.state


_PROMPT_SOURCE_BOUNDARIES = frozenset(
    {
        "supervisor_worker_prompt",
        "supervisor_auditor_prompt",
        "supervisor_repair_prompt",
        "supervisor_finish",
        "auditor_escalation",
    }
)
_ACTIVE_WORKFLOW_STATUSES = frozenset(
    {
        "initialized",
        "worker_running",
        "scope_checking",
        "tests_running",
        "auditor_running",
        "repair_pending",
    }
)


def _resume_stored_continuation(
    context: _CampaignContext,
    task: PreparedReplayTask,
    stage2_run: Path,
    continuation_path: Path,
    services: WorkflowServices,
) -> WorkflowResult:
    """Reconcile Stage 2 before accepting one stored human continuation."""
    observed = substage_status(stage2_run)
    boundary = context.state.paused_boundary
    if boundary is None:
        raise ReplayCampaignStateError(
            "stored human continuation has no exact paused boundary"
        )
    if observed.status in _ACTIVE_WORKFLOW_STATUSES and _post_audit_recovery_started(
        stage2_run,
        boundary,
    ):
        _validate_campaign_post_audit_recovery(
            context,
            task,
            stage2_run,
            boundary,
        )
        return resume_substage(stage2_run, services=services)
    if observed.status in _ACTIVE_WORKFLOW_STATUSES and _stage2_accepted_continuation(
        context,
        task,
        stage2_run,
        continuation_path,
        boundary,
    ):
        return resume_substage(stage2_run, services=services)
    if boundary not in _PROMPT_SOURCE_BOUNDARIES:
        if _stage2_accepted_continuation(
            context,
            task,
            stage2_run,
            continuation_path,
            boundary,
        ):
            return observed
        if boundary in {"worker_continuation", "repair_limit"}:
            return continue_substage(
                stage2_run,
                continuation_path,
                services=services,
            )
        raise ReplayCampaignStateError("stored paused boundary is unsupported")
    semantic_boundary = prompt_source_pause_boundary(
        stage2_run,
        services=services,
    )
    expected_campaign_boundary = {
        "initial_worker_prompt": "supervisor_worker_prompt",
        "worker_repair_prompt": "supervisor_repair_prompt",
        "auditor_prompt": "supervisor_auditor_prompt",
        "post_audit_terminal_decision": boundary,
    }[semantic_boundary]
    if boundary != expected_campaign_boundary:
        raise ReplayCampaignStateError(
            "campaign boundary contradicts the journal-proven Stage 2 boundary"
        )
    _validate_campaign_prompt_source_pause(
        context,
        task,
        stage2_run,
        semantic_boundary,
    )
    if semantic_boundary == "post_audit_terminal_decision":
        post_audit = post_audit_prompt_source_boundary(
            stage2_run,
            services=services,
        )
        if post_audit is None:
            raise ReplayCampaignStateError(
                "post-audit boundary lacks a validated terminal decision"
            )
        expected_boundary = {
            "finish": "supervisor_finish",
            "repair_prompt": "supervisor_repair_prompt",
            "human_pause": "auditor_escalation",
        }[post_audit]
        if boundary != expected_boundary:
            raise ReplayCampaignStateError(
                "campaign boundary contradicts the validated post-audit verdict"
            )
        _validate_campaign_post_audit_recovery(
            context,
            task,
            stage2_run,
            boundary,
        )
        return resume_prompt_source_substage(
            stage2_run,
            services=services,
            allow_post_audit_recovery=True,
        )
    return resume_prompt_source_substage(stage2_run, services=services)


def _validate_campaign_prompt_source_pause(
    context: _CampaignContext,
    task: PreparedReplayTask,
    stage2_run: Path,
    semantic_boundary: str,
) -> None:
    """Bind a Stage 2 prompt pause to the matching durable supervisor turn."""
    task_id = task.specification.task_id
    actions = _supervisor_actions_for_task(context.run_directory, task_id)
    expected_source = {
        "initial_worker_prompt": "worker_prompt",
        "worker_repair_prompt": "repair_prompt",
        "auditor_prompt": "auditor_prompt",
        "post_audit_terminal_decision": None,
    }[semantic_boundary]
    if expected_source is not None:
        matching_action = (
            bool(actions) and actions[-1].get("boundary") == expected_source
        )
        matching_unlaunched_intent = (
            semantic_boundary != "initial_worker_prompt"
            and _matching_unlaunched_supervisor_intent(
                context.run_directory,
                stage2_run,
                task_id,
                expected_source,
            )
        )
        if not matching_action and not matching_unlaunched_intent:
            raise ReplayCampaignStateError(
                "prompt-source pause lacks a matching durable supervisor action"
            )
    if semantic_boundary != "initial_worker_prompt":
        return
    if any(action.get("boundary") != "worker_prompt" for action in actions):
        raise ReplayCampaignStateError(
            "initial worker-prompt supervisor history is ambiguous"
        )
    def refused_worker_prompt(action: Mapping[str, object]) -> bool:
        raw = action.get("raw_action")
        accepted = action.get("accepted_action")
        return (
            accepted is None
            and isinstance(raw, dict)
            and raw.get("action") == "worker_prompt"
            and bool(action.get("rejection_reasons"))
        ) or (
            isinstance(accepted, dict)
            and accepted.get("action") == "human_pause"
        )

    if not all(refused_worker_prompt(action) for action in actions):
        raise ReplayCampaignStateError(
            "initial worker-prompt pause lacks its recorded supervisor rejection"
        )
    state = substage_status(stage2_run)
    if (
        state.latest_worker_action_id is not None
        or state.latest_audit_action_id is not None
        or context.state.gold_evaluated_task_ids
        or context.state.gold_reveal_model_turn_count is not None
        or context.state.post_gold_model_turn_count is not None
        or task_id in context.state.model_terminal_task_ids
        or task_id in context.state.completed_task_ids
        or list((context.run_directory / "tasks").glob("*/gold-reveal.json"))
        or any(
            path.is_file()
            for path in (context.run_directory / "tasks").glob("*/gold/**/*")
        )
    ):
        raise ReplayCampaignStateError(
            "initial worker-prompt recovery has later action or gold evidence"
        )


def _matching_unlaunched_supervisor_intent(
    run_directory: Path,
    stage2_run: Path,
    task_id: str,
    boundary: str,
) -> bool:
    """Recognize one hashed intent whose adapter was proven not to start."""
    requests = [
        request
        for request in _read_matching(run_directory / "decisions", "*/request.json")
        if request.get("task_id") == task_id
        and request.get("boundary") == boundary
    ]
    unlaunched = []
    for request in requests:
        action_id = request.get("action_id")
        if (
            isinstance(action_id, str)
            and not (
                run_directory / "supervisor" / "codex" / action_id
            ).exists()
        ):
            unlaunched.append(request)
    if len(unlaunched) != 1:
        return False
    latest = unlaunched[0]
    action_id = latest.get("action_id")
    if not isinstance(action_id, str):
        return False
    escalation_paths = sorted(
        (stage2_run / "escalation").glob(
            "*-prompt_source_invalid/package.json"
        )
    )
    if not escalation_paths:
        return False
    escalation = _read_json(escalation_paths[-1])
    if (
        escalation.get("prompt_source_failure_category")
        != "supervisor_adapter_not_started"
        or escalation.get("prompt_source_adapter_status") != "not_started"
    ):
        return False
    campaign_entries = _read_campaign_journal(run_directory)
    request_path = (
        run_directory / "decisions" / action_id / "request.json"
    )
    return any(
        entry.get("reason") == "supervisor_action_intent"
        and isinstance(entry.get("artifact_hashes"), dict)
        and entry["artifact_hashes"].get(str(request_path))
        == sha256_regular_file(request_path)
        for entry in campaign_entries
    )


def _post_audit_recovery_started(
    stage2_run: Path,
    boundary: str,
) -> bool:
    expected_reason = {
        "supervisor_finish": "post_audit_finish_recovery",
        "supervisor_repair_prompt": "post_audit_repair_recovery",
        "auditor_escalation": "post_audit_finish_recovery",
    }.get(boundary)
    if expected_reason is None:
        return False
    try:
        entries = [
            json.loads(line.decode("ascii"))
            for line in (stage2_run / "journal.jsonl").read_bytes().splitlines()
        ]
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReplayCampaignStateError(
            "post-audit recovery journal could not be read"
        ) from exc
    return any(
        isinstance(entry, dict) and entry.get("reason") == expected_reason
        for entry in entries
    )


def _validate_campaign_post_audit_recovery(
    context: _CampaignContext,
    task: PreparedReplayTask,
    stage2_run: Path,
    boundary: str,
) -> None:
    """Reject recovery unless campaign evidence proves pre-gold model state."""
    task_id = task.specification.task_id
    if (
        context.state.pending_human_decision is not None
        or context.state.gold_evaluated_task_ids
        or context.state.gold_reveal_model_turn_count is not None
        or context.state.post_gold_model_turn_count is not None
        or task_id in context.state.model_terminal_task_ids
        or task_id in context.state.completed_task_ids
    ):
        raise ReplayCampaignStateError(
            "post-audit recovery is forbidden after gold or model termination"
        )
    campaign_entries = _read_campaign_journal(context.run_directory)
    if any(
        entry.get("reason")
        in {
            "campaign_model_turns_frozen",
            "task_gold_evaluated",
            "campaign_gold_turns_verified",
            "campaign_completed",
        }
        for entry in campaign_entries
    ):
        raise ReplayCampaignStateError(
            "campaign journal contains later terminal or gold evidence"
        )
    if list((context.run_directory / "tasks").glob("*/gold-reveal.json")) or any(
        path.is_file()
        for path in (context.run_directory / "tasks").glob("*/gold/**/*")
    ):
        raise ReplayCampaignStateError(
            "post-audit recovery found untracked gold evaluation artifacts"
        )
    actions = _supervisor_actions_for_task(context.run_directory, task_id)
    if not actions:
        raise ReplayCampaignStateError(
            "post-audit recovery has no durable auditor supervisor action"
        )
    recovery_started = _post_audit_recovery_started(stage2_run, boundary)
    expected_source_boundary = {
        "supervisor_finish": "finish",
        "supervisor_repair_prompt": "repair_prompt",
        "auditor_escalation": "human_pause",
    }[boundary]
    latest = actions[-1]
    accepted = latest.get("accepted_action")
    latest_boundary = latest.get("boundary")
    pause_attempt = max(context.state.human_decision_count - 1, 0)
    paused_latest = (
        latest_boundary == expected_source_boundary
        and (
            (
                isinstance(accepted, dict)
                and accepted.get("action") == "human_pause"
            )
            or (
                accepted is None
                and bool(latest.get("rejection_reasons"))
            )
        )
        and isinstance(latest.get("key"), str)
        and cast(str, latest["key"]).endswith(f"-h{pause_attempt:03d}")
    )
    allowed_latest = (
        latest_boundary == "auditor_prompt"
        and isinstance(accepted, dict)
        and accepted.get("action") == "auditor_prompt"
    ) or paused_latest or (
        recovery_started
        and latest_boundary == expected_source_boundary
        and isinstance(accepted, dict)
        and accepted.get("action") == expected_source_boundary
        and isinstance(latest.get("key"), str)
        and cast(str, latest["key"]).endswith(
            f"-h{context.state.human_decision_count:03d}"
        )
    )
    if not allowed_latest:
        raise ReplayCampaignStateError(
            "a later supervisor action follows the validated auditor prompt"
        )
    recorded_action_ids = {
        action_id
        for action in actions
        for action_id in (action.get("action_id"),)
        if isinstance(action_id, str)
    }
    requests = _read_matching(context.run_directory / "decisions", "*/request.json")
    for request in requests:
        if (
            request.get("task_id") != task_id
            or request.get("boundary") not in {"finish", "repair_prompt", "human_pause"}
        ):
            continue
        action_id = request.get("action_id")
        if not isinstance(action_id, str):
            raise ReplayCampaignStateError(
                "later supervisor intent identity is malformed"
            )
        if action_id in recorded_action_ids:
            continue
        artifact = context.run_directory / "supervisor" / "codex" / action_id
        if not artifact.exists():
            continue
        if not recovery_started:
            raise ReplayCampaignStateError(
                "a later supervisor model action is present or ambiguous"
            )
        try:
            durable_result = CodexRunResult.model_validate(
                _read_json(artifact / "result.json")
            )
        except (ReplayCampaignStateError, ValidationError) as exc:
            raise ReplayCampaignStateError(
                "a later supervisor model action is present or ambiguous"
            ) from exc
        if (
            durable_result.status != "succeeded"
            or Path(durable_result.artifact_directory) != artifact
        ):
            raise ReplayCampaignStateError(
                "later supervisor model completion is not safely reusable"
            )
    stage2_action_count = len(
        list((stage2_run / "actions").glob("worker-*.json"))
    ) + len(list((stage2_run / "actions").glob("auditor-*.json")))
    if stage2_action_count < 2:
        raise ReplayCampaignStateError(
            "post-audit recovery lacks worker and auditor action evidence"
        )


def _stage2_accepted_continuation(
    context: _CampaignContext,
    task: PreparedReplayTask,
    stage2_run: Path,
    continuation_path: Path,
    boundary: str,
) -> bool:
    note_sha256 = sha256_regular_file(continuation_path)
    try:
        journal_lines = (stage2_run / "journal.jsonl").read_bytes().splitlines()
        journal = [json.loads(line.decode("ascii")) for line in journal_lines]
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReplayCampaignStateError(
            "verified Stage 2 continuation journal could not be read"
        ) from exc
    if any(
        isinstance(entry, dict)
        and isinstance(entry.get("state_updates"), dict)
        and entry["state_updates"].get("continuation_sha256") == note_sha256
        for entry in journal
    ):
        return True
    if boundary not in _PROMPT_SOURCE_BOUNDARIES:
        return False
    attempt = context.state.human_decision_count
    suffix = f"-h{attempt:03d}"
    return any(
        action.get("task_id") == task.specification.task_id
        and isinstance(action.get("key"), str)
        and cast(str, action["key"]).endswith(suffix)
        for action in _supervisor_actions_for_task(
            context.run_directory,
            task.specification.task_id,
        )
    )


class _CampaignPromptSource:
    """Persistent-supervisor bridge used only at Stage 2 action boundaries."""

    def __init__(
        self,
        context: _CampaignContext,
        task: PreparedReplayTask,
        *,
        human_note: bytes | None = None,
    ) -> None:
        self.context = context
        self.task = task
        self.human_note = human_note

    def __call__(self, request: WorkflowPromptRequest) -> WorkflowPromptDecision:
        if self.context.state.current_task_run is None:
            self.context.state = _event(
                self.context,
                "stage2_run_recorded",
                {"current_task_run": str(request.run_directory)},
                {},
            )
        attempt = _human_attempt(self.context.run_directory, self.task.specification.task_id)
        key = (
            f"{self.task.specification.task_id}-{request.action}-"
            f"r{request.repair_round:03d}-h{attempt:03d}"
        )
        action_path = self.context.run_directory / "supervisor" / "actions" / f"{key}.json"
        if action_path.exists():
            record = _read_json(action_path)
            try:
                accepted = record["accepted_action"]
                if accepted is None:
                    raise WorkflowPromptSourceError(
                        "durable supervisor action was rejected",
                        failure_category="durable_supervisor_action_rejected",
                    )
                action = SupervisorAction.model_validate(accepted)
            except (KeyError, ValidationError) as exc:
                raise WorkflowPromptSourceError(
                    "durable supervisor action is invalid",
                    failure_category="durable_supervisor_action_invalid",
                ) from exc
            session = record.get("supervisor_session_id")
            if not isinstance(session, str):
                raise WorkflowPromptSourceError(
                    "durable supervisor session UUID is missing",
                    failure_category="durable_supervisor_session_missing",
                )
            try:
                canonical_supervisor_uuid(session)
            except ValueError as exc:
                raise WorkflowPromptSourceError(
                    "durable supervisor session UUID is noncanonical",
                    failure_category="durable_supervisor_session_noncanonical",
                ) from exc
            if self.context.state.supervisor_session_id not in {None, session}:
                raise WorkflowPromptSourceError(
                    "durable supervisor session UUID contradicts campaign state",
                    failure_category="durable_supervisor_session_mismatch",
                )
            if self.context.state.supervisor_session_id is None:
                self.context.state = _event(
                    self.context,
                    "supervisor_action_recovered",
                    {"supervisor_session_id": session},
                    {str(action_path): sha256_regular_file(action_path)},
                )
            return _workflow_decision(action, self.human_note)
        rendered = build_supervisor_request(
            self.context.prepared,
            self.task,
            request,
        )
        output_schema = rendered.output_schema
        action_id = "supervisor-" + hashlib.sha256(key.encode()).hexdigest()[:16]
        decision_directory = self.context.run_directory / "decisions" / action_id
        schema_path = decision_directory / "output-schema.json"
        prompt_path = decision_directory / "supervisor-prompt.md"
        request_path = decision_directory / "request.json"
        request_value = {
            "schema_version": 1,
            "action_id": action_id,
            "task_id": self.task.specification.task_id,
            "boundary": request.action,
            "repair_round": request.repair_round,
            "prompt_sha256": rendered.sha256,
            "prompt_byte_count": rendered.byte_count,
            "visible_evidence": rendered.visible_evidence,
            "gold_evidence_included": False,
        }
        newly_prepared = not decision_directory.exists()
        if newly_prepared:
            _prepare_artifact_set(
                decision_directory,
                {
                    "output-schema.json": render_json_bytes(output_schema),
                    "supervisor-prompt.md": rendered.content,
                    "request.json": render_json_bytes(request_value),
                },
                checkpoint_prefix="campaign_supervisor_intent",
            )
            self.context.state = _event(
                self.context,
                "supervisor_action_intent",
                {},
                {
                    str(request_path): sha256_regular_file(request_path),
                    str(prompt_path): sha256_regular_file(prompt_path),
                    str(schema_path): sha256_regular_file(schema_path),
                },
            )
            _snapshot_checkpoint("after_supervisor_action_intent")
        elif (
            _read_json(request_path) != request_value
            or sha256_regular_file(prompt_path) != rendered.sha256
            or _read_json(schema_path) != output_schema
        ):
            raise WorkflowPromptSourceError(
                "durable supervisor action intent changed",
                failure_category="durable_supervisor_intent_changed",
            )
        prepared = _prepared_supervisor_request(
            self.context,
            action_id,
            rendered.content,
        )
        resume_id = self.context.state.supervisor_session_id
        result_path = (
            self.context.run_directory
            / "supervisor"
            / "codex"
            / action_id
            / "result.json"
        )
        if result_path.is_file():
            try:
                result = CodexRunResult.model_validate(_read_json(result_path))
            except ValidationError as exc:
                raise WorkflowPromptSourceError(
                    "durable supervisor result is invalid",
                    failure_category="durable_supervisor_result_invalid",
                ) from exc
        elif newly_prepared:
            result = _invoke_supervisor(
                self.context,
                prepared,
                schema_path,
                resume_id,
            )
            _snapshot_checkpoint(
                "after_supervisor_action_process_before_completion"
            )
        else:
            raise WorkflowPromptSourceError(
                "supervisor action intent has no provable completion",
                failure_category="supervisor_completion_unproven",
            )
        if result.status != "succeeded":
            raise WorkflowPromptSourceError(
                f"supervisor transport failed safely: {result.status}",
                failure_category="supervisor_transport_failure",
                adapter_status=result.status,
            )
        session_id = _exact_supervisor_session(result, resume_id)
        raw_action, raw_typed_action = _parse_supervisor_action(result)
        normalization = _normalize_supervisor_required_checks(
            raw_typed_action,
            self.task,
        )
        action = normalization.action
        referenced_path_evidence = _qualify_supervisor_referenced_paths(
            action,
            self.task,
        )
        rejection_reasons = _supervisor_action_rejections(
            action,
            request,
            self.task,
            referenced_path_evidence,
        )
        accepted_action = None if rejection_reasons else action
        record = {
            "schema_version": 1,
            "key": key,
            "action_id": action_id,
            "task_id": self.task.specification.task_id,
            "boundary": request.action,
            "repair_round": request.repair_round,
            "supervisor_session_id": session_id,
            "resume_session_id": resume_id,
            "adapter_result": result.to_dict(),
            "supervisor_prompt_body": rendered.content.decode("utf-8"),
            "raw_action": raw_action,
            "referenced_path_evidence": (
                None
                if referenced_path_evidence is None
                else [item.to_dict() for item in referenced_path_evidence]
            ),
            "raw_supervisor_required_checks": raw_action["required_checks"],
            "normalized_acceptance_test_ids": (
                None
                if normalization.normalized_acceptance_test_ids is None
                else list(normalization.normalized_acceptance_test_ids)
            ),
            "required_checks_normalization_occurred": normalization.occurred,
            "accepted_action": (
                None
                if accepted_action is None
                else accepted_action.model_dump(mode="json")
            ),
            "rejection_reasons": list(rejection_reasons),
            "recorded_at": _utc_string(self.context.services.utc_now()),
        }
        _write_json(action_path, record)
        updates: dict[str, object] = {}
        if self.context.state.supervisor_session_id is None:
            updates["supervisor_session_id"] = session_id
        self.context.state = _event(
            self.context,
            "supervisor_action_recorded",
            updates,
            {
                str(action_path): sha256_regular_file(action_path),
                str(request_path): sha256_regular_file(request_path),
                str(prompt_path): sha256_regular_file(prompt_path),
                str(schema_path): sha256_regular_file(schema_path),
            },
        )
        if rejection_reasons:
            raise WorkflowPromptSourceError(
                "Supervisor action rejected by deterministic authority metadata: "
                + ", ".join(rejection_reasons),
                failure_category="supervisor_authority_rejection",
                adapter_status=result.status,
            )
        return _workflow_decision(action, self.human_note)


def _workflow_decision(
    action: SupervisorAction,
    human_note: bytes | None = None,
) -> WorkflowPromptDecision:
    return WorkflowPromptDecision(
        action=action.action,
        prompt=action.prompt.encode("utf-8") if action.prompt else None,
        summary=action.summary,
        human_note=human_note,
    )


def _prepared_supervisor_request(
    context: _CampaignContext,
    action_id: str,
    prompt: bytes,
) -> PreparedCodexRequest:
    specification = context.prepared.specification
    request = CodexRunRequest(
        schema_version=1,
        run_id=action_id,
        role="supervisor",
        workspace=str(context.run_directory / "quarantine" / "workspace"),
        prompt_path=str(context.prepared.supervisor_policy.path),
        model=specification.supervisor_model,
        reasoning_effort=specification.supervisor_reasoning_effort,
        timeout_seconds=specification.supervisor_timeout_seconds,
    )
    return PreparedCodexRequest(
        request_path=context.prepared.specification_path,
        request=request,
        workspace=context.run_directory / "quarantine" / "workspace",
        prompt_path=context.prepared.supervisor_policy.path,
        prompt_bytes=prompt,
        prompt_sha256=hashlib.sha256(prompt).hexdigest(),
        policy=ROLE_POLICIES["supervisor"],
    )


def _invoke_supervisor(
    context: _CampaignContext,
    request: PreparedCodexRequest,
    schema_path: Path,
    resume_id: str | None,
) -> CodexRunResult:
    runs = context.run_directory / "supervisor" / "codex"
    invoker = context.services.supervisor_invoker
    confidential = (
        context.prepared.supervisor_policy.content.decode("utf-8"),
        *(
            task.stage2.contract.content.decode("utf-8")
            for task in context.prepared.tasks
        ),
    )
    if invoker is not None:
        return invoker(
            request,
            runs_dir=runs,
            codex_executable=context.codex_executable,
            environ=context.services.environ,
            output_schema=schema_path,
            resume_thread_id=resume_id,
            skip_git_repo_check=True,
            confidential_fragments=confidential,
            process_started=None,
            process_finished=None,
        )
    capability = context.isolation_capability
    if capability is None:
        raise ReplayCampaignDependencyError("supervisor isolation is unavailable")

    def isolated_launch(
        command: Sequence[str],
        prepared_request: PreparedCodexRequest,
        environment: Mapping[str, str],
        final_message_path: Path,
        output_schema: Path | None,
    ) -> CodexProcessLaunch:
        return build_bubblewrap_process_launch(
            command,
            prepared_request,
            environment,
            final_message_path,
            output_schema,
            capability=capability,
            stage4_run_root=context.run_directory,
            runtime_home=context.run_directory / "quarantine" / "codex-home",
            forbidden_roots=_forbidden_roots(
                context.prepared,
                context.run_directory,
            ),
        )

    return run_prepared_codex(
        request,
        runs_dir=runs,
        codex_executable=context.codex_executable,
        environ=context.services.environ,
        output_schema=schema_path,
        resume_thread_id=resume_id,
        skip_git_repo_check=True,
        confidential_fragments=confidential,
        rejected_confidential_fragments=(),
        durable_command_replacements={
            str(capability.authentication_file): RECORDED_AUTH_SOURCE,
        },
        process_launch_builder=isolated_launch,
        version_probe=lambda _executable, _environment, _workspace: None,
    )


def _exact_supervisor_session(
    result: CodexRunResult,
    resume_id: str | None,
) -> str:
    metadata = _read_json(Path(result.artifact_directory) / "metadata.json")
    values = metadata.get("thread_started_ids")
    if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], str):
        raise WorkflowPromptSourceError(
            "supervisor turn did not expose one exact session UUID"
        )
    value = values[0]
    try:
        canonical_supervisor_uuid(value)
    except ValueError as exc:
        raise WorkflowPromptSourceError(
            "supervisor session UUID is not canonical"
        ) from exc
    if resume_id is not None and value != resume_id:
        raise WorkflowPromptSourceError("supervisor resume changed the exact session UUID")
    return value


def _parse_supervisor_action(
    result: CodexRunResult,
) -> tuple[dict[str, Any], SupervisorAction]:
    path = Path(result.artifact_directory) / "final-message.md"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("supervisor action root is not an object")
        typed = cast(dict[str, Any], raw)
        return typed, SupervisorAction.model_validate(typed)
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValidationError,
        ValueError,
    ) as exc:
        raise WorkflowPromptSourceError(
            "supervisor structured action is missing or invalid"
        ) from exc


def _supervisor_action_rejections(
    action: SupervisorAction,
    request: WorkflowPromptRequest,
    task: PreparedReplayTask,
    referenced_path_evidence: tuple[_ReferencedPathEvidence, ...] | None,
) -> tuple[str, ...]:
    reasons: list[str] = []
    if action.requests_authority_change:
        reasons.append("authority_change_requested")
    if action.action not in {request.action, "human_pause"}:
        reasons.append("durable_boundary_mismatch")
    if referenced_path_evidence is None:
        reasons.append("referenced_path_outside_scope")
    expected_checks = tuple(
        test.specification.id for test in task.stage2.acceptance_tests
    )
    if len(action.required_checks) != len(expected_checks) or set(
        action.required_checks
    ) != set(expected_checks):
        reasons.append("acceptance_test_ids_mismatch")
    return tuple(reasons)


def _qualify_supervisor_referenced_paths(
    action: SupervisorAction,
    task: PreparedReplayTask,
) -> tuple[_ReferencedPathEvidence, ...] | None:
    """Classify concrete no-follow references without changing frozen write scope."""
    specification = task.stage2.specification
    evidence: list[_ReferencedPathEvidence] = []
    for submitted_path in action.referenced_paths:
        try:
            path = normalize_relative_path(submitted_path)
        except ValueError:
            return None
        if glob.has_magic(path):
            return None
        protected_exact = path in specification.protected_paths
        protected = protected_exact or path_matches_any(
            path,
            specification.protected_paths,
        )
        allowed = path in specification.allowed_paths
        if protected:
            authority: Literal["allowed", "protected"] = "protected"
            explicit = protected_exact
        elif allowed:
            authority = "allowed"
            explicit = True
        else:
            return None
        if not _reference_path_is_no_follow_safe(
            task.stage2.workspace,
            path,
            explicitly_authorized=explicit,
        ):
            return None
        evidence.append(
            _ReferencedPathEvidence(
                path=path,
                authority=authority,
                read_only=authority == "protected",
            )
        )
    return tuple(evidence)


def _reference_path_is_no_follow_safe(
    workspace: Path,
    path: str,
    *,
    explicitly_authorized: bool,
) -> bool:
    """Reject symlink chains and implicit directory references without following."""
    try:
        workspace_status = workspace.lstat()
        if stat.S_ISLNK(workspace_status.st_mode) or not stat.S_ISDIR(
            workspace_status.st_mode
        ):
            return False
        current = workspace
        parts = tuple(part for part in Path(path).parts if part not in {"", "."})
        for index, part in enumerate(parts):
            current = current / part
            try:
                status = current.lstat()
            except FileNotFoundError:
                return True
            if stat.S_ISLNK(status.st_mode):
                return False
            final = index == len(parts) - 1
            if not final and not stat.S_ISDIR(status.st_mode):
                return False
            if final and stat.S_ISDIR(status.st_mode) and not explicitly_authorized:
                return False
            if final and not (
                stat.S_ISREG(status.st_mode) or stat.S_ISDIR(status.st_mode)
            ):
                return False
    except (OSError, ValueError):
        return False
    return True


def _normalize_supervisor_required_checks(
    action: SupervisorAction,
    task: PreparedReplayTask,
) -> _RequiredChecksNormalization:
    """Atomically map exact command tokens to IDs without invoking a shell."""
    tests = task.stage2.acceptance_tests
    test_ids = frozenset(test.specification.id for test in tests)
    normalized_ids: list[str] = []
    occurred = False
    for check in action.required_checks:
        if check in test_ids:
            normalized_ids.append(check)
            continue
        try:
            candidate_argv = tuple(shlex.split(check, comments=False, posix=True))
        except ValueError:
            return _RequiredChecksNormalization(action, None, False)
        matches = tuple(
            test.specification.id
            for test in tests
            if test.specification.argv == candidate_argv
        )
        if len(matches) != 1:
            return _RequiredChecksNormalization(action, None, False)
        normalized_ids.append(matches[0])
        occurred = True
    normalized = tuple(normalized_ids)
    if not occurred:
        return _RequiredChecksNormalization(action, normalized, False)
    return _RequiredChecksNormalization(
        action.model_copy(update={"required_checks": normalized}),
        normalized,
        True,
    )


def _workflow_services(
    context: _CampaignContext,
    source: _CampaignPromptSource,
) -> WorkflowServices:
    base = context.services.workflow_services or WorkflowServices(
        codex_executable=context.codex_executable,
        environ=context.services.environ,
    )
    token = _stage2_token(context, source.task)
    return replace(
        base,
        prompt_source=source,
        token_factory=lambda: token,
        require_canonical_thread_ids=True,
    )


def _stage2_token(context: _CampaignContext, task: PreparedReplayTask) -> str:
    material = (
        f"{context.state.campaign_id}\0{context.state.run_token}\0"
        f"{task.specification.task_id}"
    ).encode()
    return "replay-" + hashlib.sha256(material).hexdigest()[:32]


def _expected_stage2_run(
    context: _CampaignContext,
    task: PreparedReplayTask,
) -> Path:
    return (
        context.run_directory
        / "tasks"
        / task.specification.task_id
        / "stage2"
        / f"{task.specification.task_id}-{_stage2_token(context, task)}"
    )


def _run_gold_evaluations(
    context: _CampaignContext,
    task: PreparedReplayTask,
) -> TestSuiteResult:
    evaluator = context.prepared.evaluator_for(task.specification.task_id)
    task_root = context.run_directory / "tasks" / task.specification.task_id
    gold_root = task_root / "gold"
    suite_path = gold_root / "suite.json"
    reveal_path = task_root / "gold-reveal.json"
    frozen_turns = context.state.gold_reveal_model_turn_count
    if (
        frozen_turns is None
        or len(context.state.model_terminal_task_ids) != len(context.prepared.tasks)
        or context.state.current_task_index != len(context.prepared.tasks)
    ):
        raise ReplayCampaignStateError(
            "gold evaluation is forbidden before campaign-wide model termination"
        )
    if suite_path.is_file() and reveal_path.is_file():
        try:
            recovered_suite = TestSuiteResult.model_validate(_read_json(suite_path))
        except ValidationError as exc:
            raise ReplayCampaignStateError(
                "durable gold evaluation result is invalid"
            ) from exc
        reveal = _read_json(reveal_path)
        if reveal.get("model_turn_count_before") != frozen_turns:
            raise ReplayCampaignStateError(
                "durable gold reveal counter is invalid"
            )
        if _campaign_model_turn_count(context) != frozen_turns:
            raise ReplayCampaignStateError(
                "a model turn occurred after campaign-wide hidden gold reveal"
            )
        return recovered_suite
    revealed_at = _utc_string(context.services.utc_now())
    _write_json(
        reveal_path,
        {
            "schema_version": 1,
            "task_id": task.specification.task_id,
            "revealed_at": revealed_at,
            "model_turn_count_before": frozen_turns,
            "model_turn_count_after": None,
            "model_turns_after_reveal": None,
            "zero_post_gold_turns": None,
        },
    )
    results: list[TestAttemptResult] = []
    failed = False
    for index, prepared_test in enumerate(evaluator.evaluations):
        action_id = f"gold-{index:03d}-{prepared_test.specification.id}"
        destination = (
            gold_root / action_id
        )
        result_path = destination / "result.json"
        if result_path.is_file():
            result = _load_gold_result(result_path, prepared_test, action_id)
        else:
            result = context.services.gold_test_invoker(
                prepared_test,
                destination,
                action_id,
                environ=context.services.environ,
            )
        results.append(result)
        failed = failed or not result.passed
    suite = TestSuiteResult(passed=not failed, results=tuple(results))
    _write_json(suite_path, suite.to_dict())
    after_turns = _campaign_model_turn_count(context)
    post_reveal = after_turns - frozen_turns
    completed_at = _utc_string(context.services.utc_now())
    _write_json(
        reveal_path,
        {
            "schema_version": 1,
            "task_id": task.specification.task_id,
            "revealed_at": revealed_at,
            "completed_at": completed_at,
            "model_turn_count_before": frozen_turns,
            "model_turn_count_after": after_turns,
            "model_turns_after_reveal": post_reveal,
            "zero_post_gold_turns": post_reveal == 0,
        },
    )
    if post_reveal != 0:
        raise ReplayCampaignStateError(
            "a model turn occurred after campaign-wide hidden gold reveal"
        )
    return suite


def _load_gold_result(
    path: Path,
    prepared_test: Any,
    action_id: str,
) -> TestAttemptResult:
    try:
        result = TestAttemptResult.model_validate(_read_json(path))
    except ValidationError as exc:
        raise ReplayCampaignStateError("durable gold test result is invalid") from exc
    test = prepared_test.specification
    if (
        result.action_id != action_id
        or result.test_id != test.id
        or result.argv != test.argv
        or result.cwd != str(prepared_test.cwd)
    ):
        raise ReplayCampaignStateError(
            "durable gold test result contradicts evaluator authority"
        )
    for artifact, expected in (
        (result.stdout_artifact, result.stdout_sha256),
        (result.stderr_artifact, result.stderr_sha256),
    ):
        if artifact is None:
            raise ReplayCampaignStateError("durable gold test log is missing")
        if sha256_regular_file(Path(artifact)) != expected:
            raise ReplayCampaignStateError("durable gold test log changed")
    return result


def _task_model_turn_count(
    context: _CampaignContext,
    task: PreparedReplayTask,
) -> int:
    stage2_run = _expected_stage2_run(context, task)
    stage2_turns = len(
        [
            path
            for path in (stage2_run / "actions").glob("*.json")
            if _read_json(path).get("kind") in {"worker", "auditor"}
        ]
    )
    supervisor_turns = len(
        _supervisor_actions_for_task(
            context.run_directory,
            task.specification.task_id,
        )
    )
    return stage2_turns + supervisor_turns


def _campaign_model_turn_count(context: _CampaignContext) -> int:
    return sum(
        _task_model_turn_count(context, task) for task in context.prepared.tasks
    )


def _write_model_terminal_record(
    context: _CampaignContext,
    task: PreparedReplayTask,
    workflow: WorkflowResult,
) -> Path:
    stage2 = Path(workflow.artifact_directory)
    stage2_state = _read_json(stage2 / "state.json")
    git_evidence = _read_optional_path(
        stage2_state.get("latest_git_evidence_path")
    )
    final_diff = None
    if isinstance(git_evidence, dict):
        patch = git_evidence.get("patch_artifact")
        if isinstance(patch, str):
            with suppress(OSError):
                final_diff = Path(patch).read_text(
                    encoding="utf-8",
                    errors="replace",
                )
    path = (
        context.run_directory
        / "tasks"
        / task.specification.task_id
        / "model-terminal.json"
    )
    _write_json(
        path,
        {
            "schema_version": 1,
            "task_id": task.specification.task_id,
            "recorded_at": workflow.updated_at,
            "stage2_result": workflow.to_dict(),
            "final_implementation_diff": final_diff,
            "fixed_tests": _read_matching(
                stage2 / "tests",
                "round-*/suite.json",
            ),
            "git_scope_evidence": _read_matching(
                stage2 / "git",
                "round-*/evidence.json",
            ),
            "model_turn_count": _task_model_turn_count(context, task),
            "prompt_evidence": _read_matching(
                stage2 / "prompt-evidence",
                "*.json",
            ),
            "gold_revealed": False,
        },
    )
    return path


def _write_task_summary(
    context: _CampaignContext,
    task: PreparedReplayTask,
    workflow: WorkflowResult,
    gold: TestSuiteResult,
) -> Path:
    stage2 = Path(workflow.artifact_directory)
    actions = _supervisor_actions_for_task(context.run_directory, task.specification.task_id)
    worker_reports = _read_matching(stage2 / "worker", "*.structured.json")
    auditor_reports = _read_matching(stage2 / "audits", "*.structured.json")
    state = _read_json(stage2 / "state.json")
    git_evidence = _read_optional_path(state.get("latest_git_evidence_path"))
    final_diff = None
    if isinstance(git_evidence, dict):
        patch = git_evidence.get("patch_artifact")
        if isinstance(patch, str):
            with suppress(OSError):
                final_diff = Path(patch).read_text(encoding="utf-8", errors="replace")
    metadata_paths = sorted(stage2.glob("worker/codex/*/metadata.json")) + sorted(
        stage2.glob("audits/codex/*/metadata.json")
    )
    metadata = [_read_json(path) for path in metadata_paths]
    process_count = sum(1 for item in metadata if item.get("process_launched") is True)
    supervisor_processes = sum(
        1 for item in actions if _supervisor_process_launched(item)
    )
    gold_processes = sum(
        1
        for result in gold.results
        if result.status not in {"launch_failed", "skipped"}
    )
    reveal = _read_json(
        context.run_directory
        / "tasks"
        / task.specification.task_id
        / "gold-reveal.json"
    )
    started_record = _read_json(
        context.run_directory
        / "tasks"
        / task.specification.task_id
        / "task-start.json"
    )
    started_at = cast(str, started_record["started_at"])
    ended_at = cast(str, reveal["completed_at"])
    elapsed_seconds = _duration_seconds(started_at, ended_at)
    assisted = task.specification.task_id in context.state.human_assisted_task_ids
    verdict = (
        "gold_mismatch"
        if not gold.passed
        else "human_assisted"
        if assisted
        else "autonomous"
    )
    report_path = (
        context.run_directory
        / "tasks"
        / task.specification.task_id
        / "task-report.json"
    )
    _write_json(
        report_path,
        {
            "schema_version": 1,
            "task_id": task.specification.task_id,
            "title": task.specification.title,
            "verdict": verdict,
            "supervisor_instructions_and_actions": actions,
            "worker_requests": _read_matching(
                stage2 / "worker/codex", "*/request.normalized.json"
            ),
            "worker_actions": _read_matching(stage2 / "actions", "worker-*.json"),
            "worker_handoffs": _read_matching(stage2 / "handoffs", "worker-*.json"),
            "prompt_evidence": _read_json(
                context.run_directory
                / "tasks"
                / task.specification.task_id
                / "model-terminal.json"
            )["prompt_evidence"],
            "worker_reports": worker_reports,
            "auditor_requests": _read_matching(
                stage2 / "audits/codex", "*/request.normalized.json"
            ),
            "auditor_reports": auditor_reports,
            "auditor_actions": _read_matching(stage2 / "actions", "auditor-*.json"),
            "auditor_handoffs": _read_matching(stage2 / "handoffs", "auditor-*.json"),
            "tests": _read_matching(stage2 / "tests", "round-*/suite.json"),
            "git_scope_evidence": _read_matching(
                stage2 / "git", "round-*/evidence.json"
            ),
            "repair_rounds": workflow.repair_round,
            "uuids": {
                "supervisor": context.state.supervisor_session_id,
                "worker": workflow.worker_thread_id,
                "auditors": [
                    identifier
                    for item in _read_matching(stage2 / "actions", "auditor-*.json")
                    for identifier in item.get("thread_started_ids", [])
                ],
            },
            "human_pauses_and_decisions": _human_records(
                context.run_directory,
                task.specification.task_id,
            ),
            "notifications": _notifications(context.run_directory),
            "final_diff": final_diff,
            "gold_hidden_until_terminal": True,
            "gold_reveal_counters": reveal,
            "model_turns_after_gold_reveal": reveal["model_turns_after_reveal"],
            "zero_post_gold_turns": reveal["zero_post_gold_turns"],
            "gold_evaluation": gold.to_dict(),
            "production_profile": task.specification.production_profile.model_dump(
                mode="json"
            ),
            "build_runtime_evidence": gold.to_dict(),
            "elapsed_seconds": elapsed_seconds,
            "started_at": started_at,
            "ended_at": ended_at,
            "process_count": process_count + supervisor_processes + gold_processes,
            "model_turn_count": _task_model_turn_count(context, task),
            "human_assisted": assisted,
            "stage2_result": workflow.to_dict(),
        },
    )
    return report_path


def _write_paused_task_summary(
    context: _CampaignContext,
    task: PreparedReplayTask,
    workflow: WorkflowResult,
) -> None:
    stage2 = Path(workflow.artifact_directory)
    _write_json(
        context.run_directory
        / "tasks"
        / task.specification.task_id
        / "task-report.json",
        {
            "schema_version": 1,
            "task_id": task.specification.task_id,
            "title": task.specification.title,
            "verdict": "failed",
            "supervisor_instructions_and_actions": _supervisor_actions_for_task(
                context.run_directory,
                task.specification.task_id,
            ),
            "worker_requests": _read_matching(
                stage2 / "worker/codex", "*/request.normalized.json"
            ),
            "worker_actions": _read_matching(stage2 / "actions", "worker-*.json"),
            "worker_handoffs": _read_matching(stage2 / "handoffs", "worker-*.json"),
            "prompt_evidence": _read_matching(
                stage2 / "prompt-evidence", "*.json"
            ),
            "worker_reports": _read_matching(stage2 / "worker", "*.structured.json"),
            "auditor_requests": _read_matching(
                stage2 / "audits/codex", "*/request.normalized.json"
            ),
            "auditor_reports": _read_matching(stage2 / "audits", "*.structured.json"),
            "auditor_actions": _read_matching(stage2 / "actions", "auditor-*.json"),
            "auditor_handoffs": _read_matching(stage2 / "handoffs", "auditor-*.json"),
            "tests": _read_matching(stage2 / "tests", "round-*/suite.json"),
            "git_scope_evidence": _read_matching(
                stage2 / "git", "round-*/evidence.json"
            ),
            "repair_rounds": workflow.repair_round,
            "uuids": {
                "supervisor": context.state.supervisor_session_id,
                "worker": workflow.worker_thread_id,
            },
            "human_pauses_and_decisions": _human_records(
                context.run_directory,
                task.specification.task_id,
            ),
            "notifications": _notifications(context.run_directory),
            "final_diff": None,
            "gold_hidden_until_terminal": True,
            "gold_reveal_counters": None,
            "model_turns_after_gold_reveal": None,
            "zero_post_gold_turns": None,
            "gold_evaluation": None,
            "production_profile": task.specification.production_profile.model_dump(
                mode="json"
            ),
            "build_runtime_evidence": None,
            "elapsed_seconds": _duration_seconds(
                cast(
                    str,
                    _read_json(
                        context.run_directory
                        / "tasks"
                        / task.specification.task_id
                        / "task-start.json"
                    )["started_at"],
                ),
                workflow.updated_at,
            ),
            "started_at": _read_json(
                context.run_directory
                / "tasks"
                / task.specification.task_id
                / "task-start.json"
            )["started_at"],
            "ended_at": workflow.updated_at,
            "process_count": _task_stage2_process_count(stage2)
            + sum(
                1
                for item in _supervisor_actions_for_task(
                    context.run_directory,
                    task.specification.task_id,
                )
                if _supervisor_process_launched(item)
            ),
            "model_turn_count": _task_model_turn_count(context, task),
            "human_assisted": (
                task.specification.task_id
                in context.state.human_assisted_task_ids
            ),
            "stage2_result": workflow.to_dict(),
            "escalation_evidence": _stage2_escalation_evidence(
                Path(workflow.artifact_directory)
            ),
        },
    )


def _pause_campaign(
    context: _CampaignContext,
    category: str,
    detail: str,
    task: PreparedReplayTask,
    boundary: str,
) -> ReplayCampaignState:
    safe_detail = redact_text(detail, context.prepared.sensitive_values)[:16_384]
    escalation = _stage2_escalation_evidence(
        None
        if context.state.current_task_run is None
        else Path(context.state.current_task_run)
    )
    transport_lines = ""
    error_category = escalation.get("transport_error_category")
    stderr_tail = escalation.get("transport_stderr_tail")
    prompt_failure = escalation.get("prompt_source_failure_category")
    prompt_adapter = escalation.get("prompt_source_adapter_status")
    if isinstance(error_category, str):
        transport_lines += f"- Transport error category: {error_category}\n"
    if isinstance(stderr_tail, str) and stderr_tail:
        transport_lines += (
            "- Bounded transport stderr tail:\n\n"
            "```text\n"
            f"{stderr_tail}\n"
            "```\n"
        )
    if isinstance(prompt_failure, str):
        transport_lines += (
            f"- Prompt-source failure category: {prompt_failure}\n"
        )
    if isinstance(prompt_adapter, str):
        transport_lines += (
            f"- Prompt-source adapter status: {prompt_adapter}\n"
        )
    worker_id = context.state.task_worker_session_ids.get(
        task.specification.task_id,
        "not established",
    )
    context.state = _event(
        context,
        "human_review_required",
        {
            "status": "human_paused",
            "pause_reason": category,
            "paused_boundary": boundary,
        },
        {},
    )
    packet = context.run_directory / "human-review-packet.md"
    _write_bytes(
        packet,
        (
            "# Human review required\n\n"
            f"- Campaign: {context.state.campaign_id}\n"
            f"- Task: {task.specification.task_id}\n"
            f"- Safe reason category: {category}\n"
            f"- Exact paused boundary: {boundary}\n"
            f"- Detail: {safe_detail}\n"
            f"{transport_lines}"
            f"- Stage 2 run: {context.state.current_task_run or 'not created'}\n"
            f"- Supervisor UUID: {context.state.supervisor_session_id or 'not established'}\n"
            f"- Worker UUID: {worker_id}\n"
            f"- Resume: resume-replay-campaign {context.run_directory} "
            "--decision DECISION.yaml\n"
        ).encode(),
    )
    _record_notification(
        context,
        "human_review_required",
        task.specification.task_id,
    )
    _write_report(context)
    return context.state


def _stage2_escalation_evidence(
    stage2_run: Path | None,
) -> dict[str, str]:
    """Load only bounded safe failure fields from Stage 2 evidence."""
    if stage2_run is None:
        return {}
    path = stage2_run / "escalation" / "package.json"
    try:
        value = _read_json(path)
    except ReplayCampaignStateError:
        return {}
    category = value.get("transport_error_category")
    tail = value.get("transport_stderr_tail")
    prompt_failure = value.get("prompt_source_failure_category")
    prompt_adapter = value.get("prompt_source_adapter_status")
    evidence: dict[str, str] = {}
    if isinstance(category, str) and category.startswith("auditor_"):
        evidence.update(
            {
                "transport_error_category": category[:256],
                "transport_stderr_tail": (
                    tail[-4096:] if isinstance(tail, str) else ""
                ),
            }
        )
    if isinstance(prompt_failure, str):
        evidence["prompt_source_failure_category"] = prompt_failure[:256]
    if isinstance(prompt_adapter, str):
        evidence["prompt_source_adapter_status"] = prompt_adapter[:256]
    return evidence


def _record_notification(
    context: _CampaignContext,
    category: str,
    task_id: str | None,
) -> None:
    raw_payload = {
        "campaign_id": context.state.campaign_id,
        "task_id": task_id or "-",
        "reason_category": category,
        "run_token": context.state.run_token,
        "instruction": "Inspect replay-campaign-status for this opaque run token.",
    }
    redacted = redact_json(raw_payload, context.prepared.sensitive_values)
    if not isinstance(redacted, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in redacted.items()
    ):
        raise ReplayCampaignStateError("notification redaction boundary failed")
    payload = cast(dict[str, str], redacted)
    directory = context.run_directory / "notifications"
    index = len(list(directory.glob("*.json"))) if directory.exists() else 0
    record_path = directory / f"notification-{index:03d}.json"
    attempted_at = _utc_string(context.services.utc_now())
    _write_json(
        record_path,
        {
            "schema_version": 1,
            "payload": payload,
            "status": "attempting",
            "error_category": None,
            "attempted_at": attempted_at,
            "recorded_at": attempted_at,
        },
    )
    status = "succeeded"
    error: str | None = None
    try:
        invoker = context.services.notification_invoker or _windows_notification
        invoker(payload)
    except Exception as exc:
        status = "failed"
        error = type(exc).__name__
    _write_json(
        record_path,
        {
            "schema_version": 1,
            "payload": payload,
            "status": status,
            "error_category": error,
            "attempted_at": attempted_at,
            "recorded_at": _utc_string(context.services.utc_now()),
        },
    )
    if task_id is not None:
        task_report = (
            context.run_directory / "tasks" / task_id / "task-report.json"
        )
        if task_report.is_file():
            report = _read_json(task_report)
            report["notifications"] = _notifications(context.run_directory)
            _write_json(task_report, report)


def _windows_notification(payload: Mapping[str, str]) -> None:
    executable = shutil.which("powershell.exe") or shutil.which("powershell")
    if executable is None:
        raise ReplayCampaignDependencyError("Windows PowerShell notification helper is absent")
    text = "\n".join(
        (
            f"Campaign: {payload['campaign_id']}",
            f"Task: {payload['task_id']}",
            f"Reason: {payload['reason_category']}",
            f"Run token: {payload['run_token']}",
            payload["instruction"],
        )
    )
    command = (
        executable,
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        (
            "param([string]$Message); "
            "Add-Type -AssemblyName PresentationFramework; "
            "[System.Windows.MessageBox]::Show($Message, 'Research Replay Campaign') "
            "| Out-Null"
        ),
        text,
    )
    completed = subprocess.run(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=15,
        close_fds=True,
    )
    if completed.returncode != 0:
        raise ReplayCampaignDependencyError("Windows notification helper failed")


def _prepare_isolation(
    prepared: PreparedReplayCampaign,
    run_directory: Path,
    executable: str,
    services: ReplayCampaignServices,
    *,
    recovery: bool = False,
) -> BubblewrapCapability | None:
    resolved_run = run_directory.resolve(strict=False)
    for root in _forbidden_roots(prepared, run_directory):
        if _path_contains(root, resolved_run) or _path_contains(resolved_run, root):
            raise ReplayCampaignInputError(
                "campaign run directory must be disjoint from replay and gold roots"
            )
    quarantine = run_directory / "quarantine"
    workspace = quarantine / "workspace"
    runtime_home = quarantine / "codex-home"
    try:
        quarantine.mkdir(parents=True, exist_ok=True)
        workspace.mkdir(exist_ok=True)
        runtime_home.mkdir(exist_ok=True)
        (run_directory / "decisions").mkdir(exist_ok=True)
        (run_directory / "supervisor" / "actions").mkdir(
            parents=True,
            exist_ok=True,
        )
    except OSError as exc:
        raise ReplayCampaignInputError("supervisor quarantine could not be created") from exc
    if services.supervisor_invoker is not None:
        return None
    try:
        return services.isolation_preflight(
            bubblewrap_executable=services.bubblewrap_executable,
            codex_executable=executable,
            authentication_file=services.codex_authentication_file,
            environ=services.environ,
            forbidden_roots=_forbidden_roots(prepared, run_directory),
        )
    except LiveShadowDependencyError as exc:
        label = "recovery" if recovery else "launch"
        raise ReplayCampaignDependencyError(
            f"supervisor isolation unavailable during {label}"
        ) from exc


def _forbidden_roots(
    prepared: PreparedReplayCampaign,
    run_directory: Path,
) -> tuple[Path, ...]:
    roots = [task.stage2.repository_root for task in prepared.tasks] + [
        root for evaluator in prepared.evaluators for root in evaluator.gold_roots
    ]
    unique: list[Path] = []
    for root in roots:
        resolved = root.resolve(strict=False)
        if resolved not in unique:
            unique.append(resolved)
    if len(unique) < 2:
        raise ReplayCampaignInputError(
            "campaign isolation requires distinct replay and gold roots"
        )
    return tuple(unique)


def _write_evaluator_record(
    run_directory: Path,
    prepared: PreparedReplayCampaign,
) -> Path:
    path = run_directory / "engine-only" / "evaluators.normalized.json"
    _write_json(
        path,
        {
            "schema_version": 1,
            "campaign_specification_sha256": prepared.specification_sha256,
            "evaluators": [
                evaluator.normalized_record()
                for evaluator in prepared.evaluators
            ],
        },
    )
    return path


def _path_contains(parent: Path, child: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def _resolve_campaign_codex(services: ReplayCampaignServices) -> str:
    value = services.codex_executable
    if value is None and services.workflow_services is not None:
        value = services.workflow_services.codex_executable
    try:
        return _resolve_codex_executable(value)
    except Exception as exc:
        raise ReplayCampaignDependencyError("Codex executable is required") from exc


def _event(
    context: _CampaignContext,
    reason: str,
    updates: Mapping[str, object],
    artifacts: Mapping[str, str],
) -> ReplayCampaignState:
    if context.state.journal_sequence:
        _validate_journal(context.run_directory, context.state)
    timestamp = _utc_string(context.services.utc_now())
    body = {
        "schema_version": 1,
        "sequence": context.state.journal_sequence + 1,
        "previous_state": (
            None if context.state.journal_sequence == 0 else context.state.status
        ),
        "new_state": updates.get("status", context.state.status),
        "timestamp": timestamp,
        "reason": reason,
        "artifact_hashes": dict(sorted(artifacts.items())),
        "state_updates": _json_compatible(updates),
        "previous_hash": context.state.journal_hash,
    }
    _entry, digest = append_hashed_journal_entry(
        context.run_directory / JOURNAL_FILE,
        body,
        validate=lambda _value: None,
        error_factory=ReplayCampaignStateError,
        error_message="campaign journal could not be appended",
    )
    copied = context.state.model_dump(mode="json")
    copied.update(cast(dict[str, object], _json_compatible(updates)))
    copied.update(
        {
            "journal_sequence": context.state.journal_sequence + 1,
            "journal_hash": digest,
            "updated_at": timestamp,
        }
    )
    try:
        context.state = ReplayCampaignState.model_validate(copied)
    except ValidationError as exc:
        raise ReplayCampaignStateError(
            "campaign journal update produced invalid state"
        ) from exc
    _persist_state(context.run_directory, context.state)
    return context.state


def _validate_journal(run_directory: Path, state: ReplayCampaignState) -> None:
    entries = _read_campaign_journal(run_directory)
    replay = _campaign_replay_values()
    last_timestamp: str | None = None
    for entry in entries:
        updates = cast(dict[str, object], entry["state_updates"])
        replay.update(updates)
        if entry.get("new_state") != replay["status"]:
            raise ReplayCampaignStateError("campaign journal state transition is invalid")
        last_timestamp = cast(str, entry["timestamp"])
    previous = cast(str, entries[-1]["entry_hash"]) if entries else ZERO_HASH
    if (
        len(entries) != state.journal_sequence
        or previous != state.journal_hash
        or not entries
        or last_timestamp != state.updated_at
    ):
        raise ReplayCampaignStateError("campaign state disagrees with its journal")
    snapshot = state.model_dump(mode="json")
    if any(snapshot[key] != value for key, value in replay.items()):
        raise ReplayCampaignStateError("campaign state contradicts journal replay")


def _read_campaign_journal(run_directory: Path) -> list[dict[str, Any]]:
    entries = read_hashed_journal(
        run_directory / JOURNAL_FILE,
        error_factory=ReplayCampaignStateError,
        malformed_message="campaign journal chain is invalid",
    )
    mutable = set(_campaign_replay_values())
    for entry in entries:
        updates = entry.get("state_updates")
        if not isinstance(updates, dict) or any(key not in mutable for key in updates):
            raise ReplayCampaignStateError("campaign journal state update is invalid")
        timestamp = entry.get("timestamp")
        if not isinstance(timestamp, str):
            raise ReplayCampaignStateError("campaign journal timestamp is invalid")
        artifacts = entry.get("artifact_hashes")
        if not isinstance(artifacts, dict):
            raise ReplayCampaignStateError("campaign journal artifacts are invalid")
        for path, expected in artifacts.items():
            if not isinstance(path, str) or not isinstance(expected, str):
                raise ReplayCampaignStateError("campaign artifact mapping is invalid")
            try:
                actual = sha256_regular_file(Path(path))
            except (OSError, WorkflowStateError) as exc:
                raise ReplayCampaignStateError(
                    "campaign journal evidence is missing"
                ) from exc
            if actual != expected:
                raise ReplayCampaignStateError(
                    "campaign journal evidence was replaced"
                )
    return entries


def _campaign_replay_values() -> dict[str, object]:
    return {
        "current_task_index": 0,
        "completed_task_ids": [],
        "current_task_run": None,
        "supervisor_session_id": None,
        "task_worker_session_ids": {},
        "human_assisted_task_ids": [],
        "human_decision_count": 0,
        "pending_human_decision": None,
        "continuation_note_path": None,
        "paused_boundary": None,
        "model_terminal_task_ids": [],
        "gold_evaluated_task_ids": [],
        "gold_reveal_model_turn_count": None,
        "post_gold_model_turn_count": None,
        "status": "initialized",
        "pause_reason": None,
    }


def _reconcile_state_with_journal(
    run_directory: Path,
    state: ReplayCampaignState,
) -> ReplayCampaignState:
    entries = _read_campaign_journal(run_directory)
    reconciled = reconcile_model_snapshot(
        state,
        entries,
        model=ReplayCampaignState,
        error_factory=ReplayCampaignStateError,
        error_message="campaign journal recovery produced invalid state",
    )
    if reconciled == state:
        return state
    _persist_state(run_directory, reconciled)
    _validate_journal(run_directory, reconciled)
    return reconciled


def _persist_state(run_directory: Path, state: ReplayCampaignState) -> None:
    commit_result_then_state(
        result_path=None,
        result_value=None,
        state_path=run_directory / STATE_FILE,
        state_value=state.to_dict(),
        checkpoint=_snapshot_checkpoint,
        error_factory=ReplayCampaignStateError,
        error_message="campaign state snapshot could not be committed",
    )


def _load_state(run_directory: Path) -> ReplayCampaignState:
    try:
        return ReplayCampaignState.model_validate(
            _read_json(run_directory / STATE_FILE)
        )
    except ValidationError as exc:
        raise ReplayCampaignStateError("campaign state snapshot is invalid") from exc


def _resolve_run(path: Path) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ReplayCampaignInputError("campaign run directory could not be resolved") from exc
    if not resolved.is_dir():
        raise ReplayCampaignInputError("campaign run path is not a directory")
    return resolved


@contextmanager
def _campaign_lock(
    run_directory: Path,
    utc_now: Callable[[], datetime],
) -> Iterator[None]:
    try:
        with _ShadowLock(run_directory, utc_now):
            yield
    except ShadowLockError as exc:
        raise ReplayCampaignLockError(str(exc)) from exc


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReplayCampaignStateError("campaign JSON evidence is invalid") from exc
    if not isinstance(value, dict):
        raise ReplayCampaignStateError("campaign JSON evidence must be an object")
    return cast(dict[str, Any], value)


def _json_compatible(value: object) -> object:
    if hasattr(value, "model_dump"):
        return cast(Any, value).model_dump(mode="json")
    if isinstance(value, Mapping):
        return {str(key): _json_compatible(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_compatible(item) for item in value]
    return value


def _workflow_pause_category(result: WorkflowResult) -> str:
    reason = result.pause_reason or result.status
    if result.status == "repair_limit_paused" or reason.endswith("repair_limit"):
        return "repair_rounds_exhausted"
    if reason.startswith("worker_"):
        return "worker_requires_human"
    if reason.startswith("auditor_"):
        return "auditor_requires_judgment"
    if reason.startswith("prompt_source"):
        return "supervisor_requires_human"
    return "unsafe_workflow_state"


def _paused_boundary(
    context: _CampaignContext,
    task: PreparedReplayTask,
    result: WorkflowResult,
    services: WorkflowServices,
) -> str:
    reason = result.pause_reason or result.status
    if result.status == "repair_limit_paused" or reason.endswith("repair_limit"):
        return "repair_limit"
    if reason == "auditor_escalated":
        return "auditor_escalation"
    if reason.startswith("prompt_source"):
        semantic_boundary = prompt_source_pause_boundary(
            Path(result.artifact_directory),
            services=services,
        )
        _validate_campaign_prompt_source_pause(
            context,
            task,
            Path(result.artifact_directory),
            semantic_boundary,
        )
        direct = {
            "initial_worker_prompt": "supervisor_worker_prompt",
            "worker_repair_prompt": "supervisor_repair_prompt",
            "auditor_prompt": "supervisor_auditor_prompt",
        }.get(semantic_boundary)
        if direct is not None:
            return direct
        recovered = post_audit_prompt_source_boundary(
                Path(result.artifact_directory),
                services=services,
            )
        if recovered is None:
            raise ReplayCampaignStateError(
                "terminal prompt-source pause lacks validated audit evidence"
            )
        return {
            "finish": "supervisor_finish",
            "human_pause": "auditor_escalation",
            "repair_prompt": "supervisor_repair_prompt",
        }[recovered]
    return "worker_continuation"


def _human_attempt(run_directory: Path, task_id: str) -> int:
    del task_id
    directory = run_directory / "human-decisions"
    return len(list(directory.glob("*.yaml"))) if directory.exists() else 0


def _read_optional_path(value: object) -> object:
    if not isinstance(value, str):
        return None
    with suppress(ReplayCampaignStateError):
        return _read_json(Path(value))
    return None


def _read_matching(root: Path, pattern: str) -> list[dict[str, Any]]:
    return [_read_json(path) for path in sorted(root.glob(pattern))]


def _supervisor_actions_for_task(
    run_directory: Path,
    task_id: str,
) -> list[dict[str, Any]]:
    actions = _read_matching(run_directory / "supervisor/actions", "*.json")
    selected = [
        action for action in actions if action.get("task_id") == task_id
    ]
    return sorted(
        selected,
        key=lambda action: (
            str(action.get("recorded_at", "")),
            str(action.get("action_id", "")),
        ),
    )


def _human_records(run_directory: Path, task_id: str) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for path in sorted((run_directory / "human-decisions").glob("decision-*.json")):
        record = _read_json(path)
        if record.get("task_id") == task_id:
            records.append(cast(dict[str, object], record))
    return records


def _notifications(run_directory: Path) -> list[dict[str, Any]]:
    return _read_matching(run_directory / "notifications", "*.json")


def _supervisor_process_launched(action: Mapping[str, Any]) -> bool:
    adapter = action.get("adapter_result")
    if not isinstance(adapter, dict):
        return False
    artifact = adapter.get("artifact_directory")
    if not isinstance(artifact, str):
        return False
    with suppress(ReplayCampaignStateError):
        return _read_json(Path(artifact) / "metadata.json").get(
            "process_launched"
        ) is True
    return False


def _task_stage2_process_count(stage2: Path) -> int:
    paths = sorted(stage2.glob("worker/codex/*/metadata.json")) + sorted(
        stage2.glob("audits/codex/*/metadata.json")
    )
    return sum(
        1 for path in paths if _read_json(path).get("process_launched") is True
    )


def _duration_seconds(started_at: str, ended_at: str) -> float:
    try:
        started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        ended = datetime.fromisoformat(ended_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReplayCampaignStateError(
            "campaign evidence timestamp is invalid"
        ) from exc
    return round(max(0.0, (ended - started).total_seconds()), 6)


def _write_report(context: _CampaignContext) -> None:
    _verify_completed_gold_boundaries(context)
    task_reports = _read_matching(context.run_directory / "tasks", "*/task-report.json")
    _write_json(
        context.run_directory / REPORT_FILE,
        {
            "schema_version": 1,
            "campaign_id": context.state.campaign_id,
            "title": context.prepared.specification.title,
            "status": context.state.status,
            "current_task_index": context.state.current_task_index,
            "completed_task_ids": list(context.state.completed_task_ids),
            "model_terminal_task_ids": list(
                context.state.model_terminal_task_ids
            ),
            "gold_evaluated_task_ids": list(
                context.state.gold_evaluated_task_ids
            ),
            "paused_boundary": context.state.paused_boundary,
            "gold_reveal_counters": {
                "model_turn_count_before": (
                    context.state.gold_reveal_model_turn_count
                ),
                "model_turn_count_after": (
                    context.state.post_gold_model_turn_count
                ),
            },
            "zero_post_gold_turns": (
                context.state.gold_reveal_model_turn_count is not None
                and context.state.post_gold_model_turn_count
                == context.state.gold_reveal_model_turn_count
            ),
            "supervisor_session_id": context.state.supervisor_session_id,
            "task_worker_session_ids": context.state.task_worker_session_ids,
            "human_assisted_task_ids": list(context.state.human_assisted_task_ids),
            "human_decision_count": context.state.human_decision_count,
            "human_assisted": bool(context.state.human_assisted_task_ids),
            "pause_reason": context.state.pause_reason,
            "tasks": task_reports,
            "notifications": _notifications(context.run_directory),
            "started_at": context.state.started_at,
            "updated_at": context.state.updated_at,
            "elapsed_seconds": _duration_seconds(
                context.state.started_at,
                context.state.updated_at,
            ),
            "model_turn_count": sum(
                int(task.get("model_turn_count", 0)) for task in task_reports
            ),
            "process_count": sum(
                int(task.get("process_count", 0)) for task in task_reports
            ),
        },
    )


def _verify_completed_gold_boundaries(context: _CampaignContext) -> None:
    frozen = context.state.gold_reveal_model_turn_count
    if frozen is None:
        if context.state.gold_evaluated_task_ids:
            raise ReplayCampaignStateError(
                "gold task evidence exists before the campaign-wide turn freeze"
            )
        return
    if _campaign_model_turn_count(context) != frozen:
        raise ReplayCampaignStateError(
            "campaign gained a model turn after gold reveal"
        )
    if (
        context.state.post_gold_model_turn_count is not None
        and context.state.post_gold_model_turn_count != frozen
    ):
        raise ReplayCampaignStateError(
            "campaign gold turn counters are not exactly equal"
        )
    by_id = {
        task.specification.task_id: task for task in context.prepared.tasks
    }
    for task_id in context.state.completed_task_ids:
        task = by_id.get(task_id)
        if task is None:
            raise ReplayCampaignStateError(
                "completed task has no frozen campaign authority"
            )
        reveal = _read_json(
            context.run_directory / "tasks" / task_id / "gold-reveal.json"
        )
        after = reveal.get("model_turn_count_after")
        if (
            not isinstance(after, int)
            or isinstance(after, bool)
            or reveal.get("model_turn_count_before") != frozen
            or after != frozen
        ):
            raise ReplayCampaignStateError(
                "a completed task gained a model turn after gold reveal"
            )
