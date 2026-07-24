"""Durable retrospective blind supervisor calibration engine."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import posixpath
import re
import secrets
import shutil
import socket
import stat
import tempfile
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any, Protocol, TypeVar, cast
from uuid import UUID

from pydantic import BaseModel, ValidationError

from research_automation_supervisor.codex_adapter import (
    DEFAULT_LIMITS,
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
    ShadowConfidentialityError,
    ShadowDependencyError,
    ShadowInputError,
    ShadowIntegrityError,
    ShadowLockError,
    ShadowStateError,
    WorkflowDependencyError,
)
from research_automation_supervisor.redaction import (
    would_redact_text,
)
from research_automation_supervisor.shadow_confidentiality import (
    preflight_shadow_confidentiality,
    preflight_shadow_locator,
)
from research_automation_supervisor.shadow_models import (
    BlindInputManifest,
    DeterministicAssessment,
    HumanReview,
    NormalizedSupervisorProposal,
    PathScopeFinding,
    PendingSupervisorAction,
    ProposalComparison,
    RequiredCheckCoverage,
    ShadowJournalEntry,
    ShadowResult,
    ShadowState,
    SupervisorActionRecord,
    SupervisorProposal,
)
from research_automation_supervisor.shadow_prompts import (
    RenderedBlindPrompt,
    build_blind_supervisor_prompt,
)
from research_automation_supervisor.shadow_review import (
    calculate_readiness,
    load_shadow_review,
)
from research_automation_supervisor.shadow_sources import (
    DecisionReconstruction,
    PreparedShadowSpecification,
    decision_points_artifact,
    load_shadow_specification,
)
from research_automation_supervisor.workflow_integrity import (
    STAGE1_CORE_ARTIFACT_NAMES,
    STAGE2_STAGE1_ARTIFACT_NAMES,
    CodexMetadata,
    NormalizedCodexRequest,
    Stage2CompletionManifest,
    _parse_events,
    _verify_codex_process_result,
    sha256_regular_file,
    verify_hash_mapping,
)
from research_automation_supervisor.workflow_models import (
    path_matches_any,
)

ZERO_HASH = "0" * 64
STATE_FILE = "state.json"
RESULT_FILE = "result.json"
JOURNAL_FILE = "journal.jsonl"
LOCK_FILE = "shadow.lock"

MUTABLE_STATE_FIELDS = frozenset(
    {
        "status",
        "supervisor_session_id",
        "current_decision_index",
        "completed_action_ids",
        "proposal_ids",
        "reviewed_proposal_ids",
        "pending_action",
        "pause_reason",
        "summary",
    }
)


class SupervisorInvoker(Protocol):
    """Injectable Stage 1 launch boundary used by offline fake-agent tests."""

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


@dataclass(frozen=True)
class ShadowServices:
    """Injectable process, identity, token, and clock boundaries."""

    codex_executable: str | None = None
    supervisor_invoker: SupervisorInvoker = run_prepared_codex
    environ: Mapping[str, str] | None = None
    token_factory: Callable[[], str] = lambda: secrets.token_hex(16)
    utc_now: Callable[[], datetime] = lambda: datetime.now(UTC)


DEFAULT_SHADOW_SERVICES = ShadowServices()


@dataclass
class _ShadowContext:
    prepared: PreparedShadowSpecification
    run_directory: Path
    state: ShadowState
    codex_executable: str
    services: ShadowServices


@dataclass(frozen=True)
class _SupervisorProof:
    adapter_result: CodexRunResult
    metadata: CodexMetadata
    session_ids: tuple[str, ...]
    proposal: NormalizedSupervisorProposal | None
    final_bytes: bytes
    artifact_hashes: dict[str, str]


def _sensitive_values(
    environ: Mapping[str, str] | None,
) -> tuple[str, ...]:
    _, _, sensitive_values = build_subprocess_environment(environ)
    return sensitive_values


def _initial_state(
    run_directory: Path,
    prepared: PreparedShadowSpecification,
    *,
    token: str,
    timestamp: str,
) -> ShadowState:
    return ShadowState(
        calibration_id=prepared.specification.calibration_id,
        run_token=token,
        status="initialized",
        shadow_specification_path=str(prepared.specification_path),
        shadow_specification_sha256=prepared.specification_sha256,
        policy_path=str(prepared.policy.path),
        policy_sha256=prepared.policy.sha256,
        context_hashes=tuple(
            context.manifest() for context in prepared.contexts
        ),
        source_stage2_run=str(prepared.source.run_directory),
        source_stage2_state_sha256=sha256_regular_file(
            prepared.source.run_directory / "state.json"
        ),
        source_stage2_journal_sha256=sha256_regular_file(
            prepared.source.run_directory / "journal.jsonl"
        ),
        source_substage_id=prepared.source.state.substage_id,
        supervisor_model=prepared.specification.supervisor_model,
        supervisor_reasoning_effort=(
            prepared.specification.supervisor_reasoning_effort
        ),
        supervisor_session_id=None,
        decision_count=len(prepared.source.decisions),
        current_decision_index=0,
        completed_action_ids=(),
        proposal_ids=(),
        reviewed_proposal_ids=(),
        pending_action=None,
        max_proposal_bytes=prepared.specification.max_proposal_bytes,
        minimum_reviewed_proposals=(
            prepared.specification.minimum_reviewed_proposals
        ),
        required_consecutive_acceptable=(
            prepared.specification.required_consecutive_acceptable
        ),
        artifact_directory=str(run_directory),
        pause_reason=None,
        summary="Shadow calibration initialized.",
        journal_sequence=0,
        journal_hash=ZERO_HASH,
        started_at=timestamp,
        updated_at=timestamp,
    )


def _preflight_prospective_run(
    run_directory: Path,
    prepared: PreparedShadowSpecification,
    state: ShadowState,
    *,
    executable: str,
    sensitive_values: Sequence[str],
) -> None:
    """Prove every known run/dependency path before creating the run root."""
    payloads = _initial_artifact_payloads(prepared)
    values: list[object] = [
        executable,
        prepared.normalized_dict(),
        prepared.source.identity_record(),
        decision_points_artifact(prepared.source.decisions),
        state,
        _result_for_state(state, prepared),
        tuple(
            (str(run_directory / name), content)
            for name, content in payloads.items()
        ),
        tuple(
            str(run_directory / name)
            for name in (
                "supervisor",
                "proposals",
                "comparisons",
                "reviews",
                "reports",
                "escalation",
                STATE_FILE,
                RESULT_FILE,
                JOURNAL_FILE,
                LOCK_FILE,
            )
        ),
    ]
    for decision in prepared.source.decisions:
        rendered = build_blind_supervisor_prompt(
            prepared,
            decision,
            sensitive_values=sensitive_values,
        )
        proposal_id = decision.point.decision_id
        action_id = f"supervisor-{proposal_id}"
        proposal_directory = run_directory / "proposals" / proposal_id
        comparison_directory = run_directory / "comparisons" / proposal_id
        values.append(
            (
                decision.point,
                rendered.manifest,
                rendered.output_schema,
                {
                    "action_id": action_id,
                    "proposal_id": proposal_id,
                    "workspace": prepared.source.state.workspace,
                    "codex_executable": executable,
                    "proposal_directory": str(proposal_directory),
                    "blind_manifest_path": str(
                        proposal_directory / "blind-input-manifest.json"
                    ),
                    "output_schema_path": str(
                        proposal_directory / "output-schema.json"
                    ),
                    "stage1_artifact_directory": str(
                        proposal_directory / "stage1-run"
                    ),
                    "supervisor_record_path": str(
                        run_directory / "supervisor" / f"{action_id}.json"
                    ),
                    "supervisor_result_path": str(
                        proposal_directory / "supervisor-result.json"
                    ),
                    "candidate_prompt_path": str(
                        proposal_directory / "candidate-prompt.md"
                    ),
                    "assessment_path": str(
                        proposal_directory / "assessment.json"
                    ),
                    "comparison_directory": str(comparison_directory),
                    "comparison_path": str(
                        comparison_directory / "comparison.json"
                    ),
                    "authoritative_source_path": str(
                        comparison_directory / "authoritative-source.md"
                    ),
                    "authoritative_rendered_path": str(
                        comparison_directory / "authoritative-rendered.md"
                    ),
                    "review_path": str(
                        run_directory / "reviews" / f"{proposal_id}.json"
                    ),
                },
            )
        )
    preflight_shadow_confidentiality(
        values,
        sensitive_values,
        label="prospective shadow run structure",
    )


def validate_shadow_spec(
    path: Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> PreparedShadowSpecification:
    """Validate Stage 3 inputs without writes or model launches."""
    return load_shadow_specification(path, environ=environ)


def run_shadow_calibration(
    path: Path,
    *,
    runs_dir: Path = Path("runs/shadow"),
    services: ShadowServices = DEFAULT_SHADOW_SERVICES,
) -> ShadowResult:
    """Create and synchronously drive one retrospective calibration run."""
    sensitive_values = _sensitive_values(services.environ)
    raw_path = preflight_shadow_locator(
        path,
        sensitive_values,
        label="shadow specification locator",
    )
    raw_runs_dir = preflight_shadow_locator(
        runs_dir,
        sensitive_values,
        label="shadow runs directory locator",
    )
    if services.codex_executable is not None:
        preflight_shadow_confidentiality(
            services.codex_executable,
            sensitive_values,
            label="configured Codex executable locator",
        )
    prepared = load_shadow_specification(
        Path(raw_path), environ=services.environ
    )
    executable = _resolve_codex_executable(
        services.codex_executable,
        sensitive_values=sensitive_values,
    )
    token = services.token_factory()
    if (
        not token
        or len(token) > 80
        or not token.replace("-", "").replace("_", "").isalnum()
    ):
        raise ShadowInputError("shadow run token is invalid")
    try:
        resolved_runs = Path(raw_runs_dir).resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ShadowInputError(
            "shadow runs directory could not be resolved"
        ) from exc
    run_directory = (
        resolved_runs
        / f"{prepared.specification.calibration_id}-{token}"
    )
    preflight_shadow_confidentiality(
        (
            raw_runs_dir,
            str(resolved_runs),
            str(run_directory),
            token,
            executable,
        ),
        sensitive_values,
        label="prospective shadow run path",
    )
    now = _utc_string(services.utc_now())
    state = _initial_state(
        run_directory,
        prepared,
        token=token,
        timestamp=now,
    )
    _preflight_prospective_run(
        run_directory,
        prepared,
        state,
        executable=executable,
        sensitive_values=sensitive_values,
    )
    try:
        resolved_runs.mkdir(parents=True, exist_ok=True)
        run_directory.mkdir(exist_ok=False)
    except FileExistsError as exc:
        raise ShadowInputError(
            "exclusive shadow run directory already exists"
        ) from exc
    except OSError as exc:
        raise ShadowInputError(
            "shadow run directory could not be created"
        ) from exc

    _initialize_artifacts(
        run_directory,
        prepared,
        sensitive_values=sensitive_values,
    )
    _persist_state(
        run_directory,
        state,
        prepared,
        sensitive_values=sensitive_values,
    )
    state = _journal_event(
        run_directory,
        state,
        prepared,
        event_type="transition",
        previous_state=None,
        new_state="initialized",
        action_id=None,
        proposal_id=None,
        reason="shadow_initialized",
        artifact_hashes=_initial_artifact_hashes(run_directory),
        updates={},
        utc_now=services.utc_now,
        sensitive_values=sensitive_values,
    )
    with _ShadowLock(run_directory, services.utc_now):
        context = _ShadowContext(
            prepared=prepared,
            run_directory=run_directory,
            state=state,
            codex_executable=executable,
            services=services,
        )
        context.state = _transition(
            context,
            "reconstructing",
            "decision_points_reconstructed",
            summary=(
                f"Reconstructed {len(prepared.source.decisions)} "
                "verified Stage 2 decision points."
            ),
        )
        return _drive(context)


def resume_shadow_calibration(
    run_directory: Path,
    *,
    services: ShadowServices = DEFAULT_SHADOW_SERVICES,
) -> ShadowResult:
    """Recover an interrupted active Stage 3 run without duplicate launch."""
    sensitive_values = _sensitive_values(services.environ)
    raw_run = preflight_shadow_locator(
        run_directory,
        sensitive_values,
        label="shadow run directory locator",
    )
    if services.codex_executable is not None:
        preflight_shadow_confidentiality(
            services.codex_executable,
            sensitive_values,
            label="configured Codex executable locator",
        )
    resolved = _resolve_run_directory(Path(raw_run))
    with _ShadowLock(resolved, services.utc_now):
        raw_state = _load_state(resolved)
        _preflight_durable_run(
            resolved, sensitive_values=sensitive_values
        )
        prepared = _reload_prepared(raw_state, services)
        _validate_state_result_agreement(
            resolved, raw_state, prepared
        )
        state = _reconcile_state(
            resolved,
            raw_state,
            prepared,
            sensitive_values=sensitive_values,
        )
        _validate_run(
            resolved,
            state,
            prepared,
            sensitive_values=sensitive_values,
        )
        if state.status in {
            "awaiting_reviews",
            "completed",
            "human_paused",
            "failed",
            "aborted",
        }:
            raise ShadowInputError(
                "shadow calibration state cannot be resumed automatically"
            )
        context = _ShadowContext(
            prepared=prepared,
            run_directory=resolved,
            state=state,
            codex_executable=_resolve_codex_executable(
                services.codex_executable,
                sensitive_values=sensitive_values,
            ),
            services=services,
        )
        if state.pending_action is not None:
            _finish_supervisor_action(context)
            if context.state.status == "human_paused":
                result = _result_for_state(context.state, prepared)
                preflight_shadow_confidentiality(
                    result,
                    sensitive_values,
                    label="shadow resume result",
                    integrity=True,
                )
                return result
        return _drive(context)


def shadow_calibration_status(run_directory: Path) -> ShadowResult:
    """Read and integrity-check Stage 3 state without writes or launches."""
    sensitive_values = _sensitive_values(None)
    raw_run = preflight_shadow_locator(
        run_directory,
        sensitive_values,
        label="shadow run directory locator",
    )
    resolved = _resolve_run_directory(Path(raw_run))
    state = _load_state(resolved)
    _preflight_durable_run(
        resolved, sensitive_values=sensitive_values
    )
    prepared = _reload_prepared(state, DEFAULT_SHADOW_SERVICES)
    _validate_run(
        resolved,
        state,
        prepared,
        sensitive_values=sensitive_values,
    )
    result = _load_result(resolved)
    preflight_shadow_confidentiality(
        result,
        sensitive_values,
        label="shadow status result",
        integrity=True,
    )
    return result


def record_shadow_review(
    run_directory: Path,
    proposal_id: str,
    review_path: Path,
    *,
    services: ShadowServices = DEFAULT_SHADOW_SERVICES,
) -> ShadowResult:
    """Record exactly one immutable semantic human review."""
    sensitive_values = _sensitive_values(services.environ)
    raw_run = preflight_shadow_locator(
        run_directory,
        sensitive_values,
        label="shadow run directory locator",
    )
    raw_review = preflight_shadow_locator(
        review_path,
        sensitive_values,
        label="shadow review locator",
    )
    preflight_shadow_confidentiality(
        proposal_id,
        sensitive_values,
        label="shadow proposal identifier",
    )
    resolved = _resolve_run_directory(Path(raw_run))
    with _ShadowLock(resolved, services.utc_now):
        raw_state = _load_state(resolved)
        _preflight_durable_run(
            resolved, sensitive_values=sensitive_values
        )
        prepared = _reload_prepared(raw_state, services)
        _validate_state_result_agreement(
            resolved, raw_state, prepared
        )
        state = _reconcile_state(
            resolved,
            raw_state,
            prepared,
            sensitive_values=sensitive_values,
        )
        _validate_run(
            resolved,
            state,
            prepared,
            allowed_unjournaled_review=proposal_id,
            sensitive_values=sensitive_values,
        )
        if state.status != "awaiting_reviews":
            raise ShadowInputError(
                "reviews may be recorded only while awaiting reviews"
            )
        if proposal_id not in state.proposal_ids:
            raise ShadowInputError("proposal does not exist")
        comparison = _load_comparison(resolved, proposal_id)
        if not comparison.comparison_available:
            raise ShadowInputError(
                "proposal comparison is unavailable"
            )
        destination = resolved / "reviews" / f"{proposal_id}.json"
        if proposal_id in state.reviewed_proposal_ids:
            raise ShadowInputError(
                "proposal already has an immutable human review"
            )
        review = load_shadow_review(
            Path(raw_review),
            sensitive_values=sensitive_values,
        )
        if review.proposal_id != proposal_id:
            raise ShadowInputError(
                "review proposal_id does not match the requested proposal"
            )
        if destination.exists():
            existing_review = _model_from_json(
                destination,
                HumanReview,
                "uncommitted shadow review",
            )
            preflight_shadow_confidentiality(
                (str(destination), existing_review),
                sensitive_values,
                label="unjournaled shadow review",
                integrity=True,
            )
            if existing_review != review:
                raise ShadowStateError(
                    "an unjournaled review exists with different content"
                )
        else:
            preflight_shadow_confidentiality(
                (str(destination), review),
                sensitive_values,
                label="durable shadow review",
            )
            _write_json(destination, review.model_dump(mode="json"))
        reviewed = (*state.reviewed_proposal_ids, proposal_id)
        comparable_ids = tuple(
            item
            for item in state.proposal_ids
            if _load_comparison(
                resolved, item
            ).comparison_available
        )
        completed = set(comparable_ids).issubset(reviewed)
        new_status = "completed" if completed else "awaiting_reviews"
        reason = (
            "review_recorded_completed"
            if completed
            else "review_recorded"
        )
        summary = (
            "All comparison-available proposals have immutable reviews."
            if completed
            else "Immutable human review recorded; more reviews are pending."
        )
        state = _journal_event(
            resolved,
            state,
            prepared,
            event_type="review",
            previous_state=state.status,
            new_state=new_status,
            action_id=None,
            proposal_id=proposal_id,
            reason=reason,
            artifact_hashes={
                str(destination): sha256_regular_file(destination)
            },
            updates={
                "reviewed_proposal_ids": reviewed,
                "status": new_status,
                "summary": summary,
            },
            utc_now=services.utc_now,
            sensitive_values=sensitive_values,
        )
        result = _result_for_state(state, prepared)
        preflight_shadow_confidentiality(
            result,
            sensitive_values,
            label="shadow review result",
            integrity=True,
        )
        return result


def shadow_calibration_report(
    run_directory: Path,
) -> dict[str, object]:
    """Return a read-only deterministic assessment/review/readiness report."""
    sensitive_values = _sensitive_values(None)
    raw_run = preflight_shadow_locator(
        run_directory,
        sensitive_values,
        label="shadow run directory locator",
    )
    resolved = _resolve_run_directory(Path(raw_run))
    state = _load_state(resolved)
    _preflight_durable_run(
        resolved, sensitive_values=sensitive_values
    )
    prepared = _reload_prepared(state, DEFAULT_SHADOW_SERVICES)
    _validate_run(
        resolved,
        state,
        prepared,
        sensitive_values=sensitive_values,
    )
    assessments, comparisons, reviews = _report_inputs(
        resolved, state
    )
    readiness = calculate_readiness(
        prepared.specification,
        state.proposal_ids,
        {
            decision.point.decision_id: decision.point.proposal_kind
            for decision in prepared.source.decisions
        },
        {
            proposal_id: comparison.comparison_available
            for proposal_id, comparison in comparisons.items()
        },
        assessments,
        reviews,
    )
    report = {
        "schema_version": 1,
        "calibration_id": state.calibration_id,
        "source_stage2_run": state.source_stage2_run,
        "status": state.status,
        "readiness": readiness.model_dump(mode="json"),
        "assessments": [
            assessments[proposal_id].model_dump(mode="json")
            for proposal_id in state.proposal_ids
            if proposal_id in assessments
        ],
        "reviews": [
            reviews[proposal_id].model_dump(mode="json")
            for proposal_id in state.proposal_ids
            if proposal_id in reviews
        ],
    }
    preflight_shadow_confidentiality(
        report,
        sensitive_values,
        label="shadow calibration report",
        integrity=True,
    )
    return report


def abort_shadow_calibration(
    run_directory: Path,
    reason: str,
    *,
    services: ShadowServices = DEFAULT_SHADOW_SERVICES,
) -> ShadowResult:
    """Atomically abort a non-running, nonterminal calibration."""
    sensitive_values = _sensitive_values(services.environ)
    raw_run = preflight_shadow_locator(
        run_directory,
        sensitive_values,
        label="shadow run directory locator",
    )
    preflight_shadow_confidentiality(
        reason,
        sensitive_values,
        label="shadow abort reason",
    )
    resolved = _resolve_run_directory(Path(raw_run))
    with _ShadowLock(resolved, services.utc_now):
        raw_state = _load_state(resolved)
        _preflight_durable_run(
            resolved, sensitive_values=sensitive_values
        )
        prepared = _reload_prepared(raw_state, services)
        _validate_state_result_agreement(
            resolved, raw_state, prepared
        )
        state = _reconcile_state(
            resolved,
            raw_state,
            prepared,
            sensitive_values=sensitive_values,
        )
        _validate_run(
            resolved,
            state,
            prepared,
            sensitive_values=sensitive_values,
        )
        if state.status in {"completed", "failed", "aborted"}:
            raise ShadowInputError(
                "terminal shadow calibrations cannot be aborted"
            )
        if state.status == "supervisor_running":
            raise ShadowInputError(
                "active supervisor termination is not available"
            )
        sanitized = " ".join(reason.split()).strip()
        if not sanitized:
            raise ShadowInputError("abort reason must not be empty")
        state = _journal_event(
            resolved,
            state,
            prepared,
            event_type="transition",
            previous_state=state.status,
            new_state="aborted",
            action_id=None,
            proposal_id=None,
            reason="human_abort",
            artifact_hashes={},
            updates={
                "status": "aborted",
                "pause_reason": sanitized[:16384],
                "summary": "Shadow calibration aborted by human request.",
            },
            utc_now=services.utc_now,
            sensitive_values=sensitive_values,
        )
        result = _result_for_state(state, prepared)
        preflight_shadow_confidentiality(
            result,
            sensitive_values,
            label="shadow abort result",
            integrity=True,
        )
        return result


def shadow_calibration_exit_code(status: str) -> int:
    """Map Stage 3 states to the frozen public exit-code contract."""
    return {
        "completed": 0,
        "awaiting_reviews": 5,
        "human_paused": 5,
        "failed": 4,
        "aborted": 8,
        "initialized": 4,
        "reconstructing": 4,
        "supervisor_running": 4,
        "proposal_validating": 4,
    }[status]


def _drive(context: _ShadowContext) -> ShadowResult:
    while True:
        _validate_source_frozen(context)
        status = context.state.status
        if status in {
            "awaiting_reviews",
            "completed",
            "human_paused",
            "failed",
            "aborted",
        }:
            result = _result_for_state(
                context.state, context.prepared
            )
            preflight_shadow_confidentiality(
                result,
                _sensitive_values(context.services.environ),
                label="shadow calibration result",
                integrity=True,
            )
            return result
        if context.state.pending_action is not None:
            raise ShadowStateError(
                "active drive encountered an unresolved supervisor action"
            )
        if status == "proposal_validating":
            context.state = _transition(
                context,
                "reconstructing",
                "proposal_finalized",
                summary="Supervisor proposal and comparison finalized.",
            )
            continue
        if status in {"initialized", "reconstructing"}:
            if (
                context.state.current_decision_index
                >= context.state.decision_count
            ):
                comparable = [
                    proposal_id
                    for proposal_id in context.state.proposal_ids
                    if _load_comparison(
                        context.run_directory, proposal_id
                    ).comparison_available
                ]
                final_status = (
                    "awaiting_reviews" if comparable else "completed"
                )
                final_reason = (
                    "all_proposals_generated"
                    if comparable
                    else "all_proposals_completed_without_reviews"
                )
                context.state = _transition(
                    context,
                    final_status,
                    final_reason,
                    summary=(
                        (
                            "All eligible shadow proposals are finalized; "
                            "structured human reviews are required."
                        )
                        if comparable
                        else (
                            "All eligible proposals are finalized and none "
                            "has an available authoritative comparison."
                        )
                    ),
                )
                continue
            _launch_next_supervisor(context)
            continue
        if status == "supervisor_running":
            _launch_next_supervisor(context)
            continue
        raise ShadowStateError("unsupported shadow calibration state")


def _launch_next_supervisor(context: _ShadowContext) -> None:
    decision = context.prepared.source.decisions[
        context.state.current_decision_index
    ]
    _, _, sensitive_values = build_subprocess_environment(
        context.services.environ
    )
    try:
        rendered = build_blind_supervisor_prompt(
            context.prepared,
            decision,
            sensitive_values=sensitive_values,
        )
    except ShadowConfidentialityError:
        context.state = _pause(
            context,
            "blind_input_confidentiality_collision",
            "Blind supervisor evidence failed confidentiality preflight; "
            "nothing was launched.",
        )
        return
    proposal_directory = (
        context.run_directory
        / "proposals"
        / decision.point.decision_id
    )
    manifest_path = proposal_directory / "blind-input-manifest.json"
    schema_path = proposal_directory / "output-schema.json"
    manifest_value = rendered.manifest.model_dump(mode="json")
    manifest_bytes = _render_json_bytes(manifest_value)
    schema_bytes = _canonical_json(rendered.output_schema)
    stage1_directory = proposal_directory / "stage1-run"
    action_id = f"supervisor-{decision.point.decision_id}"
    _, removed_names, _ = build_subprocess_environment(
        context.services.environ
    )
    pending = PendingSupervisorAction(
        action_id=action_id,
        proposal_id=decision.point.decision_id,
        proposal_kind=decision.point.proposal_kind,
        decision_index=context.state.current_decision_index,
        stage1_artifact_directory=str(stage1_directory),
        workspace=context.prepared.source.state.workspace,
        role="supervisor",
        model=context.prepared.specification.supervisor_model,
        reasoning_effort=(
            context.prepared.specification.supervisor_reasoning_effort
        ),
        timeout_seconds=(
            context.prepared.specification.supervisor_timeout_seconds
        ),
        sandbox="read-only",
        approval_policy="never",
        ephemeral=False,
        network_policy="disabled",
        codex_executable=context.codex_executable,
        prompt_sha256=rendered.manifest.rendered_blind_input_sha256,
        prompt_byte_count=(
            rendered.manifest.rendered_blind_input_byte_count
        ),
        output_schema_path=str(schema_path),
        output_schema_sha256=hashlib.sha256(schema_bytes).hexdigest(),
        blind_manifest_path=str(manifest_path),
        blind_manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        resume_session_id=context.state.supervisor_session_id,
        removed_environment_variable_names=removed_names,
        started_at=_utc_string(context.services.utc_now()),
    )
    request = _prepared_supervisor_request(
        context, rendered, decision
    )
    try:
        preflight_shadow_confidentiality(
            (
                str(proposal_directory),
                str(manifest_path),
                manifest_value,
                str(schema_path),
                rendered.output_schema,
                str(stage1_directory),
                pending,
                request.normalized_dict(),
                str(request.request_path),
                str(request.workspace),
                str(request.prompt_path),
                request.prompt_bytes,
            ),
            sensitive_values,
            label="supervisor action structure",
        )
    except ShadowConfidentialityError:
        context.state = _pause(
            context,
            "blind_input_confidentiality_collision",
            "Blind supervisor action paths or evidence failed "
            "confidentiality preflight; nothing was launched.",
        )
        return
    proposal_directory.mkdir(parents=True, exist_ok=True)
    _write_bytes(manifest_path, manifest_bytes)
    _write_bytes(schema_path, schema_bytes)
    if context.state.status != "supervisor_running":
        context.state = _transition(
            context,
            "supervisor_running",
            "supervisor_proposal_requested",
            summary=(
                f"Blind supervisor proposal requested for "
                f"{decision.point.decision_id}."
            ),
        )
    context.state = _journal_event(
        context.run_directory,
        context.state,
        context.prepared,
        event_type="action_intent",
        previous_state=context.state.status,
        new_state=context.state.status,
        action_id=action_id,
        proposal_id=decision.point.decision_id,
        reason="supervisor_action_intent",
        artifact_hashes={
            str(manifest_path): pending.blind_manifest_sha256,
            str(schema_path): pending.output_schema_sha256,
        },
        updates={"pending_action": pending},
        utc_now=context.services.utc_now,
        sensitive_values=sensitive_values,
    )
    try:
        returned = context.services.supervisor_invoker(
            request,
            runs_dir=proposal_directory,
            codex_executable=context.codex_executable,
            environ=context.services.environ,
            output_schema=schema_path,
            resume_thread_id=pending.resume_session_id,
            confidential_fragments=(),
        )
    except CodexAdapterError:
        context.state = _pause(
            context,
            "supervisor_adapter_input_or_dependency_failure",
            "The supervisor adapter could not complete safely.",
        )
        return
    _validate_runtime_durability(context)
    _finish_supervisor_action(context, returned)


def _prepared_supervisor_request(
    context: _ShadowContext,
    rendered: RenderedBlindPrompt,
    decision: DecisionReconstruction,
) -> PreparedCodexRequest:
    request = CodexRunRequest(
        schema_version=1,
        run_id="stage1-run",
        role="supervisor",
        workspace=context.prepared.source.state.workspace,
        prompt_path=str(context.prepared.policy.path),
        model=context.prepared.specification.supervisor_model,
        reasoning_effort=(
            context.prepared.specification.supervisor_reasoning_effort
        ),
        timeout_seconds=(
            context.prepared.specification.supervisor_timeout_seconds
        ),
    )
    del decision
    return PreparedCodexRequest(
        request_path=context.prepared.specification_path,
        request=request,
        workspace=Path(context.prepared.source.state.workspace),
        prompt_path=context.prepared.policy.path,
        prompt_bytes=rendered.content,
        prompt_sha256=rendered.manifest.rendered_blind_input_sha256,
        policy=ROLE_POLICIES["supervisor"],
    )


def _finish_supervisor_action(
    context: _ShadowContext,
    returned_result: CodexRunResult | None = None,
) -> None:
    pending = context.state.pending_action
    if pending is None:
        raise ShadowStateError(
            "supervisor completion has no matching intent"
        )
    decision = context.prepared.source.decisions[
        pending.decision_index
    ]
    try:
        proof = _verify_supervisor_artifacts(
            pending,
            decision,
            context.prepared.source.decisions,
        )
        if (
            returned_result is not None
            and returned_result != proof.adapter_result
        ):
            raise ShadowStateError(
                "returned supervisor result contradicts durable evidence"
            )
    except ShadowStateError:
        context.state = _pause(
            context,
            "supervisor_action_completion_unprovable",
            "The pending supervisor action has incomplete or contradictory "
            "completion evidence and will not be relaunched.",
        )
        return
    try:
        _preflight_comparison_material(
            context, pending, decision, proof
        )
    except ShadowConfidentialityError:
        context.state = _pause(
            context,
            "comparison_confidentiality_collision",
            "Authoritative comparison material failed confidentiality "
            "preflight and was not stored.",
        )
        return
    context.state = _transition(
        context,
        "proposal_validating",
        "supervisor_transport_completed",
        summary="Supervisor transport completed; output is being validated.",
    )
    session_integrity, session_id = _session_integrity(
        context.state.supervisor_session_id,
        pending.resume_session_id,
        proof.session_ids,
        context.prepared.source.model_session_uuids(),
    )
    finalized_hashes = _finalize_proposal_artifacts(
        context,
        pending,
        decision,
        proof,
        session_integrity=session_integrity,
    )
    record = SupervisorActionRecord(
        action_id=pending.action_id,
        proposal_id=pending.proposal_id,
        proposal_kind=pending.proposal_kind,
        complete=True,
        stage1_artifact_directory=(
            pending.stage1_artifact_directory
        ),
        adapter_result=proof.adapter_result,
        session_ids=proof.session_ids,
        structured_result_valid=proof.proposal is not None,
        artifact_hashes={
            **proof.artifact_hashes,
            **finalized_hashes,
        },
    )
    record_path = (
        context.run_directory
        / "supervisor"
        / f"{pending.action_id}.json"
    )
    sensitive_values = _sensitive_values(context.services.environ)
    preflight_shadow_confidentiality(
        (str(record_path), record),
        sensitive_values,
        label="supervisor action record",
    )
    _write_json(record_path, record.model_dump(mode="json"))
    completed = (
        *context.state.completed_action_ids,
        pending.action_id,
    )
    proposal_ids = (
        *context.state.proposal_ids,
        pending.proposal_id,
    )
    updates: dict[str, object] = {
        "pending_action": None,
        "completed_action_ids": completed,
        "proposal_ids": proposal_ids,
        "current_decision_index": pending.decision_index + 1,
        "supervisor_session_id": (
            session_id
            if context.state.supervisor_session_id is None
            else context.state.supervisor_session_id
        ),
    }
    context.state = _journal_event(
        context.run_directory,
        context.state,
        context.prepared,
        event_type="action_completion",
        previous_state=context.state.status,
        new_state=context.state.status,
        action_id=pending.action_id,
        proposal_id=pending.proposal_id,
        reason="supervisor_action_completed",
        artifact_hashes={
            **record.artifact_hashes,
            str(record_path): sha256_regular_file(record_path),
        },
        updates=updates,
        utc_now=context.services.utc_now,
        sensitive_values=sensitive_values,
    )
    if proof.adapter_result.status != "succeeded":
        context.state = _pause(
            context,
            f"supervisor_{proof.adapter_result.status}",
            "Supervisor transport failed and will not be retried "
            "automatically.",
        )
    elif proof.proposal is None:
        context.state = _pause(
            context,
            "supervisor_result_malformed",
            "Supervisor structured result is missing or invalid.",
        )
    elif not session_integrity:
        context.state = _pause(
            context,
            "supervisor_session_integrity_failed",
            "The persistent supervisor session ID is missing, ambiguous, "
            "changed, unavailable, or malformed.",
        )


def _verify_supervisor_artifacts(
    pending: PendingSupervisorAction,
    decision: DecisionReconstruction,
    all_decisions: Sequence[DecisionReconstruction],
) -> _SupervisorProof:
    directory = Path(pending.stage1_artifact_directory)
    _require_exact_directory(directory, STAGE2_STAGE1_ARTIFACT_NAMES)
    completion = _model_from_json(
        directory / "stage2-completion.json",
        Stage2CompletionManifest,
        "supervisor completion manifest",
    )
    core_paths = {
        str(directory / name) for name in STAGE1_CORE_ARTIFACT_NAMES
    }
    if set(completion.artifact_hashes) != core_paths:
        raise ShadowStateError(
            "supervisor completion manifest artifact set is incomplete"
        )
    try:
        verify_hash_mapping(completion.artifact_hashes)
    except Exception as exc:
        raise ShadowStateError(
            "supervisor completion hashes do not match"
        ) from exc
    request = _model_from_json(
        directory / "request.normalized.json",
        NormalizedCodexRequest,
        "normalized supervisor request",
    )
    metadata = _model_from_json(
        directory / "metadata.json",
        CodexMetadata,
        "supervisor metadata",
    )
    result = _model_from_json(
        directory / "result.json",
        CodexRunResult,
        "supervisor result",
    )
    manifest = _model_from_json(
        Path(pending.blind_manifest_path),
        BlindInputManifest,
        "blind input manifest",
    )
    if (
        request.run_id != "stage1-run"
        or request.role != "supervisor"
        or request.workspace != pending.workspace
        or request.prompt_path != manifest_path_source(pending)
        or request.model != pending.model
        or request.reasoning_effort != pending.reasoning_effort
        or request.timeout_seconds != pending.timeout_seconds
        or request.policy.sandbox != "read-only"
        or request.policy.approval != "never"
        or request.policy.ephemeral
    ):
        raise ShadowStateError(
            "normalized supervisor request contradicts its intent"
        )
    if (
        metadata.run_id != "stage1-run"
        or metadata.role != "supervisor"
        or metadata.workspace != pending.workspace
        or metadata.prompt_path != request.prompt_path
        or metadata.prompt_sha256 != pending.prompt_sha256
        or metadata.prompt_byte_count != pending.prompt_byte_count
        or metadata.model != pending.model
        or metadata.reasoning_effort != pending.reasoning_effort
        or metadata.timeout_seconds != pending.timeout_seconds
        or metadata.sandbox != "read-only"
        or metadata.approval_policy != "never"
        or metadata.ephemeral
        or metadata.artifact_directory
        != pending.stage1_artifact_directory
        or metadata.codex_executable != pending.codex_executable
        or metadata.resume_thread_id != pending.resume_session_id
        or metadata.output_schema_path != pending.output_schema_path
        or metadata.output_schema_sha256
        != pending.output_schema_sha256
        or metadata.stdout_limit_bytes != DEFAULT_LIMITS.stdout_bytes
        or metadata.stderr_limit_bytes != DEFAULT_LIMITS.stderr_bytes
        or metadata.removed_environment_variable_names
        != pending.removed_environment_variable_names
    ):
        raise ShadowStateError(
            "supervisor metadata contradicts its exact intent"
        )
    if (
        result.run_id != "stage1-run"
        or result.artifact_directory
        != pending.stage1_artifact_directory
        or completion.run_id != "stage1-run"
        or completion.role != "supervisor"
        or completion.artifact_directory
        != pending.stage1_artifact_directory
        or completion.prompt_sha256 != pending.prompt_sha256
        or completion.output_schema_path
        != pending.output_schema_path
        or completion.output_schema_sha256
        != pending.output_schema_sha256
        or completion.result_status != result.status
        or completion.completed_at != result.ended_at
    ):
        raise ShadowStateError(
            "supervisor completion identity is contradictory"
        )
    if (
        manifest.proposal_id != pending.proposal_id
        or manifest.proposal_kind != pending.proposal_kind
        or manifest.rendered_blind_input_sha256
        != pending.prompt_sha256
        or manifest.rendered_blind_input_byte_count
        != pending.prompt_byte_count
        or manifest.output_schema_sha256
        != pending.output_schema_sha256
        or sha256_regular_file(Path(pending.blind_manifest_path))
        != pending.blind_manifest_sha256
        or sha256_regular_file(Path(pending.output_schema_path))
        != pending.output_schema_sha256
    ):
        raise ShadowStateError(
            "blind manifest or fixed schema contradicts the intent"
        )
    try:
        prompt_hash = (directory / "prompt.sha256").read_bytes()
    except OSError as exc:
        raise ShadowStateError(
            "supervisor prompt hash artifact is unreadable"
        ) from exc
    if prompt_hash != f"{pending.prompt_sha256}\n".encode("ascii"):
        raise ShadowStateError(
            "supervisor prompt hash artifact changed"
        )
    events_bytes = _read_exact_bytes(directory / "events.jsonl")
    try:
        events, session_ids, first_thread, first_session = _parse_events(
            events_bytes
        )
    except Exception as exc:
        raise ShadowStateError(
            "supervisor event evidence is invalid"
        ) from exc
    explicit_session_ids = tuple(
        (
            value["thread_id"]
            if isinstance(value.get("thread_id"), str)
            else ""
        )
        for value in events
        if value.get("type") == "thread.started"
    )
    if (
        len(events) != metadata.valid_event_count
        or metadata.thread_started_ids != session_ids
        or metadata.thread_id != first_thread
        or metadata.session_id != first_session
        or result.event_count != len(events)
        or result.malformed_event_count
        != metadata.malformed_event_count
    ):
        raise ShadowStateError(
            "supervisor session evidence was not derived from events"
        )
    final_bytes = _read_exact_bytes(directory / "final-message.md")
    stderr_path = directory / "stderr.log"
    if (
        metadata.events_sha256
        != hashlib.sha256(events_bytes).hexdigest()
        or metadata.stderr_sha256
        != sha256_regular_file(stderr_path)
        or metadata.final_message_sha256
        != hashlib.sha256(final_bytes).hexdigest()
        or metadata.final_message_present
        != bool(final_bytes.decode("utf-8").strip())
        or result.final_message_present
        != metadata.final_message_present
    ):
        raise ShadowStateError(
            "supervisor captured-output hashes are contradictory"
        )
    try:
        _verify_codex_process_result(metadata, result)
    except Exception as exc:
        raise ShadowStateError(
            "supervisor process result is contradictory"
        ) from exc
    if (
        result.started_at != metadata.started_at
        or result.ended_at != metadata.ended_at
        or result.duration_seconds != metadata.duration_seconds
        or _parse_timestamp(metadata.ended_at)
        < _parse_timestamp(metadata.started_at)
        or _parse_timestamp(metadata.started_at)
        < _parse_timestamp(pending.started_at)
    ):
        raise ShadowStateError(
            "supervisor timing evidence is contradictory"
        )
    _verify_supervisor_command(pending, metadata)
    transport_proposal: SupervisorProposal | None = None
    if result.status == "succeeded":
        try:
            value = json.loads(
                final_bytes.decode("utf-8"),
                parse_constant=_reject_json_constant,
            )
            transport_proposal = SupervisorProposal.model_validate(value)
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            ValueError,
            ValidationError,
        ):
            transport_proposal = None
    proposal = (
        _normalize_supervisor_proposal(
            transport_proposal,
            Path(pending.workspace),
        )
        if transport_proposal is not None
        and transport_proposal.proposal_kind
        == decision.point.proposal_kind
        else None
    )
    _assert_no_authoritative_material(directory, all_decisions)
    artifact_hashes = {
        str(directory / name): sha256_regular_file(directory / name)
        for name in sorted(STAGE2_STAGE1_ARTIFACT_NAMES)
    }
    artifact_hashes[pending.blind_manifest_path] = (
        pending.blind_manifest_sha256
    )
    artifact_hashes[pending.output_schema_path] = (
        pending.output_schema_sha256
    )
    return _SupervisorProof(
        adapter_result=result,
        metadata=metadata,
        session_ids=explicit_session_ids,
        proposal=proposal,
        final_bytes=final_bytes,
        artifact_hashes=artifact_hashes,
    )


def _normalize_supervisor_proposal(
    proposal: SupervisorProposal,
    workspace: Path,
) -> NormalizedSupervisorProposal:
    """Build the persisted proposal used for deterministic assessment."""
    value = proposal.model_dump(mode="json")
    value["referenced_paths"] = [
        _normalize_proposal_path(path, workspace)
        for path in proposal.referenced_paths
    ]
    return NormalizedSupervisorProposal.model_validate(value)


def _normalize_proposal_path(value: str, workspace: Path) -> str:
    """Lexically normalize one transport path without rejecting its meaning."""
    normalized = posixpath.normpath(value.replace("\\", "/"))
    if _is_windows_absolute_path(normalized):
        return normalized
    if not posixpath.isabs(normalized):
        return normalized

    normalized_workspace = posixpath.normpath(
        workspace.as_posix().replace("\\", "/")
    )
    try:
        inside_workspace = (
            posixpath.commonpath(
                (normalized_workspace, normalized)
            )
            == normalized_workspace
        )
    except ValueError:
        inside_workspace = False
    if not inside_workspace:
        return normalized
    return posixpath.relpath(normalized, normalized_workspace)


def _is_windows_absolute_path(value: str) -> bool:
    return re.match(r"^[A-Za-z]:/", value) is not None


def manifest_path_source(pending: PendingSupervisorAction) -> str:
    """Recover the frozen policy locator from a blind manifest sibling run."""
    run_directory = Path(pending.blind_manifest_path).parents[2]
    try:
        state = ShadowState.model_validate(
            _read_json(run_directory / STATE_FILE)
        )
    except ValidationError as exc:
        raise ShadowStateError(
            "shadow state is invalid while proving supervisor request"
        ) from exc
    return state.policy_path


def _verify_supervisor_command(
    pending: PendingSupervisorAction,
    metadata: CodexMetadata,
) -> None:
    common = [
        "-c",
        f"model_reasoning_effort={pending.reasoning_effort}",
        "-c",
        'web_search="disabled"',
        "-c",
        "sandbox_workspace_write.network_access=false",
        "-c",
        "features.skill_mcp_dependency_install=false",
    ]
    if pending.resume_session_id is None:
        expected = [
            pending.codex_executable,
            "--ask-for-approval",
            "never",
            "exec",
            "--json",
            "--output-last-message",
            "<FINAL_MESSAGE_TEMP>",
            "--model",
            pending.model,
            *common,
            "--sandbox",
            "read-only",
            "--ignore-user-config",
            "--ignore-rules",
            "--strict-config",
            "--cd",
            pending.workspace,
        ]
    else:
        expected = [
            pending.codex_executable,
            "--ask-for-approval",
            "never",
            "--sandbox",
            "read-only",
            "--cd",
            pending.workspace,
            "exec",
            "resume",
            pending.resume_session_id,
            "--json",
            "--output-last-message",
            "<FINAL_MESSAGE_TEMP>",
            "--model",
            pending.model,
            *common,
            "--ignore-user-config",
            "--ignore-rules",
            "--strict-config",
        ]
    expected.extend(
        [
            "--output-schema",
            pending.output_schema_path,
            "<PROMPT_FROM_STDIN>",
        ]
    )
    if metadata.command != tuple(expected):
        raise ShadowStateError(
            "supervisor command does not preserve the frozen policy"
        )
    if "--last" in metadata.command or "--all" in metadata.command:
        raise ShadowStateError(
            "supervisor command used recency-based resume"
        )


def _assert_no_authoritative_material(
    stage1_directory: Path,
    decisions: Sequence[DecisionReconstruction],
) -> None:
    forbidden = list(
        dict.fromkeys(
            value
            for decision in decisions
            for value in (
                (
                    decision.authoritative_source.content
                    if decision.authoritative_source is not None
                    else None
                ),
                (
                    decision.authoritative_rendered.content
                    if decision.authoritative_rendered is not None
                    else None
                ),
                (
                    str(decision.authoritative_source.path).encode("utf-8")
                    if decision.authoritative_source is not None
                    else None
                ),
                (
                    decision.authoritative_source.sha256.encode("ascii")
                    if decision.authoritative_source is not None
                    else None
                ),
                (
                    decision.authoritative_rendered.rendered_sha256.encode(
                        "ascii"
                    )
                    if decision.authoritative_rendered is not None
                    else None
                ),
            )
            if value
        )
    )
    if not forbidden:
        return
    for path in stage1_directory.iterdir():
        if not path.is_file():
            continue
        content = path.read_bytes()
        if any(value in content for value in forbidden):
            raise ShadowStateError(
                "authoritative prompt material appeared in supervisor artifacts"
            )


def _session_integrity(
    known_session_id: str | None,
    resume_session_id: str | None,
    observed_ids: tuple[str, ...],
    forbidden_source_ids: frozenset[str],
) -> tuple[bool, str | None]:
    if len(observed_ids) != 1:
        return False, None
    observed = observed_ids[0]
    try:
        parsed = UUID(observed)
    except ValueError:
        return False, None
    if (
        parsed.int == 0
        or str(parsed) != observed
        or observed in forbidden_source_ids
    ):
        return False, None
    if known_session_id is None:
        return resume_session_id is None, observed
    valid = (
        resume_session_id == known_session_id
        and observed == known_session_id
    )
    return valid, known_session_id


def _preflight_comparison_material(
    context: _ShadowContext,
    pending: PendingSupervisorAction,
    decision: DecisionReconstruction,
    proof: _SupervisorProof,
) -> None:
    """Reject sensitive authoritative material before any comparison write."""
    _, _, sensitive_values = build_subprocess_environment(
        context.services.environ
    )
    comparison_directory = (
        context.run_directory
        / "comparisons"
        / decision.point.decision_id
    )
    proposal_directory = (
        context.run_directory / "proposals" / pending.proposal_id
    )
    values: list[object] = [
        str(proposal_directory / "supervisor-result.json"),
        str(proposal_directory / "candidate-prompt.md"),
        str(proposal_directory / "assessment.json"),
        str(comparison_directory),
        str(comparison_directory / "comparison.json"),
        str(comparison_directory / "authoritative-source.md"),
        str(comparison_directory / "authoritative-rendered.md"),
        decision.point.model_dump(mode="json"),
        (
            proof.proposal
            if proof.proposal is not None
            else {
                "schema_version": 1,
                "valid": False,
                "transport_status": proof.adapter_result.status,
                "proposal": None,
            }
        ),
        (
            "supervisor_result_unavailable",
            "authoritative_reconstruction_unproven",
            "supervisor_result_missing_or_invalid",
            "session_integrity_failed",
            "proposal_size_exceeded",
            "structural_redaction_collision",
            "unreviewed",
        ),
    ]
    if decision.authoritative_source is not None:
        values.extend(
            (
                str(decision.authoritative_source.path),
                decision.authoritative_source.content,
                decision.authoritative_source.sha256,
            )
        )
    if decision.authoritative_rendered is not None:
        values.extend(
            (
                decision.authoritative_rendered.content,
                decision.authoritative_rendered.rendered_sha256,
            )
        )
    preflight_shadow_confidentiality(
        values,
        sensitive_values,
        label="authoritative comparison material",
    )


def _finalize_proposal_artifacts(
    context: _ShadowContext,
    pending: PendingSupervisorAction,
    decision: DecisionReconstruction,
    proof: _SupervisorProof,
    *,
    session_integrity: bool,
) -> dict[str, str]:
    proposal_directory = (
        context.run_directory / "proposals" / pending.proposal_id
    )
    result_path = proposal_directory / "supervisor-result.json"
    candidate_path = proposal_directory / "candidate-prompt.md"
    comparison_directory = (
        context.run_directory / "comparisons" / pending.proposal_id
    )
    comparison_path = comparison_directory / "comparison.json"
    sensitive_values = _sensitive_values(context.services.environ)
    if proof.proposal is None:
        result_value: object = {
            "schema_version": 1,
            "valid": False,
            "transport_status": proof.adapter_result.status,
            "proposal": None,
        }
        candidate_bytes = b""
        comparison = ProposalComparison(
            proposal_id=pending.proposal_id,
            proposal_kind=pending.proposal_kind,
            source_stage2_action_id=decision.point.source_action_id,
            candidate_sha256=None,
            candidate_byte_count=0,
            authoritative_source_sha256=None,
            authoritative_source_byte_count=None,
            authoritative_rendered_sha256=None,
            authoritative_rendered_byte_count=None,
            comparison_available=False,
            comparison_unavailable_reason="supervisor_result_unavailable",
        )
        assessment = _malformed_assessment(
            context,
            pending,
            decision,
            proof,
            session_integrity,
        )
    else:
        proposal = proof.proposal
        result_value = proposal.model_dump(mode="json")
        candidate_bytes = (
            b""
            if proposal.prompt is None
            else proposal.prompt.encode("utf-8")
        )
        comparison = _build_comparison(
            decision,
            proposal,
            candidate_bytes,
        )
        assessment = _assess_proposal(
            context,
            pending,
            decision,
            proof,
            proposal,
            comparison,
            session_integrity=session_integrity,
        )
    assessment_path = proposal_directory / "assessment.json"
    source_path = comparison_directory / "authoritative-source.md"
    rendered_path = comparison_directory / "authoritative-rendered.md"
    preflight_shadow_confidentiality(
        (
            str(result_path),
            result_value,
            str(candidate_path),
            candidate_bytes,
            str(assessment_path),
            assessment,
            str(comparison_directory),
            str(comparison_path),
            comparison,
            (
                str(source_path),
                decision.authoritative_source.content,
            )
            if comparison.comparison_available
            and decision.authoritative_source is not None
            else (),
            (
                str(rendered_path),
                decision.authoritative_rendered.content,
            )
            if comparison.comparison_available
            and decision.authoritative_rendered is not None
            else (),
        ),
        sensitive_values,
        label="finalized proposal and comparison structure",
    )
    _write_json(result_path, result_value)
    _write_bytes(candidate_path, candidate_bytes)
    # Proposal/result/candidate are durably finalized before any
    # authoritative comparison material is created.
    _fsync_directory(proposal_directory)
    comparison_directory.mkdir(parents=True, exist_ok=True)
    if (
        comparison.comparison_available
        and decision.authoritative_source is not None
        and decision.authoritative_rendered is not None
    ):
        _write_bytes(source_path, decision.authoritative_source.content)
        _write_bytes(
            rendered_path, decision.authoritative_rendered.content
        )
    _write_json(
        comparison_path, comparison.model_dump(mode="json")
    )
    _write_json(
        assessment_path, assessment.model_dump(mode="json")
    )
    paths = {
        str(result_path),
        str(candidate_path),
        str(assessment_path),
        str(comparison_path),
    }
    for name in ("authoritative-source.md", "authoritative-rendered.md"):
        path = comparison_directory / name
        if path.exists():
            paths.add(str(path))
    return {
        locator: sha256_regular_file(Path(locator))
        for locator in sorted(paths)
    }


def _build_comparison(
    decision: DecisionReconstruction,
    proposal: SupervisorProposal,
    candidate_bytes: bytes,
) -> ProposalComparison:
    source = decision.authoritative_source
    rendered = decision.authoritative_rendered
    candidate_sha = (
        None
        if proposal.prompt is None
        else hashlib.sha256(candidate_bytes).hexdigest()
    )
    if (
        not decision.point.comparison_available
        or source is None
        or rendered is None
    ):
        return ProposalComparison(
            proposal_id=decision.point.decision_id,
            proposal_kind=decision.point.proposal_kind,
            source_stage2_action_id=decision.point.source_action_id,
            candidate_sha256=candidate_sha,
            candidate_byte_count=len(candidate_bytes),
            authoritative_source_sha256=None,
            authoritative_source_byte_count=None,
            authoritative_rendered_sha256=None,
            authoritative_rendered_byte_count=None,
            comparison_available=False,
            comparison_unavailable_reason=(
                decision.point.comparison_unavailable_reason
                or "authoritative_reconstruction_unproven"
            ),
        )
    return ProposalComparison(
        proposal_id=decision.point.decision_id,
        proposal_kind=decision.point.proposal_kind,
        source_stage2_action_id=decision.point.source_action_id,
        candidate_sha256=candidate_sha,
        candidate_byte_count=len(candidate_bytes),
        authoritative_source_sha256=source.sha256,
        authoritative_source_byte_count=len(source.content),
        authoritative_rendered_sha256=rendered.rendered_sha256,
        authoritative_rendered_byte_count=rendered.byte_count,
        comparison_available=True,
        comparison_unavailable_reason=None,
    )


def _assess_proposal(
    context: _ShadowContext,
    pending: PendingSupervisorAction,
    decision: DecisionReconstruction,
    proof: _SupervisorProof,
    proposal: NormalizedSupervisorProposal,
    comparison: ProposalComparison,
    *,
    session_integrity: bool,
) -> DeterministicAssessment:
    change_flags = {
        "contract_change_requested": proposal.contract_change_requested,
        "scope_expansion_requested": proposal.scope_expansion_requested,
        "permission_change_requested": (
            proposal.permission_change_requested
        ),
        "acceptance_change_requested": (
            proposal.acceptance_change_requested
        ),
        "convention_change_requested": (
            proposal.convention_change_requested
        ),
    }
    findings: list[PathScopeFinding] = []
    specification = context.prepared.source.prepared.specification
    seen_paths: set[str] = set()
    for path in proposal.referenced_paths:
        if path in seen_paths:
            findings.append(
                PathScopeFinding(
                    path=path,
                    reason="duplicate_normalized_path",
                )
            )
            continue
        seen_paths.add(path)
        if _is_windows_absolute_path(path) or posixpath.isabs(path):
            findings.append(
                PathScopeFinding(
                    path=path,
                    reason="absolute_outside_workspace",
                )
            )
        elif path == ".." or path.startswith("../"):
            findings.append(
                PathScopeFinding(path=path, reason="traversal_escape")
            )
        elif path in {"", "."}:
            findings.append(
                PathScopeFinding(
                    path=path,
                    reason="outside_allowed_paths",
                )
            )
        elif path_matches_any(path, specification.protected_paths):
            findings.append(
                PathScopeFinding(path=path, reason="protected_path")
            )
        elif not path_matches_any(path, specification.allowed_paths):
            findings.append(
                PathScopeFinding(
                    path=path, reason="outside_allowed_paths"
                )
            )
    test_ids = tuple(
        test.specification.id
        for test in context.prepared.source.prepared.acceptance_tests
    )
    covered = tuple(
        test_id
        for test_id in test_ids
        if test_id in proposal.required_checks
    )
    missing = tuple(
        test_id for test_id in test_ids if test_id not in covered
    )
    unknown = tuple(
        dict.fromkeys(
            check
            for check in proposal.required_checks
            if check not in test_ids
        )
    )
    coverage = RequiredCheckCoverage(
        required_test_ids=test_ids,
        covered_test_ids=covered,
        missing_test_ids=missing,
        unknown_check_ids=unknown,
    )
    _, _, sensitive_values = build_subprocess_environment(
        context.services.environ
    )
    structural_collision = any(
        would_redact_text(value, sensitive_values)
        or "<REDACTED>" in value
        for value in _proposal_strings(proposal)
    )
    byte_count = len(proof.final_bytes)
    size_compliant = byte_count <= context.state.max_proposal_bytes
    reasons: list[str] = []
    if not session_integrity:
        reasons.append("session_integrity_failed")
    if not size_compliant:
        reasons.append("proposal_size_exceeded")
    reasons.extend(
        name for name, requested in change_flags.items() if requested
    )
    reasons.extend(
        f"referenced_path_{finding.reason}:{finding.path}"
        for finding in findings
    )
    reasons.extend(
        f"required_check_missing:{test_id}" for test_id in missing
    )
    reasons.extend(
        f"required_check_unknown:{check}" for check in unknown
    )
    if structural_collision:
        reasons.append("structural_redaction_collision")
    candidate_sha = comparison.candidate_sha256
    return DeterministicAssessment(
        proposal_id=pending.proposal_id,
        proposal_kind=pending.proposal_kind,
        schema_integrity=True,
        blind_input_integrity=True,
        session_integrity=session_integrity,
        size_compliant=size_compliant,
        proposal_byte_count=byte_count,
        change_flags=change_flags,
        path_scope_findings=tuple(findings),
        required_check_coverage=coverage,
        disposition=proposal.disposition,
        disqualified=bool(reasons),
        disqualification_reasons=tuple(reasons),
        candidate_sha256=candidate_sha,
        candidate_byte_count=comparison.candidate_byte_count,
        authoritative_source_sha256=(
            comparison.authoritative_source_sha256
        ),
        authoritative_source_byte_count=(
            comparison.authoritative_source_byte_count
        ),
        authoritative_rendered_sha256=(
            comparison.authoritative_rendered_sha256
        ),
        authoritative_rendered_byte_count=(
            comparison.authoritative_rendered_byte_count
        ),
        comparison_available=comparison.comparison_available,
        review_status="unreviewed",
    )


def _malformed_assessment(
    context: _ShadowContext,
    pending: PendingSupervisorAction,
    decision: DecisionReconstruction,
    proof: _SupervisorProof,
    session_integrity: bool,
) -> DeterministicAssessment:
    del decision
    test_ids = tuple(
        test.specification.id
        for test in context.prepared.source.prepared.acceptance_tests
    )
    reasons = ["supervisor_result_missing_or_invalid"]
    if not session_integrity:
        reasons.append("session_integrity_failed")
    return DeterministicAssessment(
        proposal_id=pending.proposal_id,
        proposal_kind=pending.proposal_kind,
        schema_integrity=False,
        blind_input_integrity=True,
        session_integrity=session_integrity,
        size_compliant=(
            len(proof.final_bytes) <= context.state.max_proposal_bytes
        ),
        proposal_byte_count=len(proof.final_bytes),
        change_flags={
            "contract_change_requested": False,
            "scope_expansion_requested": False,
            "permission_change_requested": False,
            "acceptance_change_requested": False,
            "convention_change_requested": False,
        },
        path_scope_findings=(),
        required_check_coverage=RequiredCheckCoverage(
            required_test_ids=test_ids,
            covered_test_ids=(),
            missing_test_ids=test_ids,
            unknown_check_ids=(),
        ),
        disposition="malformed",
        disqualified=True,
        disqualification_reasons=tuple(reasons),
        candidate_sha256=None,
        candidate_byte_count=0,
        authoritative_source_sha256=None,
        authoritative_source_byte_count=None,
        authoritative_rendered_sha256=None,
        authoritative_rendered_byte_count=None,
        comparison_available=False,
        review_status="unreviewed",
    )


def _proposal_strings(proposal: SupervisorProposal) -> tuple[str, ...]:
    values = [
        proposal.proposal_kind,
        proposal.disposition,
        proposal.summary,
        *proposal.referenced_paths,
        *proposal.required_checks,
        *proposal.assumptions,
        *proposal.questions,
    ]
    if proposal.prompt is not None:
        values.append(proposal.prompt)
    return tuple(values)


def _pause(
    context: _ShadowContext,
    reason: str,
    summary: str,
) -> ShadowState:
    directory = (
        context.run_directory
        / "escalation"
        / f"{context.state.journal_sequence + 1:06d}-{reason}"
    )
    package_path = directory / "package.json"
    readme_path = directory / "README.md"
    pending = context.state.pending_action
    package = {
        "schema_version": 1,
        "calibration_id": context.state.calibration_id,
        "status": "human_paused",
        "reason": reason,
        "summary": summary,
        "proposal_id": (
            pending.proposal_id if pending is not None else None
        ),
        "pending_action_id": (
            pending.action_id if pending is not None else None
        ),
        "pending_resume_session_id": (
            pending.resume_session_id if pending is not None else None
        ),
        "supervisor_session_id": (
            context.state.supervisor_session_id
        ),
    }
    markdown = "\n".join(
        (
            "# Shadow calibration escalation",
            "",
            "- Schema version: `1`",
            "- Status: `human_paused`",
            f"- Reason: `{reason}`",
            f"- Pending action: "
            f"`{pending.action_id if pending is not None else 'none'}`",
            f"- Supervisor UUID: "
            f"`{context.state.supervisor_session_id or 'not available'}`",
            f"- Summary: {summary}",
            "",
        )
    )
    _, _, sensitive_values = build_subprocess_environment(
        context.services.environ
    )
    preflight_shadow_confidentiality(
        (
            str(package_path),
            str(readme_path),
            package,
            markdown,
        ),
        sensitive_values,
        label="shadow escalation package",
        integrity=True,
    )
    _write_json(
        package_path,
        package,
    )
    _write_text(readme_path, markdown)
    return _journal_event(
        context.run_directory,
        context.state,
        context.prepared,
        event_type="transition",
        previous_state=context.state.status,
        new_state="human_paused",
        action_id=None,
        proposal_id=None,
        reason=reason,
        artifact_hashes={
            str(package_path): sha256_regular_file(package_path),
            str(readme_path): sha256_regular_file(readme_path),
        },
        updates={
            "status": "human_paused",
            "pause_reason": reason,
            "summary": summary,
        },
        utc_now=context.services.utc_now,
        sensitive_values=sensitive_values,
    )


def _transition(
    context: _ShadowContext,
    new_state: str,
    reason: str,
    **updates: object,
) -> ShadowState:
    typed_state = cast(Any, new_state)
    values = {"status": typed_state, **updates}
    return _journal_event(
        context.run_directory,
        context.state,
        context.prepared,
        event_type="transition",
        previous_state=context.state.status,
        new_state=typed_state,
        action_id=None,
        proposal_id=None,
        reason=reason,
        artifact_hashes={},
        updates=values,
        utc_now=context.services.utc_now,
        sensitive_values=_sensitive_values(context.services.environ),
    )


def _journal_event(
    run_directory: Path,
    state: ShadowState,
    prepared: PreparedShadowSpecification,
    *,
    event_type: str,
    previous_state: str | None,
    new_state: str,
    action_id: str | None,
    proposal_id: str | None,
    reason: str,
    artifact_hashes: Mapping[str, str],
    updates: Mapping[str, object],
    utc_now: Callable[[], datetime],
    sensitive_values: Sequence[str] = (),
) -> ShadowState:
    if not set(updates).issubset(MUTABLE_STATE_FIELDS):
        raise ShadowStateError(
            "journal update contains unsupported state fields"
        )
    timestamp = _utc_string(utc_now())
    sequence = state.journal_sequence + 1
    body = {
        "schema_version": 1,
        "sequence": sequence,
        "event_type": event_type,
        "previous_state": previous_state,
        "new_state": new_state,
        "action_id": action_id,
        "proposal_id": proposal_id,
        "timestamp": timestamp,
        "reason": reason,
        "artifact_hashes": dict(sorted(artifact_hashes.items())),
        "state_updates": _json_compatible(updates),
        "previous_hash": state.journal_hash,
    }
    entry_hash = hashlib.sha256(_canonical_json(body)).hexdigest()
    entry = ShadowJournalEntry.model_validate(
        {**body, "entry_hash": entry_hash}
    )
    state_values = state.model_dump(mode="json")
    compatible_updates = _json_compatible(updates)
    if not isinstance(compatible_updates, dict):
        raise ShadowStateError("journal updates are not an object")
    state_values.update(compatible_updates)
    state_values.update(
        {
            "journal_sequence": sequence,
            "journal_hash": entry_hash,
            "updated_at": timestamp,
        }
    )
    try:
        updated = ShadowState.model_validate(state_values)
    except ValidationError as exc:
        raise ShadowStateError(
            "journal update produced invalid shadow state"
        ) from exc
    expected_result = _result_for_state(updated, prepared)
    journal = run_directory / JOURNAL_FILE
    preflight_shadow_confidentiality(
        (
            str(journal),
            body,
            entry,
            updated,
            expected_result,
            str(run_directory / STATE_FILE),
            str(run_directory / RESULT_FILE),
        ),
        sensitive_values,
        label="shadow journal and snapshot structure",
    )
    try:
        with journal.open("ab") as handle:
            handle.write(_canonical_json(entry.model_dump(mode="json")))
            handle.flush()
            os.fsync(handle.fileno())
        _fsync_directory(run_directory)
    except OSError as exc:
        raise ShadowStateError(
            "shadow journal could not be appended"
        ) from exc
    _persist_state(
        run_directory,
        updated,
        prepared,
        sensitive_values=sensitive_values,
    )
    return updated


def _initialize_artifacts(
    run_directory: Path,
    prepared: PreparedShadowSpecification,
    *,
    sensitive_values: Sequence[str] = (),
) -> None:
    payloads = _initial_artifact_payloads(prepared)
    preflight_shadow_confidentiality(
        tuple(
            (str(run_directory / name), content)
            for name, content in payloads.items()
        ),
        sensitive_values,
        label="initial shadow artifact structure",
    )
    for name in (
        "supervisor",
        "proposals",
        "comparisons",
        "reviews",
        "reports",
        "escalation",
    ):
        (run_directory / name).mkdir()
    for name, content in payloads.items():
        _write_bytes(run_directory / name, content)


def _initial_artifact_payloads(
    prepared: PreparedShadowSpecification,
) -> dict[str, bytes]:
    return {
        "shadow-spec.normalized.json": _render_json_bytes(
            prepared.normalized_dict()
        ),
        "shadow-spec.sha256": (
            prepared.specification_sha256 + "\n"
        ).encode("ascii"),
        "policy.sha256": (prepared.policy.sha256 + "\n").encode("ascii"),
        "context.sha256.json": _render_json_bytes(
            {
                "schema_version": 1,
                "contexts": [
                    context.manifest().model_dump(mode="json")
                    for context in prepared.contexts
                ],
            }
        ),
        "source-stage2.json": _render_json_bytes(
            prepared.source.identity_record()
        ),
        "decision-points.json": _render_json_bytes(
            decision_points_artifact(prepared.source.decisions)
        ),
        JOURNAL_FILE: b"",
    }


def _initial_artifact_hashes(
    run_directory: Path,
) -> dict[str, str]:
    names = (
        "shadow-spec.normalized.json",
        "shadow-spec.sha256",
        "policy.sha256",
        "context.sha256.json",
        "source-stage2.json",
        "decision-points.json",
    )
    return {
        str(run_directory / name): sha256_regular_file(
            run_directory / name
        )
        for name in names
    }


def _reload_prepared(
    state: ShadowState,
    services: ShadowServices,
) -> PreparedShadowSpecification:
    try:
        prepared = load_shadow_specification(
            Path(state.shadow_specification_path),
            # Structural confidentiality was frozen at validation/run time.
            # Recovery must not become environment-dependent merely because a
            # new credential value happens to equal a durable locator.
            environ={},
        )
    except WorkflowDependencyError as exc:
        raise ShadowDependencyError(str(exc)) from exc
    if (
        prepared.specification_sha256
        != state.shadow_specification_sha256
        or prepared.policy.sha256 != state.policy_sha256
        or tuple(
            context.manifest() for context in prepared.contexts
        )
        != state.context_hashes
        or str(prepared.source.run_directory)
        != state.source_stage2_run
        or sha256_regular_file(
            prepared.source.run_directory / "state.json"
        )
        != state.source_stage2_state_sha256
        or sha256_regular_file(
            prepared.source.run_directory / "journal.jsonl"
        )
        != state.source_stage2_journal_sha256
        or prepared.source.state.substage_id
        != state.source_substage_id
        or len(prepared.source.decisions) != state.decision_count
    ):
        raise ShadowStateError(
            "frozen shadow inputs or source Stage 2 identity changed"
        )
    return prepared


def _validate_source_frozen(context: _ShadowContext) -> None:
    refreshed = _reload_prepared(context.state, context.services)
    if (
        refreshed.normalized_dict()
        != context.prepared.normalized_dict()
        or decision_points_artifact(refreshed.source.decisions)
        != decision_points_artifact(context.prepared.source.decisions)
    ):
        raise ShadowStateError(
            "source decision reconstruction changed"
        )


def _validate_runtime_durability(context: _ShadowContext) -> None:
    _validate_source_frozen(context)
    sensitive_values = _sensitive_values(context.services.environ)
    _preflight_durable_run(
        context.run_directory,
        sensitive_values=sensitive_values,
    )
    _validate_journal(
        context.run_directory, context.state, context.prepared
    )
    if _load_state(context.run_directory) != context.state:
        raise ShadowStateError(
            "supervisor action changed Stage 3 state"
        )
    if _load_result(context.run_directory) != _result_for_state(
        context.state, context.prepared
    ):
        raise ShadowStateError(
            "supervisor action changed Stage 3 result"
        )


def _validate_run(
    run_directory: Path,
    state: ShadowState,
    prepared: PreparedShadowSpecification,
    *,
    allowed_unjournaled_review: str | None = None,
    sensitive_values: Sequence[str] = (),
) -> None:
    _preflight_durable_run(
        run_directory,
        sensitive_values=sensitive_values,
    )
    persisted_state = _load_state(run_directory)
    if persisted_state != state:
        raise ShadowIntegrityError(
            "shadow state changed before trusted run validation"
        )
    _validate_state_result_agreement(
        run_directory, persisted_state, prepared
    )
    _validate_journal(run_directory, state, prepared)
    if (
        _read_json(run_directory / "shadow-spec.normalized.json")
        != prepared.normalized_dict()
        or _read_exact_bytes(run_directory / "shadow-spec.sha256")
        != f"{prepared.specification_sha256}\n".encode("ascii")
        or _read_exact_bytes(run_directory / "policy.sha256")
        != f"{prepared.policy.sha256}\n".encode("ascii")
        or _read_json(run_directory / "context.sha256.json")
        != {
            "schema_version": 1,
            "contexts": [
                context.manifest().model_dump(mode="json")
                for context in prepared.contexts
            ],
        }
        or _read_json(run_directory / "decision-points.json")
        != decision_points_artifact(prepared.source.decisions)
        or _read_json(run_directory / "source-stage2.json")
        != prepared.source.identity_record()
    ):
        raise ShadowStateError(
            "frozen Stage 3 artifacts changed"
        )
    expected_proposal_directories = set(state.proposal_ids)
    if state.pending_action is not None:
        expected_proposal_directories.add(
            state.pending_action.proposal_id
        )
    elif (
        state.status in {"reconstructing", "supervisor_running"}
        and state.current_decision_index < state.decision_count
    ):
        expected_proposal_directories.add(
            prepared.source.decisions[
                state.current_decision_index
            ].point.decision_id
        )
    actual_proposal_directories = {
        path.name
        for path in (run_directory / "proposals").iterdir()
        if path.is_dir()
    }
    actual_comparison_directories = {
        path.name
        for path in (run_directory / "comparisons").iterdir()
        if path.is_dir()
    }
    if (
        not actual_proposal_directories.issubset(
            expected_proposal_directories
        )
        or not actual_comparison_directories.issubset(
            expected_proposal_directories
        )
    ):
        raise ShadowStateError(
            "proposal directories contain missing or unjournaled artifacts"
        )
    expected_reviews = {
        f"{proposal_id}.json"
        for proposal_id in state.reviewed_proposal_ids
    }
    if allowed_unjournaled_review is not None:
        expected_reviews.add(f"{allowed_unjournaled_review}.json")
    actual_reviews = {
        path.name
        for path in (run_directory / "reviews").iterdir()
        if path.is_file()
    }
    if (
        actual_reviews - expected_reviews
        or not {
            f"{proposal_id}.json"
            for proposal_id in state.reviewed_proposal_ids
        }.issubset(actual_reviews)
    ):
        raise ShadowStateError(
            "review directory contains missing or unjournaled artifacts"
        )


def _preflight_durable_run(
    run_directory: Path,
    *,
    sensitive_values: Sequence[str],
) -> None:
    """Reject sensitive strings already present in trusted Stage 3 storage."""
    values: list[object] = [str(run_directory)]
    try:
        paths = sorted(
            run_directory.rglob("*"),
            key=lambda path: str(path.relative_to(run_directory)),
        )
    except OSError as exc:
        raise ShadowIntegrityError(
            "durable Stage 3 artifacts could not be inspected"
        ) from exc
    for path in paths:
        relative = str(path.relative_to(run_directory))
        values.append(relative)
        if relative == LOCK_FILE:
            continue
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise ShadowIntegrityError(
                "durable Stage 3 artifact identity could not be inspected"
            ) from exc
        if stat.S_ISREG(metadata.st_mode):
            try:
                content = path.read_bytes()
            except OSError as exc:
                raise ShadowIntegrityError(
                    "durable Stage 3 artifact could not be read"
                ) from exc
            try:
                if path.suffix == ".json":
                    values.append(
                        json.loads(
                            content.decode("utf-8"),
                            parse_constant=_reject_json_constant,
                        )
                    )
                elif path.suffix == ".jsonl":
                    values.extend(
                        json.loads(
                            line.decode("utf-8"),
                            parse_constant=_reject_json_constant,
                        )
                        for line in content.splitlines()
                        if line
                    )
                else:
                    values.append(content.decode("utf-8"))
            except (
                UnicodeDecodeError,
                json.JSONDecodeError,
                ValueError,
            ) as exc:
                raise ShadowIntegrityError(
                    "durable Stage 3 serialized structure is invalid"
                ) from exc
    preflight_shadow_confidentiality(
        values,
        sensitive_values,
        label="durable Stage 3 artifact structure",
        integrity=True,
    )


def _validate_state_result_agreement(
    run_directory: Path,
    state: ShadowState,
    prepared: PreparedShadowSpecification,
) -> None:
    """Require the strict persisted public result to equal trusted state."""
    expected = _result_for_state(state, prepared)
    persisted = _load_result(run_directory)
    if persisted != expected:
        raise ShadowIntegrityError(
            "shadow state and result snapshots disagree"
        )


def _validate_journal(
    run_directory: Path,
    state: ShadowState,
    prepared: PreparedShadowSpecification,
) -> None:
    entries = _read_journal(run_directory)
    if (
        not entries
        or len(entries) != state.journal_sequence
        or entries[-1].entry_hash != state.journal_hash
        or entries[-1].timestamp != state.updated_at
    ):
        raise ShadowStateError(
            "shadow state does not agree with the journal head"
        )
    replay: dict[str, object] = {
        "status": "initialized",
        "supervisor_session_id": None,
        "current_decision_index": 0,
        "completed_action_ids": [],
        "proposal_ids": [],
        "reviewed_proposal_ids": [],
        "pending_action": None,
        "pause_reason": None,
        "summary": "Shadow calibration initialized.",
    }
    current_state: str | None = None
    open_pending: PendingSupervisorAction | None = None
    completed: list[str] = []
    proposals: list[str] = []
    reviewed: list[str] = []
    intents: dict[str, PendingSupervisorAction] = {}
    previous_timestamp: datetime | None = None
    for index, entry in enumerate(entries):
        timestamp = _parse_timestamp(entry.timestamp)
        if (
            previous_timestamp is not None
            and timestamp < previous_timestamp
        ):
            raise ShadowStateError(
                "shadow journal timestamps are reordered"
            )
        previous_timestamp = timestamp
        _validate_journal_form(entry)
        if index == 0:
            if (
                entry.event_type != "transition"
                or entry.previous_state is not None
                or entry.new_state != "initialized"
                or entry.reason != "shadow_initialized"
                or entry.state_updates
                or entry.artifact_hashes
                != _initial_artifact_hashes(run_directory)
            ):
                raise ShadowStateError(
                    "shadow journal initialization is invalid"
                )
            current_state = "initialized"
            continue
        if entry.previous_state != current_state:
            raise ShadowStateError(
                "shadow journal state history is discontinuous"
            )
        if not set(entry.state_updates).issubset(
            MUTABLE_STATE_FIELDS
        ):
            raise ShadowStateError(
                "shadow journal contains unsupported updates"
            )
        if entry.event_type == "action_intent":
            if (
                open_pending is not None
                or set(entry.state_updates) != {"pending_action"}
                or entry.action_id is None
                or entry.proposal_id is None
            ):
                raise ShadowStateError(
                    "supervisor intent lifecycle is invalid"
                )
            try:
                pending = PendingSupervisorAction.model_validate(
                    entry.state_updates["pending_action"]
                )
            except ValidationError as exc:
                raise ShadowStateError(
                    "supervisor intent is malformed"
                ) from exc
            if pending.decision_index >= len(
                prepared.source.decisions
            ):
                raise ShadowStateError(
                    "supervisor intent decision index is invalid"
                )
            decision = prepared.source.decisions[
                pending.decision_index
            ]
            expected_blind = build_blind_supervisor_prompt(
                prepared, decision
            )
            recorded_manifest = _model_from_json(
                Path(pending.blind_manifest_path),
                BlindInputManifest,
                "blind input manifest",
            )
            if (
                pending.action_id != entry.action_id
                or pending.proposal_id != entry.proposal_id
                or pending.decision_index != len(proposals)
                or pending.decision_index
                >= len(prepared.source.decisions)
                or prepared.source.decisions[
                    pending.decision_index
                ].point.decision_id
                != pending.proposal_id
                or prepared.source.decisions[
                    pending.decision_index
                ].point.proposal_kind
                != pending.proposal_kind
                or recorded_manifest != expected_blind.manifest
                or _read_json(Path(pending.output_schema_path))
                != expected_blind.output_schema
                or pending.prompt_sha256
                != expected_blind.manifest.rendered_blind_input_sha256
                or pending.prompt_byte_count
                != expected_blind.manifest.rendered_blind_input_byte_count
                or pending.workspace
                != prepared.source.state.workspace
                or pending.model
                != prepared.specification.supervisor_model
                or pending.reasoning_effort
                != prepared.specification.supervisor_reasoning_effort
                or pending.timeout_seconds
                != prepared.specification.supervisor_timeout_seconds
                or pending.resume_session_id
                != replay["supervisor_session_id"]
                or pending.stage1_artifact_directory
                != str(
                    run_directory
                    / "proposals"
                    / pending.proposal_id
                    / "stage1-run"
                )
                or pending.blind_manifest_path
                != str(
                    run_directory
                    / "proposals"
                    / pending.proposal_id
                    / "blind-input-manifest.json"
                )
                or pending.output_schema_path
                != str(
                    run_directory
                    / "proposals"
                    / pending.proposal_id
                    / "output-schema.json"
                )
                or entry.artifact_hashes
                != {
                    pending.blind_manifest_path: (
                        pending.blind_manifest_sha256
                    ),
                    pending.output_schema_path: (
                        pending.output_schema_sha256
                    ),
                }
            ):
                raise ShadowStateError(
                    "supervisor intent contradicts its decision"
                )
            intents[pending.action_id] = pending
            open_pending = pending
        elif entry.event_type == "action_completion":
            if (
                open_pending is None
                or entry.action_id != open_pending.action_id
                or entry.proposal_id != open_pending.proposal_id
            ):
                raise ShadowStateError(
                    "supervisor completion lifecycle is invalid"
                )
            record_path = (
                run_directory
                / "supervisor"
                / f"{open_pending.action_id}.json"
            )
            record = _model_from_json(
                record_path,
                SupervisorActionRecord,
                "supervisor action record",
            )
            if (
                record.action_id != open_pending.action_id
                or record.proposal_id != open_pending.proposal_id
                or record.proposal_kind
                != open_pending.proposal_kind
                or entry.artifact_hashes
                != {
                    **record.artifact_hashes,
                    str(record_path): sha256_regular_file(record_path),
                }
            ):
                raise ShadowStateError(
                    "supervisor action record contradicts completion"
                )
            try:
                verify_hash_mapping(record.artifact_hashes)
            except Exception as exc:
                raise ShadowStateError(
                    "supervisor action artifact hash changed"
                ) from exc
            proof = _verify_supervisor_artifacts(
                open_pending,
                prepared.source.decisions[
                    open_pending.decision_index
                ],
                prepared.source.decisions,
            )
            if (
                proof.adapter_result != record.adapter_result
                or proof.session_ids != record.session_ids
                or (proof.proposal is not None)
                != record.structured_result_valid
            ):
                raise ShadowStateError(
                    "supervisor action proof changed"
                )
            _validate_finalized_proposal(
                run_directory,
                open_pending,
                prepared.source.decisions[
                    open_pending.decision_index
                ],
                proof,
                record,
            )
            completed.append(open_pending.action_id)
            proposals.append(open_pending.proposal_id)
            if entry.state_updates.get(
                "completed_action_ids"
            ) != completed or entry.state_updates.get(
                "proposal_ids"
            ) != proposals:
                raise ShadowStateError(
                    "completed supervisor history is contradictory"
                )
            open_pending = None
        elif entry.event_type == "review":
            if (
                entry.proposal_id is None
                or entry.action_id is not None
                or entry.proposal_id in reviewed
                or entry.proposal_id not in proposals
            ):
                raise ShadowStateError(
                    "shadow review lifecycle is invalid"
                )
            review_path = (
                run_directory
                / "reviews"
                / f"{entry.proposal_id}.json"
            )
            review = _model_from_json(
                review_path, HumanReview, "shadow review"
            )
            if (
                review.proposal_id != entry.proposal_id
                or not _load_comparison(
                    run_directory, entry.proposal_id
                ).comparison_available
                or entry.artifact_hashes
                != {
                    str(review_path): sha256_regular_file(review_path)
                }
            ):
                raise ShadowStateError(
                    "shadow review evidence is contradictory"
                )
            reviewed.append(entry.proposal_id)
            if entry.state_updates.get(
                "reviewed_proposal_ids"
            ) != reviewed:
                raise ShadowStateError(
                    "review history is contradictory"
                )
            comparable = {
                proposal_id
                for proposal_id in proposals
                if _load_comparison(
                    run_directory, proposal_id
                ).comparison_available
            }
            should_complete = comparable.issubset(reviewed)
            if should_complete != (entry.new_state == "completed"):
                raise ShadowStateError(
                    "review completion state is contradictory"
                )
        elif entry.event_type != "transition":
            raise ShadowStateError(
                "shadow journal event type is unsupported"
            )
        replay.update(entry.state_updates)
        replay["status"] = entry.new_state
        current_state = entry.new_state
    if open_pending is not None:
        replay["pending_action"] = open_pending.model_dump(mode="json")
    state_values = state.model_dump(mode="json")
    if any(state_values[name] != value for name, value in replay.items()):
        raise ShadowStateError(
            "shadow state contradicts journal replay"
        )


def _validate_journal_form(entry: ShadowJournalEntry) -> None:
    """Reject syntactically valid but undefined Stage 3 journal semantics."""
    if entry.event_type == "action_intent":
        valid = (
            entry.previous_state == "supervisor_running"
            and entry.new_state == "supervisor_running"
            and entry.action_id is not None
            and entry.proposal_id is not None
            and entry.reason == "supervisor_action_intent"
            and set(entry.state_updates) == {"pending_action"}
        )
    elif entry.event_type == "action_completion":
        valid = (
            entry.previous_state == "proposal_validating"
            and entry.new_state == "proposal_validating"
            and entry.action_id is not None
            and entry.proposal_id is not None
            and entry.reason == "supervisor_action_completed"
            and set(entry.state_updates)
            == {
                "pending_action",
                "completed_action_ids",
                "proposal_ids",
                "current_decision_index",
                "supervisor_session_id",
            }
        )
    elif entry.event_type == "review":
        valid = (
            entry.previous_state == "awaiting_reviews"
            and entry.new_state in {"awaiting_reviews", "completed"}
            and entry.action_id is None
            and entry.proposal_id is not None
            and entry.reason
            in {"review_recorded", "review_recorded_completed"}
            and (
                (entry.new_state == "completed")
                == (entry.reason == "review_recorded_completed")
            )
            and set(entry.state_updates)
            == {"reviewed_proposal_ids", "status", "summary"}
            and entry.state_updates.get("status") == entry.new_state
        )
    else:
        valid = _valid_transition_form(entry)
    if not valid:
        raise ShadowStateError(
            "shadow journal event, state, action, and reason semantics "
            "are invalid"
        )


def _valid_transition_form(entry: ShadowJournalEntry) -> bool:
    if (
        entry.action_id is not None
        or entry.proposal_id is not None
        or entry.event_type != "transition"
    ):
        return False
    if entry.previous_state is None:
        return (
            entry.new_state == "initialized"
            and entry.reason == "shadow_initialized"
            and not entry.state_updates
        )
    forms = {
        (
            "initialized",
            "reconstructing",
            "decision_points_reconstructed",
        ),
        (
            "reconstructing",
            "supervisor_running",
            "supervisor_proposal_requested",
        ),
        (
            "supervisor_running",
            "proposal_validating",
            "supervisor_transport_completed",
        ),
        (
            "proposal_validating",
            "reconstructing",
            "proposal_finalized",
        ),
        (
            "reconstructing",
            "awaiting_reviews",
            "all_proposals_generated",
        ),
        (
            "reconstructing",
            "completed",
            "all_proposals_completed_without_reviews",
        ),
    }
    key = (entry.previous_state, entry.new_state, entry.reason)
    if key in forms:
        return (
            set(entry.state_updates) == {"status", "summary"}
            and entry.state_updates.get("status") == entry.new_state
            and not entry.artifact_hashes
        )
    if entry.new_state == "human_paused":
        return (
            entry.previous_state
            in {
                "reconstructing",
                "supervisor_running",
                "proposal_validating",
            }
            and set(entry.state_updates)
            == {"status", "pause_reason", "summary"}
            and entry.state_updates.get("status") == "human_paused"
            and entry.state_updates.get("pause_reason") == entry.reason
            and len(entry.artifact_hashes) == 2
            and {
                Path(locator).name
                for locator in entry.artifact_hashes
            }
            == {"package.json", "README.md"}
            and all(
                "escalation" in Path(locator).parts
                for locator in entry.artifact_hashes
            )
        )
    if entry.new_state == "aborted" and entry.reason == "human_abort":
        return (
            entry.previous_state
            in {
                "initialized",
                "reconstructing",
                "proposal_validating",
                "awaiting_reviews",
                "human_paused",
            }
            and set(entry.state_updates)
            == {"status", "pause_reason", "summary"}
            and entry.state_updates.get("status") == "aborted"
            and not entry.artifact_hashes
        )
    return False


def _validate_finalized_proposal(
    run_directory: Path,
    pending: PendingSupervisorAction,
    decision: DecisionReconstruction,
    proof: _SupervisorProof,
    record: SupervisorActionRecord,
) -> None:
    proposal_directory = (
        run_directory / "proposals" / pending.proposal_id
    )
    comparison_directory = (
        run_directory / "comparisons" / pending.proposal_id
    )
    result_path = proposal_directory / "supervisor-result.json"
    candidate_path = proposal_directory / "candidate-prompt.md"
    assessment_path = proposal_directory / "assessment.json"
    comparison_path = comparison_directory / "comparison.json"
    assessment = _model_from_json(
        assessment_path,
        DeterministicAssessment,
        "proposal assessment",
    )
    comparison = _model_from_json(
        comparison_path,
        ProposalComparison,
        "proposal comparison",
    )
    expected_paths = {
        *proof.artifact_hashes,
        str(result_path),
        str(candidate_path),
        str(assessment_path),
        str(comparison_path),
    }
    if proof.proposal is None:
        expected_result: dict[str, object] = {
            "schema_version": 1,
            "valid": False,
            "transport_status": proof.adapter_result.status,
            "proposal": None,
        }
        if (
            _read_json(result_path) != expected_result
            or _read_exact_bytes(candidate_path)
            or comparison.comparison_available
            or assessment.schema_integrity
            or assessment.comparison_available
        ):
            raise ShadowStateError(
                "malformed supervisor finalization is contradictory"
            )
    else:
        proposal = proof.proposal
        expected_candidate = (
            b""
            if proposal.prompt is None
            else proposal.prompt.encode("utf-8")
        )
        if (
            _read_json(result_path) != proposal.model_dump(mode="json")
            or _read_exact_bytes(candidate_path) != expected_candidate
            or assessment.proposal_id != pending.proposal_id
            or assessment.proposal_kind != pending.proposal_kind
            or not assessment.schema_integrity
            or assessment.disposition != proposal.disposition
            or assessment.candidate_sha256
            != comparison.candidate_sha256
            or assessment.candidate_byte_count
            != comparison.candidate_byte_count
            or assessment.comparison_available
            != comparison.comparison_available
        ):
            raise ShadowStateError(
                "supervisor proposal finalization is contradictory"
            )
        if (
            proposal.prompt is None
            and comparison.candidate_sha256 is not None
        ) or (
            proposal.prompt is not None
            and comparison.candidate_sha256
            != hashlib.sha256(expected_candidate).hexdigest()
        ):
            raise ShadowStateError(
                "candidate prompt hash is contradictory"
            )
        source = decision.authoritative_source
        rendered = decision.authoritative_rendered
        if decision.point.comparison_available:
            if (
                source is None
                or rendered is None
                or not comparison.comparison_available
            ):
                raise ShadowStateError(
                    "available authoritative comparison is missing"
                )
            source_path = comparison_directory / "authoritative-source.md"
            rendered_path = (
                comparison_directory / "authoritative-rendered.md"
            )
            expected_paths.update(
                (str(source_path), str(rendered_path))
            )
            if (
                _read_exact_bytes(source_path) != source.content
                or _read_exact_bytes(rendered_path) != rendered.content
                or comparison.authoritative_source_sha256
                != source.sha256
                or comparison.authoritative_source_byte_count
                != len(source.content)
                or comparison.authoritative_rendered_sha256
                != rendered.rendered_sha256
                or comparison.authoritative_rendered_byte_count
                != rendered.byte_count
            ):
                raise ShadowStateError(
                    "authoritative comparison content is contradictory"
                )
        elif comparison.comparison_available:
            raise ShadowStateError(
                "unproven comparison was marked available"
            )
    if (
        comparison.proposal_id != pending.proposal_id
        or comparison.proposal_kind != pending.proposal_kind
        or comparison.source_stage2_action_id
        != decision.point.source_action_id
        or set(record.artifact_hashes) != expected_paths
    ):
        raise ShadowStateError(
            "supervisor finalized artifact set is incomplete"
        )


def _read_journal(
    run_directory: Path,
) -> tuple[ShadowJournalEntry, ...]:
    try:
        lines = (run_directory / JOURNAL_FILE).read_bytes().splitlines()
    except OSError as exc:
        raise ShadowStateError("shadow journal could not be read") from exc
    previous_hash = ZERO_HASH
    entries: list[ShadowJournalEntry] = []
    for sequence, raw in enumerate(lines, start=1):
        try:
            value = json.loads(
                raw.decode("ascii"),
                parse_constant=_reject_json_constant,
            )
            entry = ShadowJournalEntry.model_validate(value)
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            ValueError,
            ValidationError,
        ) as exc:
            raise ShadowStateError("shadow journal is malformed") from exc
        body = entry.model_dump(
            mode="json", exclude={"entry_hash"}
        )
        if (
            entry.sequence != sequence
            or entry.previous_hash != previous_hash
            or hashlib.sha256(_canonical_json(body)).hexdigest()
            != entry.entry_hash
        ):
            raise ShadowStateError(
                "shadow journal sequence or hash chain is invalid"
            )
        try:
            verify_hash_mapping(entry.artifact_hashes)
        except Exception as exc:
            raise ShadowStateError(
                "shadow journal artifact hash changed"
            ) from exc
        previous_hash = entry.entry_hash
        entries.append(entry)
    return tuple(entries)


def _reconcile_state(
    run_directory: Path,
    state: ShadowState,
    prepared: PreparedShadowSpecification,
    *,
    sensitive_values: Sequence[str] = (),
) -> ShadowState:
    entries = _read_journal(run_directory)
    if state.journal_sequence > len(entries):
        raise ShadowStateError(
            "shadow state is ahead of its durable journal"
        )
    if (
        state.journal_sequence
        and entries[state.journal_sequence - 1].entry_hash
        != state.journal_hash
    ):
        raise ShadowStateError(
            "shadow state journal prefix does not match"
        )
    if state.journal_sequence == len(entries):
        return state
    values = state.model_dump(mode="json")
    for entry in entries[state.journal_sequence :]:
        values.update(entry.state_updates)
        values.update(
            {
                "status": entry.new_state,
                "journal_sequence": entry.sequence,
                "journal_hash": entry.entry_hash,
                "updated_at": entry.timestamp,
            }
        )
    try:
        reconciled = ShadowState.model_validate(values)
    except ValidationError as exc:
        raise ShadowStateError(
            "journal recovery produced invalid shadow state"
        ) from exc
    _persist_state(
        run_directory,
        reconciled,
        prepared,
        sensitive_values=sensitive_values,
        integrity=True,
    )
    return reconciled


def _persist_state(
    run_directory: Path,
    state: ShadowState,
    prepared: PreparedShadowSpecification,
    *,
    sensitive_values: Sequence[str] = (),
    integrity: bool = False,
) -> None:
    result = _result_for_state(state, prepared)
    preflight_shadow_confidentiality(
        (
            str(run_directory / STATE_FILE),
            state,
            str(run_directory / RESULT_FILE),
            result,
        ),
        sensitive_values,
        label="shadow state and result structure",
        integrity=integrity,
    )
    _write_json(
        run_directory / STATE_FILE,
        state.model_dump(mode="json"),
    )
    _write_json(
        run_directory / RESULT_FILE,
        result.to_dict(),
    )


def _result_for_state(
    state: ShadowState,
    prepared: PreparedShadowSpecification,
) -> ShadowResult:
    assessments, comparisons, reviews = _report_inputs(
        Path(state.artifact_directory),
        state,
        tolerate_missing=True,
    )
    readiness = calculate_readiness(
        prepared.specification,
        state.proposal_ids,
        {
            decision.point.decision_id: decision.point.proposal_kind
            for decision in prepared.source.decisions
        },
        {
            proposal_id: comparison.comparison_available
            for proposal_id, comparison in comparisons.items()
        },
        assessments,
        reviews,
    )
    return state.to_result(
        comparison_count=len(comparisons),
        review_count=len(reviews),
        disqualification_count=sum(
            assessment.disqualified
            for assessment in assessments.values()
        ),
        readiness=readiness.status,
    )


def _report_inputs(
    run_directory: Path,
    state: ShadowState,
    *,
    tolerate_missing: bool = False,
) -> tuple[
    dict[str, DeterministicAssessment],
    dict[str, ProposalComparison],
    dict[str, HumanReview],
]:
    assessments: dict[str, DeterministicAssessment] = {}
    comparisons: dict[str, ProposalComparison] = {}
    reviews: dict[str, HumanReview] = {}
    for proposal_id in state.proposal_ids:
        try:
            assessments[proposal_id] = _model_from_json(
                run_directory
                / "proposals"
                / proposal_id
                / "assessment.json",
                DeterministicAssessment,
                "proposal assessment",
            )
            comparisons[proposal_id] = _load_comparison(
                run_directory, proposal_id
            )
        except ShadowStateError:
            if tolerate_missing:
                continue
            raise
    for proposal_id in state.reviewed_proposal_ids:
        try:
            reviews[proposal_id] = _model_from_json(
                run_directory / "reviews" / f"{proposal_id}.json",
                HumanReview,
                "shadow review",
            )
        except ShadowStateError:
            if tolerate_missing:
                continue
            raise
    return assessments, comparisons, reviews


def _load_comparison(
    run_directory: Path,
    proposal_id: str,
) -> ProposalComparison:
    return _model_from_json(
        run_directory
        / "comparisons"
        / proposal_id
        / "comparison.json",
        ProposalComparison,
        "proposal comparison",
    )


def _load_state(run_directory: Path) -> ShadowState:
    try:
        return ShadowState.model_validate(
            _read_json(run_directory / STATE_FILE)
        )
    except ValidationError as exc:
        raise ShadowStateError(
            "shadow state snapshot is invalid"
        ) from exc


def _load_result(run_directory: Path) -> ShadowResult:
    try:
        return ShadowResult.model_validate(
            _read_json(run_directory / RESULT_FILE)
        )
    except ValidationError as exc:
        raise ShadowStateError(
            "shadow result snapshot is invalid"
        ) from exc


def _resolve_run_directory(path: Path) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ShadowInputError(
            "shadow run directory could not be resolved"
        ) from exc
    if not resolved.is_dir():
        raise ShadowInputError(
            "shadow run path is not a directory"
        )
    return resolved


def _resolve_codex_executable(
    value: str | None,
    *,
    sensitive_values: Sequence[str] = (),
) -> str:
    if value is not None:
        preflight_shadow_confidentiality(
            value,
            sensitive_values,
            label="configured Codex executable locator",
        )
    executable = value or shutil.which("codex")
    if executable is None:
        raise ShadowDependencyError("Codex executable is required")
    preflight_shadow_confidentiality(
        executable,
        sensitive_values,
        label="discovered Codex executable locator",
    )
    try:
        resolved = Path(executable).resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ShadowDependencyError(
            "Codex executable could not be resolved"
        ) from exc
    if not resolved.is_file():
        raise ShadowDependencyError(
            "Codex executable is not a regular file"
        )
    preflight_shadow_confidentiality(
        str(resolved),
        sensitive_values,
        label="resolved Codex executable path",
    )
    return str(resolved)


class _ShadowLock:
    def __init__(
        self,
        run_directory: Path,
        utc_now: Callable[[], datetime],
    ) -> None:
        self.path = run_directory / LOCK_FILE
        self.utc_now = utc_now
        self.handle: IO[str] | None = None
        self.device_inode: tuple[int, int] | None = None

    def __enter__(self) -> _ShadowLock:
        handle: IO[str] | None = None
        acquired = False
        created = False
        try:
            try:
                inspected = self.path.lstat()
            except FileNotFoundError:
                inspected = None
            if inspected is not None and (
                stat.S_ISLNK(inspected.st_mode)
                or not stat.S_ISREG(inspected.st_mode)
            ):
                raise ShadowLockError(
                    "existing shadow lock is not a regular file"
                )
            flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            flags |= getattr(os, "O_NONBLOCK", 0)
            if inspected is None:
                flags |= os.O_CREAT | os.O_EXCL
                created = True
            descriptor = os.open(self.path, flags, 0o600)
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode):
                os.close(descriptor)
                raise ShadowLockError(
                    "opened shadow lock is not a regular file"
                )
            if inspected is not None and (
                opened.st_dev,
                opened.st_ino,
            ) != (inspected.st_dev, inspected.st_ino):
                os.close(descriptor)
                raise ShadowLockError(
                    "shadow lock path changed during open"
                )
            handle = os.fdopen(
                descriptor,
                "r+",
                encoding="utf-8",
                newline="\n",
            )
            self._require_path_identity(handle.fileno())
            fcntl.flock(
                handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB
            )
            acquired = True
            self._require_path_identity(handle.fileno())
        except BlockingIOError as exc:
            if handle is not None:
                if created:
                    self._unlink_if_same(handle.fileno())
                handle.close()
            raise ShadowLockError(
                "shadow calibration is already locked"
            ) from exc
        except ShadowLockError:
            if handle is not None:
                if acquired:
                    with suppress(OSError):
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                if created:
                    self._unlink_if_same(handle.fileno())
                handle.close()
            raise
        except (OSError, UnicodeError) as exc:
            if handle is not None:
                if acquired:
                    with suppress(OSError):
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                if created:
                    self._unlink_if_same(handle.fileno())
                handle.close()
            raise ShadowLockError(
                "shadow lock could not be acquired"
            ) from exc
        handle.seek(0)
        existing_text = handle.read(65_537)
        if len(existing_text) > 65_536:
            self._release_failed_enter(handle, created)
            raise ShadowLockError(
                "existing shadow lock metadata is too large"
            )
        if existing_text:
            try:
                existing = json.loads(existing_text)
                if (
                    not isinstance(existing, dict)
                    or set(existing)
                    != {
                        "schema_version",
                        "pid",
                        "host",
                        "started_at",
                    }
                    or existing.get("schema_version") != 1
                    or not isinstance(existing.get("pid"), int)
                    or isinstance(existing.get("pid"), bool)
                    or cast(int, existing["pid"]) <= 0
                    or not isinstance(existing.get("host"), str)
                    or not cast(str, existing["host"]).strip()
                    or not isinstance(existing.get("started_at"), str)
                ):
                    raise ValueError
                _parse_timestamp(cast(str, existing["started_at"]))
            except (
                json.JSONDecodeError,
                KeyError,
                ShadowStateError,
                ValueError,
            ) as exc:
                self._release_failed_enter(handle, created)
                raise ShadowLockError(
                    "existing shadow lock metadata is invalid"
                ) from exc
            host = cast(str, existing["host"])
            pid = cast(int, existing["pid"])
            if host != socket.gethostname():
                self._release_failed_enter(handle, created)
                raise ShadowLockError(
                    "foreign-host shadow lock requires human action"
                )
            if _pid_exists(pid):
                self._release_failed_enter(handle, created)
                raise ShadowLockError(
                    "shadow lock records a live local process"
                )
        elif not created:
            self._release_failed_enter(handle, created)
            raise ShadowLockError(
                "existing shadow lock metadata is invalid"
            )
        metadata = {
            "schema_version": 1,
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "started_at": _utc_string(self.utc_now()),
        }
        self._require_path_identity(handle.fileno())
        handle.seek(0)
        handle.truncate()
        handle.write(
            json.dumps(metadata, ensure_ascii=True, sort_keys=True)
            + "\n"
        )
        handle.flush()
        os.fsync(handle.fileno())
        _fsync_directory(self.path.parent)
        opened = os.fstat(handle.fileno())
        self.handle = handle
        self.device_inode = (opened.st_dev, opened.st_ino)
        return self

    def __exit__(
        self,
        exc_type: object,
        exc: object,
        traceback: object,
    ) -> None:
        if self.handle is None:
            return
        handle = self.handle
        identity_error = False
        try:
            self._require_path_identity(handle.fileno())
            self._unlink_if_same(handle.fileno(), required=True)
            _fsync_directory(self.path.parent)
        except ShadowLockError:
            identity_error = True
        finally:
            with suppress(OSError):
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
        self.handle = None
        self.device_inode = None
        if identity_error:
            raise ShadowLockError(
                "shadow lock path changed before release"
            )

    def _require_path_identity(self, descriptor: int) -> None:
        try:
            opened = os.fstat(descriptor)
            current = self.path.lstat()
        except OSError as exc:
            raise ShadowLockError(
                "shadow lock path identity could not be verified"
            ) from exc
        if (
            not stat.S_ISREG(opened.st_mode)
            or stat.S_ISLNK(current.st_mode)
            or not stat.S_ISREG(current.st_mode)
            or (opened.st_dev, opened.st_ino)
            != (current.st_dev, current.st_ino)
        ):
            raise ShadowLockError(
                "shadow lock path identity changed"
            )

    def _unlink_if_same(
        self,
        descriptor: int,
        *,
        required: bool = False,
    ) -> None:
        try:
            opened = os.fstat(descriptor)
            current = self.path.lstat()
            same = (
                stat.S_ISREG(opened.st_mode)
                and not stat.S_ISLNK(current.st_mode)
                and stat.S_ISREG(current.st_mode)
                and (opened.st_dev, opened.st_ino)
                == (current.st_dev, current.st_ino)
            )
            if same:
                self.path.unlink()
            elif required:
                raise ShadowLockError(
                    "shadow lock path identity changed"
                )
        except FileNotFoundError:
            if required:
                raise ShadowLockError(
                    "shadow lock path disappeared"
                ) from None
        except ShadowLockError:
            raise
        except OSError as exc:
            if required:
                raise ShadowLockError(
                    "shadow lock could not be unlinked safely"
                ) from exc

    def _release_failed_enter(
        self,
        handle: IO[str],
        created: bool,
    ) -> None:
        if created:
            self._unlink_if_same(handle.fileno())
        with suppress(OSError):
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _complete_stage1_artifacts_present(directory: Path) -> bool:
    try:
        return directory.is_dir() and {
            path.name for path in directory.iterdir()
        } == STAGE2_STAGE1_ARTIFACT_NAMES
    except OSError:
        return False


def _require_exact_directory(
    directory: Path,
    names: frozenset[str],
) -> None:
    try:
        if not directory.is_dir():
            raise OSError
        actual = {path.name for path in directory.iterdir()}
    except OSError as exc:
        raise ShadowStateError(
            "supervisor Stage 1 artifact directory is incomplete"
        ) from exc
    if actual != names:
        raise ShadowStateError(
            "supervisor Stage 1 artifact set is not exact"
        )


ModelT = TypeVar("ModelT", bound=BaseModel)


def _model_from_json(
    path: Path,
    model: type[ModelT],
    label: str,
) -> ModelT:
    try:
        return model.model_validate(_read_json(path))
    except ValidationError as exc:
        raise ShadowStateError(f"{label} is invalid") from exc


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=_reject_json_constant,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ShadowStateError(
            "durable Stage 3 JSON artifact is missing or invalid"
        ) from exc
    if not isinstance(value, dict):
        raise ShadowStateError(
            "durable Stage 3 JSON artifact is not an object"
        )
    return cast(dict[str, Any], value)


def _read_exact_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ShadowStateError(
            "durable Stage 3 artifact is missing"
        ) from exc


def _write_json(path: Path, value: object) -> None:
    _atomic_write(path, _render_json_bytes(value))


def _render_json_bytes(value: object) -> bytes:
    rendered = (
        json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    return rendered.encode("utf-8")


def _write_text(path: Path, value: str) -> None:
    _atomic_write(path, value.encode("utf-8"))


def _write_bytes(path: Path, value: bytes) -> None:
    _atomic_write(path, value)


def _atomic_write(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except OSError as exc:
        raise ShadowStateError(
            "shadow artifact could not be written"
        ) from exc
    finally:
        with suppress(FileNotFoundError):
            temporary.unlink()


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
    if hasattr(value, "model_dump"):
        return cast(Any, value).model_dump(mode="json")
    if isinstance(value, Mapping):
        return {
            str(key): _json_compatible(item)
            for key, item in value.items()
        }
    if isinstance(value, (tuple, list)):
        return [_json_compatible(item) for item in value]
    return value


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


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ShadowStateError(
            "shadow journal timestamp is invalid"
        ) from exc
    if parsed.tzinfo is None:
        raise ShadowStateError(
            "shadow journal timestamp lacks a timezone"
        )
    return parsed.astimezone(UTC)


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")


def _utc_string(value: datetime) -> str:
    return (
        value.astimezone(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
