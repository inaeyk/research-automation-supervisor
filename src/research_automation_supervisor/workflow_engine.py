"""Crash-aware deterministic single-substage Stage 2 workflow engine."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import socket
import tempfile
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any, Protocol, cast

from pydantic import ValidationError

from research_automation_supervisor.codex_adapter import (
    build_subprocess_environment,
    run_prepared_codex,
)
from research_automation_supervisor.codex_models import (
    ROLE_POLICIES,
    CodexRunRequest,
    CodexRunResult,
    PreparedCodexRequest,
)
from research_automation_supervisor.errors import (
    CodexAdapterError,
    WorkflowDependencyError,
    WorkflowInputError,
    WorkflowLockError,
    WorkflowStateError,
)
from research_automation_supervisor.git_evidence import (
    GitBaseline,
    GitEvidence,
    collect_git_evidence,
    record_git_baseline,
)
from research_automation_supervisor.redaction import redact_text, would_redact_text
from research_automation_supervisor.test_runner import (
    TestAttemptResult,
    TestSuiteResult,
    run_test_attempt,
    skipped_test_result,
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
    parse_auditor_result,
    parse_worker_result,
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


@dataclass(frozen=True)
class WorkflowServices:
    """Injectable process and identity boundaries used by offline tests."""

    codex_executable: str | None = None
    codex_invoker: CodexInvoker = run_prepared_codex
    test_invoker: TestInvoker = run_test_attempt
    environ: Mapping[str, str] | None = None
    token_factory: Callable[[], str] = lambda: secrets.token_hex(16)
    utc_now: Callable[[], datetime] = lambda: datetime.now(UTC)


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
            reason="workflow_initialized",
            artifact_hashes=_frozen_artifact_hashes(prepared, baseline),
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
            context.state = _pause(
                context,
                "human_paused",
                "continuation_interrupted_before_launch",
                "Human continuation launch was interrupted and will not be guessed or repeated.",
            )
            return context.state.to_result()
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


def substage_status(run_directory: Path) -> WorkflowResult:
    """Read and integrity-check durable state without writes or launches."""
    resolved = _resolve_run_directory(run_directory)
    state = _load_state(resolved)
    _validate_journal(resolved, state)
    result = _load_result(resolved)
    if result != state.to_result():
        raise WorkflowStateError("workflow state and result snapshots disagree")
    return result


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
        prompt = _worker_prompt(context)
        _write_json(context.run_directory / "handoffs" / f"{action_id}.json", prompt.manifest())
        role = "worker"
        request = _prepared_codex_request(context, action_id, role, prompt)
        stage1_parent = context.run_directory / "worker" / "codex"
        expected_artifact = stage1_parent / action_id
        context.state = _action_intent(context, action_id, "worker", expected_artifact)
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
        record = _finalize_codex_action(context, action_id, "worker", result)
        context.state = _action_completion(context, action_id, record)
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
        if len(thread_ids) != 1 or not _valid_thread_id(thread_ids[0]):
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
            or not _valid_thread_id(thread_ids[0])
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
                result = skipped_test_result(prepared_test, destination, action_id)
                record = _write_action_record(
                    context.run_directory,
                    action_id,
                    {
                        "schema_version": 1,
                        "action_id": action_id,
                        "kind": "test",
                        "complete": True,
                        "result_path": str(destination / "result.json"),
                        "result": result.to_dict(),
                    },
                )
            results.append(_test_result_from_record(record))
            continue
        if record is None:
            context.state = _action_intent(context, action_id, "test", destination)
            result = context.services.test_invoker(
                prepared_test,
                destination,
                action_id,
                environ=context.services.environ,
            )
            _validate_runtime_durability(context)
            record = _write_action_record(
                context.run_directory,
                action_id,
                {
                    "schema_version": 1,
                    "action_id": action_id,
                    "kind": "test",
                    "complete": True,
                    "result_path": str(destination / "result.json"),
                    "result": result.to_dict(),
                },
            )
            context.state = _action_completion(context, action_id, record)
        result = _test_result_from_record(record)
        results.append(result)
        failed = not result.passed
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
        prompt = build_auditor_prompt(
            context.prepared,
            context.baseline,
            git_evidence,
            patch_bytes,
            worker,
            tests,
            prior,
        )
        _write_json(context.run_directory / "handoffs" / f"{action_id}.json", prompt.manifest())
        request = _prepared_codex_request(context, action_id, "auditor", prompt)
        stage1_parent = context.run_directory / "audits" / "codex"
        expected_artifact = stage1_parent / action_id
        context.state = _action_intent(context, action_id, "auditor", expected_artifact)
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
        record = _finalize_codex_action(context, action_id, "auditor", result)
        context.state = _action_completion(context, action_id, record)
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
        final_state = "checkpoint_paused" if context.state.checkpoint_after else "completed"
        reason = "auditor_passed_checkpoint" if context.state.checkpoint_after else "auditor_passed"
        context.state = _transition(
            context,
            final_state,
            reason,
            contract_satisfied=True,
            pause_reason=("Human checkpoint required." if context.state.checkpoint_after else None),
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
    action_id: str,
    kind: str,
    result: CodexRunResult,
) -> dict[str, Any]:
    artifact = Path(result.artifact_directory)
    metadata = _read_json(artifact / "metadata.json")
    thread_ids_value = metadata.get("thread_started_ids")
    thread_ids = (
        [item for item in thread_ids_value if isinstance(item, str)]
        if isinstance(thread_ids_value, list)
        else []
    )
    structured_path: str | None = None
    structured_valid = False
    if result.status == "succeeded":
        try:
            final_bytes = (artifact / "final-message.md").read_bytes()
            parsed = (
                parse_worker_result(final_bytes)
                if kind == "worker"
                else parse_auditor_result(final_bytes)
            )
            destination = context.run_directory / (
                "worker" if kind == "worker" else "audits"
            ) / f"{action_id}.structured.json"
            _write_json(destination, parsed.model_dump(mode="json"))
            structured_path = str(destination)
            structured_valid = True
        except (OSError, WorkflowInputError):
            structured_valid = False
    record = {
        "schema_version": 1,
        "action_id": action_id,
        "kind": kind,
        "complete": True,
        "stage1_artifact_directory": str(artifact),
        "adapter_result": result.to_dict(),
        "thread_started_ids": thread_ids,
        "structured_result_valid": structured_valid,
        "structured_result_path": structured_path,
    }
    return _write_action_record(context.run_directory, action_id, record)


def _action_intent(
    context: _WorkflowContext,
    action_id: str,
    kind: str,
    artifact: Path,
) -> WorkflowState:
    pending = PendingAction(
        action_id=action_id,
        kind=cast(Any, kind),
        artifact_path=str(artifact),
        started_at=_utc_string(context.services.utc_now()),
    )
    return _journal_event(
        context.run_directory,
        context.state,
        event_type="action_intent",
        previous_state=context.state.status,
        new_state=context.state.status,
        action_id=action_id,
        reason=f"{kind}_action_intent",
        artifact_hashes={},
        updates={"pending_action": pending},
        utc_now=context.services.utc_now,
    )


def _action_completion(
    context: _WorkflowContext,
    action_id: str,
    record: Mapping[str, Any],
) -> WorkflowState:
    completed = tuple(dict.fromkeys((*context.state.completed_action_ids, action_id)))
    return _journal_event(
        context.run_directory,
        context.state,
        event_type="action_completion",
        previous_state=context.state.status,
        new_state=context.state.status,
        action_id=action_id,
        reason="external_action_completed",
        artifact_hashes={
            "action_record": _sha256_path(
                context.run_directory / "actions" / f"{action_id}.json"
            )
        },
        updates={"pending_action": None, "completed_action_ids": completed},
        utc_now=context.services.utc_now,
    )


def _recover_pending_action(context: _WorkflowContext) -> WorkflowState:
    pending = context.state.pending_action
    if pending is None:
        return context.state
    record = _read_action_record(context.run_directory, pending.action_id)
    if record is None and pending.kind in {"worker", "auditor"}:
        artifact = Path(pending.artifact_path)
        required = (
            "request.normalized.json",
            "prompt.sha256",
            "events.jsonl",
            "stderr.log",
            "final-message.md",
            "metadata.json",
            "result.json",
        )
        if all((artifact / name).is_file() for name in required):
            try:
                adapter_result = CodexRunResult.model_validate(
                    _read_json(artifact / "result.json")
                )
            except (ValidationError, WorkflowStateError):
                adapter_result = None
            if adapter_result is not None and adapter_result.artifact_directory == str(artifact):
                record = _finalize_codex_action(
                    context,
                    pending.action_id,
                    pending.kind,
                    adapter_result,
                )
    if record is None and pending.kind == "test":
        result_path = Path(pending.artifact_path) / "result.json"
        if result_path.is_file():
            try:
                test_result = TestAttemptResult.model_validate(_read_json(result_path))
            except (ValidationError, WorkflowStateError):
                test_result = None
            if test_result is not None and test_result.action_id == pending.action_id:
                record = _write_action_record(
                    context.run_directory,
                    pending.action_id,
                    {
                        "schema_version": 1,
                        "action_id": pending.action_id,
                        "kind": "test",
                        "complete": True,
                        "result_path": str(result_path),
                        "result": test_result.to_dict(),
                    },
                )
    if record is None or record.get("complete") is not True:
        return _pause(
            context,
            "human_paused",
            "uncertain_in_flight_action",
            "An action intent has no complete artifact set; execution will not be guessed.",
        )
    return _action_completion(context, pending.action_id, record)


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
        "human_paused": {"worker_running", "aborted"},
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
        reason=reason,
        artifact_hashes={},
        updates=values,
        utc_now=context.services.utc_now,
    )
    context.state = state
    _write_escalation(context, reason, sanitized_summary)
    return state


def _journal_event(
    run_directory: Path,
    state: WorkflowState,
    *,
    event_type: str,
    previous_state: str | None,
    new_state: str,
    action_id: str | None,
    reason: str,
    artifact_hashes: Mapping[str, str],
    updates: Mapping[str, object],
    utc_now: Callable[[], datetime],
) -> WorkflowState:
    timestamp = _utc_string(utc_now())
    sequence = state.journal_sequence + 1
    body = {
        "schema_version": 1,
        "sequence": sequence,
        "event_type": event_type,
        "previous_state": previous_state,
        "new_state": new_state,
        "action_id": action_id,
        "timestamp": timestamp,
        "reason": reason,
        "artifact_hashes": dict(sorted(artifact_hashes.items())),
        "state_updates": _json_compatible(dict(updates)),
        "previous_hash": state.journal_hash,
    }
    entry_hash = hashlib.sha256(_canonical_json(body)).hexdigest()
    entry = {**body, "entry_hash": entry_hash}
    journal = run_directory / JOURNAL_FILE
    try:
        with journal.open("ab") as handle:
            handle.write(_canonical_json(entry))
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise WorkflowStateError("workflow journal could not be appended") from exc
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
    previous_hash = entries[-1]["entry_hash"] if entries else ZERO_HASH
    if sequence != state.journal_sequence or previous_hash != state.journal_hash:
        raise WorkflowStateError("workflow state does not agree with the journal head")


def _read_valid_journal(run_directory: Path) -> list[dict[str, Any]]:
    journal = run_directory / JOURNAL_FILE
    previous_hash = ZERO_HASH
    try:
        lines = journal.read_bytes().splitlines()
    except OSError as exc:
        raise WorkflowStateError("workflow journal could not be read") from exc
    entries: list[dict[str, Any]] = []
    for sequence, raw in enumerate(lines, start=1):
        try:
            value = json.loads(raw.decode("ascii"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WorkflowStateError("workflow journal is malformed") from exc
        if not isinstance(value, dict):
            raise WorkflowStateError("workflow journal entry is not an object")
        entry_hash = value.get("entry_hash")
        body = {key: item for key, item in value.items() if key != "entry_hash"}
        if body.get("sequence") != sequence or body.get("previous_hash") != previous_hash:
            raise WorkflowStateError("workflow journal sequence or hash chain is invalid")
        computed = hashlib.sha256(_canonical_json(body)).hexdigest()
        if entry_hash != computed:
            raise WorkflowStateError("workflow journal hash chain is invalid")
        previous_hash = computed
        entries.append(cast(dict[str, Any], value))
    return entries


def _reconcile_state_with_journal(
    run_directory: Path,
    state: WorkflowState,
) -> WorkflowState:
    """Replay only hash-validated journal updates omitted by an interrupted snapshot write."""
    entries = _read_valid_journal(run_directory)
    if state.journal_sequence > len(entries):
        raise WorkflowStateError("workflow state is ahead of its journal")
    if state.journal_sequence:
        recorded_hash = entries[state.journal_sequence - 1].get("entry_hash")
        if recorded_hash != state.journal_hash:
            raise WorkflowStateError("workflow state journal position is invalid")
    current = state
    for entry in entries[state.journal_sequence :]:
        updates = entry.get("state_updates")
        timestamp = entry.get("timestamp")
        sequence = entry.get("sequence")
        entry_hash = entry.get("entry_hash")
        if (
            not isinstance(updates, dict)
            or not isinstance(timestamp, str)
            or not isinstance(sequence, int)
            or not isinstance(entry_hash, str)
        ):
            raise WorkflowStateError("workflow journal recovery data is invalid")
        candidate = current.model_dump(mode="json")
        candidate.update(updates)
        candidate.update(
            {
                "updated_at": timestamp,
                "journal_sequence": sequence,
                "journal_hash": entry_hash,
            }
        )
        try:
            current = WorkflowState.model_validate(candidate)
        except ValidationError as exc:
            raise WorkflowStateError("workflow journal recovery state is invalid") from exc
    snapshots_disagree = current != state
    if not snapshots_disagree:
        try:
            snapshots_disagree = _load_result(run_directory) != current.to_result()
        except WorkflowStateError:
            snapshots_disagree = True
    if snapshots_disagree:
        _persist_state(run_directory, current)
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
                host = existing["host"]
                pid = existing["pid"]
                if not isinstance(host, str) or not isinstance(pid, int):
                    raise ValueError
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()
                raise WorkflowLockError("existing workflow lock metadata is invalid") from exc
            current_host = socket.gethostname()
            if host != current_host:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()
                raise WorkflowLockError("foreign-host workflow lock requires human action")
            if pid != os.getpid() and _pid_exists(pid):
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
    return _WorkflowContext(
        prepared=prepared,
        baseline=baseline,
        run_directory=run_directory,
        state=state,
        worker_schema=worker_schema,
        auditor_schema=auditor_schema,
        codex_executable=_resolve_codex_executable(services.codex_executable),
        services=services,
    )


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
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                return False
        except OSError:
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
            if hashlib.sha256(Path(value).read_bytes()).hexdigest() != expected:
                return False
    except (KeyError, OSError, TypeError, WorkflowStateError):
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
    _write_json(run_directory / "escalation" / "package.json", package)
    _write_text(
        run_directory / "escalation" / "README.md",
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
    return paused


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


def _valid_thread_id(value: str) -> bool:
    return re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}", value) is not None


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


def _write_escalation(context: _WorkflowContext, reason: str, summary: str) -> None:
    directory = context.run_directory / "escalation"
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
    _write_json(directory / "package.json", package)
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
    _write_text(directory / "README.md", markdown)


def _frozen_artifact_hashes(
    prepared: PreparedSubstage,
    baseline: GitBaseline,
) -> dict[str, str]:
    return {
        "specification": prepared.specification_sha256,
        "contract": prepared.contract.sha256,
        "worker_initial_prompt": prepared.worker_initial_prompt.sha256,
        "worker_repair_prompt": prepared.worker_repair_prompt.sha256,
        "auditor_prompt": prepared.auditor_prompt.sha256,
        "baseline": hashlib.sha256(_canonical_json(baseline.to_dict())).hexdigest(),
    }


def _artifact_hashes_from_updates(updates: Mapping[str, object]) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for name, value in updates.items():
        if not name.endswith("_path") or not isinstance(value, str):
            continue
        path = Path(value)
        if path.is_file():
            hashes[name] = _sha256_path(path)
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
    if not path.exists():
        return None
    value = _read_json(path)
    if value.get("action_id") != action_id or value.get("complete") is not True:
        raise WorkflowStateError("action record is incomplete or mismatched")
    return value


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
    _write_json(run_directory / STATE_FILE, state.model_dump(mode="json"))
    _write_json(run_directory / RESULT_FILE, state.to_result().to_dict())


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
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        indent=2,
        sort_keys=True,
    ) + "\n"
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        with suppress(FileNotFoundError):
            temporary.unlink()


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
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise WorkflowStateError("workflow artifact hash could not be computed") from exc


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


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
