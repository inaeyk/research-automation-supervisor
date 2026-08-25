"""Visible-only autonomous five-stage campaign controller."""

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
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol, cast

from pydantic import ValidationError

from research_automation_supervisor.candidate_export import (
    CANDIDATE_STAGING_NAME,
    TASK_INPUT_MANIFEST_NAME,
    capture_terminal_task_candidate_input,
    export_visible_candidate,
)
from research_automation_supervisor.codex_adapter import run_prepared_codex
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
from research_automation_supervisor.process_enforcement import (
    PROCESS_TERMINATION_EVIDENCE_FILENAME,
    ProcessEnforcementPolicyV1,
    load_process_termination_evidence,
)
from research_automation_supervisor.redaction import redact_json, redact_text
from research_automation_supervisor.replay_campaign_models import (
    PendingHumanDecision,
    ReplayCampaignState,
    SupervisorAction,
)
from research_automation_supervisor.replay_campaign_prompts import (
    build_supervisor_request,
    load_already_sent_authority_ledger,
)
from research_automation_supervisor.replay_campaign_sources import (
    PreparedReplayCampaign,
    PreparedReplayTask,
    load_human_replay_decision,
    load_replay_campaign_specification,
)
from research_automation_supervisor.shadow_engine import _ShadowLock, _write_bytes
from research_automation_supervisor.shadow_models import canonical_supervisor_uuid
from research_automation_supervisor.token_accounting import (
    CodexUsageBindingV1,
    load_verified_receipt,
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
    has_external_authority_locator,
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


class NotificationInvoker(Protocol):
    def __call__(self, payload: Mapping[str, str]) -> None: ...


@dataclass(frozen=True)
class ReplayCampaignServices:
    """Injectable visible-campaign process boundaries for tests."""

    codex_executable: str | None = None
    codex_identity_verifier: Callable[[str], None] | None = None
    supervisor_invoker: SupervisorInvoker | None = None
    workflow_services: WorkflowServices | None = None
    notification_invoker: NotificationInvoker | None = None
    environ: Mapping[str, str] | None = None
    process_enforcement_policy: ProcessEnforcementPolicyV1 | None = None
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


def run_replay_campaign(
    path: Path,
    *,
    runs_dir: Path = DEFAULT_REPLAY_RUNS_DIRECTORY,
    services: ReplayCampaignServices = DEFAULT_REPLAY_CAMPAIGN_SERVICES,
) -> ReplayCampaignState:
    """Create and synchronously drive one ordered visible-only campaign."""
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
    _prepare_campaign_runtime(prepared, run_directory)
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
        candidate_path=None,
        candidate_manifest_sha256=None,
        candidate_finalized_model_turn_count=None,
        status="initialized",
        pause_reason=None,
        journal_sequence=0,
        journal_hash=ZERO_HASH,
        started_at=now,
        updated_at=now,
    )
    _write_bytes(run_directory / JOURNAL_FILE, b"")
    _persist_state(run_directory, state)
    context = _CampaignContext(
        prepared=prepared,
        run_directory=run_directory,
        state=state,
        services=services,
        codex_executable=executable,
    )
    context.state = _event(
        context,
        "campaign_initialized",
        {"status": "running"},
        {str(prepared.specification_path): prepared.specification_sha256},
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
        _prepare_campaign_runtime(prepared, resolved)
        context = _CampaignContext(
            prepared=prepared,
            run_directory=resolved,
            state=state,
            services=services,
            codex_executable=executable,
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
    candidate_root = context.run_directory / "final-candidate"
    if (
        (
            candidate_root.exists()
            or (context.run_directory / CANDIDATE_STAGING_NAME).exists()
        )
        and context.state.current_task_index < len(context.prepared.tasks)
    ):
        raise ReplayCampaignStateError(
            "candidate finalization forbids any later model action"
        )
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
                direct_prompt_boundary = None
                with suppress(WorkflowInputError, WorkflowStateError):
                    semantic_boundary = prompt_source_pause_boundary(
                        Path(workflow.artifact_directory),
                        services=workflow_services,
                    )
                    direct_prompt_boundary = {
                        "initial_worker_prompt": "supervisor_worker_prompt",
                        "worker_repair_prompt": "supervisor_repair_prompt",
                        "auditor_prompt": "supervisor_auditor_prompt",
                    }.get(semantic_boundary)
                return _pause_campaign(
                    context,
                    "unsafe_workflow_state",
                    str(exc),
                    task,
                    direct_prompt_boundary
                    or context.state.paused_boundary
                    or "worker_continuation",
                )
            return _pause_campaign(
                context,
                category,
                workflow.pause_reason or workflow.status,
                task,
                boundary,
            )

        terminal_record = _write_model_terminal_record(context, task, workflow)
        task_report = _write_task_summary(context, task, workflow)
        candidate_input = capture_terminal_task_candidate_input(
            task,
            context.run_directory,
        )
        candidate_input_manifest = candidate_input / TASK_INPUT_MANIFEST_NAME
        model_terminal = (
            *context.state.model_terminal_task_ids,
            task.specification.task_id,
        )
        completed = (
            *context.state.completed_task_ids,
            task.specification.task_id,
        )
        context.state = _event(
            context,
            "visible_task_completed",
            {
                "model_terminal_task_ids": model_terminal,
                "completed_task_ids": completed,
                "current_task_index": context.state.current_task_index + 1,
                "current_task_run": None,
                "paused_boundary": None,
            },
            {
                str(terminal_record): sha256_regular_file(terminal_record),
                str(task_report): sha256_regular_file(task_report),
                str(candidate_input_manifest): sha256_regular_file(
                    candidate_input_manifest
                ),
            },
        )
        _write_report(context)

    expected_ids = tuple(
        task.specification.task_id for task in context.prepared.tasks
    )
    if (
        context.state.completed_task_ids != expected_ids
        or context.state.model_terminal_task_ids != expected_ids
    ):
        raise ReplayCampaignStateError(
            "candidate finalization requires every visible task to be terminal"
        )
    finalized_turns = _campaign_model_turn_count(context)
    candidate_path, candidate_sha256 = export_visible_candidate(
        context.prepared,
        context.run_directory,
        run_token=context.state.run_token,
        completed_task_ids=context.state.completed_task_ids,
        human_decision_count=context.state.human_decision_count,
        model_turn_count=finalized_turns,
    )
    if _campaign_model_turn_count(context) != finalized_turns:
        raise ReplayCampaignStateError(
            "a model action occurred during candidate finalization"
        )
    context.state = _event(
        context,
        "candidate_exported",
        {
            "status": "completed",
            "pause_reason": None,
            "candidate_path": str(candidate_path),
            "candidate_manifest_sha256": candidate_sha256,
            "candidate_finalized_model_turn_count": finalized_turns,
        },
        {
            str(candidate_path / "candidate-manifest.json"): (
                sha256_regular_file(
                    candidate_path / "candidate-manifest.json"
                )
            )
        },
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
    if (
        observed.status == "failed"
        and observed.pause_reason == "workflow_state_invariant_failed"
        and boundary == "worker_continuation"
    ):
        return resume_prompt_source_substage(
            stage2_run,
            services=services,
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
        matching_unlaunched_intent = _matching_unlaunched_supervisor_intent(
            context.run_directory,
            stage2_run,
            context.state.campaign_id,
            task_id,
            expected_source,
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
        or task_id in context.state.model_terminal_task_ids
        or task_id in context.state.completed_task_ids
        or (context.run_directory / "final-candidate").exists()
    ):
        raise ReplayCampaignStateError(
            "initial worker-prompt recovery has later terminal evidence"
        )


def _matching_unlaunched_supervisor_intent(
    run_directory: Path,
    stage2_run: Path,
    campaign_id: str,
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
            and (
                not (
                    run_directory / "supervisor" / "codex" / action_id
                ).exists()
                or _sealed_supervisor_prelaunch_failure(
                    run_directory,
                    action_id,
                    campaign_id,
                    task_id,
                )
            )
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
    artifact = run_directory / "supervisor" / "codex" / action_id
    expected_failure = (
        ("supervisor_transport_failure", "launch_failed")
        if artifact.exists()
        else ("supervisor_adapter_not_started", "not_started")
    )
    if (
        escalation.get("prompt_source_failure_category") != expected_failure[0]
        or escalation.get("prompt_source_adapter_status") != expected_failure[1]
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


def _sealed_supervisor_prelaunch_failure(
    run_directory: Path,
    action_id: str,
    campaign_id: str,
    task_id: str,
) -> bool:
    """Accept only a hash-sealed adapter failure that launched no process."""
    artifact = run_directory / "supervisor" / "codex" / action_id
    try:
        completion = _read_json(artifact / "stage2-completion.json")
        metadata = _read_json(artifact / "metadata.json")
        result = CodexRunResult.model_validate(_read_json(artifact / "result.json"))
        receipt_path = artifact / "usage-receipt.json"
        events_path = artifact / "events.jsonl"
        receipt = load_verified_receipt(receipt_path, event_log=events_path)
        termination = load_process_termination_evidence(
            artifact / PROCESS_TERMINATION_EVIDENCE_FILENAME
        )
        hashes = completion.get("artifact_hashes")
        if not isinstance(hashes, dict):
            return False
        for path in (
            artifact / "metadata.json",
            artifact / "result.json",
            receipt_path,
            events_path,
            artifact / PROCESS_TERMINATION_EVIDENCE_FILENAME,
        ):
            if hashes.get(str(path)) != sha256_regular_file(path):
                return False
    except (OSError, ValueError, ValidationError):
        return False
    return (
        completion.get("run_id") == action_id
        and completion.get("role") == "supervisor"
        and completion.get("result_status") == "launch_failed"
        and result.run_id == action_id
        and result.status == "launch_failed"
        and result.artifact_directory == str(artifact)
        and result.event_count == 0
        and metadata.get("run_id") == action_id
        and metadata.get("role") == "supervisor"
        and metadata.get("process_launched") is False
        and metadata.get("launch_error_present") is True
        and metadata.get("valid_event_count") == 0
        and receipt.campaign_id == campaign_id
        and receipt.task_id == task_id
        and receipt.action_id == action_id
        and receipt.role == "supervisor"
        and receipt.complete is False
        and receipt.completed_turn_count == 0
        and receipt.event_count == 0
        and termination.action_id == action_id
        and termination.phase == "termination_failed"
        and termination.invocation_id is None
        and termination.control_group is None
        and termination.process_identity is None
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
    """Reject recovery unless campaign evidence proves a pre-terminal state."""
    task_id = task.specification.task_id
    if (
        context.state.pending_human_decision is not None
        or task_id in context.state.model_terminal_task_ids
        or task_id in context.state.completed_task_ids
        or context.state.candidate_path is not None
        or context.state.candidate_manifest_sha256 is not None
        or (context.run_directory / "final-candidate").exists()
    ):
        raise ReplayCampaignStateError(
            "post-audit recovery is forbidden after model termination"
        )
    campaign_entries = _read_campaign_journal(context.run_directory)
    if any(
        entry.get("reason") == "candidate_exported"
        for entry in campaign_entries
    ):
        raise ReplayCampaignStateError(
            "campaign journal contains later terminal evidence"
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
        action_id = "supervisor-" + hashlib.sha256(key.encode()).hexdigest()[:16]
        authority_ledger = load_already_sent_authority_ledger(
            self.context.run_directory / "decisions",
            exclude_action_id=action_id,
        )
        rendered = build_supervisor_request(
            self.context.prepared,
            self.task,
            request,
            already_sent_authority=authority_ledger,
        )
        output_schema = rendered.output_schema
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
            "already_sent_authority_ledger": rendered.already_sent_authority_ledger,
            "repeated_material_block_count": rendered.repeated_material_block_count,
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
        _verify_supervisor_usage_receipt(result, prepared)
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
    task_id = context.prepared.tasks[
        context.state.current_task_index
    ].specification.task_id
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
        usage_binding=CodexUsageBindingV1(
            campaign_id=context.state.campaign_id,
            task_id=task_id,
            action_id=action_id,
            role="supervisor",
            repair_or_retry=context.state.supervisor_session_id is not None,
        ),
        usage_ledger_root=context.run_directory,
        usage_ledger_path=context.run_directory / "token-ledgers" / f"{task_id}.json",
    )


def _invoke_supervisor(
    context: _CampaignContext,
    request: PreparedCodexRequest,
    schema_path: Path,
    resume_id: str | None,
) -> CodexRunResult:
    _assert_model_actions_open(context)
    if context.services.codex_identity_verifier is not None:
        context.services.codex_identity_verifier(context.codex_executable)
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
        process_enforcement_policy=context.services.process_enforcement_policy,
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


def _verify_supervisor_usage_receipt(
    result: CodexRunResult,
    prepared: PreparedCodexRequest,
) -> None:
    """Bind recovery to the already sealed receipt instead of recounting events."""
    directory = Path(result.artifact_directory)
    events_path = directory / "events.jsonl"
    receipt_path = directory / "usage-receipt.json"
    metadata = _read_json(directory / "metadata.json")
    try:
        receipt = load_verified_receipt(receipt_path, event_log=events_path)
    except (OSError, ValueError) as exc:
        raise WorkflowPromptSourceError(
            "durable supervisor usage receipt is invalid",
            failure_category="durable_supervisor_usage_invalid",
        ) from exc
    binding = prepared.usage_binding
    if binding is None:
        raise WorkflowPromptSourceError(
            "supervisor usage binding is absent",
            failure_category="durable_supervisor_usage_invalid",
        )
    if (
        metadata.get("usage_receipt_path") != str(receipt_path)
        or metadata.get("usage_receipt_sha256") != sha256_regular_file(receipt_path)
        or metadata.get("usage_receipt_id") != receipt.receipt_id
        or metadata.get("usage_complete") != receipt.complete
        or receipt.campaign_id != binding.campaign_id
        or receipt.task_id != binding.task_id
        or receipt.action_id != binding.action_id
        or receipt.role != binding.role
        or receipt.model != prepared.request.model
    ):
        raise WorkflowPromptSourceError(
            "durable supervisor usage receipt contradicts its action",
            failure_category="durable_supervisor_usage_invalid",
        )


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
    if not _supervisor_prompt_uses_visible_authority(action.prompt):
        reasons.append("prompt_references_external_authority")
    expected_checks = tuple(
        test.specification.id for test in task.stage2.acceptance_tests
    )
    if len(action.required_checks) != len(expected_checks) or set(
        action.required_checks
    ) != set(expected_checks):
        reasons.append("acceptance_test_ids_mismatch")
    return tuple(reasons)


def _supervisor_prompt_uses_visible_authority(prompt: str) -> bool:
    normalized = prompt.replace("\\", "/")
    lowered = normalized.casefold()
    if any(
        marker in lowered
        for marker in (
            "../",
            "file://",
            "engine-only",
            "evaluation-config",
            "exact-reference",
            "gold",
            "hidden-evaluator",
            "hidden-test",
            "offline-evaluation",
        )
    ):
        return False
    return not has_external_authority_locator(normalized)


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
        codex_identity_verifier=context.services.codex_identity_verifier,
        environ=context.services.environ,
    )
    if (
        context.services.codex_identity_verifier is not None
        and base.codex_identity_verifier is None
    ):
        base = replace(
            base,
            codex_identity_verifier=context.services.codex_identity_verifier,
        )

    def guarded_codex_invoker(*args: Any, **kwargs: Any) -> CodexRunResult:
        _assert_model_actions_open(context)
        if context.services.process_enforcement_policy is not None:
            kwargs["process_enforcement_policy"] = (
                context.services.process_enforcement_policy
            )
            kwargs["containment_backend"] = base.containment_backend
        return base.codex_invoker(*args, **kwargs)

    token = _stage2_token(context, source.task)
    return replace(
        base,
        codex_invoker=guarded_codex_invoker,
        prompt_source=source,
        token_factory=lambda: token,
        require_canonical_thread_ids=True,
        usage_campaign_id=context.state.campaign_id,
        usage_task_id=source.task.specification.task_id,
        usage_ledger_root=context.run_directory,
        usage_ledger_path=(
            context.run_directory
            / "token-ledgers"
            / f"{source.task.specification.task_id}.json"
        ),
    )


def _assert_model_actions_open(context: _CampaignContext) -> None:
    if (
        context.state.candidate_path is not None
        or context.state.candidate_manifest_sha256 is not None
        or context.state.candidate_finalized_model_turn_count is not None
        or (context.run_directory / "final-candidate").exists()
        or (context.run_directory / CANDIDATE_STAGING_NAME).exists()
    ):
        raise ReplayCampaignStateError(
            "model actions are forbidden after candidate finalization"
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
        },
    )
    return path


def _write_task_summary(
    context: _CampaignContext,
    task: PreparedReplayTask,
    workflow: WorkflowResult,
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
    started_record = _read_json(
        context.run_directory
        / "tasks"
        / task.specification.task_id
        / "task-start.json"
    )
    started_at = cast(str, started_record["started_at"])
    ended_at = workflow.updated_at
    elapsed_seconds = _duration_seconds(started_at, ended_at)
    assisted = task.specification.task_id in context.state.human_assisted_task_ids
    verdict = "human_assisted" if assisted else "autonomous"
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
            "production_profile": task.specification.production_profile.model_dump(
                mode="json"
            ),
            "elapsed_seconds": elapsed_seconds,
            "started_at": started_at,
            "ended_at": ended_at,
            "process_count": process_count + supervisor_processes,
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
            "production_profile": task.specification.production_profile.model_dump(
                mode="json"
            ),
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
            f"- Resume: resume-visible-campaign {context.run_directory} "
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
        "instruction": "Inspect visible-campaign-status for this opaque run token.",
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


def _prepare_campaign_runtime(
    prepared: PreparedReplayCampaign,
    run_directory: Path,
) -> None:
    resolved_run = run_directory.resolve(strict=False)
    for root in _forbidden_roots(prepared, run_directory):
        if _path_contains(root, resolved_run) or _path_contains(resolved_run, root):
            raise ReplayCampaignInputError(
                "campaign run directory must be disjoint from task workspaces"
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


def _forbidden_roots(
    prepared: PreparedReplayCampaign,
    run_directory: Path,
) -> tuple[Path, ...]:
    roots = [task.stage2.repository_root for task in prepared.tasks]
    unique: list[Path] = []
    for root in roots:
        resolved = root.resolve(strict=False)
        if resolved not in unique:
            unique.append(resolved)
    if not unique:
        raise ReplayCampaignInputError("campaign has no task workspace roots")
    return tuple(unique)


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
        executable = _resolve_codex_executable(value)
        if services.codex_identity_verifier is not None:
            services.codex_identity_verifier(executable)
        return executable
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
        "candidate_path": None,
        "candidate_manifest_sha256": None,
        "candidate_finalized_model_turn_count": None,
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
    _verify_candidate_boundary(context)
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
            "paused_boundary": context.state.paused_boundary,
            "candidate_path": context.state.candidate_path,
            "candidate_manifest_sha256": (
                context.state.candidate_manifest_sha256
            ),
            "candidate_finalized_model_turn_count": (
                context.state.candidate_finalized_model_turn_count
            ),
            "offline_evaluation": {
                "status": "not_performed",
                "evaluation_package_status": "not_supplied",
                "commands": [],
            },
            "no_model_action_after_candidate_finalization": (
                context.state.candidate_finalized_model_turn_count is not None
                and _campaign_model_turn_count(context)
                == context.state.candidate_finalized_model_turn_count
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


def _verify_candidate_boundary(context: _CampaignContext) -> None:
    if (
        context.state.gold_evaluated_task_ids
        or context.state.gold_reveal_model_turn_count is not None
        or context.state.post_gold_model_turn_count is not None
    ):
        raise ReplayCampaignStateError(
            "visible campaign state contains forbidden legacy evaluation state"
        )
    candidate_values = (
        context.state.candidate_path,
        context.state.candidate_manifest_sha256,
        context.state.candidate_finalized_model_turn_count,
    )
    if all(value is None for value in candidate_values):
        return
    if any(value is None for value in candidate_values):
        raise ReplayCampaignStateError(
            "candidate finalization state is incomplete"
        )
    if (
        context.state.current_task_index != len(context.prepared.tasks)
        or tuple(context.state.completed_task_ids)
        != tuple(
            task.specification.task_id for task in context.prepared.tasks
        )
    ):
        raise ReplayCampaignStateError(
            "candidate was finalized before every visible task completed"
        )
    if (
        _campaign_model_turn_count(context)
        != context.state.candidate_finalized_model_turn_count
    ):
        raise ReplayCampaignStateError(
            "campaign gained a model action after candidate finalization"
        )
