"""Crash-aware Stage 4 live quarantined shadow-observation engine."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, TypeVar, cast
from uuid import UUID

from pydantic import BaseModel, ValidationError

from research_automation_supervisor.codex_adapter import (
    CodexProcessLaunch,
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
    LiveShadowDependencyError,
    LiveShadowInputError,
    LiveShadowIntegrityError,
    LiveShadowLockError,
    LiveShadowStateError,
    ShadowInputError,
    ShadowLockError,
    ShadowStateError,
    WorkflowStateError,
)
from research_automation_supervisor.git_evidence import GitBaseline
from research_automation_supervisor.live_shadow_isolation import (
    BubblewrapBackendIdentity,
    BubblewrapCapability,
    IsolationPreflight,
    build_bubblewrap_process_launch,
    load_backend_identity,
    preflight_bubblewrap_isolation,
    validate_runtime_home_contents,
    verify_recorded_bubblewrap_command,
    write_backend_identity,
)
from research_automation_supervisor.live_shadow_models import (
    AuthoritativeLaunchRecord,
    AuthoritativeRunRecord,
    AuthoritativeTerminalRecord,
    FailedSupervisorActionRecord,
    LiveComparisonUnavailableRecord,
    LiveDecisionEnvelope,
    LiveReadinessReport,
    LiveShadowFailure,
    LiveShadowJournalEntry,
    LiveShadowResult,
    LiveShadowState,
)
from research_automation_supervisor.live_shadow_prompts import (
    RenderedLiveBlindPrompt,
    build_live_blind_supervisor_prompt,
)
from research_automation_supervisor.live_shadow_review import (
    calculate_live_readiness,
    load_live_shadow_review,
)
from research_automation_supervisor.live_shadow_sources import (
    PreparedLiveShadowSpecification,
    build_live_decision_envelope,
    load_live_shadow_specification,
)
from research_automation_supervisor.shadow_confidentiality import (
    preflight_shadow_confidentiality,
    preflight_shadow_locator,
)
from research_automation_supervisor.shadow_engine import (
    _assert_no_authoritative_material,
    _build_comparison,
    _session_integrity,
    _ShadowLock,
    _verify_supervisor_artifacts,
    assess_normalized_supervisor_proposal,
)
from research_automation_supervisor.shadow_models import (
    DecisionPoint,
    DeterministicAssessment,
    HumanReview,
    NormalizedSupervisorProposal,
    PendingSupervisorAction,
    ProposalComparison,
    RequiredCheckCoverage,
    SupervisorActionRecord,
)
from research_automation_supervisor.shadow_sources import (
    DecisionReconstruction,
    reconstruct_decision_points,
)
from research_automation_supervisor.workflow_engine import (
    _validate_journal_semantics,
    _verify_journal_hash_mapping,
    read_stage2_source_for_shadow,
    workflow_exit_code,
)
from research_automation_supervisor.workflow_integrity import (
    CodexMetadata,
    JournalEntry,
    parse_journal_entry,
    sha256_regular_file,
    verify_hash_mapping,
)
from research_automation_supervisor.workflow_models import WorkflowState

ZERO_HASH = "0" * 64
STATE_FILE = "state.json"
RESULT_FILE = "result.json"
JOURNAL_FILE = "journal.jsonl"
LOCK_FILE = "live-shadow.lock"
MAX_STAGE2_JOURNAL_ENTRY_BYTES = 4 * 1024 * 1024
DEFAULT_LIVE_SHADOW_RUNS_DIRECTORY = (
    Path(tempfile.gettempdir()) / "research-automation-supervisor-live-shadow"
)

TERMINAL_LIVE_STATUSES = frozenset(
    {
        "awaiting_reviews",
        "completed",
        "shadow_degraded",
        "human_paused",
        "failed",
        "aborted",
    }
)


class SupervisorInvoker(Protocol):
    """Injectable Stage 1 supervisor boundary for offline fake-Codex tests."""

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
class LiveShadowServices:
    """Injectable process, identity, clock, and polling boundaries."""

    codex_executable: str | None = None
    supervisor_invoker: SupervisorInvoker | None = None
    isolation_preflight: IsolationPreflight = preflight_bubblewrap_isolation
    bubblewrap_executable: str | None = None
    codex_authentication_file: Path | None = None
    environ: Mapping[str, str] | None = None
    authoritative_environ: Mapping[str, str] | None = None
    token_factory: Callable[[], str] = lambda: secrets.token_hex(16)
    utc_now: Callable[[], datetime] = lambda: datetime.now(UTC)
    monotonic: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    python_executable: str = sys.executable


DEFAULT_LIVE_SHADOW_SERVICES = LiveShadowServices()


@dataclass
class _LiveContext:
    prepared: PreparedLiveShadowSpecification
    run_directory: Path
    stage2_runs_directory: Path
    state: LiveShadowState
    codex_executable: str
    services: LiveShadowServices
    isolation_capability: BubblewrapCapability | None
    isolation_dependency_failure: str | None = None
    authoritative_process: subprocess.Popen[bytes] | None = None
    supervisor_task: _SupervisorTask | None = None


@dataclass
class _SupervisorTask:
    pending: PendingSupervisorAction
    thread: threading.Thread
    returned_result: CodexRunResult | None = None
    error: BaseException | None = None


def validate_live_shadow_spec(
    path: Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> PreparedLiveShadowSpecification:
    """Validate all frozen Stage 4 inputs without writing or launching."""
    return load_live_shadow_specification(path, environ=environ)


def run_live_shadow(
    path: Path,
    *,
    runs_dir: Path = DEFAULT_LIVE_SHADOW_RUNS_DIRECTORY,
    stage2_runs_dir: Path = Path("runs/workflows"),
    services: LiveShadowServices = DEFAULT_LIVE_SHADOW_SERVICES,
) -> LiveShadowResult:
    """Launch one independent Stage 2 child and observe its durable journal."""
    sensitive_values = _sensitive_values(services.environ)
    raw_path = _preflight_locator(path, sensitive_values, "live-shadow specification")
    raw_runs = _preflight_locator(runs_dir, sensitive_values, "live-shadow runs directory")
    raw_stage2_runs = _preflight_locator(
        stage2_runs_dir,
        sensitive_values,
        "Stage 2 runs directory",
    )
    prepared = load_live_shadow_specification(
        Path(raw_path),
        environ=services.environ,
        require_clean=True,
    )
    executable = _resolve_codex_executable(
        services.codex_executable,
        sensitive_values,
    )
    token = services.token_factory()
    if (
        not token
        or len(token) > 80
        or not token.replace("-", "").replace("_", "").isalnum()
    ):
        raise LiveShadowInputError("live-shadow run token is invalid")
    try:
        resolved_runs = Path(raw_runs).resolve(strict=False)
        resolved_stage2_runs = Path(raw_stage2_runs).resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise LiveShadowInputError("run directory could not be resolved") from exc
    run_directory = resolved_runs / f"{prepared.specification.live_shadow_id}-{token}"
    _validate_run_root_separation(
        run_directory,
        resolved_stage2_runs,
        prepared,
    )
    capability = services.isolation_preflight(
        bubblewrap_executable=services.bubblewrap_executable,
        codex_executable=executable,
        authentication_file=services.codex_authentication_file,
        environ=services.environ,
        forbidden_roots=(
            prepared.stage2.repository_root,
            resolved_stage2_runs,
        ),
    )
    try:
        preflight_shadow_confidentiality(
            (
                raw_runs,
                raw_stage2_runs,
                str(resolved_runs),
                str(resolved_stage2_runs),
                str(run_directory),
                token,
                executable,
            ),
            sensitive_values,
            label="prospective live-shadow paths",
        )
    except ShadowInputError as exc:
        raise LiveShadowInputError(str(exc)) from exc
    try:
        resolved_runs.mkdir(parents=True, exist_ok=True)
        resolved_stage2_runs.mkdir(parents=True, exist_ok=True)
        run_directory.mkdir(exist_ok=False)
    except FileExistsError as exc:
        raise LiveShadowInputError(
            "exclusive live-shadow run directory already exists"
        ) from exc
    except OSError as exc:
        raise LiveShadowInputError("live-shadow run directory could not be created") from exc

    now = _utc_string(services.utc_now())
    state = _initial_state(run_directory, prepared, token=token, timestamp=now)
    _initialize_artifacts(run_directory, prepared, capability.identity)
    _persist_state(run_directory, state, prepared)
    state = _journal_event(
        run_directory,
        state,
        prepared,
        event_type="transition",
        reason="live_shadow_initialized",
        updates={},
        artifact_hashes=_initial_artifact_hashes(run_directory),
        utc_now=services.utc_now,
    )
    context = _LiveContext(
        prepared=prepared,
        run_directory=run_directory,
        stage2_runs_directory=resolved_stage2_runs,
        state=state,
        codex_executable=executable,
        services=services,
        isolation_capability=capability,
    )
    context.state = _transition(
        context,
        "authoritative_starting",
        "authoritative_launch_prepared",
        "Authoritative Stage 2 launch is being durably prepared.",
    )
    _launch_authoritative(context)
    return _drive(context)


def resume_live_shadow(
    run_directory: Path,
    *,
    services: LiveShadowServices = DEFAULT_LIVE_SHADOW_SERVICES,
) -> LiveShadowResult:
    """Reattach observation without ever launching a second Stage 2 run."""
    sensitive_values = _sensitive_values(services.environ)
    raw_run = _preflight_locator(run_directory, sensitive_values, "live-shadow run")
    resolved = _resolve_run_directory(Path(raw_run))
    state, prepared = _load_reconciled_run(resolved, services)
    if state.status in TERMINAL_LIVE_STATUSES:
        raise LiveShadowInputError("live-shadow state cannot be resumed automatically")
    launch_path = resolved / "authoritative" / "launch.json"
    if not launch_path.is_file():
        intent = _load_model(
            resolved / "authoritative" / "launch-intent.json",
            AuthoritativeLaunchRecord,
            "authoritative launch intent",
        )
        if intent.launch_state != "prepared":
            raise LiveShadowIntegrityError(
                "authoritative launch intent is contradictory"
            )
        state = _transition_state_only(
            resolved,
            state,
            prepared,
            "human_paused",
            "authoritative_launch_unproven",
            "An authoritative launch was prepared but cannot be proven; it will not be repeated.",
            services.utc_now,
        )
        return _result_for_state(state, prepared)
    launch = _load_model(
        launch_path,
        AuthoritativeLaunchRecord,
        "authoritative launch",
    )
    if launch.launch_state != "launched":
        raise LiveShadowIntegrityError(
            "authoritative launch record is not a launched identity"
        )
    if state.status == "authoritative_starting":
        state = _journal_event(
            resolved,
            state,
            prepared,
            event_type="authoritative_launch",
            reason="authoritative_stage2_launch_recovered",
            updates={
                "status": "authoritative_running",
                "summary": (
                    "Recovered the proven authoritative Stage 2 launch "
                    "without relaunching it."
                ),
            },
            artifact_hashes={
                str(resolved / "authoritative" / "launch.json"):
                sha256_regular_file(
                    resolved / "authoritative" / "launch.json"
                )
            },
            utc_now=services.utc_now,
        )
    codex_executable = services.codex_executable or launch.codex_executable
    capability: BubblewrapCapability | None = None
    isolation_failure: str | None = None
    isolation_needed = (
        state.authoritative_status is None
        or len(state.proposal_ids) < len(state.observed_decision_ids)
    )
    if isolation_needed:
        try:
            resolved_codex = _resolve_codex_executable(
                codex_executable,
                sensitive_values,
            )
            capability = services.isolation_preflight(
                bubblewrap_executable=services.bubblewrap_executable,
                codex_executable=resolved_codex,
                authentication_file=services.codex_authentication_file,
                environ=services.environ,
                forbidden_roots=(
                    prepared.stage2.repository_root,
                    Path(launch.stage2_runs_directory),
                ),
            )
            recorded_identity = load_backend_identity(
                resolved / "isolation.json"
            )
            if capability.identity != recorded_identity:
                raise LiveShadowDependencyError(
                    "Bubblewrap isolation backend identity changed"
                )
            codex_executable = resolved_codex
        except LiveShadowDependencyError:
            isolation_failure = (
                "Bubblewrap isolation is unavailable during live-shadow recovery."
            )
    else:
        codex_executable = launch.codex_executable
    context = _LiveContext(
        prepared=prepared,
        run_directory=resolved,
        stage2_runs_directory=Path(launch.stage2_runs_directory),
        state=state,
        codex_executable=codex_executable,
        services=services,
        isolation_capability=capability,
        isolation_dependency_failure=isolation_failure,
    )
    if (
        isolation_failure is not None
        and not any(
            failure.reason == "isolation_dependency_failure"
            for failure in context.state.shadow_failures
        )
    ):
        context.state = _record_shadow_failure(
            context,
            decision_id=None,
            reason="isolation_dependency_failure",
            detail=isolation_failure,
            temporal_or_integrity=True,
        )
    return _drive(context)


def live_shadow_status(run_directory: Path) -> LiveShadowResult:
    """Read and integrity-check Stage 4 without writes or launches."""
    sensitive_values = _sensitive_values(None)
    raw_run = _preflight_locator(run_directory, sensitive_values, "live-shadow run")
    resolved = _resolve_run_directory(Path(raw_run))
    state, prepared = _load_stable_run(
        resolved,
        DEFAULT_LIVE_SHADOW_SERVICES,
    )
    return _result_for_state(state, prepared)


def record_live_shadow_review(
    run_directory: Path,
    proposal_id: str,
    review_path: Path,
    *,
    services: LiveShadowServices = DEFAULT_LIVE_SHADOW_SERVICES,
) -> LiveShadowResult:
    """Record one immutable Stage 3-format human review."""
    sensitive_values = _sensitive_values(services.environ)
    raw_run = _preflight_locator(run_directory, sensitive_values, "live-shadow run")
    _preflight_locator(review_path, sensitive_values, "live-shadow review")
    try:
        preflight_shadow_confidentiality(
            proposal_id,
            sensitive_values,
            label="live-shadow proposal ID",
        )
    except ShadowInputError as exc:
        raise LiveShadowInputError(str(exc)) from exc
    resolved = _resolve_run_directory(Path(raw_run))
    state, prepared = _load_reconciled_run(resolved, services)
    if proposal_id not in state.comparison_ids:
        raise LiveShadowInputError("proposal has no finalized comparison")
    comparison = _load_comparison(resolved, proposal_id)
    if not comparison.comparison_available:
        raise LiveShadowInputError("proposal comparison is unavailable")
    destination = resolved / "reviews" / f"{proposal_id}.json"
    if proposal_id in state.reviewed_proposal_ids:
        raise LiveShadowInputError("proposal already has an immutable review")
    review = load_live_shadow_review(review_path, sensitive_values=sensitive_values)
    if review.proposal_id != proposal_id:
        raise LiveShadowInputError("review proposal ID does not match the command")
    _write_immutable_json(destination, review.model_dump(mode="json"))
    reviewed = (*state.reviewed_proposal_ids, proposal_id)
    comparable = {
        item
        for item in state.comparison_ids
        if _load_comparison(resolved, item).comparison_available
    }
    new_status = state.status
    if state.status == "awaiting_reviews" and comparable.issubset(reviewed):
        new_status = "completed"
    updates: dict[str, object] = {
        "reviewed_proposal_ids": reviewed,
        "summary": (
            "All comparison-available live proposals have immutable human reviews."
            if new_status == "completed"
            else "Immutable live-shadow review recorded."
        ),
    }
    if new_status != state.status:
        updates["status"] = new_status
    state = _journal_event(
        resolved,
        state,
        prepared,
        event_type="review",
        reason="human_review_recorded",
        action_id=None,
        decision_id=proposal_id,
        updates=updates,
        artifact_hashes={str(destination): sha256_regular_file(destination)},
        utc_now=services.utc_now,
    )
    return _result_for_state(state, prepared)


def live_shadow_report(run_directory: Path) -> dict[str, object]:
    """Build a deterministic report overlay without writing any artifact."""
    sensitive_values = _sensitive_values(None)
    raw_run = _preflight_locator(run_directory, sensitive_values, "live-shadow run")
    resolved = _resolve_run_directory(Path(raw_run))
    state, prepared = _load_stable_run(
        resolved,
        DEFAULT_LIVE_SHADOW_SERVICES,
    )
    readiness = _readiness_for_state(state, prepared)
    assessments = {
        proposal_id: _load_assessment(resolved, proposal_id)
        for proposal_id in state.comparison_ids
    }
    comparisons = {
        proposal_id: _load_comparison(resolved, proposal_id)
        for proposal_id in state.comparison_ids
    }
    report = {
        "schema_version": 1,
        "live_shadow_id": state.live_shadow_id,
        "run_token": state.run_token,
        "status": state.status,
        "authoritative": {
            "run_directory": state.authoritative_run_directory,
            "status": state.authoritative_status,
            "pause_reason": state.authoritative_pause_reason,
            "result_sha256": state.authoritative_result_sha256,
            "process_exit_code": state.authoritative_process_exit_code,
        },
        "readiness": readiness.model_dump(mode="json"),
        "assessments": [
            {
                **assessments[proposal_id].model_dump(mode="json"),
                "review_status": (
                    "reviewed"
                    if proposal_id in state.reviewed_proposal_ids
                    else "unreviewed"
                ),
            }
            for proposal_id in state.comparison_ids
        ],
        "comparisons": [
            comparisons[proposal_id].model_dump(mode="json")
            for proposal_id in state.comparison_ids
        ],
        "comparison_unavailable_records": [
            _load_unavailable_record(
                resolved,
                proposal_id,
            ).model_dump(mode="json")
            for proposal_id in state.comparison_ids
            if comparisons[
                proposal_id
            ].comparison_unavailable_reason
            == "authoritative_action_unfinished_after_terminal"
        ],
        "shadow_failures": [
            failure.model_dump(mode="json") for failure in state.shadow_failures
        ],
        "automation_enabled": False,
    }
    try:
        preflight_shadow_confidentiality(
            report,
            sensitive_values,
            label="live-shadow report",
            integrity=True,
        )
    except ShadowStateError as exc:
        raise LiveShadowIntegrityError(
            "live-shadow report failed confidentiality preflight"
        ) from exc
    return report


def abort_live_shadow(
    run_directory: Path,
    reason: str,
    *,
    services: LiveShadowServices = DEFAULT_LIVE_SHADOW_SERVICES,
) -> LiveShadowResult:
    """Stop Stage 4 observation without signaling or modifying Stage 2."""
    sensitive_values = _sensitive_values(services.environ)
    raw_run = _preflight_locator(run_directory, sensitive_values, "live-shadow run")
    try:
        preflight_shadow_confidentiality(
            reason,
            sensitive_values,
            label="abort reason",
        )
    except ShadowInputError as exc:
        raise LiveShadowInputError(str(exc)) from exc
    sanitized = " ".join(reason.split()).strip()
    if not sanitized:
        raise LiveShadowInputError("abort reason must not be empty")
    resolved = _resolve_run_directory(Path(raw_run))
    for attempt in range(100):
        try:
            state, prepared = _load_reconciled_run(resolved, services)
        except LiveShadowLockError:
            if attempt == 99:
                raise
            services.sleep(0.005)
            continue
        if state.status in {"completed", "failed", "aborted"}:
            raise LiveShadowInputError("terminal live-shadow run cannot be aborted")
        try:
            state = _journal_event(
                resolved,
                state,
                prepared,
                event_type="transition",
                reason="observer_aborted",
                updates={
                    "status": "aborted",
                    "pause_reason": sanitized[:16384],
                    "summary": (
                        "Live-shadow observation aborted; authoritative "
                        "Stage 2 was untouched."
                    ),
                },
                artifact_hashes={},
                utc_now=services.utc_now,
            )
        except LiveShadowStateError as exc:
            if (
                str(exc)
                != "live-shadow state changed before journal append"
                or attempt == 99
            ):
                raise
            services.sleep(0.005)
            continue
        except LiveShadowLockError:
            if attempt == 99:
                raise
            services.sleep(0.005)
            continue
        return _result_for_state(state, prepared)
    raise LiveShadowLockError("live-shadow observer did not yield for abort")


def live_shadow_exit_code(status: str) -> int:
    """Map Stage 4 state to its frozen public exit contract."""
    return {
        "completed": 0,
        "awaiting_reviews": 5,
        "shadow_degraded": 5,
        "human_paused": 5,
        "failed": 4,
        "aborted": 8,
        "initialized": 4,
        "authoritative_starting": 4,
        "authoritative_running": 4,
        "authoritative_terminal_shadow_pending": 4,
    }[status]


def _initial_state(
    run_directory: Path,
    prepared: PreparedLiveShadowSpecification,
    *,
    token: str,
    timestamp: str,
) -> LiveShadowState:
    return LiveShadowState(
        live_shadow_id=prepared.specification.live_shadow_id,
        run_token=token,
        status="initialized",
        specification_path=str(prepared.specification_path),
        specification_sha256=prepared.specification_sha256,
        policy_path=str(prepared.policy.path),
        policy_sha256=prepared.policy.sha256,
        context_hashes=prepared.context_manifests(),
        stage2_specification_path=str(prepared.stage2.specification_path),
        stage2_specification_sha256=prepared.stage2.specification_sha256,
        authoritative_run_directory=None,
        authoritative_substage_id=prepared.stage2.specification.substage_id,
        authoritative_status=None,
        authoritative_pause_reason=None,
        authoritative_result_sha256=None,
        authoritative_process_exit_code=None,
        authoritative_terminal_at=None,
        supervisor_model=prepared.specification.supervisor_model,
        supervisor_reasoning_effort=(
            prepared.specification.supervisor_reasoning_effort
        ),
        supervisor_session_id=None,
        observed_decision_ids=(),
        proposal_ids=(),
        comparison_ids=(),
        reviewed_proposal_ids=(),
        disqualified_proposal_ids=(),
        shadow_failures=(),
        pending_action=None,
        journal_read_offset=0,
        authoritative_journal_sequence=0,
        authoritative_journal_hash=ZERO_HASH,
        max_proposal_bytes=prepared.specification.max_proposal_bytes,
        minimum_reviewed_proposals=(
            prepared.specification.minimum_reviewed_proposals
        ),
        required_consecutive_acceptable=(
            prepared.specification.required_consecutive_acceptable
        ),
        artifact_directory=str(run_directory),
        pause_reason=None,
        summary="Live-shadow observation initialized.",
        journal_sequence=0,
        journal_hash=ZERO_HASH,
        started_at=timestamp,
        updated_at=timestamp,
    )


def _initialize_artifacts(
    run_directory: Path,
    prepared: PreparedLiveShadowSpecification,
    isolation_identity: BubblewrapBackendIdentity,
) -> None:
    for name in (
        "authoritative",
        "decisions",
        "proposals",
        "comparisons",
        "reviews",
        "reports",
        "escalation",
    ):
        (run_directory / name).mkdir(parents=True, exist_ok=False)
    quarantine = run_directory / "quarantine"
    quarantine.mkdir(exist_ok=False)
    (quarantine / "workspace").mkdir(exist_ok=False)
    runtime_home = quarantine / "codex-home"
    runtime_home.mkdir(mode=0o700, exist_ok=False)
    _write_json(
        run_directory / "live-shadow-spec.normalized.json",
        prepared.normalized_dict(),
    )
    _write_text(
        run_directory / "live-shadow-spec.sha256",
        f"{prepared.specification_sha256}\n",
    )
    _write_text(run_directory / "policy.sha256", f"{prepared.policy.sha256}\n")
    _write_json(
        run_directory / "context.sha256.json",
        {
            "schema_version": 1,
            "files": [
                manifest.model_dump(mode="json")
                for manifest in prepared.context_manifests()
            ],
        },
    )
    _write_json(
        run_directory / "source-inputs.sha256.json",
        {
            "schema_version": 1,
            "stage2_specification": prepared.stage2.specification_sha256,
            "contract": prepared.stage2.contract.sha256,
            "worker_initial": prepared.stage2.worker_initial_prompt.sha256,
            "worker_repair": prepared.stage2.worker_repair_prompt.sha256,
            "auditor": prepared.stage2.auditor_prompt.sha256,
        },
    )
    _write_text(run_directory / JOURNAL_FILE, "")
    write_backend_identity(run_directory / "isolation.json", isolation_identity)


def _initial_artifact_hashes(run_directory: Path) -> dict[str, str]:
    return {
        str(run_directory / name): sha256_regular_file(run_directory / name)
        for name in (
            "live-shadow-spec.normalized.json",
            "live-shadow-spec.sha256",
            "policy.sha256",
            "context.sha256.json",
            "source-inputs.sha256.json",
            "isolation.json",
        )
    }


def _launch_authoritative(context: _LiveContext) -> None:
    authoritative_directory = context.run_directory / "authoritative"
    launch_path = authoritative_directory / "launch.json"
    intent_path = authoritative_directory / "launch-intent.json"
    known = tuple(
        sorted(
            str(path.resolve())
            for path in context.stage2_runs_directory.iterdir()
            if path.is_dir()
        )
    )
    prepared_launch = AuthoritativeLaunchRecord(
        launch_state="prepared",
        specification_path=str(context.prepared.stage2.specification_path),
        specification_sha256=context.prepared.stage2.specification_sha256,
        stage2_runs_directory=str(context.stage2_runs_directory),
        codex_executable=context.codex_executable,
        pid=None,
        process_group_id=None,
        session_id=None,
        process_start_ticks=None,
        started_at=None,
        known_run_directories=known,
    )
    _write_immutable_json(
        intent_path,
        prepared_launch.model_dump(mode="json"),
    )
    context.state = _journal_event(
        context.run_directory,
        context.state,
        context.prepared,
        event_type="authoritative_launch",
        reason="authoritative_stage2_launch_intent",
        updates={
            "summary": (
                "The single authoritative Stage 2 launch intent is durable."
            ),
        },
        artifact_hashes={
            str(intent_path): sha256_regular_file(intent_path)
        },
        utc_now=context.services.utc_now,
    )
    child_source = (
        "import sys\n"
        "from pathlib import Path\n"
        "from research_automation_supervisor.workflow_engine import "
        "run_substage, workflow_exit_code, WorkflowServices\n"
        "result = run_substage(Path(sys.argv[1]), runs_dir=Path(sys.argv[2]), "
        "services=WorkflowServices(codex_executable=sys.argv[3]))\n"
        "raise SystemExit(workflow_exit_code(result.status))\n"
    )
    environment_source = (
        os.environ
        if context.services.authoritative_environ is None
        else context.services.authoritative_environ
    )
    environment = dict(environment_source)
    try:
        process = subprocess.Popen(
            [
                context.services.python_executable,
                "-c",
                child_source,
                str(context.prepared.stage2.specification_path),
                str(context.stage2_runs_directory),
                context.codex_executable,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=context.run_directory / "authoritative",
            env=environment,
            start_new_session=True,
        )
    except OSError as exc:
        raise LiveShadowDependencyError(
            "authoritative Stage 2 child could not be launched"
        ) from exc
    context.authoritative_process = process
    start_ticks = _process_start_ticks(process.pid)
    if start_ticks is None:
        # The child is deliberately not signaled: authoritative independence wins
        # even when Stage 4 cannot prove its process identity.
        context.state = _transition(
            context,
            "human_paused",
            "authoritative_launch_identity_unavailable",
            "Stage 2 was launched but its process identity could not be proven.",
        )
        return
    started = _utc_string(context.services.utc_now())
    launched = prepared_launch.model_copy(
        update={
            "launch_state": "launched",
            "pid": process.pid,
            "process_group_id": process.pid,
            "session_id": process.pid,
            "process_start_ticks": start_ticks,
            "started_at": started,
        }
    )
    _write_immutable_json(
        launch_path,
        launched.model_dump(mode="json"),
    )
    context.state = _journal_event(
        context.run_directory,
        context.state,
        context.prepared,
        event_type="authoritative_launch",
        reason="authoritative_stage2_launched",
        updates={
            "status": "authoritative_running",
            "summary": "Authoritative Stage 2 is running independently.",
        },
        artifact_hashes={str(launch_path): sha256_regular_file(launch_path)},
        utc_now=context.services.utc_now,
    )


def _discover_authoritative_run(context: _LiveContext) -> None:
    if context.state.authoritative_run_directory is not None:
        return
    launch = _load_model(
        context.run_directory / "authoritative" / "launch.json",
        AuthoritativeLaunchRecord,
        "authoritative launch",
    )
    known = set(launch.known_run_directories)
    candidates: list[tuple[Path, WorkflowState]] = []
    try:
        entries = tuple(context.stage2_runs_directory.iterdir())
    except OSError as exc:
        raise LiveShadowStateError("Stage 2 runs directory cannot be inspected") from exc
    for path in entries:
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if str(resolved) in known or not resolved.is_dir():
            continue
        state_path = resolved / "state.json"
        if not state_path.is_file():
            continue
        try:
            state = WorkflowState.model_validate(_read_json(state_path))
        except (ValidationError, LiveShadowStateError):
            continue
        if (
            state.substage_id == context.prepared.stage2.specification.substage_id
            and state.specification_sha256
            == context.prepared.stage2.specification_sha256
            and state.workspace == str(context.prepared.stage2.workspace)
        ):
            candidates.append((resolved, state))
    if not candidates:
        return
    if len(candidates) != 1:
        raise LiveShadowIntegrityError(
            "authoritative Stage 2 run discovery is ambiguous"
        )
    run_directory, stage2_state = candidates[0]
    record = AuthoritativeRunRecord(
        run_directory=str(run_directory),
        substage_id=stage2_state.substage_id,
        run_token=stage2_state.run_token,
        specification_sha256=stage2_state.specification_sha256,
        baseline_commit=stage2_state.baseline_commit,
        started_at=stage2_state.started_at,
        discovered_at=_utc_string(context.services.utc_now()),
    )
    record_path = context.run_directory / "authoritative" / "stage2-run.json"
    if record_path.exists():
        existing_record = _load_model(
            record_path,
            AuthoritativeRunRecord,
            "authoritative run",
        )
        if existing_record.model_dump(
            mode="json",
            exclude={"discovered_at"},
        ) != record.model_dump(
            mode="json",
            exclude={"discovered_at"},
        ):
            raise LiveShadowIntegrityError(
                "existing authoritative run record contradicts discovery"
            )
        record = existing_record
    else:
        _write_json(record_path, record.model_dump(mode="json"))
    context.state = _journal_event(
        context.run_directory,
        context.state,
        context.prepared,
        event_type="authoritative_discovered",
        reason="authoritative_run_discovered",
        updates={
            "authoritative_run_directory": str(run_directory),
            "summary": "Authoritative Stage 2 run discovered and journal observation attached.",
        },
        artifact_hashes={str(record_path): sha256_regular_file(record_path)},
        utc_now=context.services.utc_now,
    )


def _drive(context: _LiveContext) -> LiveShadowResult:
    poll_seconds = (
        context.prepared.specification.observer_poll_interval_milliseconds / 1000.0
    )
    consecutive_lock_failures = 0
    while True:
        durable = _load_state(context.run_directory)
        if durable != context.state:
            if durable.status == "aborted":
                context.state = durable
                return _result_for_state(durable, context.prepared)
            raise LiveShadowStateError("live-shadow state changed during active observation")
        if context.state.status in TERMINAL_LIVE_STATUSES:
            return _result_for_state(context.state, context.prepared)
        progressed = False
        try:
            before = context.state
            _discover_authoritative_run(context)
            progressed = progressed or context.state != before
            if context.state.authoritative_run_directory is not None:
                while _observe_next_authoritative_entry(context):
                    progressed = True
                    _finalize_ready_comparisons(context)
                    _finish_supervisor_if_ready(context)
                    _launch_next_supervisor_if_ready(context)
            _finish_supervisor_if_ready(context)
            _launch_next_supervisor_if_ready(context)
            running = _authoritative_process_running(context)
            if not running:
                if context.state.authoritative_run_directory is None:
                    context.state = _transition(
                        context,
                        "human_paused",
                        "authoritative_launch_unproven",
                        "The Stage 2 child ended before its run identity could be proven.",
                    )
                    continue
                if context.state.authoritative_status is None:
                    _record_authoritative_terminal(context)
                    progressed = True
                while _observe_next_authoritative_entry(context):
                    progressed = True
                    _finalize_ready_comparisons(context)
                    _finish_supervisor_if_ready(context)
                    _launch_next_supervisor_if_ready(context)
                _finalize_ready_comparisons(context)
                _finish_supervisor_if_ready(context)
                _launch_next_supervisor_if_ready(context)
                _finalize_after_authoritative(context)
                if context.state.status in TERMINAL_LIVE_STATUSES:
                    continue
        except LiveShadowIntegrityError:
            raise
        except LiveShadowInputError as exc:
            raise LiveShadowIntegrityError(
                "live-shadow runtime input invariant failed"
            ) from exc
        except LiveShadowStateError:
            raise
        except LiveShadowLockError:
            consecutive_lock_failures += 1
            if consecutive_lock_failures >= 100:
                raise
            context.services.sleep(min(poll_seconds, 0.01))
            continue
        consecutive_lock_failures = 0
        if not progressed:
            context.services.sleep(poll_seconds)


def _observe_next_authoritative_entry(context: _LiveContext) -> bool:
    if context.state.status in TERMINAL_LIVE_STATUSES:
        return False
    run_value = context.state.authoritative_run_directory
    if run_value is None:
        return False
    run_directory = Path(run_value)
    journal_path = run_directory / "journal.jsonl"
    try:
        with journal_path.open("rb") as handle:
            handle.seek(context.state.journal_read_offset)
            line = handle.readline(MAX_STAGE2_JOURNAL_ENTRY_BYTES + 1)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise LiveShadowStateError("authoritative journal could not be read") from exc
    if not line:
        return False
    if len(line) > MAX_STAGE2_JOURNAL_ENTRY_BYTES:
        raise LiveShadowIntegrityError("authoritative journal entry exceeds the observer bound")
    if not line.endswith(b"\n"):
        return False
    new_offset = context.state.journal_read_offset + len(line)
    try:
        with journal_path.open("rb") as handle:
            prefix = handle.read(new_offset)
    except OSError as exc:
        raise LiveShadowStateError("authoritative journal prefix could not be frozen") from exc
    if len(prefix) != new_offset:
        raise LiveShadowIntegrityError("authoritative journal prefix changed while freezing")
    entries = _parse_stage2_prefix(run_directory, prefix)
    current = entries[-1]
    if (
        current.sequence != context.state.authoritative_journal_sequence + 1
        or current.previous_hash != context.state.authoritative_journal_hash
    ):
        raise LiveShadowIntegrityError("authoritative journal observation is discontinuous")
    updates: dict[str, object] = {
        "journal_read_offset": new_offset,
        "authoritative_journal_sequence": current.sequence,
        "authoritative_journal_hash": current.entry_hash,
    }
    event_type = "authoritative_progress"
    reason = "authoritative_journal_entry_observed"
    action_id = current.action_id
    decision_id: str | None = None
    artifact_hashes: dict[str, str] = {}
    if current.event_type == "action_intent" and current.action_kind in {
        "worker",
        "auditor",
    }:
        try:
            run_record = _load_model(
                context.run_directory / "authoritative" / "stage2-run.json",
                AuthoritativeRunRecord,
                "authoritative run",
            )
            envelope = build_live_decision_envelope(
                context.prepared,
                run_record,
                entries,
                prefix,
                live_shadow_run_id=context.state.run_token,
                sensitive_values=_sensitive_values(context.services.environ),
            )
            decision_id = envelope.decision_id
            expected_ids = (*context.state.observed_decision_ids, decision_id)
            if decision_id in context.state.observed_decision_ids:
                raise LiveShadowIntegrityError(
                    "authoritative decision was observed twice"
                )
            artifact_hashes = _freeze_decision_artifacts(context, envelope)
            updates["observed_decision_ids"] = expected_ids
            updates["summary"] = f"Frozen live decision envelope {decision_id}."
            event_type = "decision"
            reason = "live_decision_envelope_frozen"
        except LiveShadowInputError:
            updates["summary"] = (
                "A live decision envelope was withheld by confidentiality preflight."
            )
            context.state = _journal_event(
                context.run_directory,
                context.state,
                context.prepared,
                event_type="authoritative_progress",
                reason="live_decision_envelope_withheld",
                action_id=action_id,
                decision_id=None,
                updates=updates,
                artifact_hashes={},
                utc_now=context.services.utc_now,
            )
            context.state = _record_shadow_failure(
                context,
                decision_id=None,
                reason="temporal_envelope_confidentiality_failure",
                detail=(
                    "The point-in-time envelope failed confidentiality preflight "
                    "and was not sent to the supervisor."
                ),
                temporal_or_integrity=True,
            )
            return True
    context.state = _journal_event(
        context.run_directory,
        context.state,
        context.prepared,
        event_type=event_type,
        reason=reason,
        action_id=action_id,
        decision_id=decision_id,
        updates=updates,
        artifact_hashes=artifact_hashes,
        utc_now=context.services.utc_now,
    )
    return True


def _parse_stage2_prefix(
    run_directory: Path,
    content: bytes,
) -> tuple[JournalEntry, ...]:
    if not content or not content.endswith(b"\n"):
        raise LiveShadowIntegrityError("authoritative journal prefix is incomplete")
    entries: list[JournalEntry] = []
    previous_hash = ZERO_HASH
    for sequence, raw in enumerate(content.splitlines(), start=1):
        try:
            value = json.loads(
                raw.decode("ascii"),
                parse_constant=_reject_json_constant,
            )
            entry = parse_journal_entry(value)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, WorkflowStateError) as exc:
            raise LiveShadowIntegrityError("authoritative journal prefix is malformed") from exc
        body = entry.model_dump(mode="json", exclude={"entry_hash"})
        computed = hashlib.sha256(_canonical_json(body)).hexdigest()
        if (
            entry.sequence != sequence
            or entry.previous_hash != previous_hash
            or entry.entry_hash != computed
            or _canonical_json(entry.model_dump(mode="json")).rstrip(b"\n") != raw
        ):
            raise LiveShadowIntegrityError(
                "authoritative journal prefix hash chain is invalid"
            )
        try:
            _verify_journal_hash_mapping(
                entry,
                allow_missing_continuation_source=False,
            )
        except WorkflowStateError as exc:
            raise LiveShadowIntegrityError(
                "authoritative journal references invalid evidence"
            ) from exc
        previous_hash = entry.entry_hash
        entries.append(entry)
    try:
        _validate_journal_semantics(run_directory, entries, None)
    except WorkflowStateError as exc:
        raise LiveShadowIntegrityError(
            "authoritative journal prefix semantics are invalid"
        ) from exc
    return tuple(entries)


def _freeze_decision_artifacts(
    context: _LiveContext,
    envelope: LiveDecisionEnvelope,
) -> dict[str, str]:
    directory = context.run_directory / "decisions" / envelope.decision_id
    envelope_path = directory / "envelope.json"
    envelope_hash_path = directory / "envelope.sha256"
    manifest_path = directory / "blind-input-manifest.json"
    schema_path = directory / "output-schema.json"
    rendered = build_live_blind_supervisor_prompt(
        context.prepared,
        envelope,
        sensitive_values=_sensitive_values(context.services.environ),
    )
    expected: dict[Path, bytes] = {
        envelope_path: _render_json_bytes(envelope.model_dump(mode="json")),
        envelope_hash_path: f"{envelope.envelope_sha256}\n".encode("ascii"),
        manifest_path: _render_json_bytes(rendered.manifest.model_dump(mode="json")),
        schema_path: _canonical_json(rendered.output_schema),
    }
    if directory.exists():
        for path, content in expected.items():
            try:
                if path.read_bytes() != content:
                    raise LiveShadowIntegrityError(
                        "existing immutable decision artifact contradicts recapture"
                    )
            except OSError as exc:
                raise LiveShadowIntegrityError(
                    "existing immutable decision artifact is unreadable"
                ) from exc
    else:
        directory.mkdir(parents=True, exist_ok=False)
        for path, content in expected.items():
            _write_bytes(path, content)
    return {
        str(path): sha256_regular_file(path)
        for path in sorted(expected, key=str)
    }


def _launch_next_supervisor_if_ready(context: _LiveContext) -> None:
    if (
        context.supervisor_task is not None
        or context.state.pending_action is not None
        or context.state.status in TERMINAL_LIVE_STATUSES
    ):
        return
    remaining = [
        decision_id
        for decision_id in context.state.observed_decision_ids
        if decision_id not in context.state.proposal_ids
    ]
    if not remaining:
        return
    if context.isolation_capability is None:
        return
    decision_id = remaining[0]
    if (
        context.state.supervisor_session_id is None
        and context.state.proposal_ids
    ):
        _finalize_unlaunchable_decision(
            context,
            decision_id,
            "supervisor_session_unavailable",
            "The one persistent supervisor session is unavailable; no replacement was launched.",
        )
        return
    envelope = _load_envelope(context.run_directory, decision_id)
    quarantine_workspace = _quarantine_workspace(context.run_directory)
    _validate_quarantine_workspace(context.run_directory / "quarantine")
    rendered = build_live_blind_supervisor_prompt(
        context.prepared,
        envelope,
        sensitive_values=_sensitive_values(context.services.environ),
    )
    proposal_directory = context.run_directory / "proposals" / decision_id
    proposal_directory.mkdir(parents=True, exist_ok=True)
    schema_path = context.run_directory / "decisions" / decision_id / "output-schema.json"
    manifest_path = (
        context.run_directory
        / "decisions"
        / decision_id
        / "blind-input-manifest.json"
    )
    stage1_directory = proposal_directory / "stage1-run"
    _, removed_names, _ = build_subprocess_environment(context.services.environ)
    pending = PendingSupervisorAction(
        action_id=f"supervisor-{decision_id}",
        proposal_id=decision_id,
        proposal_kind=envelope.proposal_kind,
        decision_index=envelope.ordinal - 1,
        stage1_artifact_directory=str(stage1_directory),
        workspace=str(quarantine_workspace),
        role="supervisor",
        model=context.prepared.specification.supervisor_model,
        reasoning_effort=context.prepared.specification.supervisor_reasoning_effort,
        timeout_seconds=context.prepared.specification.supervisor_timeout_seconds,
        sandbox="read-only",
        approval_policy="never",
        ephemeral=False,
        network_policy="disabled",
        codex_executable=context.codex_executable,
        prompt_sha256=rendered.manifest.rendered_blind_input_sha256,
        prompt_byte_count=rendered.manifest.rendered_blind_input_byte_count,
        output_schema_path=str(schema_path),
        output_schema_sha256=sha256_regular_file(schema_path),
        blind_manifest_path=str(manifest_path),
        blind_manifest_sha256=sha256_regular_file(manifest_path),
        resume_session_id=context.state.supervisor_session_id,
        removed_environment_variable_names=removed_names,
        started_at=_utc_string(context.services.utc_now()),
    )
    request = _prepared_supervisor_request(context, envelope, rendered)
    context.state = _journal_event(
        context.run_directory,
        context.state,
        context.prepared,
        event_type="shadow_action_intent",
        reason="supervisor_action_intent",
        action_id=pending.action_id,
        decision_id=decision_id,
        updates={
            "pending_action": pending,
            "summary": f"Quarantined supervisor turn launched for {decision_id}.",
        },
        artifact_hashes={
            str(schema_path): pending.output_schema_sha256,
            str(manifest_path): pending.blind_manifest_sha256,
        },
        utc_now=context.services.utc_now,
    )
    holder = _SupervisorTask(
        pending=pending,
        thread=threading.Thread(),
    )

    def invoke() -> None:
        try:
            if context.services.supervisor_invoker is not None:
                holder.returned_result = context.services.supervisor_invoker(
                    request,
                    runs_dir=proposal_directory,
                    codex_executable=context.codex_executable,
                    environ=context.services.environ,
                    output_schema=schema_path,
                    resume_thread_id=pending.resume_session_id,
                    confidential_fragments=(),
                )
            else:
                capability = context.isolation_capability
                if capability is None:
                    raise LiveShadowDependencyError(
                        "Bubblewrap isolation is unavailable"
                    )

                def isolated_launch(
                    command: Sequence[str],
                    prepared_request: PreparedCodexRequest,
                    environment: Mapping[str, str],
                    final_message_path: Path,
                    resolved_schema: Path | None,
                ) -> CodexProcessLaunch:
                    return build_bubblewrap_process_launch(
                        command,
                        prepared_request,
                        environment,
                        final_message_path,
                        resolved_schema,
                        capability=capability,
                        stage4_run_root=context.run_directory,
                        runtime_home=_codex_runtime_home(
                            context.run_directory
                        ),
                        forbidden_roots=_isolation_forbidden_roots(
                            context
                        ),
                    )

                holder.returned_result = run_prepared_codex(
                    request,
                    runs_dir=proposal_directory,
                    codex_executable=context.codex_executable,
                    environ=context.services.environ,
                    output_schema=schema_path,
                    resume_thread_id=pending.resume_session_id,
                    confidential_fragments=(),
                    process_launch_builder=isolated_launch,
                    version_probe=lambda _executable, _environment, _workspace: None,
                )
        except BaseException as exc:
            holder.error = exc

    holder.thread = threading.Thread(
        target=invoke,
        name=f"live-shadow-{decision_id}",
        daemon=True,
    )
    context.supervisor_task = holder
    holder.thread.start()


def _prepared_supervisor_request(
    context: _LiveContext,
    envelope: LiveDecisionEnvelope,
    rendered: RenderedLiveBlindPrompt,
) -> PreparedCodexRequest:
    del envelope
    request = CodexRunRequest(
        schema_version=1,
        run_id="stage1-run",
        role="supervisor",
        workspace=str(_quarantine_workspace(context.run_directory)),
        prompt_path=str(context.prepared.policy.path),
        model=context.prepared.specification.supervisor_model,
        reasoning_effort=context.prepared.specification.supervisor_reasoning_effort,
        timeout_seconds=context.prepared.specification.supervisor_timeout_seconds,
    )
    return PreparedCodexRequest(
        request_path=context.prepared.specification_path,
        request=request,
        workspace=_quarantine_workspace(context.run_directory),
        prompt_path=context.prepared.policy.path,
        prompt_bytes=rendered.content,
        prompt_sha256=rendered.manifest.rendered_blind_input_sha256,
        policy=ROLE_POLICIES["supervisor"],
    )


def _finish_supervisor_if_ready(context: _LiveContext) -> None:
    if context.state.status in TERMINAL_LIVE_STATUSES:
        return
    pending = context.state.pending_action
    if pending is None:
        return
    task = context.supervisor_task
    if task is not None and task.thread.is_alive():
        return
    completion = Path(pending.stage1_artifact_directory) / "stage2-completion.json"
    if task is None and not completion.is_file():
        return
    if task is not None and task.error is not None:
        context.supervisor_task = None
        _record_failed_proposal(
            context,
            pending,
            "supervisor_adapter_failure",
            "The supervisor adapter failed; authoritative Stage 2 was unaffected.",
        )
        return
    envelope = _load_envelope(context.run_directory, pending.proposal_id)
    decision = _proof_decision(envelope)
    try:
        proof = _verify_supervisor_artifacts(
            pending,
            decision,
            (decision,),
            prompt_source_path=str(context.prepared.policy.path),
            proposal_workspace=context.prepared.stage2.workspace,
            command_verifier=(
                lambda command_pending, metadata: (
                    _verify_isolated_supervisor_command(
                        context,
                        command_pending,
                        metadata,
                    )
                )
                if context.services.supervisor_invoker is None
                else None
            ),
        )
        if (
            task is not None
            and task.returned_result is not None
            and task.returned_result != proof.adapter_result
        ):
            raise LiveShadowStateError(
                "returned supervisor result contradicts durable evidence"
            )
    except Exception:
        context.supervisor_task = None
        _record_failed_proposal(
            context,
            pending,
            "supervisor_action_completion_unprovable",
            "Supervisor completion evidence is incomplete or contradictory.",
        )
        return
    session_integrity, session_id = _session_integrity(
        context.state.supervisor_session_id,
        pending.resume_session_id,
        proof.session_ids,
        _source_session_uuids(context),
    )
    proposal_directory = context.run_directory / "proposals" / pending.proposal_id
    result_path = proposal_directory / "supervisor-result.json"
    candidate_path = proposal_directory / "candidate-prompt.md"
    if proof.proposal is None:
        result_value: object = {
            "schema_version": 1,
            "valid": False,
            "transport_status": proof.adapter_result.status,
            "proposal": None,
        }
        candidate = b""
    else:
        result_value = proof.proposal.model_dump(mode="json")
        candidate = (
            b""
            if proof.proposal.prompt is None
            else proof.proposal.prompt.encode("utf-8")
        )
    try:
        preflight_shadow_confidentiality(
            (
                result_value,
                candidate,
                str(result_path),
                str(candidate_path),
            ),
            _sensitive_values(context.services.environ),
            label="live supervisor proposal",
        )
    except ShadowInputError:
        context.supervisor_task = None
        _record_failed_proposal(
            context,
            pending,
            "supervisor_confidentiality_collision",
            (
                "The supervisor proposal failed confidentiality preflight and "
                "was withheld without affecting Stage 2."
            ),
        )
        return
    _write_immutable_json(result_path, result_value)
    _write_immutable_bytes(candidate_path, candidate)
    record = SupervisorActionRecord(
        action_id=pending.action_id,
        proposal_id=pending.proposal_id,
        proposal_kind=pending.proposal_kind,
        complete=True,
        stage1_artifact_directory=pending.stage1_artifact_directory,
        adapter_result=proof.adapter_result,
        session_ids=proof.session_ids,
        structured_result_valid=proof.proposal is not None,
        artifact_hashes={
            **proof.artifact_hashes,
            str(result_path): sha256_regular_file(result_path),
            str(candidate_path): sha256_regular_file(candidate_path),
        },
    )
    record_path = proposal_directory / "supervisor-action.json"
    _write_immutable_json(record_path, record.model_dump(mode="json"))
    proposals = (*context.state.proposal_ids, pending.proposal_id)
    context.state = _journal_event(
        context.run_directory,
        context.state,
        context.prepared,
        event_type="shadow_action_completion",
        reason="supervisor_action_completed",
        action_id=pending.action_id,
        decision_id=pending.proposal_id,
        updates={
            "pending_action": None,
            "proposal_ids": proposals,
            "supervisor_session_id": (
                session_id
                if context.state.supervisor_session_id is None
                else context.state.supervisor_session_id
            ),
            "summary": f"Quarantined supervisor proposal finalized for {pending.proposal_id}.",
        },
        artifact_hashes={
            **record.artifact_hashes,
            str(record_path): sha256_regular_file(record_path),
        },
        utc_now=context.services.utc_now,
    )
    context.supervisor_task = None
    if proof.adapter_result.status != "succeeded":
        context.state = _record_shadow_failure(
            context,
            decision_id=pending.proposal_id,
            reason=f"supervisor_{proof.adapter_result.status}",
            detail="Supervisor transport failed without affecting Stage 2.",
            temporal_or_integrity=False,
        )
    elif proof.proposal is None:
        context.state = _record_shadow_failure(
            context,
            decision_id=pending.proposal_id,
            reason="supervisor_result_malformed",
            detail="Supervisor structured result is missing or invalid.",
            temporal_or_integrity=False,
        )
    elif not session_integrity:
        context.state = _record_shadow_failure(
            context,
            decision_id=pending.proposal_id,
            reason="supervisor_session_integrity_failed",
            detail="Persistent supervisor UUID evidence is invalid or collides with Stage 2.",
            temporal_or_integrity=True,
        )


def _record_failed_proposal(
    context: _LiveContext,
    pending: PendingSupervisorAction,
    reason: str,
    detail: str,
) -> None:
    proposal_directory = context.run_directory / "proposals" / pending.proposal_id
    proposal_directory.mkdir(parents=True, exist_ok=True)
    Path(pending.stage1_artifact_directory).mkdir(parents=True, exist_ok=True)
    result_path = proposal_directory / "supervisor-result.json"
    candidate_path = proposal_directory / "candidate-prompt.md"
    result_value = {
        "schema_version": 1,
        "valid": False,
        "transport_status": "unprovable",
        "proposal": None,
    }
    _write_immutable_json(result_path, result_value)
    _write_immutable_bytes(candidate_path, b"")
    failed_action_path = _write_failed_supervisor_action(
        context,
        pending,
        reason,
        detail,
    )
    context.state = _journal_event(
        context.run_directory,
        context.state,
        context.prepared,
        event_type="shadow_action_completion",
        reason="supervisor_action_failed",
        action_id=pending.action_id,
        decision_id=pending.proposal_id,
        updates={
            "pending_action": None,
            "proposal_ids": (*context.state.proposal_ids, pending.proposal_id),
            "summary": f"Shadow supervisor failure recorded for {pending.proposal_id}.",
        },
        artifact_hashes={
            str(result_path): sha256_regular_file(result_path),
            str(candidate_path): sha256_regular_file(candidate_path),
            str(failed_action_path): sha256_regular_file(failed_action_path),
        },
        utc_now=context.services.utc_now,
    )
    context.state = _record_shadow_failure(
        context,
        decision_id=pending.proposal_id,
        reason=reason,
        detail=detail,
        temporal_or_integrity=reason.endswith("unprovable"),
    )


def _finalize_unlaunchable_decision(
    context: _LiveContext,
    decision_id: str,
    reason: str,
    detail: str,
) -> None:
    envelope = _load_envelope(context.run_directory, decision_id)
    pending = PendingSupervisorAction(
        action_id=f"supervisor-{decision_id}",
        proposal_id=decision_id,
        proposal_kind=envelope.proposal_kind,
        decision_index=envelope.ordinal - 1,
        stage1_artifact_directory=str(
            context.run_directory / "proposals" / decision_id / "stage1-run"
        ),
        workspace=str(_quarantine_workspace(context.run_directory)),
        role="supervisor",
        model=context.prepared.specification.supervisor_model,
        reasoning_effort=context.prepared.specification.supervisor_reasoning_effort,
        timeout_seconds=context.prepared.specification.supervisor_timeout_seconds,
        sandbox="read-only",
        approval_policy="never",
        ephemeral=False,
        network_policy="disabled",
        codex_executable=context.codex_executable,
        prompt_sha256=ZERO_HASH,
        prompt_byte_count=1,
        output_schema_path=str(
            context.run_directory / "decisions" / decision_id / "output-schema.json"
        ),
        output_schema_sha256=sha256_regular_file(
            context.run_directory / "decisions" / decision_id / "output-schema.json"
        ),
        blind_manifest_path=str(
            context.run_directory
            / "decisions"
            / decision_id
            / "blind-input-manifest.json"
        ),
        blind_manifest_sha256=sha256_regular_file(
            context.run_directory
            / "decisions"
            / decision_id
            / "blind-input-manifest.json"
        ),
        resume_session_id=None,
        removed_environment_variable_names=(),
        started_at=_utc_string(context.services.utc_now()),
    )
    proposal_directory = context.run_directory / "proposals" / decision_id
    proposal_directory.mkdir(parents=True, exist_ok=True)
    Path(pending.stage1_artifact_directory).mkdir(parents=True, exist_ok=True)
    result_path = proposal_directory / "supervisor-result.json"
    candidate_path = proposal_directory / "candidate-prompt.md"
    _write_immutable_json(
        result_path,
        {
            "schema_version": 1,
            "valid": False,
            "transport_status": "not_launched",
            "proposal": None,
        },
    )
    _write_immutable_bytes(candidate_path, b"")
    failed_action_path = _write_failed_supervisor_action(
        context,
        pending,
        reason,
        detail,
    )
    context.state = _journal_event(
        context.run_directory,
        context.state,
        context.prepared,
        event_type="shadow_action_completion",
        reason="supervisor_action_not_launched",
        action_id=pending.action_id,
        decision_id=decision_id,
        updates={
            "proposal_ids": (*context.state.proposal_ids, decision_id),
            "summary": f"Unlaunchable shadow proposal recorded for {decision_id}.",
        },
        artifact_hashes={
            str(result_path): sha256_regular_file(result_path),
            str(candidate_path): sha256_regular_file(candidate_path),
            str(failed_action_path): sha256_regular_file(failed_action_path),
        },
        utc_now=context.services.utc_now,
    )
    context.state = _record_shadow_failure(
        context,
        decision_id=decision_id,
        reason=reason,
        detail=detail,
        temporal_or_integrity=reason == "supervisor_session_unavailable",
    )


def _write_failed_supervisor_action(
    context: _LiveContext,
    pending: PendingSupervisorAction,
    reason: str,
    detail: str,
) -> Path:
    path = (
        context.run_directory
        / "proposals"
        / pending.proposal_id
        / "failed-supervisor-action.json"
    )
    candidate = FailedSupervisorActionRecord(
        action_id=pending.action_id,
        proposal_id=pending.proposal_id,
        proposal_kind=pending.proposal_kind,
        stage1_artifact_directory=pending.stage1_artifact_directory,
        resume_session_id=pending.resume_session_id,
        prompt_sha256=pending.prompt_sha256,
        blind_manifest_sha256=pending.blind_manifest_sha256,
        output_schema_sha256=pending.output_schema_sha256,
        reason=reason,
        detail=detail,
        finalized_at=_utc_string(context.services.utc_now()),
    )
    if path.exists():
        existing = _load_model(
            path,
            FailedSupervisorActionRecord,
            "failed supervisor action",
        )
        if existing.model_dump(
            mode="json",
            exclude={"finalized_at"},
        ) != candidate.model_dump(
            mode="json",
            exclude={"finalized_at"},
        ):
            raise LiveShadowStateError(
                "failed supervisor action contradicts durable recovery"
            )
        return path
    _write_immutable_json(path, candidate.model_dump(mode="json"))
    return path


def _finalize_ready_comparisons(context: _LiveContext) -> None:
    if context.state.authoritative_status is None:
        return
    for proposal_id in context.state.proposal_ids:
        if proposal_id in context.state.comparison_ids:
            continue
        _finalize_comparison(context, proposal_id)


def _finalize_comparison(context: _LiveContext, proposal_id: str) -> None:
    envelope = _load_envelope(context.run_directory, proposal_id)
    entries = _observed_stage2_entries(context)
    completion_index = next(
        (
            index
            for index, entry in enumerate(entries)
            if entry.event_type == "action_completion"
            and entry.action_id == envelope.source_action_id
        ),
        None,
    )
    if completion_index is None:
        return
    reconstruction: DecisionReconstruction | None = None
    reconstruction_error = False
    run_directory = Path(cast(str, context.state.authoritative_run_directory))
    try:
        baseline = GitBaseline.model_validate(_read_json(run_directory / "baseline.json"))
        reconstructions = reconstruct_decision_points(
            run_directory,
            context.prepared.stage2,
            baseline,
            entries[: completion_index + 1],
        )
        reconstruction = next(
            item for item in reconstructions if item.point.decision_id == proposal_id
        )
    except (ValidationError, WorkflowStateError, StopIteration):
        reconstruction_error = True
    proposal_directory = context.run_directory / "proposals" / proposal_id
    result_value = _read_json(proposal_directory / "supervisor-result.json")
    proposal: NormalizedSupervisorProposal | None
    try:
        proposal = NormalizedSupervisorProposal.model_validate(result_value)
    except ValidationError:
        proposal = None
    proof = None
    record_path = proposal_directory / "supervisor-action.json"
    if record_path.exists():
        pending = _pending_from_envelope(context, envelope)
        decision = _proof_decision(envelope)
        try:
            proof = _verify_supervisor_artifacts(
                pending,
                decision,
                (decision,),
                prompt_source_path=str(context.prepared.policy.path),
                proposal_workspace=context.prepared.stage2.workspace,
                command_verifier=(
                    lambda command_pending, metadata: (
                        _verify_isolated_supervisor_command(
                            context,
                            command_pending,
                            metadata,
                        )
                    )
                    if context.services.supervisor_invoker is None
                    else None
                ),
            )
        except Exception:
            proof = None
    candidate_path = proposal_directory / "candidate-prompt.md"
    candidate_bytes = candidate_path.read_bytes()
    comparison_directory = context.run_directory / "comparisons" / proposal_id
    comparison_directory.mkdir(parents=True, exist_ok=True)
    comparison_path = comparison_directory / "comparison.json"
    assessment_path = proposal_directory / "assessment.json"
    source_path = comparison_directory / "authoritative-source.md"
    rendered_path = comparison_directory / "authoritative-rendered.md"
    if (
        reconstruction_error
        or reconstruction is None
        or proposal is None
        or proof is None
    ):
        reason = (
            "authoritative_reconstruction_unproven"
            if reconstruction_error or reconstruction is None
            else "supervisor_result_unavailable"
        )
        comparison = ProposalComparison(
            proposal_id=proposal_id,
            proposal_kind=envelope.proposal_kind,
            source_stage2_action_id=envelope.source_action_id,
            candidate_sha256=(
                hashlib.sha256(candidate_bytes).hexdigest()
                if candidate_bytes
                else None
            ),
            candidate_byte_count=len(candidate_bytes),
            authoritative_source_sha256=None,
            authoritative_source_byte_count=None,
            authoritative_rendered_sha256=None,
            authoritative_rendered_byte_count=None,
            comparison_available=False,
            comparison_unavailable_reason=reason,
        )
        assessment = _unavailable_assessment(
            context,
            envelope,
            comparison,
            reason,
            proof.final_bytes if proof is not None else b"",
        )
    else:
        _assert_no_authoritative_material(
            Path(proof.adapter_result.artifact_directory),
            (reconstruction,),
        )
        comparison = _build_comparison(
            reconstruction,
            proposal,
            candidate_bytes,
        )
        session_ok, _ = _session_integrity(
            (
                None
                if pending.decision_index == 0
                else context.state.supervisor_session_id
            ),
            pending.resume_session_id,
            proof.session_ids,
            _source_session_uuids(context),
        )
        assessment = assess_normalized_supervisor_proposal(
            proposal_id=proposal_id,
            proposal_kind=envelope.proposal_kind,
            proposal=proposal,
            final_bytes=proof.final_bytes,
            comparison=comparison,
            specification=context.prepared.stage2.specification,
            test_ids=tuple(
                test.specification.id
                for test in context.prepared.stage2.acceptance_tests
            ),
            max_proposal_bytes=context.state.max_proposal_bytes,
            session_integrity=session_ok,
            sensitive_values=_sensitive_values(context.services.environ),
        )
    comparison_confidentiality_failure = False
    try:
        preflight_shadow_confidentiality(
            (
                comparison,
                assessment,
                (
                    reconstruction.authoritative_source.content
                    if reconstruction is not None
                    and comparison.comparison_available
                    and reconstruction.authoritative_source is not None
                    else b""
                ),
                (
                    reconstruction.authoritative_rendered.content
                    if reconstruction is not None
                    and comparison.comparison_available
                    and reconstruction.authoritative_rendered is not None
                    else b""
                ),
            ),
            _sensitive_values(context.services.environ),
            label="live authoritative comparison",
        )
    except ShadowInputError:
        reason = "comparison_confidentiality_collision"
        comparison = ProposalComparison(
            proposal_id=proposal_id,
            proposal_kind=envelope.proposal_kind,
            source_stage2_action_id=envelope.source_action_id,
            candidate_sha256=(
                hashlib.sha256(candidate_bytes).hexdigest()
                if candidate_bytes
                else None
            ),
            candidate_byte_count=len(candidate_bytes),
            authoritative_source_sha256=None,
            authoritative_source_byte_count=None,
            authoritative_rendered_sha256=None,
            authoritative_rendered_byte_count=None,
            comparison_available=False,
            comparison_unavailable_reason=reason,
        )
        assessment = _unavailable_assessment(
            context,
            envelope,
            comparison,
            reason,
            b"",
        )
        reconstruction = None
        comparison_confidentiality_failure = True
    if (
        comparison.comparison_available
        and reconstruction is not None
        and reconstruction.authoritative_source is not None
        and reconstruction.authoritative_rendered is not None
    ):
        _write_immutable_bytes(
            source_path,
            reconstruction.authoritative_source.content,
        )
        _write_immutable_bytes(
            rendered_path,
            reconstruction.authoritative_rendered.content,
        )
    elif source_path.exists() or rendered_path.exists():
        raise LiveShadowStateError(
            "unavailable comparison has orphaned authoritative material"
        )
    _write_immutable_json(
        comparison_path,
        comparison.model_dump(mode="json"),
    )
    _write_immutable_json(
        assessment_path,
        assessment.model_dump(mode="json"),
    )
    paths = [comparison_path, assessment_path]
    if source_path.exists():
        paths.extend((source_path, rendered_path))
    updates: dict[str, object] = {
        "comparison_ids": (*context.state.comparison_ids, proposal_id),
        "summary": f"Post-finalization comparison recorded for {proposal_id}.",
    }
    if assessment.disqualified:
        updates["disqualified_proposal_ids"] = (
            *context.state.disqualified_proposal_ids,
            proposal_id,
        )
    context.state = _journal_event(
        context.run_directory,
        context.state,
        context.prepared,
        event_type="comparison",
        reason=(
            "comparison_finalized"
            if comparison.comparison_available
            else "comparison_unavailable"
        ),
        action_id=None,
        decision_id=proposal_id,
        updates=updates,
        artifact_hashes={
            str(path): sha256_regular_file(path) for path in paths
        },
        utc_now=context.services.utc_now,
    )
    if reconstruction_error:
        context.state = _record_shadow_failure(
            context,
            decision_id=proposal_id,
            reason="comparison_reconstruction_failure",
            detail="Authoritative prompt reconstruction could not be proven.",
            temporal_or_integrity=True,
        )
    if comparison_confidentiality_failure:
        context.state = _record_shadow_failure(
            context,
            decision_id=proposal_id,
            reason="comparison_confidentiality_collision",
            detail=(
                "Authoritative comparison material failed confidentiality "
                "preflight and was withheld."
            ),
            temporal_or_integrity=True,
        )


def _finalize_unfinished_authoritative_comparison(
    context: _LiveContext,
    proposal_id: str,
) -> None:
    """Finalize one decision whose terminal Stage 2 action can no longer complete."""
    if proposal_id in context.state.comparison_ids:
        return
    envelope = _load_envelope(context.run_directory, proposal_id)
    if any(
        entry.event_type == "action_completion"
        and entry.action_id == envelope.source_action_id
        for entry in _observed_stage2_entries(context)
    ):
        raise LiveShadowStateError(
            "unfinished authoritative comparison has a durable action completion"
        )
    run_record = _load_model(
        context.run_directory / "authoritative" / "stage2-run.json",
        AuthoritativeRunRecord,
        "authoritative run",
    )
    terminal = _load_model(
        context.run_directory / "authoritative" / "result.json",
        AuthoritativeTerminalRecord,
        "authoritative terminal result",
    )
    if (
        context.state.authoritative_status is None
        or context.state.authoritative_result_sha256 is None
        or context.state.authoritative_run_directory != terminal.run_directory
        or context.state.authoritative_status != terminal.status
        or context.state.authoritative_pause_reason != terminal.pause_reason
        or context.state.authoritative_result_sha256 != terminal.result_sha256
        or context.state.authoritative_journal_sequence
        != terminal.journal_sequence
        or context.state.authoritative_journal_hash != terminal.journal_head
    ):
        raise LiveShadowIntegrityError(
            "unfinished authoritative comparison lacks a verified terminal journal"
        )
    proposal_directory = context.run_directory / "proposals" / proposal_id
    candidate_path = proposal_directory / "candidate-prompt.md"
    candidate_bytes = candidate_path.read_bytes()
    reason = "authoritative_action_unfinished_after_terminal"
    comparison = ProposalComparison(
        proposal_id=proposal_id,
        proposal_kind=envelope.proposal_kind,
        source_stage2_action_id=envelope.source_action_id,
        candidate_sha256=(
            hashlib.sha256(candidate_bytes).hexdigest()
            if candidate_bytes
            else None
        ),
        candidate_byte_count=len(candidate_bytes),
        authoritative_source_sha256=None,
        authoritative_source_byte_count=None,
        authoritative_rendered_sha256=None,
        authoritative_rendered_byte_count=None,
        comparison_available=False,
        comparison_unavailable_reason=reason,
    )
    assessment = _unavailable_assessment(
        context,
        envelope,
        comparison,
        reason,
        b"",
    )
    record_body = {
        "schema_version": 1,
        "decision_id": proposal_id,
        "source_action_id": envelope.source_action_id,
        "envelope_sha256": envelope.envelope_sha256,
        "authoritative_run_directory": terminal.run_directory,
        "authoritative_run_token": run_record.run_token,
        "authoritative_substage_id": run_record.substage_id,
        "authoritative_status": terminal.status,
        "authoritative_pause_reason": terminal.pause_reason,
        "authoritative_result_sha256": terminal.result_sha256,
        "authoritative_journal_sha256": terminal.journal_sha256,
        "authoritative_journal_sequence": terminal.journal_sequence,
        "authoritative_journal_hash": terminal.journal_head,
        "reason": reason,
        "recorded_at": _utc_string(context.services.utc_now()),
    }
    record = LiveComparisonUnavailableRecord.model_validate(
        {
            **record_body,
            "record_sha256": hashlib.sha256(
                _canonical_json(record_body)
            ).hexdigest(),
        }
    )
    comparison_directory = context.run_directory / "comparisons" / proposal_id
    comparison_directory.mkdir(parents=True, exist_ok=True)
    comparison_path = comparison_directory / "comparison.json"
    unavailable_path = comparison_directory / "comparison-unavailable.json"
    assessment_path = proposal_directory / "assessment.json"
    source_path = comparison_directory / "authoritative-source.md"
    rendered_path = comparison_directory / "authoritative-rendered.md"
    if source_path.exists() or rendered_path.exists():
        raise LiveShadowStateError(
            "unfinished authoritative comparison contains source material"
        )
    if unavailable_path.exists():
        existing = _load_unavailable_record(context.run_directory, proposal_id)
        if existing.model_dump(
            mode="json",
            exclude={"recorded_at", "record_sha256"},
        ) != record.model_dump(
            mode="json",
            exclude={"recorded_at", "record_sha256"},
        ):
            raise LiveShadowStateError(
                "comparison-unavailable record contradicts durable recovery"
            )
        record = existing
    _write_immutable_json(
        comparison_path,
        comparison.model_dump(mode="json"),
    )
    _write_immutable_json(
        assessment_path,
        assessment.model_dump(mode="json"),
    )
    _write_immutable_json(
        unavailable_path,
        record.model_dump(mode="json"),
    )
    context.state = _journal_event(
        context.run_directory,
        context.state,
        context.prepared,
        event_type="comparison",
        reason="comparison_unavailable",
        action_id=None,
        decision_id=proposal_id,
        updates={
            "comparison_ids": (*context.state.comparison_ids, proposal_id),
            "disqualified_proposal_ids": (
                *context.state.disqualified_proposal_ids,
                proposal_id,
            ),
            "summary": (
                f"Terminal unfinished authoritative action finalized for {proposal_id}."
            ),
        },
        artifact_hashes={
            str(comparison_path): sha256_regular_file(comparison_path),
            str(assessment_path): sha256_regular_file(assessment_path),
            str(unavailable_path): sha256_regular_file(unavailable_path),
        },
        utc_now=context.services.utc_now,
    )
    context.state = _record_shadow_failure(
        context,
        decision_id=proposal_id,
        reason=reason,
        detail=(
            "The authoritative Stage 2 process exited with its observed action "
            "unfinished; comparison was boundedly finalized without prompt material."
        ),
        temporal_or_integrity=False,
    )


def _record_authoritative_terminal(context: _LiveContext) -> None:
    run_value = context.state.authoritative_run_directory
    if run_value is None:
        raise LiveShadowIntegrityError("terminal Stage 2 has no discovered run")
    run_directory = Path(run_value)
    try:
        result, state, _ = read_stage2_source_for_shadow(run_directory)
    except WorkflowStateError as exc:
        raise LiveShadowIntegrityError(
            "terminal authoritative Stage 2 failed trusted validation"
        ) from exc
    process_exit = (
        context.authoritative_process.poll()
        if context.authoritative_process is not None
        else None
    )
    expected_exit = workflow_exit_code(result.status)
    terminal_candidate = AuthoritativeTerminalRecord(
        run_directory=str(run_directory),
        status=result.status,
        pause_reason=result.pause_reason,
        process_exit_code=process_exit,
        expected_exit_code=expected_exit,
        result_sha256=sha256_regular_file(run_directory / "result.json"),
        journal_sha256=sha256_regular_file(run_directory / "journal.jsonl"),
        journal_head=state.journal_hash,
        journal_sequence=state.journal_sequence,
        observed_at=_utc_string(context.services.utc_now()),
    )
    result_path = context.run_directory / "authoritative" / "result.json"
    if result_path.exists():
        terminal = _load_model(
            result_path,
            AuthoritativeTerminalRecord,
            "authoritative terminal result",
        )
        if (
            terminal.run_directory != terminal_candidate.run_directory
            or terminal.status != terminal_candidate.status
            or terminal.pause_reason != terminal_candidate.pause_reason
            or terminal.expected_exit_code
            != terminal_candidate.expected_exit_code
            or terminal.result_sha256 != terminal_candidate.result_sha256
            or terminal.journal_sha256 != terminal_candidate.journal_sha256
            or terminal.journal_head != terminal_candidate.journal_head
            or terminal.journal_sequence
            != terminal_candidate.journal_sequence
        ):
            raise LiveShadowIntegrityError(
                "existing authoritative terminal record contradicts recovery"
            )
    else:
        terminal = terminal_candidate
        _write_immutable_json(
            result_path,
            terminal.model_dump(mode="json"),
        )
    context.state = _journal_event(
        context.run_directory,
        context.state,
        context.prepared,
        event_type="authoritative_terminal",
        reason="authoritative_stage2_terminal",
        updates={
            "status": "authoritative_terminal_shadow_pending",
            "authoritative_status": result.status,
            "authoritative_pause_reason": result.pause_reason,
            "authoritative_result_sha256": terminal.result_sha256,
            "authoritative_process_exit_code": terminal.process_exit_code,
            "authoritative_terminal_at": terminal.observed_at,
            "summary": (
                "Authoritative Stage 2 is terminal; pending shadow work is isolated."
            ),
        },
        artifact_hashes={str(result_path): sha256_regular_file(result_path)},
        utc_now=context.services.utc_now,
    )


def _finalize_after_authoritative(context: _LiveContext) -> None:
    if context.state.authoritative_status is None:
        return
    terminal_at = context.state.authoritative_terminal_at
    if terminal_at is None:
        raise LiveShadowIntegrityError("authoritative terminal timestamp is missing")
    deadline = (
        _parse_timestamp(terminal_at).timestamp()
        + context.prepared.specification.shadow_completion_timeout_seconds
    )
    now_timestamp = context.services.utc_now().astimezone(UTC).timestamp()
    timed_out = now_timestamp >= deadline
    task = context.supervisor_task
    if task is not None and task.thread.is_alive():
        if not timed_out:
            return
        pending = context.state.pending_action
        if pending is not None:
            context.supervisor_task = None
            _record_failed_proposal(
                context,
                pending,
                "shadow_completion_timeout",
                "The supervisor did not finalize before the shadow completion timeout.",
            )
    elif context.state.pending_action is not None:
        completion = (
            Path(context.state.pending_action.stage1_artifact_directory)
            / "stage2-completion.json"
        )
        if completion.is_file():
            _finish_supervisor_if_ready(context)
        elif not timed_out:
            return
        else:
            pending = context.state.pending_action
            _record_failed_proposal(
                context,
                pending,
                "supervisor_action_completion_unprovable",
                "Pending supervisor intent has no provable completion and was not relaunched.",
            )
    remaining = [
        decision_id
        for decision_id in context.state.observed_decision_ids
        if decision_id not in context.state.proposal_ids
    ]
    if remaining and not timed_out:
        _launch_next_supervisor_if_ready(context)
        return
    for decision_id in remaining:
        dependency_unavailable = context.isolation_dependency_failure is not None
        _finalize_unlaunchable_decision(
            context,
            decision_id,
            (
                "isolation_dependency_failure"
                if dependency_unavailable
                else "shadow_completion_timeout"
            ),
            (
                "Queued supervisor turn could not launch because Bubblewrap "
                "isolation was unavailable during recovery."
                if dependency_unavailable
                else (
                    "Queued supervisor turn was not launched before the "
                    "completion timeout."
                )
            ),
        )
    _finalize_ready_comparisons(context)
    if timed_out:
        for proposal_id in tuple(context.state.proposal_ids):
            if proposal_id not in context.state.comparison_ids:
                _finalize_unfinished_authoritative_comparison(
                    context,
                    proposal_id,
                )
    if len(context.state.comparison_ids) < len(context.state.proposal_ids):
        return
    unavailable = [
        proposal_id
        for proposal_id in context.state.comparison_ids
        if not _load_comparison(
            context.run_directory, proposal_id
        ).comparison_available
    ]
    for proposal_id in unavailable:
        comparison = _load_comparison(
            context.run_directory,
            proposal_id,
        )
        reason = comparison.comparison_unavailable_reason
        if (
            reason == "authoritative_action_unfinished_after_terminal"
            and not any(
                failure.decision_id == proposal_id
                and failure.reason == reason
                for failure in context.state.shadow_failures
            )
        ):
            context.state = _record_shadow_failure(
                context,
                decision_id=proposal_id,
                reason=reason,
                detail=(
                    "The authoritative Stage 2 process exited with its observed "
                    "action unfinished; comparison was boundedly finalized "
                    "without prompt material."
                ),
                temporal_or_integrity=False,
            )
    for proposal_id in context.state.proposal_ids:
        failed_path = (
            context.run_directory
            / "proposals"
            / proposal_id
            / "failed-supervisor-action.json"
        )
        if not failed_path.exists():
            continue
        failed = _load_model(
            failed_path,
            FailedSupervisorActionRecord,
            "failed supervisor action",
        )
        if any(
            failure.decision_id == proposal_id
            and failure.reason == failed.reason
            for failure in context.state.shadow_failures
        ):
            continue
        context.state = _record_shadow_failure(
            context,
            decision_id=proposal_id,
            reason=failed.reason,
            detail=failed.detail,
            temporal_or_integrity=failed.reason.endswith("unprovable"),
        )
    if context.state.shadow_failures or unavailable:
        context.state = _transition(
            context,
            "shadow_degraded",
            "shadow_observation_degraded",
            (
                "Authoritative Stage 2 finished independently; one or more "
                "shadow proposals are not comparable."
            ),
        )
        return
    comparable = [
        proposal_id
        for proposal_id in context.state.comparison_ids
        if _load_comparison(context.run_directory, proposal_id).comparison_available
    ]
    if comparable:
        context.state = _transition(
            context,
            "awaiting_reviews",
            "all_live_proposals_finalized",
            "All comparable live proposals await immutable human reviews.",
        )
    else:
        context.state = _transition(
            context,
            "shadow_degraded",
            "no_comparable_live_proposals",
            "Authoritative Stage 2 finished, but no comparable shadow proposal exists.",
        )


def _authoritative_process_running(context: _LiveContext) -> bool:
    if context.authoritative_process is not None:
        return context.authoritative_process.poll() is None
    launch = _load_model(
        context.run_directory / "authoritative" / "launch.json",
        AuthoritativeLaunchRecord,
        "authoritative launch",
    )
    if (
        launch.launch_state != "launched"
        or launch.pid is None
        or launch.process_start_ticks is None
    ):
        return False
    return _process_identity_running(launch.pid, launch.process_start_ticks)


def _record_shadow_failure(
    context: _LiveContext,
    *,
    decision_id: str | None,
    reason: str,
    detail: str,
    temporal_or_integrity: bool,
) -> LiveShadowState:
    failure = LiveShadowFailure(
        decision_id=decision_id,
        reason=reason,
        detail=detail,
        temporal_or_integrity=temporal_or_integrity,
        recorded_at=_utc_string(context.services.utc_now()),
    )
    return _journal_event(
        context.run_directory,
        context.state,
        context.prepared,
        event_type="shadow_failure",
        reason="shadow_failure_recorded",
        action_id=None,
        decision_id=decision_id,
        updates={
            "shadow_failures": (*context.state.shadow_failures, failure),
            "summary": "Isolated shadow failure recorded; Stage 2 was not changed.",
        },
        artifact_hashes={},
        utc_now=context.services.utc_now,
    )


def _unavailable_assessment(
    context: _LiveContext,
    envelope: LiveDecisionEnvelope,
    comparison: ProposalComparison,
    reason: str,
    final_bytes: bytes,
) -> DeterministicAssessment:
    test_ids = tuple(
        test.specification.id for test in context.prepared.stage2.acceptance_tests
    )
    return DeterministicAssessment(
        proposal_id=envelope.decision_id,
        proposal_kind=envelope.proposal_kind,
        schema_integrity=reason != "supervisor_result_unavailable",
        blind_input_integrity=True,
        session_integrity=False,
        size_compliant=len(final_bytes) <= context.state.max_proposal_bytes,
        proposal_byte_count=len(final_bytes),
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
        disqualification_reasons=(reason,),
        candidate_sha256=comparison.candidate_sha256,
        candidate_byte_count=comparison.candidate_byte_count,
        authoritative_source_sha256=None,
        authoritative_source_byte_count=None,
        authoritative_rendered_sha256=None,
        authoritative_rendered_byte_count=None,
        comparison_available=False,
        review_status="unreviewed",
    )


def _transition(
    context: _LiveContext,
    new_status: str,
    reason: str,
    summary: str,
) -> LiveShadowState:
    allowed: dict[str, set[str]] = {
        "initialized": {"authoritative_starting", "aborted", "failed"},
        "authoritative_starting": {
            "authoritative_running",
            "human_paused",
            "aborted",
            "failed",
        },
        "authoritative_running": {
            "authoritative_terminal_shadow_pending",
            "human_paused",
            "aborted",
            "failed",
        },
        "authoritative_terminal_shadow_pending": {
            "awaiting_reviews",
            "shadow_degraded",
            "human_paused",
            "aborted",
            "failed",
        },
        "awaiting_reviews": {"completed", "aborted", "failed"},
        "shadow_degraded": {"aborted", "failed"},
        "human_paused": {"aborted", "failed"},
    }
    if new_status not in allowed.get(context.state.status, set()):
        raise LiveShadowStateError(
            f"invalid live-shadow transition {context.state.status} -> {new_status}"
        )
    context.state = _journal_event(
        context.run_directory,
        context.state,
        context.prepared,
        event_type="transition",
        reason=reason,
        updates={
            "status": new_status,
            "pause_reason": (
                reason if new_status in {"human_paused", "failed"} else None
            ),
            "summary": summary,
        },
        artifact_hashes={},
        utc_now=context.services.utc_now,
    )
    return context.state


def _transition_state_only(
    run_directory: Path,
    state: LiveShadowState,
    prepared: PreparedLiveShadowSpecification,
    new_status: str,
    reason: str,
    summary: str,
    utc_now: Callable[[], datetime],
) -> LiveShadowState:
    return _journal_event(
        run_directory,
        state,
        prepared,
        event_type="transition",
        reason=reason,
        updates={
            "status": new_status,
            "pause_reason": reason,
            "summary": summary,
        },
        artifact_hashes={},
        utc_now=utc_now,
    )


def _journal_event(
    run_directory: Path,
    state: LiveShadowState,
    prepared: PreparedLiveShadowSpecification,
    *,
    event_type: str,
    reason: str,
    updates: Mapping[str, object],
    artifact_hashes: Mapping[str, str],
    utc_now: Callable[[], datetime],
    action_id: str | None = None,
    decision_id: str | None = None,
) -> LiveShadowState:
    with _live_lock(run_directory, utc_now):
        durable = _load_state(run_directory)
        if durable != state:
            if durable.status == "aborted":
                return durable
            raise LiveShadowStateError("live-shadow state changed before journal append")
        if state.journal_sequence:
            _validate_live_journal(run_directory, state)
        try:
            verify_hash_mapping(artifact_hashes)
        except WorkflowStateError as exc:
            raise LiveShadowStateError("Stage 4 journal artifact mapping is invalid") from exc
        timestamp = _utc_string(utc_now())
        new_status = cast(str, updates.get("status", state.status))
        body = {
            "schema_version": 1,
            "sequence": state.journal_sequence + 1,
            "event_type": event_type,
            "previous_state": None if state.journal_sequence == 0 else state.status,
            "new_state": new_status,
            "action_id": action_id,
            "decision_id": decision_id,
            "timestamp": timestamp,
            "reason": reason,
            "artifact_hashes": dict(sorted(artifact_hashes.items())),
            "state_updates": _json_compatible(dict(updates)),
            "previous_hash": state.journal_hash,
        }
        entry_hash = hashlib.sha256(_canonical_json(body)).hexdigest()
        entry_value = {**body, "entry_hash": entry_hash}
        try:
            preflight_shadow_confidentiality(
                entry_value,
                prepared.sensitive_values,
                label="live-shadow journal structure",
                integrity=True,
            )
        except ShadowStateError as exc:
            raise LiveShadowIntegrityError(
                "live-shadow journal structure failed confidentiality preflight"
            ) from exc
        try:
            LiveShadowJournalEntry.model_validate(entry_value)
        except ValidationError as exc:
            raise LiveShadowStateError("Stage 4 journal entry is invalid") from exc
        try:
            with (run_directory / JOURNAL_FILE).open("ab") as handle:
                handle.write(_canonical_json(entry_value))
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise LiveShadowStateError("Stage 4 journal could not be appended") from exc
        _snapshot_checkpoint("after_journal_fsync")
        copied = dict(updates)
        copied.update(
            {
                "journal_sequence": state.journal_sequence + 1,
                "journal_hash": entry_hash,
                "updated_at": timestamp,
            }
        )
        new_state = state.model_copy(update=copied)
        _persist_state(run_directory, new_state, prepared)
        return new_state


def _validate_live_journal(
    run_directory: Path,
    state: LiveShadowState,
) -> tuple[LiveShadowJournalEntry, ...]:
    entries = _read_live_journal(run_directory)
    if (
        len(entries) != state.journal_sequence
        or entries[-1].entry_hash != state.journal_hash
        or entries[-1].timestamp != state.updated_at
    ):
        raise LiveShadowStateError("Stage 4 state does not match its journal head")
    _validate_live_journal_replay(entries, state)
    return entries


def _read_live_journal(
    run_directory: Path,
) -> tuple[LiveShadowJournalEntry, ...]:
    path = run_directory / JOURNAL_FILE
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise LiveShadowStateError("Stage 4 journal could not be read") from exc
    if not content or not content.endswith(b"\n"):
        raise LiveShadowStateError("Stage 4 journal is empty or truncated")
    entries: list[LiveShadowJournalEntry] = []
    previous_hash = ZERO_HASH
    for sequence, raw in enumerate(content.splitlines(), start=1):
        try:
            value = json.loads(
                raw.decode("ascii"),
                parse_constant=_reject_json_constant,
            )
            entry = LiveShadowJournalEntry.model_validate(value)
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            ValueError,
            ValidationError,
        ) as exc:
            raise LiveShadowStateError("Stage 4 journal is malformed") from exc
        body = entry.model_dump(mode="json", exclude={"entry_hash"})
        computed = hashlib.sha256(_canonical_json(body)).hexdigest()
        if (
            entry.sequence != sequence
            or entry.previous_hash != previous_hash
            or entry.entry_hash != computed
            or _canonical_json(entry.model_dump(mode="json")).rstrip(b"\n") != raw
        ):
            raise LiveShadowStateError("Stage 4 journal hash chain is invalid")
        try:
            verify_hash_mapping(entry.artifact_hashes)
        except WorkflowStateError as exc:
            raise LiveShadowStateError(
                "Stage 4 journal references replaced evidence"
            ) from exc
        previous_hash = entry.entry_hash
        entries.append(entry)
    return tuple(entries)


def _validate_live_journal_replay(
    entries: Sequence[LiveShadowJournalEntry],
    state: LiveShadowState,
) -> None:
    mutable = {
        "status",
        "authoritative_run_directory",
        "authoritative_status",
        "authoritative_pause_reason",
        "authoritative_result_sha256",
        "authoritative_process_exit_code",
        "authoritative_terminal_at",
        "supervisor_session_id",
        "observed_decision_ids",
        "proposal_ids",
        "comparison_ids",
        "reviewed_proposal_ids",
        "disqualified_proposal_ids",
        "shadow_failures",
        "pending_action",
        "journal_read_offset",
        "authoritative_journal_sequence",
        "authoritative_journal_hash",
        "pause_reason",
        "summary",
    }
    replay: dict[str, object] = {
        "status": "initialized",
        "authoritative_run_directory": None,
        "authoritative_status": None,
        "authoritative_pause_reason": None,
        "authoritative_result_sha256": None,
        "authoritative_process_exit_code": None,
        "authoritative_terminal_at": None,
        "supervisor_session_id": None,
        "observed_decision_ids": [],
        "proposal_ids": [],
        "comparison_ids": [],
        "reviewed_proposal_ids": [],
        "disqualified_proposal_ids": [],
        "shadow_failures": [],
        "pending_action": None,
        "journal_read_offset": 0,
        "authoritative_journal_sequence": 0,
        "authoritative_journal_hash": ZERO_HASH,
        "pause_reason": None,
        "summary": "Live-shadow observation initialized.",
    }
    current: str | None = None
    for index, entry in enumerate(entries):
        if index == 0:
            if (
                entry.event_type != "transition"
                or entry.previous_state is not None
                or entry.new_state != "initialized"
                or entry.reason != "live_shadow_initialized"
            ):
                raise LiveShadowStateError("Stage 4 initialization journal form is invalid")
            current = "initialized"
        elif entry.previous_state != current:
            raise LiveShadowStateError("Stage 4 journal state history is discontinuous")
        if not set(entry.state_updates).issubset(mutable):
            raise LiveShadowStateError("Stage 4 journal updates immutable state")
        if entry.new_state != entry.state_updates.get("status", current):
            raise LiveShadowStateError("Stage 4 journal new state is contradictory")
        replay.update(entry.state_updates)
        current = entry.new_state
    state_value = state.model_dump(mode="json")
    if any(state_value[field] != value for field, value in replay.items()):
        raise LiveShadowStateError("Stage 4 state contradicts journal replay")


def _persist_state(
    run_directory: Path,
    state: LiveShadowState,
    prepared: PreparedLiveShadowSpecification,
) -> None:
    result = _result_for_state(state, prepared)
    try:
        preflight_shadow_confidentiality(
            (
                str(run_directory / STATE_FILE),
                state,
                str(run_directory / RESULT_FILE),
                result,
            ),
            prepared.sensitive_values,
            label="live-shadow state and result structure",
            integrity=True,
        )
    except ShadowStateError as exc:
        raise LiveShadowIntegrityError(
            "live-shadow state or result failed confidentiality preflight"
        ) from exc
    # The journal is the semantic source of truth.  The public result may be
    # replaced first, but state.json is the final snapshot commit point.
    _snapshot_checkpoint("before_result_replacement")
    _write_json(run_directory / RESULT_FILE, result.model_dump(mode="json"))
    _snapshot_checkpoint("after_result_replacement")
    _snapshot_checkpoint("before_state_replacement")
    _write_json(run_directory / STATE_FILE, state.model_dump(mode="json"))
    _snapshot_checkpoint("after_state_replacement")


def _result_for_state(
    state: LiveShadowState,
    prepared: PreparedLiveShadowSpecification,
) -> LiveShadowResult:
    readiness = _readiness_for_state(state, prepared)
    return LiveShadowResult(
        live_shadow_id=state.live_shadow_id,
        run_token=state.run_token,
        status=state.status,
        authoritative_stage2_specification=state.stage2_specification_path,
        authoritative_stage2_run=state.authoritative_run_directory,
        authoritative_stage2_status=state.authoritative_status,
        authoritative_pause_reason=state.authoritative_pause_reason,
        authoritative_result_sha256=state.authoritative_result_sha256,
        authoritative_process_exit_code=state.authoritative_process_exit_code,
        supervisor_model=state.supervisor_model,
        supervisor_reasoning_effort=state.supervisor_reasoning_effort,
        supervisor_session_id=state.supervisor_session_id,
        observed_decision_count=len(state.observed_decision_ids),
        proposal_count=len(state.proposal_ids),
        comparison_count=len(state.comparison_ids),
        review_count=len(state.reviewed_proposal_ids),
        disqualification_count=len(state.disqualified_proposal_ids),
        shadow_failure_count=len(state.shadow_failures),
        readiness=readiness.status,
        artifact_directory=state.artifact_directory,
        pause_reason=state.pause_reason,
        summary=state.summary,
        started_at=state.started_at,
        updated_at=state.updated_at,
    )


def _readiness_for_state(
    state: LiveShadowState,
    prepared: PreparedLiveShadowSpecification,
) -> LiveReadinessReport:
    comparisons = {
        proposal_id: _load_comparison(Path(state.artifact_directory), proposal_id)
        for proposal_id in state.comparison_ids
    }
    assessments = {
        proposal_id: _load_assessment(Path(state.artifact_directory), proposal_id)
        for proposal_id in state.comparison_ids
    }
    reviews = {
        proposal_id: _load_review(Path(state.artifact_directory), proposal_id)
        for proposal_id in state.reviewed_proposal_ids
    }
    kinds = {
        proposal_id: _load_envelope(
            Path(state.artifact_directory), proposal_id
        ).proposal_kind
        for proposal_id in state.proposal_ids
    }
    return calculate_live_readiness(
        prepared.specification,
        state.proposal_ids,
        kinds,
        {
            proposal_id: comparison.comparison_available
            for proposal_id, comparison in comparisons.items()
        },
        assessments,
        reviews,
        authoritative_status=state.authoritative_status,
        shadow_failures=state.shadow_failures,
    )


def _validate_run(
    run_directory: Path,
    state: LiveShadowState,
    prepared: PreparedLiveShadowSpecification,
) -> None:
    if state.artifact_directory != str(run_directory):
        raise LiveShadowStateError("Stage 4 artifact directory identity changed")
    _validate_live_journal(run_directory, state)
    if (
        prepared.specification_sha256 != state.specification_sha256
        or prepared.policy.sha256 != state.policy_sha256
        or prepared.context_manifests() != state.context_hashes
        or prepared.stage2.specification_sha256
        != state.stage2_specification_sha256
        or prepared.specification.live_shadow_id != state.live_shadow_id
        or prepared.stage2.specification.substage_id
        != state.authoritative_substage_id
    ):
        raise LiveShadowIntegrityError("frozen live-shadow inputs drifted")
    source_hashes = _read_json(run_directory / "source-inputs.sha256.json")
    expected_source = {
        "schema_version": 1,
        "stage2_specification": prepared.stage2.specification_sha256,
        "contract": prepared.stage2.contract.sha256,
        "worker_initial": prepared.stage2.worker_initial_prompt.sha256,
        "worker_repair": prepared.stage2.worker_repair_prompt.sha256,
        "auditor": prepared.stage2.auditor_prompt.sha256,
    }
    if source_hashes != expected_source:
        raise LiveShadowIntegrityError("frozen Stage 2 source hashes drifted")
    load_backend_identity(run_directory / "isolation.json")
    _validate_quarantine_workspace(run_directory / "quarantine")
    if state.authoritative_run_directory is not None:
        authoritative_path = Path(state.authoritative_run_directory)
        run_record = _load_model(
            run_directory / "authoritative" / "stage2-run.json",
            AuthoritativeRunRecord,
            "authoritative run",
        )
        if (
            run_record.run_directory != state.authoritative_run_directory
            or run_record.substage_id != state.authoritative_substage_id
            or run_record.specification_sha256
            != state.stage2_specification_sha256
        ):
            raise LiveShadowIntegrityError(
                "authoritative Stage 2 run identity changed"
            )
        try:
            with (authoritative_path / "journal.jsonl").open("rb") as handle:
                observed_prefix = handle.read(state.journal_read_offset)
        except OSError as exc:
            raise LiveShadowIntegrityError(
                "authoritative Stage 2 observed prefix is unavailable"
            ) from exc
        if len(observed_prefix) != state.journal_read_offset:
            raise LiveShadowIntegrityError(
                "authoritative Stage 2 observed prefix was truncated"
            )
        observed_entries = (
            _parse_stage2_prefix(authoritative_path, observed_prefix)
            if observed_prefix
            else ()
        )
        if (
            len(observed_entries) != state.authoritative_journal_sequence
            or (
                observed_entries[-1].entry_hash
                if observed_entries
                else ZERO_HASH
            )
            != state.authoritative_journal_hash
        ):
            raise LiveShadowIntegrityError(
                "authoritative Stage 2 observed prefix identity changed"
            )
        if state.authoritative_status is not None:
            try:
                authoritative_result, authoritative_state, _ = (
                    read_stage2_source_for_shadow(authoritative_path)
                )
            except WorkflowStateError as exc:
                raise LiveShadowIntegrityError(
                    "terminal authoritative Stage 2 integrity failed"
                ) from exc
            terminal = _load_model(
                run_directory / "authoritative" / "result.json",
                AuthoritativeTerminalRecord,
                "authoritative terminal result",
            )
            actual_result_hash = sha256_regular_file(
                authoritative_path / "result.json"
            )
            if (
                terminal.run_directory != state.authoritative_run_directory
                or terminal.status != state.authoritative_status
                or terminal.pause_reason != state.authoritative_pause_reason
                or terminal.process_exit_code
                != state.authoritative_process_exit_code
                or terminal.expected_exit_code
                != workflow_exit_code(authoritative_result.status)
                or terminal.observed_at != state.authoritative_terminal_at
                or
                authoritative_result.status != state.authoritative_status
                or authoritative_result.pause_reason
                != state.authoritative_pause_reason
                or actual_result_hash != state.authoritative_result_sha256
                or terminal.result_sha256 != actual_result_hash
                or terminal.journal_head != authoritative_state.journal_hash
                or terminal.journal_sequence
                != authoritative_state.journal_sequence
            ):
                raise LiveShadowIntegrityError(
                    "terminal authoritative Stage 2 result identity changed"
                )
    for collection, label in (
        (state.observed_decision_ids, "decision"),
        (state.proposal_ids, "proposal"),
        (state.comparison_ids, "comparison"),
        (state.reviewed_proposal_ids, "review"),
        (state.disqualified_proposal_ids, "disqualification"),
    ):
        if len(collection) != len(set(collection)):
            raise LiveShadowStateError(f"Stage 4 {label} history contains duplicates")
    if not set(state.proposal_ids).issubset(state.observed_decision_ids):
        raise LiveShadowStateError("proposal history cites an unobserved decision")
    if not set(state.comparison_ids).issubset(state.proposal_ids):
        raise LiveShadowStateError("comparison history cites an absent proposal")
    if not set(state.reviewed_proposal_ids).issubset(state.comparison_ids):
        raise LiveShadowStateError("review history cites an absent comparison")
    if not set(state.disqualified_proposal_ids).issubset(state.comparison_ids):
        raise LiveShadowStateError("disqualification history cites an absent assessment")
    if state.status == "completed":
        comparable_ids = {
            proposal_id
            for proposal_id in state.comparison_ids
            if _load_comparison(
                run_directory,
                proposal_id,
            ).comparison_available
        }
        if (
            state.authoritative_status is None
            or not comparable_ids.issubset(state.reviewed_proposal_ids)
            or state.shadow_failures
        ):
            raise LiveShadowStateError(
                "completed live-shadow state contradicts its evidence"
            )
    for decision_id in state.observed_decision_ids:
        _load_envelope(run_directory, decision_id)
        decision_directory = run_directory / "decisions" / decision_id
        for name in (
            "envelope.sha256",
            "blind-input-manifest.json",
            "output-schema.json",
        ):
            sha256_regular_file(decision_directory / name)
    for proposal_id in state.proposal_ids:
        proposal_directory = run_directory / "proposals" / proposal_id
        sha256_regular_file(proposal_directory / "supervisor-result.json")
        sha256_regular_file(proposal_directory / "candidate-prompt.md")
        failed_action_path = proposal_directory / "failed-supervisor-action.json"
        completed_action_path = proposal_directory / "supervisor-action.json"
        if failed_action_path.exists() and completed_action_path.exists():
            raise LiveShadowStateError(
                "supervisor action is both completed and failed"
            )
        if failed_action_path.exists():
            failed = _load_model(
                failed_action_path,
                FailedSupervisorActionRecord,
                "failed supervisor action",
            )
            envelope = _load_envelope(run_directory, proposal_id)
            if (
                failed.action_id != f"supervisor-{proposal_id}"
                or failed.proposal_id != proposal_id
                or failed.proposal_kind != envelope.proposal_kind
                or failed.stage1_artifact_directory
                != str(proposal_directory / "stage1-run")
                or failed.blind_manifest_sha256
                != sha256_regular_file(
                    run_directory
                    / "decisions"
                    / proposal_id
                    / "blind-input-manifest.json"
                )
                or failed.output_schema_sha256
                != sha256_regular_file(
                    run_directory
                    / "decisions"
                    / proposal_id
                    / "output-schema.json"
                )
            ):
                raise LiveShadowStateError(
                    "failed supervisor action identity changed"
                )
    for proposal_id in state.comparison_ids:
        comparison = _load_comparison(run_directory, proposal_id)
        assessment = _load_assessment(run_directory, proposal_id)
        if comparison.proposal_id != proposal_id or assessment.proposal_id != proposal_id:
            raise LiveShadowStateError("comparison or assessment identity changed")
        if assessment.disqualified != (
            proposal_id in state.disqualified_proposal_ids
        ):
            raise LiveShadowStateError("disqualification history contradicts assessment")
        comparison_directory = run_directory / "comparisons" / proposal_id
        source = comparison_directory / "authoritative-source.md"
        rendered = comparison_directory / "authoritative-rendered.md"
        if comparison.comparison_available:
            sha256_regular_file(source)
            sha256_regular_file(rendered)
        elif source.exists() or rendered.exists():
            raise LiveShadowStateError(
                "unavailable comparison contains authoritative material"
            )
        unavailable_path = comparison_directory / "comparison-unavailable.json"
        if (
            comparison.comparison_unavailable_reason
            == "authoritative_action_unfinished_after_terminal"
        ):
            unavailable = _load_unavailable_record(
                run_directory,
                proposal_id,
            )
            envelope = _load_envelope(run_directory, proposal_id)
            terminal = _load_model(
                run_directory / "authoritative" / "result.json",
                AuthoritativeTerminalRecord,
                "authoritative terminal result",
            )
            run_record = _load_model(
                run_directory / "authoritative" / "stage2-run.json",
                AuthoritativeRunRecord,
                "authoritative run",
            )
            if (
                unavailable.decision_id != proposal_id
                or unavailable.source_action_id
                != comparison.source_stage2_action_id
                or unavailable.source_action_id
                != envelope.source_action_id
                or unavailable.envelope_sha256
                != envelope.envelope_sha256
                or unavailable.authoritative_run_directory
                != terminal.run_directory
                or unavailable.authoritative_run_token
                != run_record.run_token
                or unavailable.authoritative_substage_id
                != run_record.substage_id
                or unavailable.authoritative_status != terminal.status
                or unavailable.authoritative_pause_reason
                != terminal.pause_reason
                or unavailable.authoritative_result_sha256
                != terminal.result_sha256
                or unavailable.authoritative_journal_sha256
                != terminal.journal_sha256
                or unavailable.authoritative_journal_sequence
                != terminal.journal_sequence
                or unavailable.authoritative_journal_hash
                != terminal.journal_head
            ):
                raise LiveShadowStateError(
                    "comparison-unavailable terminal binding changed"
                )
        elif unavailable_path.exists():
            raise LiveShadowStateError(
                "comparison has an unexpected unavailable-record artifact"
            )
    for proposal_id in state.reviewed_proposal_ids:
        review = _load_review(run_directory, proposal_id)
        if review.proposal_id != proposal_id:
            raise LiveShadowStateError("review identity changed")
    expected_result = _result_for_state(state, prepared)
    actual_result = _load_result(run_directory)
    if actual_result != expected_result:
        raise LiveShadowStateError("Stage 4 state and result snapshots disagree")


def _reload_prepared(
    state: LiveShadowState,
    services: LiveShadowServices,
) -> PreparedLiveShadowSpecification:
    try:
        prepared = load_live_shadow_specification(
            Path(state.specification_path),
            environ=services.environ,
            require_clean=False,
        )
    except LiveShadowInputError as exc:
        raise LiveShadowIntegrityError(
            "frozen Stage 4 inputs can no longer be loaded exactly"
        ) from exc
    return prepared


def _load_stable_run(
    run_directory: Path,
    services: LiveShadowServices,
) -> tuple[LiveShadowState, PreparedLiveShadowSpecification]:
    """Read one stable snapshot without healing journal-ahead crash state."""
    for attempt in range(20):
        state = _load_state(run_directory)
        prepared = _reload_prepared(state, services)
        try:
            _validate_run(run_directory, state, prepared)
        except LiveShadowStateError as exc:
            if (
                str(exc) != "Stage 4 state does not match its journal head"
                or attempt == 19
            ):
                raise
            services.sleep(0.005)
            continue
        return state, prepared
    raise LiveShadowStateError("live-shadow snapshot did not stabilize")


def _load_reconciled_run(
    run_directory: Path,
    services: LiveShadowServices,
) -> tuple[LiveShadowState, PreparedLiveShadowSpecification]:
    """Reconcile only a trustworthy journal-ahead snapshot under the run lock."""
    with _live_lock(run_directory, services.utc_now):
        state = _load_state(run_directory)
        prepared = _reload_prepared(state, services)
        entries = _read_live_journal(run_directory)
        persisted_result = _load_result(run_directory)
        if state.journal_sequence > len(entries):
            raise LiveShadowStateError(
                "live-shadow state is ahead of its durable journal"
            )
        if (
            state.journal_sequence
            and entries[state.journal_sequence - 1].entry_hash
            != state.journal_hash
        ) or (
            state.journal_sequence == 0
            and state.journal_hash != ZERO_HASH
        ):
            raise LiveShadowStateError(
                "live-shadow state journal prefix does not match"
            )
        _validate_live_journal_replay(
            entries[: state.journal_sequence],
            state,
        )
        if state.journal_sequence < len(entries):
            pre_recovery_result = _result_for_state(state, prepared)
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
                state = LiveShadowState.model_validate(values)
            except ValidationError as exc:
                raise LiveShadowStateError(
                    "live-shadow journal recovery produced invalid state"
                ) from exc
            _validate_live_journal_replay(entries, state)
            recovered_result = _result_for_state(state, prepared)
            if persisted_result not in (
                pre_recovery_result,
                recovered_result,
            ):
                raise LiveShadowStateError(
                    "live-shadow result snapshot contradicts every recoverable "
                    "journal generation"
                )
            _persist_state(run_directory, state, prepared)
        _validate_run(run_directory, state, prepared)
        return state, prepared


def _observed_stage2_entries(context: _LiveContext) -> tuple[JournalEntry, ...]:
    run_value = context.state.authoritative_run_directory
    if run_value is None:
        return ()
    path = Path(run_value) / "journal.jsonl"
    try:
        with path.open("rb") as handle:
            content = handle.read(context.state.journal_read_offset)
    except OSError as exc:
        raise LiveShadowStateError("observed authoritative prefix is unavailable") from exc
    if len(content) != context.state.journal_read_offset:
        raise LiveShadowIntegrityError("observed authoritative prefix was truncated")
    return _parse_stage2_prefix(Path(run_value), content)


def _proof_decision(envelope: LiveDecisionEnvelope) -> DecisionReconstruction:
    point = DecisionPoint(
        decision_id=envelope.decision_id,
        proposal_kind=envelope.proposal_kind,
        source_action_id=envelope.source_action_id,
        repair_round=envelope.repair_round,
        ordinal=envelope.ordinal,
        journal_sequence=envelope.journal_intent_sequence,
        evidence_sha256=hashlib.sha256(
            _canonical_json(envelope.triggering_evidence)
        ).hexdigest(),
        comparison_available=False,
        comparison_unavailable_reason="authoritative_action_pending",
    )
    return DecisionReconstruction(
        point=point,
        blind_evidence=envelope.triggering_evidence,
        authoritative_source=None,
        authoritative_rendered=None,
    )


def _pending_from_envelope(
    context: _LiveContext,
    envelope: LiveDecisionEnvelope,
) -> PendingSupervisorAction:
    matches: list[PendingSupervisorAction] = []
    for entry in _validate_live_journal(context.run_directory, context.state):
        if (
            entry.event_type == "shadow_action_intent"
            and entry.decision_id == envelope.decision_id
        ):
            try:
                matches.append(
                    PendingSupervisorAction.model_validate(
                        entry.state_updates["pending_action"]
                    )
                )
            except (KeyError, ValidationError) as exc:
                raise LiveShadowStateError(
                    "supervisor intent is invalid"
                ) from exc
    if len(matches) != 1:
        raise LiveShadowStateError(
            "proposal does not have exactly one supervisor intent"
        )
    return matches[0]


def _source_session_uuids(context: _LiveContext) -> frozenset[str]:
    run_value = context.state.authoritative_run_directory
    if run_value is None:
        return frozenset()
    identifiers: set[str] = set()
    actions = Path(run_value) / "actions"
    if not actions.is_dir():
        return frozenset()
    for path in actions.glob("*.json"):
        try:
            value = _read_json(path)
        except LiveShadowStateError:
            continue
        if value.get("kind") not in {"worker", "auditor"}:
            continue
        thread_ids = value.get("thread_started_ids")
        if not isinstance(thread_ids, list):
            continue
        for item in thread_ids:
            if not isinstance(item, str):
                continue
            try:
                parsed = UUID(item)
            except ValueError:
                continue
            if parsed.int != 0 and str(parsed) == item:
                identifiers.add(item)
    return frozenset(identifiers)


def _load_envelope(
    run_directory: Path,
    decision_id: str,
) -> LiveDecisionEnvelope:
    envelope = _load_model(
        run_directory / "decisions" / decision_id / "envelope.json",
        LiveDecisionEnvelope,
        "live decision envelope",
    )
    value = envelope.model_dump(mode="json")
    expected = cast(str, value.pop("envelope_sha256"))
    if hashlib.sha256(_canonical_json(value)).hexdigest() != expected:
        raise LiveShadowStateError("live decision envelope self-hash is invalid")
    try:
        hash_text = (
            run_directory / "decisions" / decision_id / "envelope.sha256"
        ).read_bytes()
    except OSError as exc:
        raise LiveShadowStateError("live decision envelope hash file is unreadable") from exc
    if hash_text != f"{expected}\n".encode("ascii"):
        raise LiveShadowStateError("live decision envelope hash file changed")
    return envelope


def _load_comparison(
    run_directory: Path,
    proposal_id: str,
) -> ProposalComparison:
    return _load_model(
        run_directory / "comparisons" / proposal_id / "comparison.json",
        ProposalComparison,
        "live proposal comparison",
    )


def _load_assessment(
    run_directory: Path,
    proposal_id: str,
) -> DeterministicAssessment:
    return _load_model(
        run_directory / "proposals" / proposal_id / "assessment.json",
        DeterministicAssessment,
        "live deterministic assessment",
    )


def _load_unavailable_record(
    run_directory: Path,
    proposal_id: str,
) -> LiveComparisonUnavailableRecord:
    record = _load_model(
        run_directory
        / "comparisons"
        / proposal_id
        / "comparison-unavailable.json",
        LiveComparisonUnavailableRecord,
        "comparison-unavailable record",
    )
    body = record.model_dump(mode="json", exclude={"record_sha256"})
    if hashlib.sha256(_canonical_json(body)).hexdigest() != record.record_sha256:
        raise LiveShadowStateError(
            "comparison-unavailable record self-hash is invalid"
        )
    return record


def _load_review(run_directory: Path, proposal_id: str) -> HumanReview:
    return _load_model(
        run_directory / "reviews" / f"{proposal_id}.json",
        HumanReview,
        "live human review",
    )


ModelT = TypeVar("ModelT", bound=BaseModel)


def _load_model(path: Path, model: type[ModelT], label: str) -> ModelT:
    try:
        return model.model_validate(_read_json(path))
    except ValidationError as exc:
        raise LiveShadowStateError(f"{label} is invalid") from exc


def _load_state(run_directory: Path) -> LiveShadowState:
    return _load_model(run_directory / STATE_FILE, LiveShadowState, "Stage 4 state")


def _load_result(run_directory: Path) -> LiveShadowResult:
    return _load_model(run_directory / RESULT_FILE, LiveShadowResult, "Stage 4 result")


def _validate_run_root_separation(
    run_directory: Path,
    stage2_runs_directory: Path,
    prepared: PreparedLiveShadowSpecification,
) -> None:
    try:
        resolved = run_directory.resolve(strict=False)
        stage2_runs = stage2_runs_directory.resolve(strict=False)
        repository = prepared.stage2.repository_root.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise LiveShadowInputError("live-shadow run separation could not be resolved") from exc
    if (
        _paths_overlap(resolved, repository)
        or _paths_overlap(resolved, stage2_runs)
    ):
        raise LiveShadowInputError(
            "Stage 4 run, authoritative repository, and Stage 2 runs must be separate"
        )


def _validate_quarantine_workspace(path: Path) -> None:
    try:
        status = path.lstat()
        if (
            not path.is_dir()
            or path.is_symlink()
            or os.path.ismount(path)
        ):
            raise OSError
        entries = {entry.name: entry for entry in path.iterdir()}
        if set(entries) != {"workspace", "codex-home"}:
            raise OSError
        workspace = entries["workspace"]
        runtime_home = entries["codex-home"]
        for directory in (workspace, runtime_home):
            directory_status = directory.lstat()
            if (
                not stat.S_ISDIR(directory_status.st_mode)
                or directory.is_symlink()
                or os.path.ismount(directory)
            ):
                raise OSError
        if tuple(workspace.iterdir()):
            raise OSError
        validate_runtime_home_contents(runtime_home)
    except OSError as exc:
        raise LiveShadowIntegrityError(
            "quarantine workspace is unavailable or not an exact directory"
        ) from exc
    if not status:
        raise LiveShadowIntegrityError("quarantine workspace status is unavailable")


def _quarantine_workspace(run_directory: Path) -> Path:
    return run_directory / "quarantine" / "workspace"


def _codex_runtime_home(run_directory: Path) -> Path:
    return run_directory / "quarantine" / "codex-home"


def _isolation_forbidden_roots(context: _LiveContext) -> tuple[Path, ...]:
    roots = [
        context.prepared.stage2.repository_root,
        context.stage2_runs_directory,
    ]
    if context.state.authoritative_run_directory is not None:
        roots.append(Path(context.state.authoritative_run_directory))
    return tuple(roots)


def _verify_isolated_supervisor_command(
    context: _LiveContext,
    pending: PendingSupervisorAction,
    metadata: CodexMetadata,
) -> None:
    verify_recorded_bubblewrap_command(
        pending,
        metadata,
        identity=load_backend_identity(
            context.run_directory / "isolation.json"
        ),
        runtime_home=_codex_runtime_home(context.run_directory),
        authentication_file=(
            context.isolation_capability.authentication_file
            if context.isolation_capability is not None
            else None
        ),
    )


def _resolve_codex_executable(
    configured: str | None,
    sensitive_values: Sequence[str],
) -> str:
    try:
        if configured is not None:
            preflight_shadow_confidentiality(
                configured,
                sensitive_values,
                label="configured Codex executable",
            )
    except ShadowInputError as exc:
        raise LiveShadowInputError(str(exc)) from exc
    candidate = configured or shutil.which("codex")
    if candidate is None:
        raise LiveShadowDependencyError("Codex executable is required")
    try:
        resolved = Path(candidate).resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise LiveShadowDependencyError("Codex executable could not be resolved") from exc
    if not resolved.is_file():
        raise LiveShadowDependencyError("Codex executable is not a regular file")
    try:
        preflight_shadow_confidentiality(
            str(resolved),
            sensitive_values,
            label="resolved Codex executable",
        )
    except ShadowInputError as exc:
        raise LiveShadowInputError(str(exc)) from exc
    return str(resolved)


def _preflight_locator(
    value: str | os.PathLike[str],
    sensitive_values: Sequence[str],
    label: str,
) -> str:
    try:
        return preflight_shadow_locator(value, sensitive_values, label=label)
    except ShadowInputError as exc:
        raise LiveShadowInputError(str(exc)) from exc


def _sensitive_values(environ: Mapping[str, str] | None) -> tuple[str, ...]:
    _, _, values = build_subprocess_environment(environ)
    return values


def _resolve_run_directory(path: Path) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise LiveShadowInputError("live-shadow run directory could not be resolved") from exc
    if not resolved.is_dir():
        raise LiveShadowInputError("live-shadow run path is not a directory")
    return resolved


@contextmanager
def _live_lock(
    run_directory: Path,
    utc_now: Callable[[], datetime],
) -> Iterator[None]:
    try:
        with _ShadowLock(run_directory, utc_now):
            yield
    except ShadowLockError as exc:
        raise LiveShadowLockError(str(exc)) from exc


def _process_start_ticks(pid: int) -> int | None:
    try:
        value = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        close = value.rfind(")")
        fields = value[close + 2 :].split()
        ticks = int(fields[19])
    except (OSError, ValueError, IndexError):
        return None
    return ticks if ticks > 0 else None


def _process_identity_running(pid: int, expected_ticks: int) -> bool:
    try:
        value = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        close = value.rfind(")")
        fields = value[close + 2 :].split()
        process_state = fields[0]
        ticks = int(fields[19])
    except (OSError, ValueError, IndexError):
        return False
    return ticks == expected_ticks and process_state != "Z"


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LiveShadowStateError("Stage 4 timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise LiveShadowStateError("Stage 4 timestamp must be UTC")
    return parsed


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=_reject_json_constant,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise LiveShadowStateError("durable Stage 4 JSON artifact is invalid") from exc
    if not isinstance(value, dict):
        raise LiveShadowStateError("durable Stage 4 JSON artifact is not an object")
    return cast(dict[str, Any], value)


def _write_json(path: Path, value: object) -> None:
    _atomic_write(path, _render_json_bytes(value))


def _write_immutable_json(path: Path, value: object) -> None:
    _write_immutable_bytes(path, _render_json_bytes(value))


def _write_text(path: Path, value: str) -> None:
    _atomic_write(path, value.encode("utf-8"))


def _write_bytes(path: Path, value: bytes) -> None:
    _atomic_write(path, value)


def _write_immutable_bytes(path: Path, value: bytes) -> None:
    if path.exists():
        try:
            existing = path.read_bytes()
        except OSError as exc:
            raise LiveShadowStateError(
                "immutable Stage 4 artifact is unreadable"
            ) from exc
        if existing != value:
            raise LiveShadowStateError(
                "immutable Stage 4 artifact contradicts durable recovery"
            )
        return
    _atomic_write(path, value)


def _snapshot_checkpoint(name: str) -> None:
    """Deterministic no-op boundary used by crash-ordering regression tests."""
    del name


def _atomic_write(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except OSError as exc:
        raise LiveShadowStateError("Stage 4 artifact could not be written") from exc
    finally:
        with suppress(FileNotFoundError):
            temporary.unlink()


def _render_json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            _json_compatible(value),
            ensure_ascii=True,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("ascii")


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(
            _json_compatible(value),
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("ascii")


def _json_compatible(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {str(key): _json_compatible(item) for key, item in value.items()}
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


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _paths_overlap(left: Path, right: Path) -> bool:
    return _is_relative_to(left, right) or _is_relative_to(right, left)


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")


def _utc_string(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
