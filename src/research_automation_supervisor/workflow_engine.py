"""Crash-aware deterministic single-substage Stage 2 workflow engine."""

from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import socket
import stat
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any, Literal, Protocol, cast

from pydantic import ValidationError

from research_automation_supervisor.codex_adapter import (
    DEFAULT_LIMITS,
    build_subprocess_environment,
    run_prepared_codex,
)
from research_automation_supervisor.codex_models import (
    MAX_PROMPT_BYTES,
    ROLE_POLICIES,
    CodexRunRequest,
    CodexRunResult,
    PreparedCodexRequest,
)
from research_automation_supervisor.durable_state import (
    append_hashed_journal_entry,
    atomic_write_json,
    commit_state_then_result,
    reconcile_model_snapshot,
)
from research_automation_supervisor.errors import (
    CodexAdapterError,
    WorkflowDependencyError,
    WorkflowInputError,
    WorkflowLockError,
    WorkflowPromptSourceError,
    WorkflowStateError,
)
from research_automation_supervisor.git_evidence import (
    GitBaseline,
    GitEvidence,
    collect_git_evidence,
    record_git_baseline,
)
from research_automation_supervisor.redaction import redact_text, would_redact_text
from research_automation_supervisor.shadow_models import canonical_supervisor_uuid
from research_automation_supervisor.test_runner import (
    TestAttemptResult,
    TestSuiteResult,
    run_test_attempt,
    skipped_test_result,
)
from research_automation_supervisor.workflow_integrity import (
    CodexActionRecord,
    JournalEntry,
    PromptHandoff,
    TestActionRecord,
    parse_action_record,
    parse_journal_entry,
    sha256_regular_file,
    verify_codex_action_record,
    verify_codex_artifacts,
    verify_hash_mapping,
    verify_test_action_record,
    verify_test_artifacts,
)
from research_automation_supervisor.workflow_models import (
    ACTIVE_STATUSES,
    PAUSED_STATUSES,
    TERMINAL_STATUSES,
    AuditorModelResult,
    HumanFile,
    PendingAction,
    PreparedSubstage,
    PreparedWorkflowTest,
    WorkerModelResult,
    WorkflowResult,
    WorkflowState,
    load_continuation_instruction,
    load_substage_specification,
)
from research_automation_supervisor.workflow_prompts import (
    AUDITOR_OUTPUT_SCHEMA,
    WORKER_OUTPUT_SCHEMA,
    RenderedWorkflowPrompt,
    build_audit_repair_prompt,
    build_auditor_prompt,
    build_fixed_test_repair_prompt,
    build_human_continuation_prompt,
    build_initial_worker_prompt,
    write_output_schemas,
)

ZERO_HASH = "0" * 64
STATE_FILE = "state.json"
RESULT_FILE = "result.json"
JOURNAL_FILE = "journal.jsonl"
LOCK_FILE = "workflow.lock"

JournalSemanticForm = tuple[str, str | None, str, str | None, bool, str]
_TRANSPORT_FAILURE_STATUSES = frozenset(
    {
        "launch_failed",
        "timed_out",
        "output_limit_exceeded",
        "permission_blocked",
        "malformed_event_stream",
        "process_failed",
        "missing_final_message",
    }
)


def _build_journal_semantic_forms() -> frozenset[JournalSemanticForm]:
    """Return every exact event/state/action/reason form Stage 2 may persist."""
    forms: set[JournalSemanticForm] = set()

    def transition(previous: str | None, new: str, *reasons: str) -> None:
        forms.update(
            ("transition", previous, new, None, False, reason)
            for reason in reasons
        )

    transition(None, "initialized", "workflow_initialized")
    transition("initialized", "worker_running", "initial_worker_requested")
    transition("initialized", "aborted", "human_abort")
    transition(
        "worker_running",
        "scope_checking",
        "worker_reported_completed",
    )
    transition("scope_checking", "tests_running", "scope_check_passed")
    transition("scope_checking", "repair_pending", "scope_check_failed")
    transition(
        "scope_checking",
        "repair_limit_paused",
        "scope_check_failed_repair_limit",
    )
    transition("tests_running", "auditor_running", "fixed_tests_passed")
    transition("tests_running", "repair_pending", "fixed_test_failed")
    transition(
        "tests_running",
        "repair_limit_paused",
        "fixed_test_failed_repair_limit",
    )
    transition("auditor_running", "completed", "auditor_passed")
    transition(
        "auditor_running",
        "checkpoint_paused",
        "auditor_passed_checkpoint",
    )
    transition(
        "auditor_running",
        "repair_pending",
        "auditor_repairable_failure",
    )
    transition(
        "auditor_running",
        "repair_limit_paused",
        "auditor_repairable_failure_repair_limit",
    )
    transition(
        "repair_pending",
        "worker_running",
        "automatic_repair_worker_resume",
    )
    transition(
        "human_paused",
        "worker_running",
        "human_continuation_requested",
        "prompt_source_human_resume",
    )
    transition(
        "human_paused",
        "auditor_running",
        "prompt_source_human_resume",
    )
    transition(
        "human_paused",
        "repair_pending",
        "prompt_source_human_resume",
    )
    transition(
        "repair_limit_paused",
        "worker_running",
        "human_continuation_requested",
    )
    transition("human_paused", "aborted", "human_abort")
    transition("repair_limit_paused", "aborted", "human_abort")

    recoverable_states = (
        "initialized",
        "worker_running",
        "scope_checking",
        "tests_running",
        "auditor_running",
        "repair_pending",
    )
    recovery_pause_reasons = (
        "frozen_or_repository_drift",
        "frozen_input_drift",
        "repository_identity_drift",
        "recovery_input_or_state_invalid",
    )
    for previous in recoverable_states:
        transition(previous, "human_paused", *recovery_pause_reasons)
        transition(previous, "failed", "workflow_state_invariant_failed")

    transition(
        "worker_running",
        "human_paused",
        "worker_adapter_input_or_dependency_failure",
        "worker_thread_id_missing_or_ambiguous",
        "worker_thread_id_changed_or_missing",
        "worker_structured_result_invalid",
        "worker_blocked",
        "worker_needs_human",
        "continuation_interrupted_before_launch",
        "uncertain_in_flight_action",
        *(f"worker_{status}" for status in _TRANSPORT_FAILURE_STATUSES),
    )
    transition(
        "scope_checking",
        "human_paused",
        "git_evidence_failure",
        "patch_evidence_incomplete",
    )
    transition(
        "tests_running",
        "human_paused",
        "uncertain_in_flight_action",
    )
    transition(
        "auditor_running",
        "human_paused",
        "auditor_adapter_input_or_dependency_failure",
        "auditor_thread_id_missing_or_noncanonical",
        "auditor_structured_result_invalid",
        "auditor_escalated",
        "auditor_pass_state_invariant_failed",
        "patch_evidence_missing",
        "patch_evidence_incomplete",
        "uncertain_in_flight_action",
        *(f"auditor_{status}" for status in _TRANSPORT_FAILURE_STATUSES),
    )
    transition(
        "repair_pending",
        "human_paused",
        "missing_worker_thread_id",
    )
    for previous in (
        "worker_running",
        "auditor_running",
        "repair_pending",
    ):
        transition(
            previous,
            "human_paused",
            "prompt_source_human_pause",
            "prompt_source_invalid",
        )

    evidence_reasons = {
        "worker_running": (
            "worker_result_validated",
            "worker_pause_evidence_saved",
        ),
        "scope_checking": ("git_scope_evidence_collected",),
        "tests_running": ("fixed_tests_finalized",),
        "auditor_running": ("auditor_result_validated",),
        "human_paused": ("escalation_package_written",),
        "repair_limit_paused": ("escalation_package_written",),
    }
    for workflow_status, reasons in evidence_reasons.items():
        forms.update(
            (
                "evidence",
                workflow_status,
                workflow_status,
                None,
                False,
                reason,
            )
            for reason in reasons
        )

    action_states = {
        "worker": "worker_running",
        "auditor": "auditor_running",
        "test": "tests_running",
    }
    for action_kind, workflow_status in action_states.items():
        forms.add(
            (
                "action_intent",
                workflow_status,
                workflow_status,
                action_kind,
                True,
                f"{action_kind}_action_intent",
            )
        )
        forms.add(
            (
                "action_completion",
                workflow_status,
                workflow_status,
                action_kind,
                True,
                f"{action_kind}_action_completed",
            )
        )
    return frozenset(forms)


JOURNAL_SEMANTIC_FORMS = _build_journal_semantic_forms()
_EVIDENCE_UPDATE_FIELDS = {
    "worker_result_validated": frozenset(
        {
            "worker_thread_id",
            "latest_worker_action_id",
            "latest_worker_result_path",
            "continuation_path",
            "continuation_sha256",
            "summary",
        }
    ),
    "worker_pause_evidence_saved": frozenset(
        {"latest_git_evidence_path", "scope_compliant"}
    ),
    "git_scope_evidence_collected": frozenset(
        {"latest_git_evidence_path", "scope_compliant"}
    ),
    "fixed_tests_finalized": frozenset({"latest_tests_path", "tests_passed"}),
    "auditor_result_validated": frozenset(
        {
            "latest_audit_action_id",
            "latest_audit_result_path",
            "prior_audit_result_paths",
            "contract_satisfied",
            "summary",
        }
    ),
    "escalation_package_written": frozenset(),
}


class CodexInvoker(Protocol):
    def __call__(
        self,
        prepared: PreparedCodexRequest,
        *,
        runs_dir: Path,
        codex_executable: str,
        environ: Mapping[str, str] | None,
        output_schema: Path,
        resume_thread_id: str | None,
        confidential_fragments: Sequence[str],
    ) -> CodexRunResult: ...


class TestInvoker(Protocol):
    def __call__(
        self,
        prepared_test: PreparedWorkflowTest,
        artifact_directory: Path,
        action_id: str,
        *,
        environ: Mapping[str, str] | None,
    ) -> TestAttemptResult: ...


PromptBoundary = Literal[
    "worker_prompt",
    "auditor_prompt",
    "repair_prompt",
    "finish",
    "human_pause",
]


@dataclass(frozen=True)
class WorkflowPromptRequest:
    """One optional Stage 2 prompt/action boundary exposed to a controller."""

    action: PromptBoundary
    default_prompt: RenderedWorkflowPrompt | None
    run_directory: Path
    workspace: Path
    substage_id: str
    title: str
    repair_round: int
    repair_trigger: str | None
    worker_thread_id: str | None
    latest_worker_result_path: Path | None
    latest_audit_result_path: Path | None
    latest_git_evidence_path: Path | None
    latest_tests_path: Path | None


@dataclass(frozen=True)
class WorkflowPromptDecision:
    """Supervisor advisory bytes or terminal action returned at a boundary."""

    action: PromptBoundary
    prompt: bytes | None
    summary: str
    human_note: bytes | None = None


class WorkflowPromptSource(Protocol):
    """Optional replay-only prompt/action source; Stage 2 remains authoritative."""

    def __call__(self, request: WorkflowPromptRequest) -> WorkflowPromptDecision: ...


@dataclass(frozen=True)
class WorkflowServices:
    """Injectable process and identity boundaries used by offline tests."""

    codex_executable: str | None = None
    codex_invoker: CodexInvoker = run_prepared_codex
    test_invoker: TestInvoker = run_test_attempt
    environ: Mapping[str, str] | None = None
    token_factory: Callable[[], str] = lambda: secrets.token_hex(16)
    utc_now: Callable[[], datetime] = lambda: datetime.now(UTC)
    prompt_source: WorkflowPromptSource | None = None
    require_canonical_thread_ids: bool = False


DEFAULT_WORKFLOW_SERVICES = WorkflowServices()


@dataclass
class _WorkflowContext:
    prepared: PreparedSubstage
    baseline: GitBaseline
    run_directory: Path
    state: WorkflowState
    worker_schema: Path
    auditor_schema: Path
    codex_executable: str
    services: WorkflowServices
    continuation: HumanFile | None = None
    continuation_from_state: str | None = None


def validate_substage(
    path: Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> PreparedSubstage:
    """Validate every frozen input and the clean Git baseline without writes."""
    _, _, sensitive_values = build_subprocess_environment(environ)
    return load_substage_specification(path, sensitive_values=sensitive_values)


def run_substage(
    path: Path,
    *,
    runs_dir: Path = Path("runs/workflows"),
    services: WorkflowServices = DEFAULT_WORKFLOW_SERVICES,
) -> WorkflowResult:
    """Create one exclusive run and synchronously drive it to a pause or terminal state."""
    _, _, sensitive_values = build_subprocess_environment(services.environ)
    prepared = load_substage_specification(path, sensitive_values=sensitive_values)
    baseline = record_git_baseline(prepared.workspace, environ=services.environ)
    if not baseline.clean:
        raise WorkflowInputError("workspace must be clean, including untracked files")
    executable = _resolve_codex_executable(services.codex_executable)
    token = services.token_factory()
    if not token or len(token) > 80 or not token.replace("-", "").replace("_", "").isalnum():
        raise WorkflowInputError("workflow run token is invalid")
    try:
        resolved_runs_dir = runs_dir.resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise WorkflowInputError("workflow runs directory could not be resolved") from exc
    run_directory = resolved_runs_dir / f"{prepared.specification.substage_id}-{token}"
    structural = (str(runs_dir), str(resolved_runs_dir), str(run_directory), token)
    if any(would_redact_text(value, sensitive_values) for value in structural):
        raise WorkflowInputError(
            "prospective workflow run path has a structural redaction collision"
        )
    try:
        resolved_runs_dir.mkdir(parents=True, exist_ok=True)
        run_directory.mkdir(exist_ok=False)
    except FileExistsError as exc:
        raise WorkflowInputError("exclusive workflow run directory already exists") from exc
    except OSError as exc:
        raise WorkflowInputError("workflow run directory could not be created") from exc

    try:
        _initialize_run_artifacts(run_directory, prepared, baseline)
        worker_schema, auditor_schema = write_output_schemas(run_directory / "handoffs")
        now = _utc_string(services.utc_now())
        state = WorkflowState(
            substage_id=prepared.specification.substage_id,
            run_token=token,
            status="initialized",
            repair_round=0,
            max_repair_rounds=prepared.specification.max_repair_rounds,
            checkpoint_after=prepared.specification.checkpoint_after,
            specification_path=str(prepared.specification_path),
            specification_sha256=prepared.specification_sha256,
            contract_sha256=prepared.contract.sha256,
            prompts_sha256={
                "worker_initial": prepared.worker_initial_prompt.sha256,
                "worker_repair": prepared.worker_repair_prompt.sha256,
                "auditor": prepared.auditor_prompt.sha256,
            },
            workspace=str(prepared.workspace),
            repository_root=str(prepared.repository_root),
            baseline_commit=baseline.head,
            baseline_branch=baseline.branch,
            worker_thread_id=None,
            latest_worker_action_id=None,
            latest_audit_action_id=None,
            latest_worker_result_path=None,
            latest_audit_result_path=None,
            latest_git_evidence_path=None,
            latest_tests_path=None,
            prior_audit_result_paths=(),
            completed_action_ids=(),
            pending_action=None,
            repair_trigger=None,
            continuation_path=None,
            continuation_sha256=None,
            tests_passed=False,
            scope_compliant=False,
            contract_satisfied=False,
            pause_reason=None,
            summary="Workflow initialized.",
            artifact_directory=str(run_directory),
            journal_sequence=0,
            journal_hash=ZERO_HASH,
            started_at=now,
            updated_at=now,
        )
        _persist_state(run_directory, state)
        state = _journal_event(
            run_directory,
            state,
            event_type="transition",
            previous_state=None,
            new_state="initialized",
            action_id=None,
            action_kind=None,
            reason="workflow_initialized",
            artifact_hashes=_frozen_artifact_hashes(run_directory),
            updates={},
            utc_now=services.utc_now,
        )
        with _WorkflowLock(run_directory, services.utc_now):
            context = _WorkflowContext(
                prepared=prepared,
                baseline=baseline,
                run_directory=run_directory,
                state=state,
                worker_schema=worker_schema,
                auditor_schema=auditor_schema,
                codex_executable=executable,
                services=services,
            )
            context.state = _transition(
                context,
                "worker_running",
                "initial_worker_requested",
                summary="Initial worker turn requested.",
            )
            return _drive(context)
    except BaseException:
        # The exclusive directory is intentional durable evidence even when initialization fails.
        raise


def resume_substage(
    run_directory: Path,
    *,
    services: WorkflowServices = DEFAULT_WORKFLOW_SERVICES,
) -> WorkflowResult:
    """Resume a nonterminal interrupted run without repeating completed actions."""
    resolved = _resolve_run_directory(run_directory)
    with _WorkflowLock(resolved, services.utc_now):
        state = _load_state(resolved)
        state = _reconcile_state_with_journal(resolved, state)
        _validate_normalized_action_intents(resolved, state)
        if state.status in TERMINAL_STATUSES or state.status in PAUSED_STATUSES:
            raise WorkflowInputError("workflow state cannot be resumed automatically")
        if not _raw_frozen_inputs_match(resolved, state):
            return _pause_state_only(
                resolved,
                state,
                services,
                "frozen_input_drift",
                "Frozen specification, contract, or prompt hashes changed.",
            ).to_result()
        if not _raw_repository_matches(state, services):
            return _pause_state_only(
                resolved,
                state,
                services,
                "repository_identity_drift",
                "Workspace repository identity or baseline changed.",
            ).to_result()
        try:
            context = _load_context(resolved, state, services)
        except (WorkflowInputError, WorkflowStateError):
            return _pause_state_only(
                resolved,
                state,
                services,
                "recovery_input_or_state_invalid",
                "Frozen recovery inputs could not be validated safely.",
            ).to_result()
        if not _frozen_inputs_match(context):
            context.state = _pause(
                context,
                "human_paused",
                "frozen_input_drift",
                "Frozen specification, contract, or prompt hashes changed.",
            )
            return context.state.to_result()
        if not _repository_matches(context):
            context.state = _pause(
                context,
                "human_paused",
                "repository_identity_drift",
                "Workspace repository identity or baseline changed.",
            )
            return context.state.to_result()
        if context.state.continuation_path is not None and context.state.pending_action is None:
            _, _, sensitive_values = build_subprocess_environment(
                services.environ
            )
            instruction = load_continuation_instruction(
                Path(context.state.continuation_path),
                sensitive_values=sensitive_values,
                workspace=context.prepared.workspace,
                protected_paths=context.prepared.specification.protected_paths,
            )
            if instruction.sha256 != context.state.continuation_sha256:
                raise WorkflowStateError(
                    "accepted continuation bytes changed before action launch"
                )
            continuation_entry = next(
                (
                    entry
                    for entry in reversed(_read_valid_journal(resolved))
                    if entry.reason == "human_continuation_requested"
                ),
                None,
            )
            if continuation_entry is None:
                raise WorkflowStateError(
                    "accepted continuation boundary is unavailable"
                )
            context.continuation = instruction
            context.continuation_from_state = continuation_entry.previous_state
        context.state = _recover_pending_action(context)
        if context.state.status == "human_paused":
            return context.state.to_result()
        return _drive(context)


def continue_substage(
    run_directory: Path,
    instruction_path: Path,
    *,
    services: WorkflowServices = DEFAULT_WORKFLOW_SERVICES,
) -> WorkflowResult:
    """Resume the exact worker thread once with an exact human-written instruction."""
    resolved = _resolve_run_directory(run_directory)
    with _WorkflowLock(resolved, services.utc_now):
        state = _load_state(resolved)
        state = _reconcile_state_with_journal(resolved, state)
        _validate_normalized_action_intents(resolved, state)
        if state.status not in PAUSED_STATUSES:
            raise WorkflowInputError(
                "human continuation is allowed only from a human or limit pause"
            )
        context = _load_context(resolved, state, services)
        if not _frozen_inputs_match(context) or not _repository_matches(context):
            raise WorkflowInputError(
                "frozen inputs and repository identity must match before continue"
            )
        if context.state.worker_thread_id is None:
            raise WorkflowInputError(
                "human continuation requires the stored explicit worker thread ID"
            )
        if context.state.pending_action is not None:
            raise WorkflowInputError(
                "an uncertain in-flight action must be resolved before continue"
            )
        _, _, sensitive_values = build_subprocess_environment(services.environ)
        instruction = load_continuation_instruction(
            instruction_path,
            sensitive_values=sensitive_values,
            workspace=context.prepared.workspace,
            protected_paths=context.prepared.specification.protected_paths,
        )
        next_round = context.state.repair_round + 1
        context.continuation = instruction
        context.continuation_from_state = context.state.status
        context.state = _transition(
            context,
            "worker_running",
            "human_continuation_requested",
            repair_round=next_round,
            repair_trigger="human",
            continuation_path=str(instruction.path),
            continuation_sha256=instruction.sha256,
            pause_reason=None,
            summary="Exact human continuation queued for the persistent worker.",
        )
        return _drive(context)


def resume_prompt_source_substage(
    run_directory: Path,
    *,
    services: WorkflowServices,
) -> WorkflowResult:
    """Re-enter the exact replay prompt boundary after an immutable human decision."""
    if services.prompt_source is None:
        raise WorkflowInputError("prompt-source resume requires an explicit prompt source")
    resolved = _resolve_run_directory(run_directory)
    with _WorkflowLock(resolved, services.utc_now):
        state = _load_state(resolved)
        state = _reconcile_state_with_journal(resolved, state)
        _validate_normalized_action_intents(resolved, state)
        if state.status != "human_paused" or state.pause_reason not in {
            "prompt_source_human_pause",
            "prompt_source_invalid",
        }:
            raise WorkflowInputError(
                "prompt-source resume is allowed only from a replay prompt-source pause"
            )
        entries = _read_valid_journal(resolved)
        pause_entry = next(
            (
                entry
                for entry in reversed(entries)
                if entry.event_type == "transition"
                and entry.new_state == "human_paused"
                and entry.reason
                in {"prompt_source_human_pause", "prompt_source_invalid"}
            ),
            None,
        )
        if pause_entry is None or pause_entry.previous_state not in {
            "worker_running",
            "auditor_running",
            "repair_pending",
        }:
            raise WorkflowStateError("prompt-source pause origin is unavailable")
        context = _load_context(resolved, state, services)
        if not _frozen_inputs_match(context) or not _repository_matches(context):
            raise WorkflowInputError(
                "frozen inputs and repository identity must match before replay resume"
            )
        context.state = _transition(
            context,
            cast(Any, pause_entry.previous_state),
            "prompt_source_human_resume",
            pause_reason=None,
            summary="Human decision resumed the exact replay prompt boundary.",
        )
        return _drive(context)


def substage_status(run_directory: Path) -> WorkflowResult:
    """Read and integrity-check durable state without writes or launches."""
    resolved = _resolve_run_directory(run_directory)
    state = _load_state(resolved)
    _validate_journal(resolved, state)
    _validate_normalized_action_intents(resolved, state)
    if not _raw_frozen_inputs_match(resolved, state):
        raise WorkflowStateError("frozen workflow inputs no longer match durable state")
    result = _load_result(resolved)
    if result != state.to_result():
        raise WorkflowStateError("workflow state and result snapshots disagree")
    return result


def read_stage2_source_for_shadow(
    run_directory: Path,
) -> tuple[WorkflowResult, WorkflowState, tuple[JournalEntry, ...]]:
    """Strict Stage 2 read allowing only an absent continuation source file.

    This is a Stage 3-only trust path. Public Stage 2 status remains strict.
    The journal still anchors the exact continuation locator and expected hash;
    an existing file must match, and every other durable artifact remains
    mandatory.
    """
    resolved = _resolve_run_directory(run_directory)
    state = _load_state(resolved)
    entries = tuple(
        _read_valid_journal(
            resolved,
            allow_missing_continuation_source=True,
        )
    )
    if (
        not entries
        or len(entries) != state.journal_sequence
        or entries[-1].entry_hash != state.journal_hash
        or entries[-1].timestamp != state.updated_at
    ):
        raise WorkflowStateError(
            "workflow state does not agree with the journal head"
        )
    _validate_journal_semantics(resolved, entries, state)
    _validate_normalized_action_intents(
        resolved,
        state,
        entries=entries,
    )
    if not _raw_frozen_inputs_match(resolved, state):
        raise WorkflowStateError(
            "frozen workflow inputs no longer match durable state"
        )
    result = _load_result(resolved)
    if result != state.to_result():
        raise WorkflowStateError(
            "workflow state and result snapshots disagree"
        )
    return result, state, entries


def abort_substage(
    run_directory: Path,
    reason: str,
    *,
    services: WorkflowServices = DEFAULT_WORKFLOW_SERVICES,
) -> WorkflowResult:
    """Atomically abort a paused or initialized workflow; active termination is not implemented."""
    resolved = _resolve_run_directory(run_directory)
    with _WorkflowLock(resolved, services.utc_now):
        state = _load_state(resolved)
        state = _reconcile_state_with_journal(resolved, state)
        _validate_normalized_action_intents(resolved, state)
        if not _raw_frozen_inputs_match(resolved, state):
            raise WorkflowInputError("frozen workflow inputs changed before abort")
        if not _raw_repository_matches(state, services):
            raise WorkflowInputError("repository identity changed before abort")
        if state.status in TERMINAL_STATUSES:
            raise WorkflowInputError("terminal workflows cannot be aborted")
        if state.status in ACTIVE_STATUSES:
            raise WorkflowInputError("active-process termination is not available in Stage 2")
        _, _, sensitive_values = build_subprocess_environment(services.environ)
        sanitized = redact_text(" ".join(reason.split()), sensitive_values).strip()
        if not sanitized:
            raise WorkflowInputError("abort reason must not be empty")
        state = _journal_event(
            resolved,
            state,
            event_type="transition",
            previous_state=state.status,
            new_state="aborted",
            action_id=None,
            action_kind=None,
            reason="human_abort",
            artifact_hashes={},
            updates={
                "status": "aborted",
                "pause_reason": sanitized[:16384],
                "summary": "Workflow aborted by human request.",
            },
            utc_now=services.utc_now,
        )
        return state.to_result()


def workflow_exit_code(status: str) -> int:
    """Map durable workflow states to the frozen Stage 2 CLI exit contract."""
    return {
        "completed": 0,
        "human_paused": 5,
        "repair_limit_paused": 6,
        "checkpoint_paused": 7,
        "failed": 4,
        "aborted": 8,
        "initialized": 4,
        "worker_running": 4,
        "scope_checking": 4,
        "tests_running": 4,
        "auditor_running": 4,
        "repair_pending": 4,
    }[status]


def _drive(context: _WorkflowContext) -> WorkflowResult:
    try:
        return _drive_unchecked(context)
    except WorkflowStateError:
        # Persist `failed` only when the existing journal is still trustworthy enough to do so.
        _validate_journal(context.run_directory, context.state)
        context.state = _journal_event(
            context.run_directory,
            context.state,
            event_type="transition",
            previous_state=context.state.status,
            new_state="failed",
            action_id=None,
            action_kind=None,
            reason="workflow_state_invariant_failed",
            artifact_hashes={},
            updates={
                "status": "failed",
                "pause_reason": "workflow_state_invariant_failed",
                "summary": "An unrecoverable local workflow invariant failed.",
            },
            utc_now=context.services.utc_now,
        )
        return context.state.to_result()


def _drive_unchecked(context: _WorkflowContext) -> WorkflowResult:
    while True:
        status = context.state.status
        if status in TERMINAL_STATUSES or status in PAUSED_STATUSES:
            return context.state.to_result()
        if not _frozen_inputs_match(context) or not _repository_matches(context):
            context.state = _pause(
                context,
                "human_paused",
                "frozen_or_repository_drift",
                "Frozen inputs or repository identity changed during the workflow.",
            )
            continue
        if status == "worker_running":
            _handle_worker(context)
        elif status == "scope_checking":
            _handle_scope(context)
        elif status == "tests_running":
            _handle_tests(context)
        elif status == "auditor_running":
            _handle_auditor(context)
        elif status == "repair_pending":
            if context.state.worker_thread_id is None:
                context.state = _pause(
                    context,
                    "human_paused",
                    "missing_worker_thread_id",
                    "The persistent worker thread ID is unavailable.",
                )
            else:
                context.state = _transition(
                    context,
                    "worker_running",
                    "automatic_repair_worker_resume",
                    repair_round=context.state.repair_round + 1,
                    summary="Repair turn queued on the exact persistent worker thread.",
                )
        elif status == "initialized":
            context.state = _transition(
                context,
                "worker_running",
                "initial_worker_requested",
                summary="Initial worker turn requested.",
            )
        else:
            raise WorkflowStateError("workflow reached an unsupported state")


def _handle_worker(context: _WorkflowContext) -> None:
    action_id = f"worker-r{context.state.repair_round:03d}"
    record = _read_action_record(context.run_directory, action_id)
    if record is None:
        default_prompt = _worker_prompt(context)
        boundary: Literal["worker_prompt", "repair_prompt"] = (
            "worker_prompt"
            if context.state.repair_round == 0
            else "repair_prompt"
        )
        prompt = _source_prompt(context, boundary, default_prompt)
        if prompt is None:
            return
        role: Literal["worker"] = "worker"
        _persist_prompt_evidence(context, action_id, role, prompt)
        handoff_path = context.run_directory / "handoffs" / f"{action_id}.json"
        _write_json(handoff_path, prompt.manifest())
        request = _prepared_codex_request(context, action_id, role, prompt)
        stage1_parent = context.run_directory / "worker" / "codex"
        expected_artifact = stage1_parent / action_id
        context.state = _codex_action_intent(
            context,
            request,
            "worker",
            expected_artifact,
            handoff_path,
            context.worker_schema,
        )
        try:
            result = context.services.codex_invoker(
                request,
                runs_dir=stage1_parent,
                codex_executable=context.codex_executable,
                environ=context.services.environ,
                output_schema=context.worker_schema,
                resume_thread_id=(
                    context.state.worker_thread_id
                    if context.state.repair_round > 0
                    else None
                ),
                confidential_fragments=_confidential_fragments(context, prompt),
            )
            _validate_runtime_durability(context)
        except CodexAdapterError:
            context.state = _pause(
                context,
                "human_paused",
                "worker_adapter_input_or_dependency_failure",
                "The worker adapter could not complete safely.",
            )
            return
        record = _finalize_codex_action(context, result)
        context.state = _action_completion(context, action_id, record)
    else:
        record = _verified_existing_action_record(context, action_id, "worker")
    _consume_worker_record(context, action_id, record)


def _consume_worker_record(
    context: _WorkflowContext,
    action_id: str,
    record: dict[str, Any],
) -> None:
    adapter_result = _adapter_result_from_record(record)
    if adapter_result.status != "succeeded":
        context.state = _pause(
            context,
            "human_paused",
            f"worker_{adapter_result.status}",
            "Worker transport failed and will not be retried automatically.",
        )
        return
    thread_ids = _record_thread_ids(record)
    worker_thread_id: str | None
    if context.state.repair_round == 0:
        if len(thread_ids) != 1 or not _valid_thread_id(
            thread_ids[0],
            canonical=context.services.require_canonical_thread_ids,
        ):
            context.state = _pause(
                context,
                "human_paused",
                "worker_thread_id_missing_or_ambiguous",
                "Initial worker turn did not expose one unambiguous thread.started ID.",
            )
            return
        worker_thread_id = thread_ids[0]
    else:
        worker_thread_id = context.state.worker_thread_id
        if (
            worker_thread_id is None
            or len(thread_ids) != 1
            or not _valid_thread_id(
                thread_ids[0],
                canonical=context.services.require_canonical_thread_ids,
            )
            or thread_ids[0] != worker_thread_id
        ):
            context.state = _pause(
                context,
                "human_paused",
                "worker_thread_id_changed_or_missing",
                "Resumed worker turn did not report the exact stored thread ID.",
            )
            return
    structured_path = record.get("structured_result_path")
    if not isinstance(structured_path, str):
        context.state = _pause(
            context,
            "human_paused",
            "worker_structured_result_invalid",
            "Worker structured result is missing or invalid.",
        )
        return
    try:
        worker = WorkerModelResult.model_validate(_read_json(Path(structured_path)))
    except (OSError, ValidationError, WorkflowStateError):
        context.state = _pause(
            context,
            "human_paused",
            "worker_structured_result_invalid",
            "Worker structured result is missing or invalid.",
        )
        return
    updates: dict[str, object] = {
        "worker_thread_id": worker_thread_id,
        "latest_worker_action_id": action_id,
        "latest_worker_result_path": structured_path,
        "continuation_path": None,
        "continuation_sha256": None,
        "summary": worker.summary or "Worker turn completed.",
    }
    context.state = _update_state(context, "worker_result_validated", updates)
    if worker.status != "completed":
        try:
            evidence = _collect_round_git_evidence(context)
            context.state = _update_state(
                context,
                "worker_pause_evidence_saved",
                {
                    "latest_git_evidence_path": str(
                        Path(evidence.patch_artifact).parent / "evidence.json"
                    ),
                    "scope_compliant": evidence.scope_compliant,
                },
            )
        except (WorkflowInputError, WorkflowDependencyError):
            pass
        context.state = _pause(
            context,
            "human_paused",
            f"worker_{worker.status}",
            "Worker explicitly requested human attention.",
        )
        return
    context.state = _transition(
        context,
        "scope_checking",
        "worker_reported_completed",
        **updates,
    )


def _handle_scope(context: _WorkflowContext) -> None:
    try:
        evidence = _collect_round_git_evidence(context)
    except (WorkflowInputError, WorkflowDependencyError):
        context.state = _pause(
            context,
            "human_paused",
            "git_evidence_failure",
            "Git evidence could not be collected safely.",
        )
        return
    evidence_path = Path(evidence.patch_artifact).parent / "evidence.json"
    context.state = _update_state(
        context,
        "git_scope_evidence_collected",
        {
            "latest_git_evidence_path": str(evidence_path),
            "scope_compliant": evidence.scope_compliant,
        },
    )
    if not evidence.patch_complete:
        context.state = _pause(
            context,
            "human_paused",
            "patch_evidence_incomplete",
            "Complete patch evidence exceeds the 25 MiB limit; audit was not launched.",
        )
    elif not evidence.scope_compliant:
        _queue_or_limit_repair(context, "scope", "scope_check_failed")
    else:
        context.state = _transition(
            context,
            "tests_running",
            "scope_check_passed",
            scope_compliant=True,
            tests_passed=False,
            summary="Git scope checks passed; fixed tests are running.",
        )


def _handle_tests(context: _WorkflowContext) -> None:
    results: list[TestAttemptResult] = []
    failed = False
    first_failure_action_id: str | None = None
    for index, prepared_test in enumerate(context.prepared.acceptance_tests):
        action_id = _test_action_id(context.state.repair_round, index, prepared_test)
        destination = (
            context.run_directory
            / "tests"
            / f"round-{context.state.repair_round:03d}"
            / action_id
        )
        record = _read_action_record(context.run_directory, action_id)
        if failed:
            if record is None:
                context.state = _test_action_intent(
                    context,
                    action_id,
                    prepared_test,
                    destination,
                    skipped_after_action_id=first_failure_action_id,
                )
                result = skipped_test_result(prepared_test, destination, action_id)
                record = _finalize_test_action(context, result)
                context.state = _action_completion(context, action_id, record)
            else:
                record = _verified_existing_action_record(context, action_id, "test")
            skipped = _test_result_from_record(record)
            if skipped.status != "skipped":
                raise WorkflowStateError("test after the first failure was not skipped")
            results.append(skipped)
            continue
        if record is None:
            context.state = _test_action_intent(
                context,
                action_id,
                prepared_test,
                destination,
                skipped_after_action_id=None,
            )
            result = context.services.test_invoker(
                prepared_test,
                destination,
                action_id,
                environ=context.services.environ,
            )
            _validate_runtime_durability(context)
            record = _finalize_test_action(context, result)
            context.state = _action_completion(context, action_id, record)
        else:
            record = _verified_existing_action_record(context, action_id, "test")
        result = _test_result_from_record(record)
        results.append(result)
        failed = not result.passed
        if failed:
            first_failure_action_id = action_id
    suite = TestSuiteResult(passed=not failed, results=tuple(results))
    suite_path = (
        context.run_directory
        / "tests"
        / f"round-{context.state.repair_round:03d}"
        / "suite.json"
    )
    _write_json(suite_path, suite.to_dict())
    context.state = _update_state(
        context,
        "fixed_tests_finalized",
        {"latest_tests_path": str(suite_path), "tests_passed": suite.passed},
    )
    if not suite.passed:
        _queue_or_limit_repair(context, "test", "fixed_test_failed")
    else:
        context.state = _transition(
            context,
            "auditor_running",
            "fixed_tests_passed",
            tests_passed=True,
            summary="All fixed acceptance tests passed; fresh audit requested.",
        )


def _handle_auditor(context: _WorkflowContext) -> None:
    action_id = f"auditor-r{context.state.repair_round:03d}"
    record = _read_action_record(context.run_directory, action_id)
    if record is None:
        worker = _latest_worker(context)
        git_evidence = _latest_git(context)
        tests = _latest_tests(context).results
        prior = _prior_audits(context)
        try:
            patch_bytes = Path(git_evidence.patch_artifact).read_bytes()
        except OSError:
            context.state = _pause(
                context,
                "human_paused",
                "patch_evidence_missing",
                "Complete patch evidence is unavailable; audit was not launched.",
            )
            return
        if not git_evidence.patch_complete:
            context.state = _pause(
                context,
                "human_paused",
                "patch_evidence_incomplete",
                "Incomplete patch evidence cannot be sent to an auditor.",
            )
            return
        default_prompt = build_auditor_prompt(
            context.prepared,
            context.baseline,
            git_evidence,
            patch_bytes,
            worker,
            tests,
            prior,
        )
        prompt = _source_prompt(context, "auditor_prompt", default_prompt)
        if prompt is None:
            return
        _persist_prompt_evidence(context, action_id, "auditor", prompt)
        handoff_path = context.run_directory / "handoffs" / f"{action_id}.json"
        _write_json(handoff_path, prompt.manifest())
        request = _prepared_codex_request(context, action_id, "auditor", prompt)
        stage1_parent = context.run_directory / "audits" / "codex"
        expected_artifact = stage1_parent / action_id
        context.state = _codex_action_intent(
            context,
            request,
            "auditor",
            expected_artifact,
            handoff_path,
            context.auditor_schema,
        )
        try:
            result = context.services.codex_invoker(
                request,
                runs_dir=stage1_parent,
                codex_executable=context.codex_executable,
                environ=context.services.environ,
                output_schema=context.auditor_schema,
                resume_thread_id=None,
                confidential_fragments=_confidential_fragments(context, prompt),
            )
            _validate_runtime_durability(context)
        except CodexAdapterError:
            context.state = _pause(
                context,
                "human_paused",
                "auditor_adapter_input_or_dependency_failure",
                "The auditor adapter could not complete safely.",
            )
            return
        record = _finalize_codex_action(context, result)
        context.state = _action_completion(context, action_id, record)
    else:
        record = _verified_existing_action_record(context, action_id, "auditor")
    _consume_auditor_record(context, action_id, record)


def _consume_auditor_record(
    context: _WorkflowContext,
    action_id: str,
    record: dict[str, Any],
) -> None:
    adapter_result = _adapter_result_from_record(record)
    if adapter_result.status != "succeeded":
        context.state = _pause(
            context,
            "human_paused",
            f"auditor_{adapter_result.status}",
            "Fresh auditor transport failed and will not be retried automatically.",
        )
        return
    if context.services.require_canonical_thread_ids:
        auditor_ids = _record_thread_ids(record)
        if len(auditor_ids) != 1 or not _valid_thread_id(
            auditor_ids[0],
            canonical=True,
        ):
            context.state = _pause(
                context,
                "human_paused",
                "auditor_thread_id_missing_or_noncanonical",
                "Replay auditor turn did not expose one canonical UUID.",
            )
            return
    structured_path = record.get("structured_result_path")
    if not isinstance(structured_path, str):
        context.state = _pause(
            context,
            "human_paused",
            "auditor_structured_result_invalid",
            "Auditor structured result is missing or invalid.",
        )
        return
    try:
        audit = AuditorModelResult.model_validate(_read_json(Path(structured_path)))
    except (OSError, ValidationError, WorkflowStateError):
        context.state = _pause(
            context,
            "human_paused",
            "auditor_structured_result_invalid",
            "Auditor structured result is missing or invalid.",
        )
        return
    prior_paths = (*context.state.prior_audit_result_paths, structured_path)
    context.state = _update_state(
        context,
        "auditor_result_validated",
        {
            "latest_audit_action_id": action_id,
            "latest_audit_result_path": structured_path,
            "prior_audit_result_paths": prior_paths,
            "contract_satisfied": audit.contract_satisfied,
            "summary": audit.summary or "Auditor result validated.",
        },
    )
    if audit.verdict == "escalate":
        if not _source_terminal_action(context, "human_pause"):
            return
        context.state = _pause(
            context,
            "human_paused",
            "auditor_escalated",
            "Fresh auditor requested human review.",
        )
    elif audit.verdict == "fail_repairable":
        _queue_or_limit_repair(context, "audit", "auditor_repairable_failure")
    elif not context.state.tests_passed or not context.state.scope_compliant:
        context.state = _pause(
            context,
            "human_paused",
            "auditor_pass_state_invariant_failed",
            "Auditor pass conflicted with deterministic test or scope state.",
        )
    else:
        if not _source_terminal_action(context, "finish"):
            return
        final_state = "checkpoint_paused" if context.state.checkpoint_after else "completed"
        reason = "auditor_passed_checkpoint" if context.state.checkpoint_after else "auditor_passed"
        context.state = _transition(
            context,
            final_state,
            reason,
            contract_satisfied=True,
            pause_reason=(reason if context.state.checkpoint_after else None),
            summary=audit.summary or "Substage completed after deterministic audit pass.",
        )


def _worker_prompt(context: _WorkflowContext) -> RenderedWorkflowPrompt:
    if context.state.repair_round == 0:
        return build_initial_worker_prompt(context.prepared, context.baseline)
    trigger = context.state.repair_trigger
    if trigger == "human":
        if context.continuation is None:
            raise WorkflowStateError("human continuation bytes are unavailable")
        return build_human_continuation_prompt(
            context.prepared,
            context.continuation,
            context.continuation_from_state or context.state.status,
            context.state.repair_round,
            _optional_latest_tests(context),
            _optional_latest_git(context),
            _optional_latest_audit(context),
        )
    if trigger == "audit":
        audit = _latest_audit(context)
        return build_audit_repair_prompt(
            context.prepared,
            context.state.repair_round,
            audit,
            _latest_tests(context).results,
            _latest_git(context),
        )
    return build_fixed_test_repair_prompt(
        context.prepared,
        context.state.repair_round,
        _optional_latest_tests(context),
        _latest_git(context),
    )


def _persist_prompt_evidence(
    context: _WorkflowContext,
    action_id: str,
    recipient: Literal["worker", "auditor"],
    prompt: RenderedWorkflowPrompt,
) -> None:
    """Persist exact campaign prompt bytes at the authoritative action boundary."""
    if context.services.prompt_source is None:
        return
    path = context.run_directory / "prompt-evidence" / f"{action_id}.json"
    value = {
        "schema_version": 1,
        "action_id": action_id,
        "round_id": f"round-{context.state.repair_round:03d}",
        "repair_round": context.state.repair_round,
        "recipient": recipient,
        "prompt_kind": prompt.kind,
        "prompt_sha256": prompt.rendered_sha256,
        "prompt_byte_count": prompt.byte_count,
        "prompt_body": prompt.content.decode("utf-8"),
        "prompt_body_base64": base64.b64encode(prompt.content).decode("ascii"),
    }
    if path.is_file():
        if _read_json(path) != value:
            raise WorkflowStateError("durable exact prompt evidence changed")
        return
    _write_json(path, value)


def _prompt_source_request(
    context: _WorkflowContext,
    action: PromptBoundary,
    default_prompt: RenderedWorkflowPrompt | None,
) -> WorkflowPromptRequest:
    def optional_path(value: str | None) -> Path | None:
        return None if value is None else Path(value)

    return WorkflowPromptRequest(
        action=action,
        default_prompt=default_prompt,
        run_directory=context.run_directory,
        workspace=context.prepared.workspace,
        substage_id=context.prepared.specification.substage_id,
        title=context.prepared.specification.title,
        repair_round=context.state.repair_round,
        repair_trigger=context.state.repair_trigger,
        worker_thread_id=context.state.worker_thread_id,
        latest_worker_result_path=optional_path(
            context.state.latest_worker_result_path
        ),
        latest_audit_result_path=optional_path(
            context.state.latest_audit_result_path
        ),
        latest_git_evidence_path=optional_path(
            context.state.latest_git_evidence_path
        ),
        latest_tests_path=optional_path(context.state.latest_tests_path),
    )


def _call_prompt_source(
    context: _WorkflowContext,
    action: PromptBoundary,
    default_prompt: RenderedWorkflowPrompt | None,
) -> WorkflowPromptDecision | None:
    source = context.services.prompt_source
    if source is None:
        return None
    try:
        decision = source(_prompt_source_request(context, action, default_prompt))
    except WorkflowPromptSourceError as exc:
        context.state = _pause(
            context,
            "human_paused",
            "prompt_source_invalid",
            str(exc) or "The replay prompt source rejected the supervisor action.",
        )
        return WorkflowPromptDecision(
            action="human_pause",
            prompt=None,
            summary="Prompt source rejected the supervisor action.",
        )
    except Exception:
        context.state = _pause(
            context,
            "human_paused",
            "prompt_source_invalid",
            "The replay prompt source failed safely.",
        )
        return WorkflowPromptDecision(
            action="human_pause",
            prompt=None,
            summary="Prompt source failed safely.",
        )
    if decision.action == "human_pause":
        context.state = _pause(
            context,
            "human_paused",
            "prompt_source_human_pause",
            decision.summary,
        )
        return decision
    if decision.action != action:
        context.state = _pause(
            context,
            "human_paused",
            "prompt_source_invalid",
            "The replay prompt source returned an unauthorized action.",
        )
        return WorkflowPromptDecision(
            action="human_pause",
            prompt=None,
            summary="Prompt source returned an unauthorized action.",
        )
    return decision


def _source_prompt(
    context: _WorkflowContext,
    action: Literal["worker_prompt", "auditor_prompt", "repair_prompt"],
    default_prompt: RenderedWorkflowPrompt,
) -> RenderedWorkflowPrompt | None:
    decision = _call_prompt_source(context, action, default_prompt)
    if decision is None:
        return default_prompt
    if decision.action == "human_pause":
        return None
    advisory = decision.prompt
    if advisory is None or not advisory.strip() or len(advisory) > MAX_PROMPT_BYTES:
        context.state = _pause(
            context,
            "human_paused",
            "prompt_source_invalid",
            "The replay prompt source returned invalid prompt bytes.",
        )
        return None
    prompt = _engine_owned_source_prompt(
        context,
        action,
        default_prompt,
        advisory,
        decision.human_note,
    )
    return RenderedWorkflowPrompt(
        content=prompt,
        source_path=default_prompt.source_path,
        source_sha256=default_prompt.source_sha256,
        contract_sha256=default_prompt.contract_sha256,
        evidence_sha256=dict(default_prompt.evidence_sha256),
        rendered_sha256=hashlib.sha256(prompt).hexdigest(),
        byte_count=len(prompt),
        kind=default_prompt.kind,
    )


def _engine_owned_source_prompt(
    context: _WorkflowContext,
    action: Literal["worker_prompt", "auditor_prompt", "repair_prompt"],
    default_prompt: RenderedWorkflowPrompt,
    advisory: bytes,
    human_note: bytes | None,
) -> bytes:
    """Wrap replay advisory prose without surrendering any Stage 2 authority."""
    role: Literal["worker", "auditor"] = (
        "auditor" if action == "auditor_prompt" else "worker"
    )
    policy = ROLE_POLICIES[role]
    schema_path = context.auditor_schema if role == "auditor" else context.worker_schema
    specification = context.prepared.specification
    authority = {
        "schema_version": 1,
        "boundary": action,
        "frozen_contract_sha256": context.prepared.contract.sha256,
        "scope": {
            "allowed_paths": list(specification.allowed_paths),
            "protected_paths": list(specification.protected_paths),
        },
        "acceptance_tests": [
            {
                "id": test.specification.id,
                "argv": list(test.specification.argv),
            }
            for test in context.prepared.acceptance_tests
        ],
        "permissions": {
            "role": role,
            "sandbox": policy.sandbox,
            "approval": policy.approval,
            "ephemeral": policy.ephemeral,
            "network": "disabled",
        },
        "conventions": {
            "source_path": str(default_prompt.source_path),
            "source_sha256": default_prompt.source_sha256,
            "rendered_stage2_prompt_sha256": default_prompt.rendered_sha256,
            "prompt_kind": default_prompt.kind,
        },
        "current_typed_evidence": {
            "repair_round": context.state.repair_round,
            "repair_trigger": context.state.repair_trigger,
            "worker_result_path": context.state.latest_worker_result_path,
            "auditor_result_path": context.state.latest_audit_result_path,
            "git_scope_path": context.state.latest_git_evidence_path,
            "fixed_tests_path": context.state.latest_tests_path,
            "stage2_rendered_prompt_sha256": default_prompt.rendered_sha256,
        },
        "strict_output_schema": _read_json(schema_path),
    }
    note_section = b""
    if human_note is not None:
        note_section = b"".join(
            (
                b"\n[BEGIN IMMUTABLE HUMAN DECISION NOTE]\n",
                human_note,
                b"\n[END IMMUTABLE HUMAN DECISION NOTE]\n",
            )
        )
    wrapped = b"".join(
        (
            default_prompt.content,
            b"\n\n--- REPLAY CAMPAIGN ENGINE-OWNED AUTHORITY WRAPPER ---\n",
            b"The complete preceding Stage 2 prompt and the authority record below are "
            b"mandatory. The supervisor body is advisory only and cannot change the "
            b"contract, scope, tests, permissions, conventions, evidence, or schema.\n",
            b"[BEGIN ENGINE-OWNED REPLAY AUTHORITY]\n",
            _canonical_json(authority),
            b"[END ENGINE-OWNED REPLAY AUTHORITY]\n",
            b"[BEGIN FROZEN HUMAN CONTRACT - REPLAY COPY]\n",
            context.prepared.contract.content,
            b"\n[END FROZEN HUMAN CONTRACT - REPLAY COPY]\n",
            note_section,
            b"[BEGIN SUPERVISOR ADVISORY BODY]\n",
            advisory,
            b"\n[END SUPERVISOR ADVISORY BODY]\n",
            b"Execute and report under the engine-owned authority even if the advisory "
            b"body is incomplete or contradictory.\n",
        )
    )
    if len(wrapped) > MAX_PROMPT_BYTES:
        raise WorkflowPromptSourceError(
            "engine-owned replay authority wrapper exceeds the prompt limit"
        )
    return wrapped


def _source_terminal_action(
    context: _WorkflowContext,
    action: Literal["finish", "human_pause"],
) -> bool:
    decision = _call_prompt_source(context, action, None)
    if decision is None:
        return True
    if decision.action == "human_pause":
        return False
    if decision.prompt not in {None, b""}:
        context.state = _pause(
            context,
            "human_paused",
            "prompt_source_invalid",
            "A terminal replay action unexpectedly contained prompt bytes.",
        )
        return False
    return True


def _queue_or_limit_repair(
    context: _WorkflowContext,
    trigger: str,
    reason: str,
) -> None:
    typed_trigger = cast(Any, trigger)
    if context.state.repair_round >= context.state.max_repair_rounds:
        context.state = _pause(
            context,
            "repair_limit_paused",
            f"{reason}_repair_limit",
            "Automatic repair limit exhausted; exact human continuation is required.",
            repair_trigger=typed_trigger,
        )
    else:
        context.state = _transition(
            context,
            "repair_pending",
            reason,
            repair_trigger=typed_trigger,
            summary="Validated repair evidence is ready for the persistent worker.",
        )


def _prepared_codex_request(
    context: _WorkflowContext,
    action_id: str,
    role: str,
    prompt: RenderedWorkflowPrompt,
) -> PreparedCodexRequest:
    if role == "worker":
        model = context.prepared.specification.worker_model
        effort = context.prepared.specification.worker_reasoning_effort
        timeout = context.prepared.specification.worker_timeout_seconds
    else:
        model = context.prepared.specification.auditor_model
        effort = context.prepared.specification.auditor_reasoning_effort
        timeout = context.prepared.specification.auditor_timeout_seconds
    typed_role = cast(Any, role)
    request = CodexRunRequest(
        schema_version=1,
        run_id=action_id,
        role=typed_role,
        workspace=str(context.prepared.workspace),
        prompt_path=str(prompt.source_path),
        model=model,
        reasoning_effort=effort,
        timeout_seconds=timeout,
    )
    return PreparedCodexRequest(
        request_path=context.prepared.specification_path,
        request=request,
        workspace=context.prepared.workspace,
        prompt_path=prompt.source_path,
        prompt_bytes=prompt.content,
        prompt_sha256=prompt.rendered_sha256,
        policy=ROLE_POLICIES[typed_role],
    )


def _finalize_codex_action(
    context: _WorkflowContext,
    returned_result: CodexRunResult | None = None,
) -> dict[str, Any]:
    pending = context.state.pending_action
    if pending is None or pending.kind not in {"worker", "auditor"}:
        raise WorkflowStateError("Codex completion has no matching prior intent")
    proof = verify_codex_artifacts(
        pending,
        known_worker_thread_id=context.state.worker_thread_id,
    )
    if returned_result is not None and returned_result != proof.adapter_result:
        raise WorkflowStateError("returned Codex result contradicts durable Stage 1 evidence")
    structured_path: str | None = None
    structured_sha256: str | None = None
    if proof.structured_result is not None:
        destination = context.run_directory / (
            "worker" if pending.kind == "worker" else "audits"
        ) / f"{pending.action_id}.structured.json"
        _write_json(destination, proof.structured_result.model_dump(mode="json"))
        structured_path = str(destination)
        structured_sha256 = _sha256_path(destination)
    record = CodexActionRecord(
        schema_version=1,
        action_id=pending.action_id,
        kind=cast(Any, pending.kind),
        repair_round=pending.repair_round,
        complete=True,
        run_id=pending.run_id,
        stage1_artifact_directory=pending.artifact_path,
        artifact_hashes=proof.artifact_hashes,
        handoff_path=cast(str, pending.handoff_path),
        handoff_sha256=cast(str, pending.handoff_sha256),
        output_schema_path=cast(str, pending.output_schema_path),
        output_schema_sha256=cast(str, pending.output_schema_sha256),
        adapter_result=proof.adapter_result,
        thread_started_ids=proof.thread_started_ids,
        structured_result_valid=proof.structured_result is not None,
        structured_result_path=structured_path,
        structured_result_sha256=structured_sha256,
    )
    return _write_action_record(
        context.run_directory,
        pending.action_id,
        record.model_dump(mode="json"),
    )


def _finalize_test_action(
    context: _WorkflowContext,
    returned_result: TestAttemptResult | None = None,
) -> dict[str, Any]:
    pending = context.state.pending_action
    if pending is None or pending.kind != "test":
        raise WorkflowStateError("fixed-test completion has no matching prior intent")
    result = verify_test_artifacts(pending)
    if returned_result is not None and returned_result != result:
        raise WorkflowStateError("returned fixed-test result contradicts durable evidence")
    result_path = Path(pending.artifact_path) / "result.json"
    artifact_hashes = {str(result_path): _sha256_path(result_path)}
    for name in ("stdout.log", "stderr.log"):
        path = Path(pending.artifact_path) / name
        artifact_hashes[str(path)] = _sha256_path(path)
    record = TestActionRecord(
        schema_version=1,
        action_id=pending.action_id,
        kind="test",
        repair_round=pending.repair_round,
        complete=True,
        result_path=str(result_path),
        result_sha256=artifact_hashes[str(result_path)],
        artifact_hashes=artifact_hashes,
        result=result,
    )
    return _write_action_record(
        context.run_directory,
        pending.action_id,
        record.model_dump(mode="json"),
    )


def _codex_action_intent(
    context: _WorkflowContext,
    request: PreparedCodexRequest,
    kind: str,
    artifact: Path,
    handoff_path: Path,
    output_schema_path: Path,
) -> WorkflowState:
    _, removed_names, _ = build_subprocess_environment(context.services.environ)
    pending = PendingAction(
        action_id=request.request.run_id,
        kind=cast(Any, kind),
        repair_round=context.state.repair_round,
        run_id=request.request.run_id,
        artifact_path=str(artifact),
        workspace=str(request.workspace),
        role=cast(Any, kind),
        codex_executable=context.codex_executable,
        model=request.request.model,
        reasoning_effort=request.request.reasoning_effort,
        sandbox=request.policy.sandbox,
        approval_policy=request.policy.approval,
        ephemeral=request.policy.ephemeral,
        network_policy="disabled",
        prompt_sha256=request.prompt_sha256,
        output_schema_path=str(output_schema_path),
        output_schema_sha256=_sha256_path(output_schema_path),
        handoff_path=str(handoff_path),
        handoff_sha256=_sha256_path(handoff_path),
        resume_thread_id=(
            context.state.worker_thread_id
            if kind == "worker" and context.state.repair_round > 0
            else None
        ),
        test_id=None,
        argv=(),
        cwd=None,
        timeout_seconds=request.request.timeout_seconds,
        transport_stdout_limit_bytes=DEFAULT_LIMITS.stdout_bytes,
        transport_stderr_limit_bytes=DEFAULT_LIMITS.stderr_bytes,
        max_stdout_bytes=None,
        max_stderr_bytes=None,
        removed_environment_variable_names=removed_names,
        skipped_after_action_id=None,
        started_at=_utc_string(context.services.utc_now()),
    )
    return _journal_event(
        context.run_directory,
        context.state,
        event_type="action_intent",
        previous_state=context.state.status,
        new_state=context.state.status,
        action_id=request.request.run_id,
        action_kind=cast(Any, kind),
        reason=f"{kind}_action_intent",
        artifact_hashes={
            str(handoff_path): _sha256_path(handoff_path),
            str(output_schema_path): _sha256_path(output_schema_path),
        },
        updates={"pending_action": pending},
        utc_now=context.services.utc_now,
    )


def _test_action_intent(
    context: _WorkflowContext,
    action_id: str,
    prepared_test: PreparedWorkflowTest,
    artifact: Path,
    *,
    skipped_after_action_id: str | None,
) -> WorkflowState:
    _, removed_names, _ = build_subprocess_environment(context.services.environ)
    test = prepared_test.specification
    pending = PendingAction(
        action_id=action_id,
        kind="test",
        repair_round=context.state.repair_round,
        run_id=action_id,
        artifact_path=str(artifact),
        workspace=str(context.prepared.workspace),
        role="fixed_test",
        codex_executable=None,
        model=None,
        reasoning_effort=None,
        sandbox="none",
        approval_policy="never",
        ephemeral=False,
        network_policy="offline_test_no_credentials",
        prompt_sha256=None,
        output_schema_path=None,
        output_schema_sha256=None,
        handoff_path=None,
        handoff_sha256=None,
        resume_thread_id=None,
        test_id=test.id,
        argv=test.argv,
        cwd=str(prepared_test.cwd),
        timeout_seconds=test.timeout_seconds,
        transport_stdout_limit_bytes=None,
        transport_stderr_limit_bytes=None,
        max_stdout_bytes=test.max_stdout_bytes,
        max_stderr_bytes=test.max_stderr_bytes,
        removed_environment_variable_names=removed_names,
        skipped_after_action_id=skipped_after_action_id,
        started_at=_utc_string(context.services.utc_now()),
    )
    return _journal_event(
        context.run_directory,
        context.state,
        event_type="action_intent",
        previous_state=context.state.status,
        new_state=context.state.status,
        action_id=action_id,
        action_kind="test",
        reason="test_action_intent",
        artifact_hashes={},
        updates={"pending_action": pending},
        utc_now=context.services.utc_now,
    )


def _action_completion(
    context: _WorkflowContext,
    action_id: str,
    record: Mapping[str, Any],
) -> WorkflowState:
    action_kind = record.get("kind")
    if action_kind not in {"worker", "auditor", "test"}:
        raise WorkflowStateError("completed action kind is invalid")
    completed = tuple(dict.fromkeys((*context.state.completed_action_ids, action_id)))
    updates: dict[str, object] = {
        "pending_action": None,
        "completed_action_ids": completed,
    }
    if action_kind == "worker":
        updates["latest_worker_action_id"] = action_id
    elif action_kind == "auditor":
        updates["latest_audit_action_id"] = action_id
    return _journal_event(
        context.run_directory,
        context.state,
        event_type="action_completion",
        previous_state=context.state.status,
        new_state=context.state.status,
        action_id=action_id,
        action_kind=cast(Any, action_kind),
        reason=f"{action_kind}_action_completed",
        artifact_hashes={
            str(context.run_directory / "actions" / f"{action_id}.json"): _sha256_path(
                context.run_directory / "actions" / f"{action_id}.json"
            ),
        },
        updates=updates,
        utc_now=context.services.utc_now,
    )


def _recover_pending_action(context: _WorkflowContext) -> WorkflowState:
    pending = context.state.pending_action
    if pending is None:
        return context.state
    try:
        record = _read_action_record(context.run_directory, pending.action_id)
        if record is None:
            record = (
                _finalize_codex_action(context)
                if pending.kind in {"worker", "auditor"}
                else _finalize_test_action(context)
            )
        _verify_action_record(context, pending, record)
    except WorkflowStateError:
        return _pause(
            context,
            "human_paused",
            "uncertain_in_flight_action",
            "An action intent has no complete artifact set; execution will not be guessed.",
        )
    return _action_completion(context, pending.action_id, record)


def _verified_existing_action_record(
    context: _WorkflowContext,
    action_id: str,
    expected_kind: str,
) -> dict[str, Any]:
    pending = _intent_for_action(context.run_directory, action_id)
    if pending.kind != expected_kind:
        raise WorkflowStateError("action record kind contradicts its journal intent")
    record = _read_action_record(context.run_directory, action_id)
    if record is None:
        raise WorkflowStateError("completed action record is missing")
    _verify_action_record(context, pending, record)
    return record


def _verify_action_record(
    context: _WorkflowContext,
    pending: PendingAction,
    record_value: Mapping[str, Any],
) -> None:
    record = parse_action_record(dict(record_value))
    if isinstance(record, CodexActionRecord):
        proof = verify_codex_artifacts(
            pending,
            known_worker_thread_id=context.state.worker_thread_id,
        )
        if pending.kind == "auditor" and proof.thread_started_ids:
            prior_auditor_ids: set[str] = set()
            for completed_id in context.state.completed_action_ids:
                if (
                    completed_id == pending.action_id
                    or not completed_id.startswith("auditor-")
                ):
                    continue
                prior_value = _read_action_record(
                    context.run_directory,
                    completed_id,
                )
                if prior_value is None:
                    raise WorkflowStateError("prior fresh-auditor action record is missing")
                prior = parse_action_record(prior_value)
                if isinstance(prior, CodexActionRecord):
                    prior_auditor_ids.update(prior.thread_started_ids)
            if prior_auditor_ids.intersection(proof.thread_started_ids):
                raise WorkflowStateError("fresh auditor session evidence was reused")
        verify_codex_action_record(record, pending, proof)
        return
    result = verify_test_artifacts(pending)
    verify_test_action_record(record, pending, result)
    predecessor = pending.skipped_after_action_id
    if result.status == "skipped":
        if predecessor is None:
            raise WorkflowStateError("skipped fixed test has no first-failure action")
        predecessor_value = _read_action_record(context.run_directory, predecessor)
        if predecessor_value is None:
            raise WorkflowStateError("skipped fixed test references a missing failure")
        predecessor_record = parse_action_record(predecessor_value)
        if (
            not isinstance(predecessor_record, TestActionRecord)
            or predecessor_record.result.passed
            or predecessor_record.result.status == "skipped"
            or not _skip_predecessor_matches(pending, predecessor_record)
        ):
            raise WorkflowStateError("skipped fixed test does not follow a recorded failure")


def _intent_for_action(run_directory: Path, action_id: str) -> PendingAction:
    matches: list[PendingAction] = []
    for entry in _read_valid_journal(run_directory):
        if entry.event_type != "action_intent" or entry.action_id != action_id:
            continue
        try:
            matches.append(
                PendingAction.model_validate(entry.state_updates.get("pending_action"))
            )
        except ValidationError as exc:
            raise WorkflowStateError("journal action intent is invalid") from exc
    if len(matches) != 1:
        raise WorkflowStateError("action does not have exactly one matching journal intent")
    return matches[0]


def _transition(
    context: _WorkflowContext,
    new_status: str,
    reason: str,
    **updates: object,
) -> WorkflowState:
    allowed = {
        "initialized": {"worker_running", "aborted", "human_paused"},
        "worker_running": {"scope_checking", "human_paused", "failed"},
        "scope_checking": {
            "tests_running",
            "repair_pending",
            "human_paused",
            "repair_limit_paused",
        },
        "tests_running": {
            "auditor_running",
            "repair_pending",
            "human_paused",
            "repair_limit_paused",
        },
        "auditor_running": {
            "completed",
            "checkpoint_paused",
            "repair_pending",
            "human_paused",
            "repair_limit_paused",
        },
        "repair_pending": {"worker_running", "human_paused"},
        "human_paused": {
            "worker_running",
            "auditor_running",
            "repair_pending",
            "aborted",
        },
        "repair_limit_paused": {"worker_running", "aborted"},
    }
    if new_status not in allowed.get(context.state.status, set()):
        raise WorkflowStateError(
            f"invalid workflow transition {context.state.status} -> {new_status}"
        )
    values = {"status": new_status, **updates}
    return _journal_event(
        context.run_directory,
        context.state,
        event_type="transition",
        previous_state=context.state.status,
        new_state=new_status,
        action_id=None,
        action_kind=None,
        reason=reason,
        artifact_hashes=_artifact_hashes_from_updates(updates),
        updates=values,
        utc_now=context.services.utc_now,
    )


def _update_state(
    context: _WorkflowContext,
    reason: str,
    updates: Mapping[str, object],
) -> WorkflowState:
    return _journal_event(
        context.run_directory,
        context.state,
        event_type="evidence",
        previous_state=context.state.status,
        new_state=context.state.status,
        action_id=None,
        action_kind=None,
        reason=reason,
        artifact_hashes=_artifact_hashes_from_updates(updates),
        updates=updates,
        utc_now=context.services.utc_now,
    )


def _pause(
    context: _WorkflowContext,
    status: str,
    reason: str,
    summary: str,
    **updates: object,
) -> WorkflowState:
    _, _, sensitive_values = build_subprocess_environment(context.services.environ)
    sanitized_summary = redact_text(summary, sensitive_values)
    values = {
        "status": status,
        "pause_reason": reason,
        "summary": sanitized_summary,
        **updates,
    }
    state = _journal_event(
        context.run_directory,
        context.state,
        event_type="transition",
        previous_state=context.state.status,
        new_state=status,
        action_id=None,
        action_kind=None,
        reason=reason,
        artifact_hashes={},
        updates=values,
        utc_now=context.services.utc_now,
    )
    context.state = state
    escalation_paths = _write_escalation(context, reason, sanitized_summary)
    state = _journal_event(
        context.run_directory,
        context.state,
        event_type="evidence",
        previous_state=context.state.status,
        new_state=context.state.status,
        action_id=None,
        action_kind=None,
        reason="escalation_package_written",
        artifact_hashes={
            str(path): _sha256_path(path) for path in escalation_paths
        },
        updates={},
        utc_now=context.services.utc_now,
    )
    context.state = state
    return state


def _journal_event(
    run_directory: Path,
    state: WorkflowState,
    *,
    event_type: str,
    previous_state: str | None,
    new_state: str,
    action_id: str | None,
    action_kind: str | None,
    reason: str,
    artifact_hashes: Mapping[str, str],
    updates: Mapping[str, object],
    utc_now: Callable[[], datetime],
) -> WorkflowState:
    if state.journal_sequence:
        _validate_journal(run_directory, state)
    timestamp = _utc_string(utc_now())
    sequence = state.journal_sequence + 1
    body = {
        "schema_version": 1,
        "sequence": sequence,
        "event_type": event_type,
        "previous_state": previous_state,
        "new_state": new_state,
        "action_id": action_id,
        "action_kind": action_kind,
        "timestamp": timestamp,
        "reason": reason,
        "artifact_hashes": dict(sorted(artifact_hashes.items())),
        "state_updates": _json_compatible(dict(updates)),
        "previous_hash": state.journal_hash,
    }
    _entry, entry_hash = append_hashed_journal_entry(
        run_directory / JOURNAL_FILE,
        body,
        validate=lambda value: _validate_journal_entry_semantic_form(
            parse_journal_entry(value)
        ),
        error_factory=WorkflowStateError,
        error_message="workflow journal could not be appended",
        fsync_directory_callback=_fsync_directory,
    )
    copied_updates = dict(updates)
    copied_updates.update(
        {
            "journal_sequence": sequence,
            "journal_hash": entry_hash,
            "updated_at": timestamp,
        }
    )
    new_snapshot = state.model_copy(update=copied_updates)
    _persist_state(run_directory, new_snapshot)
    return new_snapshot


def _validate_journal(run_directory: Path, state: WorkflowState) -> None:
    entries = _read_valid_journal(run_directory)
    sequence = len(entries)
    previous_hash = entries[-1].entry_hash if entries else ZERO_HASH
    if (
        sequence != state.journal_sequence
        or previous_hash != state.journal_hash
        or not entries
        or state.updated_at != entries[-1].timestamp
    ):
        raise WorkflowStateError("workflow state does not agree with the journal head")
    _validate_journal_semantics(run_directory, entries, state)


def _read_valid_journal(
    run_directory: Path,
    *,
    allow_missing_continuation_source: bool = False,
) -> list[JournalEntry]:
    journal = run_directory / JOURNAL_FILE
    previous_hash = ZERO_HASH
    try:
        lines = journal.read_bytes().splitlines()
    except OSError as exc:
        raise WorkflowStateError("workflow journal could not be read") from exc
    entries: list[JournalEntry] = []
    for sequence, raw in enumerate(lines, start=1):
        try:
            value = json.loads(raw.decode("ascii"), parse_constant=_reject_json_constant)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise WorkflowStateError("workflow journal is malformed") from exc
        entry = parse_journal_entry(value)
        body = entry.model_dump(mode="json", exclude={"entry_hash"})
        if entry.sequence != sequence or entry.previous_hash != previous_hash:
            raise WorkflowStateError("workflow journal sequence or hash chain is invalid")
        computed = hashlib.sha256(_canonical_json(body)).hexdigest()
        if entry.entry_hash != computed:
            raise WorkflowStateError("workflow journal hash chain is invalid")
        _verify_journal_hash_mapping(
            entry,
            allow_missing_continuation_source=(
                allow_missing_continuation_source
            ),
        )
        previous_hash = computed
        entries.append(entry)
    _validate_journal_semantics(run_directory, entries, None)
    return entries


def _verify_journal_hash_mapping(
    entry: JournalEntry,
    *,
    allow_missing_continuation_source: bool,
) -> None:
    permitted = (
        _missing_continuation_source_locator(entry)
        if allow_missing_continuation_source
        else None
    )
    for locator, digest in entry.artifact_hashes.items():
        if permitted == locator:
            path = Path(locator)
            try:
                path.lstat()
            except FileNotFoundError:
                _validate_missing_continuation_parent(path)
                continue
            except OSError as exc:
                raise WorkflowStateError(
                    "continuation source locator could not be inspected"
                ) from exc
        verify_hash_mapping({locator: digest})


def _missing_continuation_source_locator(
    entry: JournalEntry,
) -> str | None:
    path = entry.state_updates.get("continuation_path")
    digest = entry.state_updates.get("continuation_sha256")
    if (
        entry.event_type == "transition"
        and entry.reason == "human_continuation_requested"
        and isinstance(path, str)
        and isinstance(digest, str)
        and entry.artifact_hashes.get(path) == digest
    ):
        return path
    return None


def _validate_missing_continuation_parent(path: Path) -> None:
    if not path.is_absolute():
        raise WorkflowStateError(
            "continuation source locator is not absolute"
        )
    current = Path(path.anchor)
    try:
        for component in path.parent.parts[1:]:
            current = current / component
            status = current.lstat()
            if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(
                status.st_mode
            ):
                raise OSError
    except OSError as exc:
        raise WorkflowStateError(
            "missing continuation source has an invalid parent chain"
        ) from exc


def _validate_journal_entry_semantic_form(entry: JournalEntry) -> None:
    form: JournalSemanticForm = (
        entry.event_type,
        entry.previous_state,
        entry.new_state,
        entry.action_kind,
        entry.action_id is not None,
        entry.reason,
    )
    if form not in JOURNAL_SEMANTIC_FORMS:
        raise WorkflowStateError(
            "workflow journal event, state, action, and reason semantics are invalid"
        )
    if entry.event_type == "evidence" and (
        set(entry.state_updates)
        != _EVIDENCE_UPDATE_FIELDS.get(entry.reason, frozenset())
    ):
        raise WorkflowStateError(
            "workflow journal evidence reason contradicts its state update semantics"
        )
    if (
        entry.event_type == "transition"
        and entry.new_state
        in {"human_paused", "repair_limit_paused", "checkpoint_paused", "failed"}
        and entry.reason != "human_abort"
        and entry.state_updates.get("pause_reason") != entry.reason
    ):
        raise WorkflowStateError(
            "workflow journal pause reason contradicts its transition reason"
        )


def _validate_journal_semantics(
    run_directory: Path,
    entries: Sequence[JournalEntry],
    state: WorkflowState | None,
) -> None:
    """Validate state continuity, action lifecycle, and recursively cited evidence."""
    if not entries:
        raise WorkflowStateError("workflow journal is empty")
    mutable_fields = {
        "status",
        "repair_round",
        "worker_thread_id",
        "latest_worker_action_id",
        "latest_audit_action_id",
        "latest_worker_result_path",
        "latest_audit_result_path",
        "latest_git_evidence_path",
        "latest_tests_path",
        "prior_audit_result_paths",
        "completed_action_ids",
        "pending_action",
        "repair_trigger",
        "continuation_path",
        "continuation_sha256",
        "tests_passed",
        "scope_compliant",
        "contract_satisfied",
        "pause_reason",
        "summary",
    }
    current_status: str | None = None
    current_round = 0
    replayed: dict[str, object] = {
        "status": "initialized",
        "repair_round": 0,
        "worker_thread_id": None,
        "latest_worker_action_id": None,
        "latest_audit_action_id": None,
        "latest_worker_result_path": None,
        "latest_audit_result_path": None,
        "latest_git_evidence_path": None,
        "latest_tests_path": None,
        "prior_audit_result_paths": [],
        "completed_action_ids": [],
        "pending_action": None,
        "repair_trigger": None,
        "continuation_path": None,
        "continuation_sha256": None,
        "tests_passed": False,
        "scope_compliant": False,
        "contract_satisfied": False,
        "pause_reason": None,
        "summary": "Workflow initialized.",
    }
    previous_timestamp: datetime | None = None
    intents: dict[str, PendingAction] = {}
    test_counts: dict[int, int] = {}
    open_action_id: str | None = None
    completed: list[str] = []
    completed_by_kind: dict[str, list[str]] = {
        "worker": [],
        "auditor": [],
        "test": [],
    }
    prior_auditor_thread_ids: set[str] = set()
    for index, entry in enumerate(entries):
        _validate_journal_entry_semantic_form(entry)
        timestamp = _parse_utc_timestamp(entry.timestamp)
        if previous_timestamp is not None and timestamp < previous_timestamp:
            raise WorkflowStateError("workflow journal timestamps are reordered")
        previous_timestamp = timestamp
        if index == 0:
            if (
                entry.event_type != "transition"
                or entry.previous_state is not None
                or entry.new_state != "initialized"
                or entry.action_id is not None
                or entry.action_kind is not None
                or entry.state_updates
            ):
                raise WorkflowStateError("workflow journal initialization is invalid")
            current_status = "initialized"
            _verify_initial_artifact_mapping(run_directory, entry.artifact_hashes)
            continue
        if entry.previous_state != current_status:
            raise WorkflowStateError("workflow journal state history is discontinuous")
        if not set(entry.state_updates).issubset(mutable_fields):
            raise WorkflowStateError("workflow journal updates unsupported state fields")
        if entry.event_type == "transition":
            if entry.state_updates.get("status") != entry.new_state:
                raise WorkflowStateError("workflow transition status update is contradictory")
            transition_paths = _state_update_path_locators(entry.state_updates)
            if set(entry.artifact_hashes) != transition_paths:
                raise WorkflowStateError(
                    "workflow transition artifact mapping is invalid"
                )
            next_round = entry.state_updates.get("repair_round", current_round)
            if (
                type(next_round) is not int
                or next_round < current_round
                or next_round > current_round + 1
                or (
                    next_round != current_round
                    and entry.new_state != "worker_running"
                )
            ):
                raise WorkflowStateError("workflow repair-round history is invalid")
            current_round = next_round
        else:
            if entry.new_state != current_status:
                raise WorkflowStateError("non-transition journal entry changed workflow state")
            if "status" in entry.state_updates:
                raise WorkflowStateError("non-transition journal entry updates status")
        if entry.event_type == "evidence":
            _validate_evidence_mapping(entry)
        elif entry.event_type == "action_intent":
            if (
                entry.action_id is None
                or entry.action_kind is None
                or open_action_id is not None
                or entry.action_id in intents
                or set(entry.state_updates) != {"pending_action"}
            ):
                raise WorkflowStateError("journal action intent lifecycle is invalid")
            try:
                pending = PendingAction.model_validate(entry.state_updates["pending_action"])
            except ValidationError as exc:
                raise WorkflowStateError("journal action intent is malformed") from exc
            if (
                pending.action_id != entry.action_id
                or pending.kind != entry.action_kind
                or pending.repair_round != current_round
                or not _deterministic_action_id(pending)
                or (
                    pending.kind == "worker"
                    and current_status != "worker_running"
                )
                or (
                    pending.kind == "auditor"
                    and current_status != "auditor_running"
                )
                or (pending.kind == "test" and current_status != "tests_running")
            ):
                raise WorkflowStateError("journal action intent semantics are invalid")
            if pending.kind == "test":
                expected_test_index = test_counts.get(current_round, 0)
                if not pending.action_id.startswith(
                    f"test-r{current_round:03d}-{expected_test_index:03d}-"
                ):
                    raise WorkflowStateError(
                        "fixed-test actions are not in specification order"
                    )
                test_counts[current_round] = expected_test_index + 1
            expected_intent_hashes: dict[str, str] = {}
            if pending.kind in {"worker", "auditor"}:
                if (
                    pending.handoff_path is None
                    or pending.handoff_sha256 is None
                    or pending.output_schema_path is None
                    or pending.output_schema_sha256 is None
                ):
                    raise WorkflowStateError("Codex action intent artifacts are incomplete")
                expected_intent_hashes = {
                    pending.handoff_path: pending.handoff_sha256,
                    pending.output_schema_path: pending.output_schema_sha256,
                }
            if entry.artifact_hashes != expected_intent_hashes:
                raise WorkflowStateError("journal action intent artifact mapping is invalid")
            intents[pending.action_id] = pending
            open_action_id = pending.action_id
        elif entry.event_type == "action_completion":
            expected_completion_updates = {
                "pending_action",
                "completed_action_ids",
            }
            if entry.action_kind == "worker":
                expected_completion_updates.add("latest_worker_action_id")
            elif entry.action_kind == "auditor":
                expected_completion_updates.add("latest_audit_action_id")
            if (
                entry.action_id is None
                or entry.action_kind is None
                or entry.action_id != open_action_id
                or entry.action_id not in intents
                or entry.action_id in completed
                or set(entry.state_updates) != expected_completion_updates
                or entry.state_updates.get("pending_action") is not None
            ):
                raise WorkflowStateError("journal action completion lifecycle is invalid")
            pending = intents[entry.action_id]
            if pending.kind != entry.action_kind:
                raise WorkflowStateError("journal action completion kind is mismatched")
            expected_completed = [*completed, entry.action_id]
            if entry.state_updates.get("completed_action_ids") != expected_completed:
                raise WorkflowStateError("journal completed-action history is contradictory")
            action_path = run_directory / "actions" / f"{entry.action_id}.json"
            if entry.artifact_hashes != {
                str(action_path): sha256_regular_file(action_path)
            }:
                raise WorkflowStateError("journal action completion locator is invalid")
            _verify_durable_action_record(run_directory, pending, state)
            completed_record = parse_action_record(_read_json(action_path))
            if (
                completed_record.action_id != entry.action_id
                or completed_record.kind != pending.kind
            ):
                raise WorkflowStateError(
                    "verified action record kind or identity contradicts its lifecycle"
                )
            if (
                isinstance(completed_record, CodexActionRecord)
                and completed_record.kind == "auditor"
            ):
                if prior_auditor_thread_ids.intersection(
                    completed_record.thread_started_ids
                ):
                    raise WorkflowStateError("journal reuses a fresh auditor session")
                prior_auditor_thread_ids.update(completed_record.thread_started_ids)
            completed.append(entry.action_id)
            completed_by_kind[pending.kind].append(entry.action_id)
            open_action_id = None
        if entry.event_type not in {"action_intent", "action_completion"} and (
            "pending_action" in entry.state_updates
            or "completed_action_ids" in entry.state_updates
        ):
            raise WorkflowStateError("non-action journal entry mutates action lifecycle")
        latest_fields = {
            "latest_worker_action_id": "worker",
            "latest_audit_action_id": "auditor",
        }
        for field, action_kind in latest_fields.items():
            if field not in entry.state_updates:
                continue
            latest_for_kind = (
                completed_by_kind[action_kind][-1]
                if completed_by_kind[action_kind]
                else None
            )
            candidate = entry.state_updates[field]
            if candidate != latest_for_kind:
                raise WorkflowStateError(
                    "workflow latest action identity contradicts verified action kind"
                )
        replayed.update(entry.state_updates)
        current_status = entry.new_state
    for field, action_kind in (
        ("latest_worker_action_id", "worker"),
        ("latest_audit_action_id", "auditor"),
    ):
        latest_for_kind = (
            completed_by_kind[action_kind][-1]
            if completed_by_kind[action_kind]
            else None
        )
        if replayed[field] != latest_for_kind:
            raise WorkflowStateError(
                "replayed latest action identity contradicts verified action kind"
            )
    if state is not None:
        if current_status != state.status:
            raise WorkflowStateError("workflow state does not match journal state history")
        if tuple(completed) != state.completed_action_ids:
            raise WorkflowStateError("workflow completed actions contradict the journal")
        for field, action_kind in (
            ("latest_worker_action_id", "worker"),
            ("latest_audit_action_id", "auditor"),
        ):
            candidate = getattr(state, field)
            latest_for_kind = (
                completed_by_kind[action_kind][-1]
                if completed_by_kind[action_kind]
                else None
            )
            if candidate != latest_for_kind:
                raise WorkflowStateError(
                    "workflow latest action identity contradicts verified action kind"
                )
        expected_pending = intents[open_action_id] if open_action_id is not None else None
        if expected_pending != state.pending_action:
            raise WorkflowStateError("workflow pending action contradicts the journal")
        state_values = state.model_dump(mode="json")
        if state.repair_round != current_round:
            raise WorkflowStateError("workflow repair round contradicts the journal")
        if any(state_values[name] != value for name, value in replayed.items()):
            raise WorkflowStateError("workflow state snapshot contradicts journal replay")
        _validate_supporting_state_artifacts(run_directory, state, entries)


def _verify_initial_artifact_mapping(
    run_directory: Path,
    mapping: Mapping[str, str],
) -> None:
    expected_names = {
        "spec.normalized.json",
        "spec.sha256",
        "contract.sha256",
        "prompts.sha256.json",
        "baseline.json",
        "handoffs/worker-output-schema.json",
        "handoffs/auditor-output-schema.json",
    }
    expected_paths = {str(run_directory / name) for name in expected_names}
    if set(mapping) != expected_paths:
        raise WorkflowStateError("workflow initialization artifact mapping is incomplete")


def _validate_evidence_mapping(entry: JournalEntry) -> None:
    path_updates = _state_update_path_locators(entry.state_updates)
    expected_paths = set(path_updates)
    for locator in tuple(path_updates):
        path = Path(locator)
        if path.name == "evidence.json" and path.parent.parent.name == "git":
            try:
                evidence = GitEvidence.model_validate(_read_json(path))
            except ValidationError as exc:
                raise WorkflowStateError("Git evidence artifact is invalid") from exc
            expected_paths.add(evidence.patch_artifact)
    if entry.reason == "escalation_package_written":
        if entry.state_updates or not entry.artifact_hashes:
            raise WorkflowStateError("escalation evidence journal entry is invalid")
        for locator in entry.artifact_hashes:
            path = Path(locator)
            if path.name not in {"package.json", "README.md"} or "escalation" not in path.parts:
                raise WorkflowStateError("escalation artifact locator is invalid")
        expected_paths = set(entry.artifact_hashes)
    if set(entry.artifact_hashes) != expected_paths:
        raise WorkflowStateError("journal evidence mapping omits a state artifact locator")
    for locator in entry.artifact_hashes:
        path = Path(locator)
        if path.name == "evidence.json" and path.parent.parent.name == "git":
            _verify_git_evidence_artifact(path)
        elif path.name == "suite.json" and path.parent.parent.name == "tests":
            _verify_test_suite_artifact(path)


def _state_update_path_locators(updates: Mapping[str, object]) -> set[str]:
    return {
        item
        for name, value in updates.items()
        if (name.endswith("_path") or name == "prior_audit_result_paths")
        and value is not None
        for item in (value if isinstance(value, list) else [value])
        if isinstance(item, str)
    }


def _verify_git_evidence_artifact(path: Path) -> None:
    try:
        evidence = GitEvidence.model_validate(_read_json(path))
    except ValidationError as exc:
        raise WorkflowStateError("Git evidence artifact is invalid") from exc
    patch = Path(evidence.patch_artifact)
    if patch != path.parent / "patch.txt":
        raise WorkflowStateError("Git patch locator is not exact")
    try:
        content = patch.read_bytes()
    except OSError as exc:
        raise WorkflowStateError("Git patch artifact is missing") from exc
    if len(content) != evidence.patch_stored_byte_count:
        raise WorkflowStateError("Git patch stored byte count does not match")
    if evidence.patch_complete and (
        len(content) != evidence.patch_byte_count
        or hashlib.sha256(content).hexdigest() != evidence.patch_sha256
    ):
        raise WorkflowStateError("complete Git patch evidence hash does not match")
    if not evidence.patch_complete:
        try:
            lines = content.decode("ascii").splitlines()
            marker = json.loads(lines[1])
        except (UnicodeDecodeError, IndexError, json.JSONDecodeError) as exc:
            raise WorkflowStateError("truncated Git patch marker is invalid") from exc
        if (
            len(lines) != 2
            or lines[0] != "PATCH EVIDENCE TRUNCATED; AUDIT MUST NOT RUN"
            or not isinstance(marker, dict)
            or set(marker)
            != {"complete", "patch_byte_count", "patch_sha256", "reason"}
            or marker.get("complete") is not False
            or marker.get("patch_byte_count") != evidence.patch_byte_count
            or marker.get("patch_sha256") != evidence.patch_sha256
            or marker.get("reason")
            != "patch evidence exceeds the 25 MiB workflow limit"
        ):
            raise WorkflowStateError("truncated Git patch marker contradicts evidence")


def _verify_test_suite_artifact(path: Path) -> None:
    try:
        suite = TestSuiteResult.model_validate(_read_json(path))
    except ValidationError as exc:
        raise WorkflowStateError("fixed-test suite artifact is invalid") from exc
    failed = False
    for result in suite.results:
        if failed and result.status != "skipped":
            raise WorkflowStateError("fixed-test suite did not skip after the first failure")
        if not failed and result.status == "skipped":
            raise WorkflowStateError("fixed-test suite skipped before a recorded failure")
        run_directory = path.parents[2]
        action = parse_action_record(
            _read_json(run_directory / "actions" / f"{result.action_id}.json")
        )
        if not isinstance(action, TestActionRecord) or action.result != result:
            raise WorkflowStateError("fixed-test suite contradicts its action record")
        failed = failed or not result.passed
    if suite.passed != (not failed):
        raise WorkflowStateError("fixed-test suite pass flag is contradictory")


def _validate_supporting_state_artifacts(
    run_directory: Path,
    state: WorkflowState,
    entries: Sequence[JournalEntry],
) -> None:
    _verify_initial_state_artifacts(run_directory, state)
    recorded_paths = {
        locator for entry in entries for locator in entry.artifact_hashes
    }
    state_paths = [
        state.latest_worker_result_path,
        state.latest_audit_result_path,
        state.latest_git_evidence_path,
        state.latest_tests_path,
        state.continuation_path,
        *state.prior_audit_result_paths,
    ]
    if any(path is not None and path not in recorded_paths for path in state_paths):
        raise WorkflowStateError("workflow state cites evidence absent from the journal")
    if (
        state.latest_worker_action_id is not None
        and state.latest_worker_action_id not in state.completed_action_ids
    ) or (
        state.latest_audit_action_id is not None
        and state.latest_audit_action_id not in state.completed_action_ids
    ):
        raise WorkflowStateError("workflow latest action IDs are not completed actions")
    if state.latest_git_evidence_path is not None:
        _verify_git_evidence_artifact(Path(state.latest_git_evidence_path))
    if state.latest_tests_path is not None:
        _verify_test_suite_artifact(Path(state.latest_tests_path))
    if state.status in {"completed", "checkpoint_paused"}:
        if (
            state.latest_worker_action_id is None
            or state.latest_audit_action_id is None
            or state.latest_worker_result_path is None
            or state.latest_audit_result_path is None
            or state.latest_git_evidence_path is None
            or state.latest_tests_path is None
            or not state.tests_passed
            or not state.scope_compliant
            or not state.contract_satisfied
            or state.pending_action is not None
        ):
            raise WorkflowStateError("closed workflow lacks complete supporting evidence")
        try:
            audit = AuditorModelResult.model_validate(
                _read_json(Path(state.latest_audit_result_path))
            )
        except ValidationError as exc:
            raise WorkflowStateError("closed workflow auditor result is invalid") from exc
        if audit.verdict != "pass":
            raise WorkflowStateError("closed workflow does not have a passing fresh audit")


def _verify_initial_state_artifacts(
    run_directory: Path,
    state: WorkflowState,
) -> None:
    try:
        if (run_directory / "spec.sha256").read_text(encoding="ascii") != (
            state.specification_sha256 + "\n"
        ):
            raise WorkflowStateError("frozen specification hash artifact contradicts state")
        if (run_directory / "contract.sha256").read_text(encoding="ascii") != (
            state.contract_sha256 + "\n"
        ):
            raise WorkflowStateError("frozen contract hash artifact contradicts state")
    except (OSError, UnicodeDecodeError) as exc:
        raise WorkflowStateError("frozen hash artifact is unreadable") from exc
    normalized = _read_json(run_directory / "spec.normalized.json")
    prompts = _read_json(run_directory / "prompts.sha256.json")
    try:
        baseline = GitBaseline.model_validate(
            _read_json(run_directory / "baseline.json")
        )
    except ValidationError as exc:
        raise WorkflowStateError("frozen Git baseline artifact is invalid") from exc
    if (
        normalized.get("specification_path") != state.specification_path
        or normalized.get("substage_id") != state.substage_id
        or normalized.get("workspace") != state.workspace
        or normalized.get("repository_root") != state.repository_root
        or baseline.workspace != state.workspace
        or baseline.repository_root != state.repository_root
        or baseline.head != state.baseline_commit
        or baseline.branch != state.baseline_branch
    ):
        raise WorkflowStateError("normalized specification or baseline contradicts state")
    expected_prompt_paths = {
        "worker_initial": normalized.get("worker_initial_prompt_path"),
        "worker_repair": normalized.get("worker_repair_prompt_path"),
        "auditor": normalized.get("auditor_prompt_path"),
    }
    if set(prompts) != set(expected_prompt_paths):
        raise WorkflowStateError("frozen prompt hash artifact has unsupported fields")
    for name, path in expected_prompt_paths.items():
        value = prompts.get(name)
        if (
            not isinstance(value, dict)
            or set(value) != {"path", "sha256"}
            or value.get("path") != path
            or value.get("sha256") != state.prompts_sha256.get(name)
        ):
            raise WorkflowStateError("frozen prompt hash artifact contradicts state")
    if (
        _read_json(run_directory / "handoffs/worker-output-schema.json")
        != WORKER_OUTPUT_SCHEMA
        or _read_json(run_directory / "handoffs/auditor-output-schema.json")
        != AUDITOR_OUTPUT_SCHEMA
    ):
        raise WorkflowStateError("engine-owned output schema artifact changed")


def _verify_durable_action_record(
    run_directory: Path,
    pending: PendingAction,
    state: WorkflowState | None,
) -> None:
    value = _read_json(run_directory / "actions" / f"{pending.action_id}.json")
    record = parse_action_record(value)
    if isinstance(record, CodexActionRecord):
        proof = verify_codex_artifacts(
            pending,
            known_worker_thread_id=(
                state.worker_thread_id if state is not None else None
            ),
        )
        verify_codex_action_record(record, pending, proof)
    else:
        result = verify_test_artifacts(pending)
        verify_test_action_record(record, pending, result)
        if result.status == "skipped":
            predecessor = pending.skipped_after_action_id
            if predecessor is None:
                raise WorkflowStateError("skipped fixed test has no recorded failure")
            predecessor_value = _read_json(
                run_directory / "actions" / f"{predecessor}.json"
            )
            predecessor_record = parse_action_record(predecessor_value)
            if (
                not isinstance(predecessor_record, TestActionRecord)
                or predecessor_record.result.passed
                or predecessor_record.result.status == "skipped"
                or not _skip_predecessor_matches(pending, predecessor_record)
            ):
                raise WorkflowStateError("skipped fixed test predecessor is contradictory")


def _deterministic_action_id(pending: PendingAction) -> bool:
    if pending.kind == "worker":
        return pending.action_id == f"worker-r{pending.repair_round:03d}"
    if pending.kind == "auditor":
        return pending.action_id == f"auditor-r{pending.repair_round:03d}"
    match = re.fullmatch(r"test-r(\d{3})-(\d{3})-(.+)-([0-9a-f]{8})", pending.action_id)
    if match is None or pending.test_id is None:
        return False
    component = pending.test_id[:45]
    digest = hashlib.sha256(pending.test_id.encode("utf-8")).hexdigest()[:8]
    return (
        int(match.group(1)) == pending.repair_round
        and match.group(3) == component
        and match.group(4) == digest
    )


def _skip_predecessor_matches(
    pending: PendingAction,
    predecessor: TestActionRecord,
) -> bool:
    current = re.fullmatch(r"test-r\d{3}-(\d{3})-.+-[0-9a-f]{8}", pending.action_id)
    prior = re.fullmatch(
        r"test-r\d{3}-(\d{3})-.+-[0-9a-f]{8}",
        predecessor.action_id,
    )
    return (
        current is not None
        and prior is not None
        and predecessor.repair_round == pending.repair_round
        and int(prior.group(1)) < int(current.group(1))
    )


def _parse_utc_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise WorkflowStateError("workflow journal timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise WorkflowStateError("workflow journal timestamp is not UTC")
    return parsed


def _reconcile_state_with_journal(
    run_directory: Path,
    state: WorkflowState,
) -> WorkflowState:
    """Replay only hash-validated journal updates omitted by an interrupted snapshot write."""
    entries = _read_valid_journal(run_directory)
    raw_entries = tuple(entry.model_dump(mode="json") for entry in entries)
    current = reconcile_model_snapshot(
        state,
        raw_entries,
        model=WorkflowState,
        error_factory=WorkflowStateError,
        error_message="workflow journal recovery state is invalid",
    )
    snapshots_disagree = current != state
    if not snapshots_disagree:
        try:
            snapshots_disagree = _load_result(run_directory) != current.to_result()
        except WorkflowStateError:
            snapshots_disagree = True
    if snapshots_disagree:
        _persist_state(run_directory, current)
    _validate_journal(run_directory, current)
    return current


class _WorkflowLock:
    def __init__(self, run_directory: Path, utc_now: Callable[[], datetime]) -> None:
        self.path = run_directory / LOCK_FILE
        self.utc_now = utc_now
        self.handle: IO[str] | None = None

    def __enter__(self) -> _WorkflowLock:
        try:
            handle = self.path.open("a+", encoding="utf-8")
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            with suppress(Exception):
                handle.close()
            raise WorkflowLockError("workflow is already locked by another process") from exc
        except OSError as exc:
            raise WorkflowLockError("workflow lock could not be acquired") from exc
        handle.seek(0)
        existing_text = handle.read()
        if existing_text.strip():
            try:
                existing = json.loads(existing_text)
                if (
                    not isinstance(existing, dict)
                    or set(existing)
                    != {"schema_version", "pid", "host", "started_at"}
                    or existing.get("schema_version") != 1
                ):
                    raise ValueError
                host = existing["host"]
                pid = existing["pid"]
                started_at = existing["started_at"]
                if (
                    not isinstance(host, str)
                    or not host.strip()
                    or not isinstance(pid, int)
                    or isinstance(pid, bool)
                    or pid <= 0
                    or not isinstance(started_at, str)
                ):
                    raise ValueError
                _parse_utc_timestamp(started_at)
            except (
                json.JSONDecodeError,
                KeyError,
                TypeError,
                ValueError,
                WorkflowStateError,
            ) as exc:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()
                raise WorkflowLockError("existing workflow lock metadata is invalid") from exc
            current_host = socket.gethostname()
            if host != current_host:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()
                raise WorkflowLockError("foreign-host workflow lock requires human action")
            if _pid_exists(pid):
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()
                raise WorkflowLockError("workflow lock records a live local process")
        metadata = {
            "schema_version": 1,
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "started_at": _utc_string(self.utc_now()),
        }
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps(metadata, ensure_ascii=True, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
        self.handle = handle
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self.handle is not None:
            self.handle.seek(0)
            self.handle.truncate()
            self.handle.flush()
            os.fsync(self.handle.fileno())
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()
            self.handle = None


def _load_context(
    run_directory: Path,
    state: WorkflowState,
    services: WorkflowServices,
) -> _WorkflowContext:
    _validate_journal(run_directory, state)
    _, _, sensitive_values = build_subprocess_environment(services.environ)
    prepared = load_substage_specification(
        Path(state.specification_path),
        sensitive_values=sensitive_values,
        require_clean=False,
    )
    baseline = GitBaseline.model_validate(_read_json(run_directory / "baseline.json"))
    worker_schema = run_directory / "handoffs" / "worker-output-schema.json"
    auditor_schema = run_directory / "handoffs" / "auditor-output-schema.json"
    if not worker_schema.is_file() or not auditor_schema.is_file():
        raise WorkflowStateError("engine-owned output schemas are missing")
    if (
        _read_json(worker_schema) != WORKER_OUTPUT_SCHEMA
        or _read_json(auditor_schema) != AUDITOR_OUTPUT_SCHEMA
    ):
        raise WorkflowStateError("engine-owned output schemas changed")
    context = _WorkflowContext(
        prepared=prepared,
        baseline=baseline,
        run_directory=run_directory,
        state=state,
        worker_schema=worker_schema,
        auditor_schema=auditor_schema,
        codex_executable=_resolve_codex_executable(services.codex_executable),
        services=services,
    )
    _validate_context_action_intents(context)
    return context


def _validate_context_action_intents(context: _WorkflowContext) -> None:
    """Cross-check journal intents against the still-frozen specification and policies."""
    continuation_hashes = {
        value
        for entry in _read_valid_journal(context.run_directory)
        for name, value in entry.state_updates.items()
        if name == "continuation_sha256" and isinstance(value, str)
    }
    for entry in _read_valid_journal(context.run_directory):
        if entry.event_type != "action_intent":
            continue
        try:
            pending = PendingAction.model_validate(
                entry.state_updates.get("pending_action")
            )
        except ValidationError as exc:
            raise WorkflowStateError("journal action intent is invalid") from exc
        if pending.workspace != str(context.prepared.workspace):
            raise WorkflowStateError("action intent workspace contradicts the frozen specification")
        if pending.kind in {"worker", "auditor"}:
            handoff_path = (
                context.run_directory / "handoffs" / f"{pending.action_id}.json"
            )
            expected_schema = (
                context.worker_schema
                if pending.kind == "worker"
                else context.auditor_schema
            )
            specification = context.prepared.specification
            expected_model = (
                specification.worker_model
                if pending.kind == "worker"
                else specification.auditor_model
            )
            expected_effort = (
                specification.worker_reasoning_effort
                if pending.kind == "worker"
                else specification.auditor_reasoning_effort
            )
            expected_timeout = (
                specification.worker_timeout_seconds
                if pending.kind == "worker"
                else specification.auditor_timeout_seconds
            )
            expected_artifact = context.run_directory / (
                "worker/codex" if pending.kind == "worker" else "audits/codex"
            ) / pending.action_id
            if (
                pending.handoff_path != str(handoff_path)
                or pending.output_schema_path != str(expected_schema)
                or pending.output_schema_sha256 != _sha256_path(expected_schema)
                or pending.model != expected_model
                or pending.codex_executable != context.codex_executable
                or pending.reasoning_effort != expected_effort
                or pending.timeout_seconds != expected_timeout
                or pending.artifact_path != str(expected_artifact)
            ):
                raise WorkflowStateError(
                    "Codex action intent contradicts the frozen specification"
                )
            try:
                handoff = PromptHandoff.model_validate(_read_json(handoff_path))
            except ValidationError as exc:
                raise WorkflowStateError("prompt handoff is invalid") from exc
            if handoff.contract_sha256 != context.state.contract_sha256:
                raise WorkflowStateError("prompt handoff contract hash changed")
            if pending.kind == "auditor":
                valid_source = (
                    handoff.kind == "auditor"
                    and handoff.source_sha256
                    == context.state.prompts_sha256["auditor"]
                )
            elif handoff.kind == "initial_worker":
                valid_source = (
                    pending.repair_round == 0
                    and handoff.source_sha256
                    == context.state.prompts_sha256["worker_initial"]
                )
            elif handoff.kind == "human_continuation":
                valid_source = handoff.source_sha256 in continuation_hashes
            else:
                valid_source = (
                    handoff.source_sha256
                    == context.state.prompts_sha256["worker_repair"]
                )
            if not valid_source:
                raise WorkflowStateError("prompt handoff source is not a frozen human input")
            if pending.kind == "worker" and pending.repair_round > 0:
                if pending.resume_thread_id != context.state.worker_thread_id:
                    raise WorkflowStateError("worker intent does not use the exact stored thread")
            elif pending.resume_thread_id is not None:
                raise WorkflowStateError("initial worker or auditor intent attempted resume")
            continue
        match = re.fullmatch(
            r"test-r\d{3}-(\d{3})-.+-[0-9a-f]{8}",
            pending.action_id,
        )
        if match is None:
            raise WorkflowStateError("fixed-test action ID is invalid")
        index = int(match.group(1))
        if index >= len(context.prepared.acceptance_tests):
            raise WorkflowStateError("fixed-test action index is outside the specification")
        prepared_test = context.prepared.acceptance_tests[index]
        test = prepared_test.specification
        expected_artifact = (
            context.run_directory
            / "tests"
            / f"round-{pending.repair_round:03d}"
            / pending.action_id
        )
        if (
            pending.test_id != test.id
            or pending.argv != test.argv
            or pending.cwd != str(prepared_test.cwd)
            or pending.timeout_seconds != test.timeout_seconds
            or pending.max_stdout_bytes != test.max_stdout_bytes
            or pending.max_stderr_bytes != test.max_stderr_bytes
            or pending.artifact_path != str(expected_artifact)
        ):
            raise WorkflowStateError(
                "fixed-test action intent contradicts the frozen specification"
            )


def _validate_normalized_action_intents(
    run_directory: Path,
    state: WorkflowState,
    *,
    entries: Sequence[JournalEntry] | None = None,
) -> None:
    """Cross-check status-time action identity without Git or process launches."""
    normalized = _read_json(run_directory / "spec.normalized.json")
    tests = normalized.get("acceptance_tests")
    if not isinstance(tests, list):
        raise WorkflowStateError("normalized specification tests are invalid")
    expected_workspace = normalized.get("workspace")
    journal_entries = (
        tuple(entries)
        if entries is not None
        else tuple(_read_valid_journal(run_directory))
    )
    for entry in journal_entries:
        if entry.event_type != "action_intent":
            continue
        try:
            pending = PendingAction.model_validate(
                entry.state_updates.get("pending_action")
            )
        except ValidationError as exc:
            raise WorkflowStateError("journal action intent is invalid") from exc
        if pending.workspace != expected_workspace or pending.workspace != state.workspace:
            raise WorkflowStateError("action intent workspace is not frozen")
        if pending.kind == "worker":
            expected = (
                normalized.get("worker_model"),
                normalized.get("worker_reasoning_effort"),
                normalized.get("worker_timeout_seconds"),
            )
            if (
                pending.resume_thread_id
                != (state.worker_thread_id if pending.repair_round > 0 else None)
            ):
                raise WorkflowStateError("worker action did not use the frozen exact thread ID")
        elif pending.kind == "auditor":
            expected = (
                normalized.get("auditor_model"),
                normalized.get("auditor_reasoning_effort"),
                normalized.get("auditor_timeout_seconds"),
            )
            if pending.resume_thread_id is not None:
                raise WorkflowStateError("fresh auditor action attempted session resume")
        else:
            match = re.fullmatch(
                r"test-r\d{3}-(\d{3})-.+-[0-9a-f]{8}",
                pending.action_id,
            )
            if match is None or int(match.group(1)) >= len(tests):
                raise WorkflowStateError("fixed-test action index is not frozen")
            test = tests[int(match.group(1))]
            if not isinstance(test, dict) or (
                pending.test_id,
                list(pending.argv),
                pending.cwd,
                pending.timeout_seconds,
                pending.max_stdout_bytes,
                pending.max_stderr_bytes,
            ) != (
                test.get("id"),
                test.get("argv"),
                test.get("cwd"),
                test.get("timeout_seconds"),
                test.get("max_stdout_bytes"),
                test.get("max_stderr_bytes"),
            ):
                raise WorkflowStateError("fixed-test action is not the frozen argv action")
            continue
        if (
            pending.model,
            pending.reasoning_effort,
            pending.timeout_seconds,
        ) != expected:
            raise WorkflowStateError("Codex action model policy is not frozen")


def _initialize_run_artifacts(
    run_directory: Path,
    prepared: PreparedSubstage,
    baseline: GitBaseline,
) -> None:
    for name in ("actions", "worker", "tests", "audits", "git", "handoffs", "escalation"):
        (run_directory / name).mkdir()
    _write_json(run_directory / "spec.normalized.json", prepared.normalized_dict())
    _write_text(run_directory / "spec.sha256", prepared.specification_sha256 + "\n")
    _write_text(run_directory / "contract.sha256", prepared.contract.sha256 + "\n")
    _write_json(
        run_directory / "prompts.sha256.json",
        {
            "worker_initial": {
                "path": str(prepared.worker_initial_prompt.path),
                "sha256": prepared.worker_initial_prompt.sha256,
            },
            "worker_repair": {
                "path": str(prepared.worker_repair_prompt.path),
                "sha256": prepared.worker_repair_prompt.sha256,
            },
            "auditor": {
                "path": str(prepared.auditor_prompt.path),
                "sha256": prepared.auditor_prompt.sha256,
            },
        },
    )
    _write_json(run_directory / "baseline.json", baseline.to_dict())
    _write_text(run_directory / JOURNAL_FILE, "")


def _collect_round_git_evidence(context: _WorkflowContext) -> GitEvidence:
    _, _, sensitive_values = build_subprocess_environment(context.services.environ)
    return collect_git_evidence(
        context.prepared.workspace,
        context.baseline,
        context.prepared.specification.allowed_paths,
        context.prepared.specification.protected_paths,
        context.run_directory / "git" / f"round-{context.state.repair_round:03d}",
        sensitive_values=sensitive_values,
        environ=context.services.environ,
    )


def _frozen_inputs_match(context: _WorkflowContext) -> bool:
    prepared = context.prepared
    state = context.state
    values = {
        "specification": (prepared.specification_path, state.specification_sha256),
        "contract": (prepared.contract.path, state.contract_sha256),
        "worker_initial": (
            prepared.worker_initial_prompt.path,
            state.prompts_sha256.get("worker_initial", ""),
        ),
        "worker_repair": (
            prepared.worker_repair_prompt.path,
            state.prompts_sha256.get("worker_repair", ""),
        ),
        "auditor": (prepared.auditor_prompt.path, state.prompts_sha256.get("auditor", "")),
    }
    for path, expected in values.values():
        try:
            if sha256_regular_file(path) != expected:
                return False
        except WorkflowStateError:
            return False
    return True


def _repository_matches(context: _WorkflowContext) -> bool:
    try:
        current = record_git_baseline(
            context.prepared.workspace,
            environ=context.services.environ,
        )
    except (WorkflowInputError, WorkflowDependencyError):
        return False
    return (
        current.repository_root == context.baseline.repository_root
        and current.head == context.baseline.head
        and current.branch == context.baseline.branch
    )


def _raw_frozen_inputs_match(run_directory: Path, state: WorkflowState) -> bool:
    """Check frozen source hashes without trusting or reparsing changed source YAML."""
    try:
        normalized = _read_json(run_directory / "spec.normalized.json")
        paths = {
            "specification": (state.specification_path, state.specification_sha256),
            "contract": (normalized["contract_path"], state.contract_sha256),
            "worker_initial": (
                normalized["worker_initial_prompt_path"],
                state.prompts_sha256["worker_initial"],
            ),
            "worker_repair": (
                normalized["worker_repair_prompt_path"],
                state.prompts_sha256["worker_repair"],
            ),
            "auditor": (
                normalized["auditor_prompt_path"],
                state.prompts_sha256["auditor"],
            ),
        }
        for value, expected in paths.values():
            if not isinstance(value, str) or not isinstance(expected, str):
                return False
            if sha256_regular_file(Path(value)) != expected:
                return False
    except (KeyError, TypeError, WorkflowStateError):
        return False
    return True


def _raw_repository_matches(
    state: WorkflowState,
    services: WorkflowServices,
) -> bool:
    try:
        current = record_git_baseline(Path(state.workspace), environ=services.environ)
    except (WorkflowInputError, WorkflowDependencyError):
        return False
    return (
        current.repository_root == state.repository_root
        and current.head == state.baseline_commit
        and current.branch == state.baseline_branch
    )


def _pause_state_only(
    run_directory: Path,
    state: WorkflowState,
    services: WorkflowServices,
    reason: str,
    summary: str,
) -> WorkflowState:
    """Durably pause recovery when current frozen inputs cannot build a full context."""
    paused = _journal_event(
        run_directory,
        state,
        event_type="transition",
        previous_state=state.status,
        new_state="human_paused",
        action_id=None,
        action_kind=None,
        reason=reason,
        artifact_hashes={},
        updates={
            "status": "human_paused",
            "pause_reason": reason,
            "summary": summary,
        },
        utc_now=services.utc_now,
    )
    package = {
        "schema_version": 1,
        "substage_id": paused.substage_id,
        "status": paused.status,
        "repair_round": paused.repair_round,
        "reason": reason,
        "summary": summary,
        "worker_thread_id": paused.worker_thread_id,
        "updated_at": paused.updated_at,
    }
    escalation_directory = (
        run_directory
        / "escalation"
        / f"{paused.journal_sequence:06d}-{reason}"
    )
    package_path = escalation_directory / "package.json"
    readme_path = escalation_directory / "README.md"
    _write_json(package_path, package)
    _write_text(
        readme_path,
        "\n".join(
            (
                "# Workflow escalation",
                "",
                "- Status: `human_paused`",
                f"- Reason: `{reason}`",
                f"- Summary: {summary}",
                "",
            )
        ),
    )
    root_package = run_directory / "escalation" / "package.json"
    root_readme = run_directory / "escalation" / "README.md"
    mirror_paths: tuple[Path, ...] = ()
    if not root_package.exists() and not root_readme.exists():
        _write_json(root_package, package)
        _write_text(root_readme, readme_path.read_text(encoding="utf-8"))
        mirror_paths = (root_package, root_readme)
    return _journal_event(
        run_directory,
        paused,
        event_type="evidence",
        previous_state=paused.status,
        new_state=paused.status,
        action_id=None,
        action_kind=None,
        reason="escalation_package_written",
        artifact_hashes={
            str(path): _sha256_path(path)
            for path in (package_path, readme_path, *mirror_paths)
        },
        updates={},
        utc_now=services.utc_now,
    )


def _validate_runtime_durability(context: _WorkflowContext) -> None:
    """Detect any external action that touched engine-owned state or journal artifacts."""
    _validate_journal(context.run_directory, context.state)
    if _load_state(context.run_directory) != context.state:
        raise WorkflowStateError("external action changed the workflow state snapshot")
    if _load_result(context.run_directory) != context.state.to_result():
        raise WorkflowStateError("external action changed the workflow result snapshot")


def _latest_worker(context: _WorkflowContext) -> WorkerModelResult:
    path = context.state.latest_worker_result_path
    if path is None:
        raise WorkflowStateError("latest worker result is unavailable")
    try:
        return WorkerModelResult.model_validate(_read_json(Path(path)))
    except ValidationError as exc:
        raise WorkflowStateError("latest worker result is invalid") from exc


def _latest_audit(context: _WorkflowContext) -> AuditorModelResult:
    path = context.state.latest_audit_result_path
    if path is None:
        raise WorkflowStateError("latest auditor result is unavailable")
    try:
        return AuditorModelResult.model_validate(_read_json(Path(path)))
    except ValidationError as exc:
        raise WorkflowStateError("latest auditor result is invalid") from exc


def _latest_git(context: _WorkflowContext) -> GitEvidence:
    path = context.state.latest_git_evidence_path
    if path is None:
        raise WorkflowStateError("latest Git evidence is unavailable")
    try:
        return GitEvidence.model_validate(_read_json(Path(path)))
    except ValidationError as exc:
        raise WorkflowStateError("latest Git evidence is invalid") from exc


def _latest_tests(context: _WorkflowContext) -> TestSuiteResult:
    path = context.state.latest_tests_path
    if path is None:
        raise WorkflowStateError("latest fixed-test evidence is unavailable")
    try:
        return TestSuiteResult.model_validate(_read_json(Path(path)))
    except ValidationError as exc:
        raise WorkflowStateError("latest fixed-test evidence is invalid") from exc


def _optional_latest_tests(context: _WorkflowContext) -> tuple[TestAttemptResult, ...]:
    return () if context.state.latest_tests_path is None else _latest_tests(context).results


def _optional_latest_git(context: _WorkflowContext) -> GitEvidence | None:
    return None if context.state.latest_git_evidence_path is None else _latest_git(context)


def _optional_latest_audit(context: _WorkflowContext) -> AuditorModelResult | None:
    return None if context.state.latest_audit_result_path is None else _latest_audit(context)


def _prior_audits(context: _WorkflowContext) -> tuple[AuditorModelResult, ...]:
    try:
        return tuple(
            AuditorModelResult.model_validate(_read_json(Path(path)))
            for path in context.state.prior_audit_result_paths
        )
    except ValidationError as exc:
        raise WorkflowStateError("prior auditor evidence is invalid") from exc


def _test_action_id(
    repair_round: int,
    index: int,
    prepared_test: PreparedWorkflowTest,
) -> str:
    identifier = prepared_test.specification.id
    digest = hashlib.sha256(identifier.encode("utf-8")).hexdigest()[:8]
    component = identifier[:45]
    return f"test-r{repair_round:03d}-{index:03d}-{component}-{digest}"


def _test_result_from_record(record: Mapping[str, Any]) -> TestAttemptResult:
    value = record.get("result")
    try:
        return TestAttemptResult.model_validate(value)
    except ValidationError as exc:
        raise WorkflowStateError("fixed-test action record is invalid") from exc


def _adapter_result_from_record(record: Mapping[str, Any]) -> CodexRunResult:
    try:
        return CodexRunResult.model_validate(record.get("adapter_result"))
    except ValidationError as exc:
        raise WorkflowStateError("Codex action record is invalid") from exc


def _record_thread_ids(record: Mapping[str, Any]) -> tuple[str, ...]:
    value = record.get("thread_started_ids")
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        return ()
    return tuple(cast(list[str], value))


def _valid_thread_id(value: str, *, canonical: bool = False) -> bool:
    if not canonical:
        return re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}", value) is not None
    try:
        canonical_supervisor_uuid(value)
    except ValueError:
        return False
    return True


def _confidential_fragments(
    context: _WorkflowContext,
    prompt: RenderedWorkflowPrompt,
) -> tuple[str, ...]:
    fragments = [
        context.prepared.contract.content.decode("utf-8"),
        prompt.content.decode("utf-8"),
    ]
    if prompt.source_path == context.prepared.worker_initial_prompt.path:
        fragments.append(context.prepared.worker_initial_prompt.content.decode("utf-8"))
    elif prompt.source_path == context.prepared.worker_repair_prompt.path:
        fragments.append(context.prepared.worker_repair_prompt.content.decode("utf-8"))
    elif prompt.source_path == context.prepared.auditor_prompt.path:
        fragments.append(context.prepared.auditor_prompt.content.decode("utf-8"))
    elif context.continuation is not None:
        fragments.append(context.continuation.content.decode("utf-8"))
    return tuple(fragments)


def _write_escalation(
    context: _WorkflowContext,
    reason: str,
    summary: str,
) -> tuple[Path, ...]:
    root = context.run_directory / "escalation"
    directory = root / f"{context.state.journal_sequence:06d}-{reason}"
    package = {
        "schema_version": 1,
        "substage_id": context.state.substage_id,
        "status": context.state.status,
        "repair_round": context.state.repair_round,
        "reason": reason,
        "summary": summary,
        "worker_thread_id": context.state.worker_thread_id,
        "latest_worker_action_id": context.state.latest_worker_action_id,
        "latest_audit_action_id": context.state.latest_audit_action_id,
        "latest_git_evidence_path": context.state.latest_git_evidence_path,
        "latest_tests_path": context.state.latest_tests_path,
        "updated_at": context.state.updated_at,
    }
    package_path = directory / "package.json"
    readme_path = directory / "README.md"
    _write_json(package_path, package)
    markdown = "\n".join(
        (
            "# Workflow escalation",
            "",
            f"- Status: `{context.state.status}`",
            f"- Reason: `{reason}`",
            f"- Repair round: `{context.state.repair_round}`",
            f"- Summary: {summary}",
            "",
        )
    )
    _write_text(readme_path, markdown)
    root_package = root / "package.json"
    root_readme = root / "README.md"
    mirror_paths: tuple[Path, ...] = ()
    if not root_package.exists() and not root_readme.exists():
        _write_json(root_package, package)
        _write_text(root_readme, markdown)
        mirror_paths = (root_package, root_readme)
    return (package_path, readme_path, *mirror_paths)


def _frozen_artifact_hashes(
    run_directory: Path,
) -> dict[str, str]:
    names = (
        "spec.normalized.json",
        "spec.sha256",
        "contract.sha256",
        "prompts.sha256.json",
        "baseline.json",
        "handoffs/worker-output-schema.json",
        "handoffs/auditor-output-schema.json",
    )
    return {
        str(run_directory / name): _sha256_path(run_directory / name)
        for name in names
    }


def _artifact_hashes_from_updates(updates: Mapping[str, object]) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for name, value in updates.items():
        values: Sequence[object]
        if name == "prior_audit_result_paths" and isinstance(value, (tuple, list)):
            values = value
        elif name.endswith("_path"):
            values = (value,)
        else:
            continue
        for item in values:
            if not isinstance(item, str):
                continue
            path = Path(item)
            if path.is_file():
                hashes[str(path)] = _sha256_path(path)
                if path.name == "evidence.json" and path.parent.parent.name == "git":
                    evidence = GitEvidence.model_validate(_read_json(path))
                    patch = Path(evidence.patch_artifact)
                    hashes[str(patch)] = _sha256_path(patch)
    return hashes


def _write_action_record(
    run_directory: Path,
    action_id: str,
    value: Mapping[str, Any],
) -> dict[str, Any]:
    record = dict(value)
    _write_json(run_directory / "actions" / f"{action_id}.json", record)
    return record


def _read_action_record(run_directory: Path, action_id: str) -> dict[str, Any] | None:
    path = run_directory / "actions" / f"{action_id}.json"
    try:
        path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise WorkflowStateError("action record locator could not be inspected") from exc
    sha256_regular_file(path)
    record = parse_action_record(_read_json(path))
    if record.action_id != action_id or record.complete is not True:
        raise WorkflowStateError("action record is incomplete or mismatched")
    return record.model_dump(mode="json")


def _load_state(run_directory: Path) -> WorkflowState:
    try:
        return WorkflowState.model_validate(_read_json(run_directory / STATE_FILE))
    except ValidationError as exc:
        raise WorkflowStateError("workflow state snapshot is invalid") from exc


def _load_result(run_directory: Path) -> WorkflowResult:
    try:
        return WorkflowResult.model_validate(_read_json(run_directory / RESULT_FILE))
    except ValidationError as exc:
        raise WorkflowStateError("workflow result snapshot is invalid") from exc


def _persist_state(run_directory: Path, state: WorkflowState) -> None:
    commit_state_then_result(
        state_path=run_directory / STATE_FILE,
        state_value=state.model_dump(mode="json"),
        result_path=run_directory / RESULT_FILE,
        result_value=state.to_result().to_dict(),
        checkpoint=_snapshot_checkpoint,
        error_factory=WorkflowStateError,
        error_message="workflow state and result could not be committed",
        fsync_directory_callback=_fsync_directory,
    )


def _snapshot_checkpoint(name: str) -> None:
    """Deterministic no-op boundary used by crash-ordering regression tests."""
    del name


def _resolve_run_directory(path: Path) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise WorkflowInputError("workflow run directory could not be resolved") from exc
    if not resolved.is_dir():
        raise WorkflowInputError("workflow run path is not a directory")
    return resolved


def _resolve_codex_executable(value: str | None) -> str:
    executable = value or shutil.which("codex")
    if executable is None:
        raise WorkflowDependencyError("Codex executable is required")
    try:
        resolved = Path(executable).resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise WorkflowDependencyError("Codex executable could not be resolved") from exc
    if not resolved.is_file():
        raise WorkflowDependencyError("Codex executable is not a regular file")
    return str(resolved)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkflowStateError("durable JSON artifact is missing or invalid") from exc
    if not isinstance(value, dict):
        raise WorkflowStateError("durable JSON artifact root is not an object")
    return cast(dict[str, Any], value)


def _write_json(path: Path, value: object) -> None:
    atomic_write_json(
        path,
        value,
        error_factory=WorkflowStateError,
        error_message="workflow artifact could not be written",
        fsync_directory_callback=_fsync_directory,
    )


def _write_text(path: Path, value: str) -> None:
    try:
        path.write_text(value, encoding="utf-8", newline="\n")
    except OSError as exc:
        raise WorkflowStateError("workflow artifact could not be written") from exc


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("ascii")


def _json_compatible(value: object) -> object:
    """Convert strict model updates to deterministic JSON-compatible values."""
    if hasattr(value, "model_dump"):
        model = cast(Any, value)
        return model.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {str(key): _json_compatible(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_compatible(item) for item in value]
    return value


def _sha256_path(path: Path) -> str:
    return sha256_regular_file(path)


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(path, flags)
    primary: OSError | None = None
    try:
        os.fsync(descriptor)
    except OSError as exc:
        primary = exc
    try:
        os.close(descriptor)
    except OSError:
        if primary is None:
            raise
    if primary is not None:
        raise primary


def _pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _utc_string(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
