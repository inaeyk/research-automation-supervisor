"""Thin deterministic PA-5C3 benchmark campaign orchestration.

The campaign owns only enumeration, durable action binding, PA-5A delegation, and
PA-5C2 aggregation.  Scientific execution and recovery remain child-workflow concerns.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol, TypedDict, TypeVar, cast

from pydantic import BaseModel, ValidationError

from research_automation_supervisor.durable_state import (
    ZERO_HASH,
    append_hashed_journal_entry,
    atomic_write_bytes,
    atomic_write_json,
    canonical_json,
    read_hashed_journal,
    reconcile_model_snapshot,
    render_json_bytes,
)
from research_automation_supervisor.errors import (
    PhysicsBenchmarkCampaignInputError,
    PhysicsBenchmarkCampaignLockError,
    PhysicsBenchmarkCampaignStateError,
    PhysicsBenchmarkScoringError,
    SupervisorError,
    WorkflowInputError,
    WorkflowStateError,
)
from research_automation_supervisor.physics_benchmark_blindness import (
    PhysicsBlindFixtureCatalogV1,
    load_blind_fixture_catalog,
)
from research_automation_supervisor.physics_benchmark_campaign_models import (
    CampaignAggregateReceiptV1,
    CampaignChildAuthorityV1,
    CampaignScorerActionStartV1,
    CampaignScorerResultReceiptV1,
    CampaignScoringArtifactsV1,
    CampaignTerminalChildV1,
    PhysicsBenchmarkCampaignJournalEntryV1,
    PhysicsBenchmarkCampaignManifestV1,
    PhysicsBenchmarkCampaignStateV1,
    PhysicsBenchmarkCampaignStatusV1,
)
from research_automation_supervisor.physics_benchmark_child_workflow import (
    execute_qualified_benchmark_child_recovery,
    launch_qualified_benchmark_child,
)
from research_automation_supervisor.physics_benchmark_scoring import (
    ExactBenchmarkExpectedRunsV1,
    ExactBenchmarkObservedRun,
    ExactBenchmarkRunArtifacts,
    ExactBenchmarkScoreReportV1,
    bind_exact_benchmark_run,
    issue_expected_run_manifest,
    score_exact_physics_benchmark,
)
from research_automation_supervisor.physics_workflow import (
    DEFAULT_PHYSICS_WORKFLOW_SERVICES,
    PhysicsWorkflowServices,
    load_physics_substage_specification,
    physics_substage_status,
)
from research_automation_supervisor.physics_workflow_models import PhysicsWorkflowStateV2
from research_automation_supervisor.workflow_engine import (
    DEFAULT_WORKFLOW_SERVICES,
    WorkflowServices,
)
from research_automation_supervisor.workflow_integrity import sha256_regular_file
from research_automation_supervisor.workflow_recovery import (
    DEFAULT_RECOVERY_SERVICES,
    RECEIPT_ROOT,
    RecoveryExecutionV1,
    RecoveryServices,
    build_recovery_plan,
    discover_workflow_runs,
)
from research_automation_supervisor.workflow_recovery_models import (
    RecoveryAttemptReceiptV1,
    RecoveryOutcomeV1,
    RecoveryPlanV1,
    RunIndexEntryV1,
    RunIndexV1,
)

MANIFEST_FILE = "campaign-manifest-v1.json"
STATE_FILE = "campaign-state-v1.json"
JOURNAL_FILE = "campaign-journal-v1.jsonl"
LOCK_FILE = "campaign.lock"
CHILD_RUNS_DIRECTORY = "child-runs"
ACTIONS_DIRECTORY = "actions"
TERMINAL_DIRECTORY = "terminal-observations"
EXPECTED_RUNS_FILE = "expected-pa5c2-runs-v1.json"
SCORER_ACTION_FILE = "actions/scoring/scorer-action-start-v1.json"
SCORER_RESULT_FILE = "actions/scoring/scorer-result-v1.json"
AGGREGATE_FILE = "aggregate-pa5c2-v1.json"
ACTION_TREE_FILE = "campaign-action-tree-v1.json"
COMPLETION_RECEIPT_FILE = "campaign-completion-v1.json"

_TERMINAL_CHILD_STATUSES = frozenset(
    {"completed", "checkpoint_paused", "infrastructure_stopped", "failed", "aborted"}
)
ModelT = TypeVar("ModelT", bound=BaseModel)


class _ManifestStateAuthority(TypedDict):
    campaign_id: str
    manifest_sha256: str
    repository_root: str
    scorer_catalog_path: str
    catalog_id: Literal["physics_benchmark_blind_authority_v1"]
    catalog_sha256: str
    expected_child_set_sha256: str
    scorer_authority_sha256: str


@dataclass(frozen=True)
class CampaignChildRequest:
    """Operator input used only to freeze the complete child set."""

    case_id: str
    variant_id: Literal["variant_001", "variant_002"]
    repetition_id: int
    specification_path: Path


class ChildLauncher(Protocol):
    def __call__(
        self,
        child: CampaignChildAuthorityV1,
        catalog: PhysicsBlindFixtureCatalogV1,
        *,
        child_runs_directory: Path,
        repository_root: Path,
        workflow_services: WorkflowServices,
        physics_services: PhysicsWorkflowServices,
    ) -> None: ...


class ChildRecoveryExecutor(Protocol):
    def __call__(
        self,
        child: CampaignChildAuthorityV1,
        catalog: PhysicsBlindFixtureCatalogV1,
        plan: RecoveryPlanV1,
        *,
        repository_root: Path,
        attempt_token: str,
        recovery_services: RecoveryServices,
    ) -> RecoveryExecutionV1: ...


class TerminalBinder(Protocol):
    def __call__(
        self,
        child: CampaignChildAuthorityV1,
        entry: RunIndexEntryV1,
        catalog: PhysicsBlindFixtureCatalogV1,
        *,
        repository_root: Path,
    ) -> CampaignTerminalChildV1: ...


class CampaignScorer(Protocol):
    def __call__(
        self,
        expected: ExactBenchmarkExpectedRunsV1,
        observed: tuple[ExactBenchmarkObservedRun, ...],
        *,
        catalog_path: Path,
        repository_root: Path,
    ) -> ExactBenchmarkScoreReportV1: ...


@dataclass(frozen=True)
class PhysicsBenchmarkCampaignServices:
    """Qualification seams; production defaults are only qualified child boundaries."""

    workflow_services: WorkflowServices = DEFAULT_WORKFLOW_SERVICES
    physics_services: PhysicsWorkflowServices = DEFAULT_PHYSICS_WORKFLOW_SERVICES
    recovery_services: RecoveryServices = DEFAULT_RECOVERY_SERVICES
    child_launcher: ChildLauncher = launch_qualified_benchmark_child
    child_recovery_executor: ChildRecoveryExecutor = execute_qualified_benchmark_child_recovery
    run_discoverer: Callable[..., RunIndexV1] = discover_workflow_runs
    recovery_planner: Callable[[Path], RecoveryPlanV1] = build_recovery_plan
    terminal_binder: TerminalBinder = lambda child, entry, catalog, *, repository_root: (
        _bind_terminal_child(child, entry, catalog, repository_root=repository_root)
    )
    scorer: CampaignScorer = score_exact_physics_benchmark
    checkpoint: Callable[[str], None] = lambda _name: None
    utc_now: Callable[[], datetime] = lambda: datetime.now(UTC)


DEFAULT_CAMPAIGN_SERVICES = PhysicsBenchmarkCampaignServices()


@dataclass
class _CampaignContext:
    run_directory: Path
    manifest: PhysicsBenchmarkCampaignManifestV1
    catalog: PhysicsBlindFixtureCatalogV1
    state: PhysicsBenchmarkCampaignStateV1
    services: PhysicsBenchmarkCampaignServices

    @property
    def child_runs_directory(self) -> Path:
        return Path(self.manifest.child_runs_directory)

    @property
    def repository_root(self) -> Path:
        return Path(self.manifest.repository_root)


def create_physics_benchmark_campaign(
    campaign_id: str,
    children: Sequence[CampaignChildRequest],
    *,
    run_directory: Path,
    catalog_path: Path,
    repository_root: Path,
    services: PhysicsBenchmarkCampaignServices = DEFAULT_CAMPAIGN_SERVICES,
) -> PhysicsBenchmarkCampaignStateV1:
    """Freeze one complete manifest, create its durable state, and drive sequentially."""
    root = _create_campaign_directory(run_directory)
    child_runs = root / CHILD_RUNS_DIRECTORY
    _make_directory(child_runs)
    try:
        manifest, catalog = _freeze_manifest(
            campaign_id,
            children,
            child_runs_directory=child_runs,
            catalog_path=catalog_path,
            repository_root=repository_root,
        )
        _write_once_or_verify_json(
            root / MANIFEST_FILE,
            manifest.model_dump(mode="json"),
            "campaign manifest",
        )
        atomic_write_bytes(
            root / JOURNAL_FILE,
            b"",
            error_factory=PhysicsBenchmarkCampaignStateError,
            error_message="campaign journal could not be initialized",
        )
        now = _utc_string(services.utc_now())
        authority = _manifest_state_authority(manifest)
        state = PhysicsBenchmarkCampaignStateV1(
            **authority,
            status="running",
            reason_code="campaign_initialized",
            journal_sequence=0,
            journal_hash=ZERO_HASH,
            started_at=now,
            updated_at=now,
        )
        _persist_state(root, state)
        context = _CampaignContext(root, manifest, catalog, state, services)
        context.state = _event(
            context,
            "campaign_initialized",
            _action_id("initialize", manifest.manifest_sha256),
            {
                **authority,
                "status": "running",
                "reason_code": "campaign_initialized",
            },
            {str(root / MANIFEST_FILE): sha256_regular_file(root / MANIFEST_FILE)},
        )
        with _campaign_lock(root, services.utc_now):
            return _drive(context)
    except BaseException:
        # The exclusive directory is intentional evidence of any interrupted creation.
        raise


def resume_physics_benchmark_campaign(
    run_directory: Path,
    *,
    services: PhysicsBenchmarkCampaignServices = DEFAULT_CAMPAIGN_SERVICES,
) -> PhysicsBenchmarkCampaignStateV1:
    """Resume campaign work while delegating every child recovery decision to PA-5A."""
    root = _resolve_campaign_directory(run_directory)
    with _campaign_lock(root, services.utc_now):
        manifest = _load_manifest(root)
        state = _load_reconciled_state(root, persist=True)
        _verify_manifest_state_binding(root, manifest, state)
        catalog = _load_current_catalog(manifest)
        context = _CampaignContext(root, manifest, catalog, state, services)
        _verify_durable_artifact_rebinding(context)
        if state.status == "completed":
            _validate_completed(context)
            return context.state
        try:
            _verify_frozen_manifest_authority(context)
        except (PhysicsBenchmarkCampaignInputError, WorkflowInputError) as exc:
            del exc
            return _route(
                context,
                "infrastructure_blocked",
                "stale_campaign_authority",
                None,
            )
        return _drive(context)


def physics_benchmark_campaign_status(
    run_directory: Path,
    *,
    services: PhysicsBenchmarkCampaignServices = DEFAULT_CAMPAIGN_SERVICES,
) -> PhysicsBenchmarkCampaignStateV1:
    """Read, reconcile, and verify one campaign without mutation."""
    root = _resolve_campaign_directory(run_directory)
    manifest = _load_manifest(root)
    state = _load_reconciled_state(root, persist=False)
    _verify_manifest_state_binding(root, manifest, state)
    catalog = _load_current_catalog(manifest)
    context = _CampaignContext(root, manifest, catalog, state, services)
    _verify_durable_artifact_rebinding(context)
    if state.status == "completed":
        _validate_completed(context)
    return context.state


def _freeze_manifest(
    campaign_id: str,
    requests: Sequence[CampaignChildRequest],
    *,
    child_runs_directory: Path,
    catalog_path: Path,
    repository_root: Path,
) -> tuple[PhysicsBenchmarkCampaignManifestV1, PhysicsBlindFixtureCatalogV1]:
    if not requests:
        raise PhysicsBenchmarkCampaignInputError("campaign child set is empty")
    request_keys = tuple((item.case_id, item.variant_id, item.repetition_id) for item in requests)
    if len(request_keys) != len(set(request_keys)):
        raise PhysicsBenchmarkCampaignInputError(
            "campaign contains a duplicate case/variant/repetition child"
        )
    root = _canonical_directory(repository_root, "repository root")
    catalog_file = _canonical_file(catalog_path, "scorer catalog")
    catalog = load_blind_fixture_catalog(catalog_file)
    children: list[CampaignChildAuthorityV1] = []
    for request in requests:
        pair = catalog.pair(request.case_id)
        if request.variant_id not in {item.variant_id for item in pair.variants}:
            raise PhysicsBenchmarkCampaignInputError("campaign variant is unavailable")
        prepared = load_physics_substage_specification(request.specification_path)
        coordinates = {
            "campaign_id": campaign_id,
            "case_id": request.case_id,
            "variant_id": request.variant_id,
            "repetition_id": request.repetition_id,
        }
        digest = hashlib.sha256(canonical_json(coordinates)).hexdigest()
        child_run_id = f"child-{digest[:32]}"
        run_token = f"bench-{digest[:32]}"
        workflow_run = child_runs_directory / f"{prepared.specification.substage_id}-{run_token}"
        try:
            child = CampaignChildAuthorityV1(
                campaign_id=campaign_id,
                child_run_id=child_run_id,
                case_id=request.case_id,
                variant_id=request.variant_id,
                repetition_id=request.repetition_id,
                substage_id=prepared.specification.substage_id,
                run_token=run_token,
                workflow_run_directory=str(workflow_run),
                specification_path=str(prepared.specification_path),
                specification_sha256=prepared.specification_sha256,
                workspace=str(prepared.workspace),
                repository_root=str(prepared.repository_root),
                baseline_commit=prepared.software_prepared.baseline_commit,
                physics_contract_path=str(prepared.physics_contract_path),
                physics_contract_sha256=prepared.physics_contract_sha256,
                oracle_catalog_path=str(prepared.oracle_catalog_path),
                oracle_catalog_sha256=prepared.oracle_catalog_sha256,
                auditor_config_path=str(prepared.auditor_config_path),
                auditor_config_sha256=prepared.auditor_config_sha256,
            )
        except ValidationError as exc:
            raise PhysicsBenchmarkCampaignInputError("campaign child authority is invalid") from exc
        children.append(child)
    payload: dict[str, object] = {
        "schema_version": 1,
        "campaign_id": campaign_id,
        "repository_root": str(root),
        "child_runs_directory": str(child_runs_directory),
        "scorer_catalog_path": str(catalog_file),
        "catalog_id": catalog.catalog_id,
        "catalog_sha256": catalog.canonical_sha256(),
        "children": [
            item.model_dump(mode="json")
            for item in sorted(
                children,
                key=lambda child: (child.case_id, child.variant_id, child.repetition_id),
            )
        ],
    }
    try:
        manifest = PhysicsBenchmarkCampaignManifestV1.model_validate(
            {
                **payload,
                "manifest_sha256": hashlib.sha256(canonical_json(payload)).hexdigest(),
            }
        )
    except ValidationError as exc:
        raise PhysicsBenchmarkCampaignInputError("campaign manifest is invalid") from exc
    return manifest, catalog


def _drive(context: _CampaignContext) -> PhysicsBenchmarkCampaignStateV1:
    _verify_manifest_state_binding(context.run_directory, context.manifest, context.state)
    _verify_durable_artifact_rebinding(context)
    for index, child in enumerate(context.manifest.children):
        if child.child_run_id not in context.state.registered_child_run_ids:
            context.services.checkpoint(f"before_child_registration:{child.child_run_id}")
            _register_child(context, child)
            context.services.checkpoint(f"after_child_registration:{child.child_run_id}")

        while child.child_run_id not in {
            item.child_run_id for item in context.state.terminal_children
        }:
            index_value, issue = _discover_exact_children(context)
            if issue is not None:
                return _route(context, "infrastructure_blocked", issue, child.child_run_id)
            entry = index_value.get(child.workflow_run_directory)
            child_path = Path(child.workflow_run_directory)
            if entry is None:
                if child_path.exists():
                    return _route(
                        context,
                        "infrastructure_blocked",
                        "child_run_record_unavailable",
                        child.child_run_id,
                    )
                if child.child_run_id in context.state.launch_intent_child_run_ids:
                    return _route(
                        context,
                        "infrastructure_blocked",
                        "child_launch_state_ambiguous",
                        child.child_run_id,
                    )
                try:
                    _verify_child_authority(child)
                except (PhysicsBenchmarkCampaignStateError, WorkflowInputError):
                    return _route(
                        context,
                        "infrastructure_blocked",
                        "stale_child_authority",
                        child.child_run_id,
                    )
                context.services.checkpoint(
                    f"after_registration_before_child_launch:{child.child_run_id}"
                )
                _record_launch_intent(context, child)
                context.services.checkpoint(f"after_child_launch_intent:{child.child_run_id}")
                try:
                    context.services.child_launcher(
                        child,
                        context.catalog,
                        child_runs_directory=context.child_runs_directory,
                        repository_root=context.repository_root,
                        workflow_services=context.services.workflow_services,
                        physics_services=context.services.physics_services,
                    )
                except SupervisorError:
                    return _route(
                        context,
                        "infrastructure_blocked",
                        "child_launch_failed_closed",
                        child.child_run_id,
                    )
                context.services.checkpoint(
                    f"after_child_terminal_before_campaign_observation:{child.child_run_id}"
                )
                continue

            reason = _entry_authority_issue(child, entry)
            if reason is not None:
                return _route(context, "infrastructure_blocked", reason, child.child_run_id)
            try:
                plan = context.services.recovery_planner(Path(entry.run_directory))
            except (WorkflowInputError, WorkflowStateError):
                return _route(
                    context,
                    "infrastructure_blocked",
                    "child_recovery_plan_unavailable",
                    child.child_run_id,
                )
            if _plan_authority_issue(child, entry, plan) is not None:
                return _route(
                    context,
                    "infrastructure_blocked",
                    "child_recovery_plan_substituted",
                    child.child_run_id,
                )
            if plan.disposition in {"auto_resume", "finish_finalization"}:
                context.state = _route(
                    context,
                    "resumable",
                    plan.reason_code,
                    child.child_run_id,
                )
                execution = _delegate_recovery(context, child, plan)
                if execution is None or execution.outcome.status in {"blocked", "failed"}:
                    return _route(
                        context,
                        "infrastructure_blocked",
                        "child_recovery_failed_closed",
                        child.child_run_id,
                    )
                context.services.checkpoint(
                    f"after_child_terminal_before_campaign_observation:{child.child_run_id}"
                )
                continue
            if plan.disposition == "blocked":
                routed: PhysicsBenchmarkCampaignStatusV1 = (
                    "running"
                    if plan.process_reconciliation == "active_matching"
                    else "infrastructure_blocked"
                )
                return _route(context, routed, plan.reason_code, child.child_run_id)
            if plan.disposition == "reopen_pause":
                routed = (
                    "insufficient_evidence"
                    if plan.observed_status == "evidence_paused"
                    else "human_review_required"
                )
                return _route(context, routed, plan.reason_code, child.child_run_id)
            if plan.disposition != "already_terminal" or entry.completion != "terminal":
                return _route(
                    context,
                    "infrastructure_blocked",
                    "child_terminal_state_unproven",
                    child.child_run_id,
                )
            if plan.observed_status in {"failed", "aborted"}:
                return _route(
                    context,
                    "child_failed",
                    f"child_{plan.observed_status}",
                    child.child_run_id,
                )
            try:
                observed = context.services.terminal_binder(
                    child,
                    entry,
                    context.catalog,
                    repository_root=context.repository_root,
                )
            except (PhysicsBenchmarkCampaignStateError, PhysicsBenchmarkScoringError):
                return _route(
                    context,
                    "infrastructure_blocked",
                    "child_pa5c2_verification_failed",
                    child.child_run_id,
                )
            if _terminal_observation_issue(child, observed) is not None:
                return _route(
                    context,
                    "infrastructure_blocked",
                    "child_pa5c2_identity_substituted",
                    child.child_run_id,
                )
            _record_terminal_observation(context, observed)
        if index + 1 < len(context.manifest.children):
            context.services.checkpoint(f"between_child_runs:{child.child_run_id}")

    _, issue = _discover_exact_children(context, require_complete=True)
    if issue is not None:
        return _route(context, "infrastructure_blocked", issue, None)
    if (
        context.state.scorer_action_id is not None
        and context.state.scorer_result_sha256 is None
    ):
        return _route(
            context,
            "infrastructure_blocked",
            "ambiguous_partial_scorer_action",
            None,
        )
    if context.state.status != "ready_to_aggregate":
        context.state = _event(
            context,
            "campaign_ready_to_aggregate",
            _action_id("ready", context.manifest.manifest_sha256),
            {
                "status": "ready_to_aggregate",
                "reason_code": "all_children_terminal_verified",
                "blocking_child_run_id": None,
            },
            _terminal_artifact_hashes(context.state.terminal_children),
        )
    context.services.checkpoint("after_all_children_terminal_before_scoring")
    try:
        return _aggregate_and_finalize(context)
    except (PhysicsBenchmarkCampaignStateError, PhysicsBenchmarkScoringError):
        return _route(
            context,
            "infrastructure_blocked",
            "campaign_aggregation_failed_closed",
            None,
        )


def _register_child(context: _CampaignContext, child: CampaignChildAuthorityV1) -> None:
    path = (
        context.run_directory / ACTIONS_DIRECTORY / "registrations" / (f"{child.child_run_id}.json")
    )
    value = {
        "schema_version": 1,
        "action_id": f"register-{child.child_run_id}",
        "manifest_sha256": context.manifest.manifest_sha256,
        "child_authority_sha256": child.canonical_sha256(),
        "child": child.model_dump(mode="json"),
    }
    _write_once_or_verify_json(path, value, "child registration")
    registered = (*context.state.registered_child_run_ids, child.child_run_id)
    context.state = _event(
        context,
        "child_registered",
        f"register-{child.child_run_id}",
        {"registered_child_run_ids": registered, "status": "running"},
        {str(path): sha256_regular_file(path)},
    )


def _record_launch_intent(context: _CampaignContext, child: CampaignChildAuthorityV1) -> None:
    path = context.run_directory / ACTIONS_DIRECTORY / "launches" / f"{child.child_run_id}.json"
    value = {
        "schema_version": 1,
        "action_id": f"launch-{child.child_run_id}",
        "manifest_sha256": context.manifest.manifest_sha256,
        "child_run_id": child.child_run_id,
        "child_authority_sha256": child.canonical_sha256(),
        "workflow_run_directory": child.workflow_run_directory,
        "delegated_entrypoint": "pa4_run_substage",
    }
    _write_once_or_verify_json(path, value, "child launch intent")
    intended = (*context.state.launch_intent_child_run_ids, child.child_run_id)
    context.state = _event(
        context,
        "child_launch_intended",
        f"launch-{child.child_run_id}",
        {
            "launch_intent_child_run_ids": intended,
            "status": "running",
            "reason_code": "child_launch_delegated",
            "blocking_child_run_id": child.child_run_id,
        },
        {str(path): sha256_regular_file(path)},
    )


def _record_terminal_observation(
    context: _CampaignContext, observed: CampaignTerminalChildV1
) -> None:
    existing = {item.child_run_id: item for item in context.state.terminal_children}.get(
        observed.child_run_id
    )
    if existing is not None:
        if existing != observed:
            raise PhysicsBenchmarkCampaignStateError("terminal child observation changed")
        return
    path = context.run_directory / TERMINAL_DIRECTORY / f"{observed.child_run_id}.json"
    value = observed.model_dump(mode="json")
    _write_once_or_verify_json(path, value, "terminal child observation")
    context.state = _event(
        context,
        "child_terminal_observed",
        _action_id("observe", observed.canonical_sha256()),
        {
            "terminal_children": (*context.state.terminal_children, observed),
            "status": "running",
            "reason_code": "terminal_child_pa5c2_verified",
            "blocking_child_run_id": None,
        },
        {str(path): sha256_regular_file(path)},
    )


def _terminal_observation_issue(
    child: CampaignChildAuthorityV1,
    observed: CampaignTerminalChildV1,
) -> str | None:
    identity = observed.scoring_identity
    valid = (
        observed.child_run_id == child.child_run_id,
        observed.child_authority_sha256 == child.canonical_sha256(),
        identity.case_id == child.case_id,
        identity.variant_id == child.variant_id,
        identity.repetition_id == child.repetition_id,
    )
    return None if all(valid) else "child_pa5c2_identity_substituted"


def _delegate_recovery(
    context: _CampaignContext,
    child: CampaignChildAuthorityV1,
    plan: RecoveryPlanV1,
) -> RecoveryExecutionV1 | None:
    root = context.run_directory / ACTIONS_DIRECTORY / "recoveries" / child.child_run_id
    existing = _recovery_action_records(root, plan.canonical_sha256())
    for action_path, action in existing:
        attempt_token = cast(str, action["attempt_token"])
        plan_receipts, outcome_receipts = _pa5a_receipts(
            context.child_runs_directory, attempt_token
        )
        if len(plan_receipts) > 1 or len(outcome_receipts) > 1:
            raise PhysicsBenchmarkCampaignStateError("duplicate PA-5A recovery receipts")
        if outcome_receipts:
            attempt = _load_model(plan_receipts[0], RecoveryAttemptReceiptV1)
            outcome = _load_model(outcome_receipts[0], RecoveryOutcomeV1)
            if attempt.plan_sha256 != plan.canonical_sha256():
                raise PhysicsBenchmarkCampaignStateError("PA-5A plan receipt was substituted")
            if outcome.plan_sha256 != attempt.plan_sha256:
                raise PhysicsBenchmarkCampaignStateError("PA-5A outcome receipt was substituted")
            del action_path
            return RecoveryExecutionV1(
                plan=attempt.plan,
                outcome=outcome,
                plan_receipt_path=plan_receipts[0],
                outcome_receipt_path=outcome_receipts[0],
            )
        if not plan_receipts:
            return _execute_recovery(context, child, plan, attempt_token, action_path)

    attempt_number = len(existing) + 1
    seed = hashlib.sha256(
        canonical_json(
            {
                "manifest_sha256": context.manifest.manifest_sha256,
                "child_run_id": child.child_run_id,
                "plan_sha256": plan.canonical_sha256(),
            }
        )
    ).hexdigest()
    attempt_token = f"camp-{seed[:32]}-{attempt_number:03d}"
    action_id = f"recover-{seed[:24]}-{attempt_number:03d}"
    path = root / f"{action_id}.json"
    value = {
        "schema_version": 1,
        "action_id": action_id,
        "attempt_token": attempt_token,
        "child_run_id": child.child_run_id,
        "plan_sha256": plan.canonical_sha256(),
        "delegated_entrypoint": "pa5a_execute_recovery_plan",
    }
    _write_once_or_verify_json(path, value, "campaign recovery delegation")
    context.services.checkpoint(f"after_recovery_delegation_intent:{child.child_run_id}")
    return _execute_recovery(context, child, plan, attempt_token, path)


def _execute_recovery(
    context: _CampaignContext,
    child: CampaignChildAuthorityV1,
    plan: RecoveryPlanV1,
    attempt_token: str,
    action_path: Path,
) -> RecoveryExecutionV1:
    execution = context.services.child_recovery_executor(
        child,
        context.catalog,
        plan,
        repository_root=context.repository_root,
        attempt_token=attempt_token,
        recovery_services=context.services.recovery_services,
    )
    outcome_path = action_path.with_suffix(".outcome.json")
    value = {
        "schema_version": 1,
        "action_id": json.loads(action_path.read_text(encoding="utf-8"))["action_id"],
        "plan_sha256": execution.plan.canonical_sha256(),
        "outcome_sha256": execution.outcome.canonical_sha256(),
        "pa5a_plan_receipt_path": str(execution.plan_receipt_path),
        "pa5a_plan_receipt_sha256": sha256_regular_file(execution.plan_receipt_path),
        "pa5a_outcome_receipt_path": str(execution.outcome_receipt_path),
        "pa5a_outcome_receipt_sha256": sha256_regular_file(execution.outcome_receipt_path),
    }
    _write_once_or_verify_json(outcome_path, value, "campaign recovery outcome")
    return execution


def _aggregate_and_finalize(
    context: _CampaignContext,
) -> PhysicsBenchmarkCampaignStateV1:
    _verify_durable_artifact_rebinding(context)
    rebound = _rebind_all_terminal_children(context)
    identities = tuple(item.scoring_identity for item in rebound)
    expected = issue_expected_run_manifest(context.catalog, identities)
    observed = tuple(_to_observed(item) for item in rebound)
    expected_path = context.run_directory / EXPECTED_RUNS_FILE
    scorer_action_path = context.run_directory / SCORER_ACTION_FILE
    scorer_result_path = context.run_directory / SCORER_RESULT_FILE
    aggregate_path = context.run_directory / AGGREGATE_FILE
    scorer_action = _expected_scorer_action(context, expected)
    action_started_now = context.state.scorer_action_id is None
    if action_started_now:
        context.services.checkpoint("aggregation:before_pa5c2_scoring")
        _write_once_or_verify_json(
            expected_path,
            expected.model_dump(mode="json"),
            "expected PA-5C2 run manifest",
        )
        _write_once_or_verify_json(
            scorer_action_path,
            scorer_action.model_dump(mode="json"),
            "scorer action-start receipt",
        )
        expected_file_sha = sha256_regular_file(expected_path)
        scorer_action_file_sha = sha256_regular_file(scorer_action_path)
        context.state = _event(
            context,
            "campaign_scorer_action_started",
            scorer_action.action_id,
            {
                "expected_run_manifest_path": str(expected_path),
                "expected_run_manifest_sha256": expected.manifest_sha256,
                "scorer_action_id": scorer_action.action_id,
                "scorer_action_path": str(scorer_action_path),
                "scorer_action_sha256": scorer_action_file_sha,
            },
            {
                str(expected_path): expected_file_sha,
                str(scorer_action_path): scorer_action_file_sha,
            },
        )
        context.services.checkpoint("aggregation:after_scorer_action_start_persisted")
    else:
        durable_action = _load_model(scorer_action_path, CampaignScorerActionStartV1)
        durable_expected = _load_model(expected_path, ExactBenchmarkExpectedRunsV1)
        if durable_action != scorer_action or durable_expected != expected:
            raise PhysicsBenchmarkCampaignStateError("durable scorer action identity changed")

    if context.state.scorer_result_sha256 is None:
        if not action_started_now:
            raise PhysicsBenchmarkCampaignStateError("scorer action has ambiguous completion")
        report = context.services.scorer(
            expected,
            observed,
            catalog_path=Path(context.manifest.scorer_catalog_path),
            repository_root=context.repository_root,
        )
        context.services.checkpoint("aggregation:after_scorer_computation_before_result_persistence")
        receipt = CampaignScorerResultReceiptV1(
            action_id=scorer_action.action_id,
            campaign_id=context.manifest.campaign_id,
            campaign_manifest_sha256=context.manifest.manifest_sha256,
            scorer_action_start_sha256=sha256_regular_file(scorer_action_path),
            expected_run_manifest_sha256=expected.manifest_sha256,
            scorer_authority_sha256=context.state.scorer_authority_sha256,
            result_sha256=report.canonical_sha256(),
            result=report,
        )
        _write_once_or_verify_json(
            scorer_result_path,
            receipt.model_dump(mode="json"),
            "scorer result receipt",
        )
        result_file_sha = sha256_regular_file(scorer_result_path)
        context.state = _event(
            context,
            "campaign_scorer_result_persisted",
            scorer_action.action_id,
            {
                "scorer_result_path": str(scorer_result_path),
                "scorer_result_sha256": result_file_sha,
                "scorer_result_semantic_sha256": receipt.result_sha256,
            },
            {str(scorer_result_path): result_file_sha},
        )
        context.services.checkpoint("aggregation:after_scorer_result_durable_before_aggregate")
    else:
        receipt = _load_model(scorer_result_path, CampaignScorerResultReceiptV1)
        _verify_scorer_result_receipt(context, scorer_action, receipt)
        report = receipt.result

    _verify_durable_artifact_rebinding(context)
    aggregate_seed = hashlib.sha256(
        canonical_json(
            {
                "scorer_action_id": scorer_action.action_id,
                "scorer_result_sha256": context.state.scorer_result_sha256,
                "scorer_result_semantic_sha256": context.state.scorer_result_semantic_sha256,
            }
        )
    ).hexdigest()
    aggregate_action_id = f"aggregate-{aggregate_seed[:32]}"
    if context.state.aggregate_result_sha256 is None:
        aggregate_receipt = CampaignAggregateReceiptV1(
            action_id=aggregate_action_id,
            campaign_id=context.manifest.campaign_id,
            campaign_manifest_sha256=context.manifest.manifest_sha256,
            scorer_action_id=scorer_action.action_id,
            scorer_result_receipt_sha256=cast(str, context.state.scorer_result_sha256),
            scorer_result_semantic_sha256=cast(
                str, context.state.scorer_result_semantic_sha256
            ),
            expected_run_manifest_sha256=expected.manifest_sha256,
            result=report,
        )
        _write_once_or_verify_json(
            aggregate_path,
            aggregate_receipt.model_dump(mode="json"),
            "PA-5C2 aggregate result",
        )
        aggregate_sha = sha256_regular_file(aggregate_path)
        context.state = _event(
            context,
            "campaign_aggregate_persisted",
            aggregate_action_id,
            {
                "aggregate_action_id": aggregate_action_id,
                "aggregate_result_path": str(aggregate_path),
                "aggregate_result_sha256": aggregate_sha,
            },
            {str(aggregate_path): aggregate_sha},
        )
    else:
        aggregate_receipt = _load_model(aggregate_path, CampaignAggregateReceiptV1)
        aggregate_sha = sha256_regular_file(aggregate_path)
        if aggregate_receipt.result != report:
            raise PhysicsBenchmarkCampaignStateError("durable aggregate result changed")
    context.services.checkpoint("aggregation:after_aggregate_durable_write_before_completion")
    if (
        context.state.expected_run_manifest_sha256 != expected.manifest_sha256
        or context.state.scorer_action_id != scorer_action.action_id
        or context.state.aggregate_action_id != aggregate_action_id
        or context.state.aggregate_result_sha256 != aggregate_sha
    ):
        raise PhysicsBenchmarkCampaignStateError("campaign aggregate state changed")
    return _finalize_campaign(context, report)


def _finalize_campaign(
    context: _CampaignContext,
    report: ExactBenchmarkScoreReportV1,
) -> PhysicsBenchmarkCampaignStateV1:
    _verify_durable_artifact_rebinding(context)
    action_tree_path = context.run_directory / ACTION_TREE_FILE
    completion_path = context.run_directory / COMPLETION_RECEIPT_FILE
    action_tree = _build_action_tree(context)
    if context.state.action_tree_sha256 is None:
        context.services.checkpoint("finalization:before_action_tree")
        _write_once_or_verify_json(action_tree_path, action_tree, "campaign action tree")
        action_tree_sha = sha256_regular_file(action_tree_path)
        context.state = _event(
            context,
            "campaign_action_tree_persisted",
            _action_id("action-tree", action_tree_sha),
            {
                "action_tree_path": str(action_tree_path),
                "action_tree_sha256": action_tree_sha,
            },
            {str(action_tree_path): action_tree_sha},
        )
    else:
        _write_once_or_verify_json(action_tree_path, action_tree, "campaign action tree")
        action_tree_sha = sha256_regular_file(action_tree_path)
    context.services.checkpoint("finalization:after_action_tree")
    completion = {
        "schema_version": 1,
        "campaign_id": context.manifest.campaign_id,
        "campaign_manifest_sha256": context.manifest.manifest_sha256,
        "aggregate_action_id": context.state.aggregate_action_id,
        "aggregate_result_sha256": context.state.aggregate_result_sha256,
        "aggregate_semantic_sha256": report.canonical_sha256(),
        "expected_run_manifest_sha256": context.state.expected_run_manifest_sha256,
        "scorer_action_id": context.state.scorer_action_id,
        "scorer_action_sha256": context.state.scorer_action_sha256,
        "scorer_result_sha256": context.state.scorer_result_sha256,
        "scorer_result_semantic_sha256": context.state.scorer_result_semantic_sha256,
        "action_tree_sha256": action_tree_sha,
        "exact_child_bijection": True,
        "all_pa5c2_proofs_verified": True,
    }
    if context.state.completion_receipt_sha256 is None:
        context.services.checkpoint("finalization:before_completion_receipt")
        _write_once_or_verify_json(completion_path, completion, "campaign completion receipt")
        completion_sha = sha256_regular_file(completion_path)
        context.state = _event(
            context,
            "campaign_completion_receipt_persisted",
            _action_id("completion-receipt", completion_sha),
            {
                "completion_receipt_path": str(completion_path),
                "completion_receipt_sha256": completion_sha,
            },
            {str(completion_path): completion_sha},
        )
    else:
        _write_once_or_verify_json(completion_path, completion, "campaign completion receipt")
        completion_sha = sha256_regular_file(completion_path)
    context.services.checkpoint("finalization:after_completion_receipt_before_state")
    _verify_durable_artifact_rebinding(context)
    if context.state.status != "completed":
        context.state = _event(
            context,
            "campaign_completed",
            _action_id("complete", context.state.aggregate_result_sha256 or ZERO_HASH),
            {
                "status": "completed",
                "reason_code": "campaign_completed",
                "blocking_child_run_id": None,
            },
            {},
        )
    return context.state


def _bind_terminal_child(
    child: CampaignChildAuthorityV1,
    entry: RunIndexEntryV1,
    catalog: PhysicsBlindFixtureCatalogV1,
    *,
    repository_root: Path,
) -> CampaignTerminalChildV1:
    run_directory = Path(child.workflow_run_directory)
    result = physics_substage_status(run_directory)
    state = _load_model(run_directory / "state.json", PhysicsWorkflowStateV2)
    if result != state.to_result() or state.status not in _TERMINAL_CHILD_STATUSES:
        raise PhysicsBenchmarkCampaignStateError("child is not an exact terminal PA-4 state")
    expected = (
        state.substage_id == child.substage_id,
        state.run_token == child.run_token,
        state.specification_path == child.specification_path,
        state.specification_sha256 == child.specification_sha256,
        state.workspace == child.workspace,
        state.repository_root == child.repository_root,
        state.baseline_commit == child.baseline_commit,
        state.physics_contract_path == child.physics_contract_path,
        state.physics_contract_sha256 == child.physics_contract_sha256,
        state.oracle_catalog_path == child.oracle_catalog_path,
        state.oracle_catalog_sha256 == child.oracle_catalog_sha256,
        state.auditor_config_path == child.auditor_config_path,
        state.auditor_config_sha256 == child.auditor_config_sha256,
        state.artifact_directory == child.workflow_run_directory,
        entry.state_sha256 == sha256_regular_file(run_directory / "state.json"),
        entry.journal_hash == state.journal_hash,
        entry.journal_sequence == state.journal_sequence,
    )
    if not all(expected):
        raise PhysicsBenchmarkCampaignStateError("terminal child authority is stale or substituted")
    evidence_roots = {Path(item.output_directory).parent for item in state.oracle_evidence}
    if len(evidence_roots) != 1 or state.physics_auditor_action_directory is None:
        raise PhysicsBenchmarkCampaignStateError("terminal child scoring artifacts are incomplete")
    artifacts = ExactBenchmarkRunArtifacts(
        case_id=child.case_id,
        variant_id=child.variant_id,
        repetition_id=child.repetition_id,
        contract_path=Path(child.physics_contract_path),
        execution_config_path=Path(child.auditor_config_path),
        workspace=Path(child.workspace),
        oracle_evidence_root=next(iter(evidence_roots)),
        output_directory=Path(state.physics_auditor_action_directory),
        attempt_number=state.repair_round + 1,
    )
    identity = bind_exact_benchmark_run(catalog, artifacts, repository_root=repository_root)
    if (
        identity.case_id,
        identity.variant_id,
        identity.repetition_id,
    ) != (child.case_id, child.variant_id, child.repetition_id):
        raise PhysicsBenchmarkCampaignStateError("PA-5C2 identity contradicts child coordinates")
    return CampaignTerminalChildV1(
        child_run_id=child.child_run_id,
        child_authority_sha256=child.canonical_sha256(),
        workflow_status=state.status,
        state_sha256=entry.state_sha256,
        journal_sha256=entry.journal_sha256,
        journal_hash=entry.journal_hash,
        scoring_identity=identity,
        scoring_artifacts=CampaignScoringArtifactsV1(
            contract_path=str(artifacts.contract_path),
            execution_config_path=str(artifacts.execution_config_path),
            workspace=str(artifacts.workspace),
            oracle_evidence_root=str(artifacts.oracle_evidence_root),
            output_directory=str(artifacts.output_directory),
            attempt_number=artifacts.attempt_number,
        ),
    )


def _to_observed(child: CampaignTerminalChildV1) -> ExactBenchmarkObservedRun:
    artifacts = child.scoring_artifacts
    return ExactBenchmarkObservedRun(
        identity=child.scoring_identity,
        artifacts=ExactBenchmarkRunArtifacts(
            case_id=child.scoring_identity.case_id,
            variant_id=child.scoring_identity.variant_id,
            repetition_id=child.scoring_identity.repetition_id,
            contract_path=Path(artifacts.contract_path),
            execution_config_path=Path(artifacts.execution_config_path),
            workspace=Path(artifacts.workspace),
            oracle_evidence_root=Path(artifacts.oracle_evidence_root),
            output_directory=Path(artifacts.output_directory),
            attempt_number=artifacts.attempt_number,
        ),
    )


def _rebind_all_terminal_children(
    context: _CampaignContext,
) -> tuple[CampaignTerminalChildV1, ...]:
    index, issue = _discover_exact_children(context, require_complete=True)
    if issue is not None:
        raise PhysicsBenchmarkCampaignStateError(issue)
    stored = {item.child_run_id: item for item in context.state.terminal_children}
    expected_ids = {item.child_run_id for item in context.manifest.children}
    if set(stored) != expected_ids:
        raise PhysicsBenchmarkCampaignStateError("terminal child identity set is incomplete")
    rebound: list[CampaignTerminalChildV1] = []
    for child in context.manifest.children:
        entry = index.get(child.workflow_run_directory)
        if entry is None or entry.completion != "terminal":
            raise PhysicsBenchmarkCampaignStateError("terminal child disappeared")
        plan = context.services.recovery_planner(Path(entry.run_directory))
        if plan.disposition != "already_terminal":
            raise PhysicsBenchmarkCampaignStateError("child terminal proof is not current")
        current = context.services.terminal_binder(
            child,
            entry,
            context.catalog,
            repository_root=context.repository_root,
        )
        if current != stored[child.child_run_id]:
            raise PhysicsBenchmarkCampaignStateError("stored terminal child was substituted")
        rebound.append(current)
    keys = {
        (
            item.scoring_identity.case_id,
            item.scoring_identity.variant_id,
            item.scoring_identity.repetition_id,
        )
        for item in rebound
    }
    expected_keys = {
        (item.case_id, item.variant_id, item.repetition_id) for item in context.manifest.children
    }
    if keys != expected_keys or len(keys) != len(rebound):
        raise PhysicsBenchmarkCampaignStateError("exact repetition coverage failed")
    return tuple(rebound)


def _discover_exact_children(
    context: _CampaignContext,
    *,
    require_complete: bool = False,
) -> tuple[dict[str, RunIndexEntryV1], str | None]:
    try:
        index = context.services.run_discoverer(
            context.child_runs_directory,
            persist_cache=False,
        )
    except (WorkflowInputError, WorkflowStateError):
        return {}, "child_discovery_failed"
    if index.issues:
        return {}, "child_discovery_integrity_failed"
    expected = {item.workflow_run_directory for item in context.manifest.children}
    entries = {item.run_directory: item for item in index.entries}
    if len(entries) != len(index.entries):
        return {}, "duplicate_child_run"
    actual_directories: set[str] = set()
    try:
        for path in context.child_runs_directory.iterdir():
            if path.name == RECEIPT_ROOT or not path.is_dir():
                continue
            if path.is_symlink():
                return {}, "unsafe_child_run_path"
            actual_directories.add(str(path.resolve(strict=True)))
    except (OSError, RuntimeError):
        return {}, "child_discovery_failed"
    if not actual_directories.issubset(expected):
        return {}, "extra_child_run"
    if not set(entries).issubset(expected):
        return {}, "extra_child_run"
    run_tokens = tuple(item.run_token for item in index.entries)
    if len(run_tokens) != len(set(run_tokens)):
        return {}, "duplicate_child_run"
    if require_complete:
        if set(entries) != expected:
            return {}, "missing_child_run"
        if any(item.completion != "terminal" for item in entries.values()):
            return {}, "nonterminal_child_run"
    return entries, None


def _entry_authority_issue(child: CampaignChildAuthorityV1, entry: RunIndexEntryV1) -> str | None:
    if entry.workflow_schema_version != 2:
        return "child_not_pa4_workflow"
    if entry.run_directory != child.workflow_run_directory:
        return "substituted_child_run"
    if entry.substage_id != child.substage_id or entry.run_token != child.run_token:
        return "substituted_child_identity"
    return None


def _plan_authority_issue(
    child: CampaignChildAuthorityV1,
    entry: RunIndexEntryV1,
    plan: RecoveryPlanV1,
) -> str | None:
    values = (
        plan.run_directory == child.workflow_run_directory,
        plan.workflow_schema_version == 2,
        plan.substage_id == child.substage_id,
        plan.run_token == child.run_token,
        plan.journal_hash == entry.journal_hash,
        plan.journal_sequence == entry.journal_sequence,
        plan.state_sha256 == entry.state_sha256,
        plan.journal_sha256 == entry.journal_sha256,
    )
    return None if all(values) else "child_recovery_plan_substituted"


def _manifest_state_authority(
    manifest: PhysicsBenchmarkCampaignManifestV1,
) -> _ManifestStateAuthority:
    child_set_sha256 = hashlib.sha256(
        canonical_json([item.model_dump(mode="json") for item in manifest.children])
    ).hexdigest()
    scorer_authority_sha256 = hashlib.sha256(
        canonical_json(
            {
                "delegated_entrypoint": "pa5c2_score_exact_physics_benchmark",
                "scorer_catalog_path": manifest.scorer_catalog_path,
                "catalog_id": manifest.catalog_id,
                "catalog_sha256": manifest.catalog_sha256,
            }
        )
    ).hexdigest()
    return {
        "campaign_id": manifest.campaign_id,
        "manifest_sha256": manifest.manifest_sha256,
        "repository_root": manifest.repository_root,
        "scorer_catalog_path": manifest.scorer_catalog_path,
        "catalog_id": manifest.catalog_id,
        "catalog_sha256": manifest.catalog_sha256,
        "expected_child_set_sha256": child_set_sha256,
        "scorer_authority_sha256": scorer_authority_sha256,
    }


def _verify_manifest_state_binding(
    run_directory: Path,
    manifest: PhysicsBenchmarkCampaignManifestV1,
    state: PhysicsBenchmarkCampaignStateV1,
) -> None:
    authority = _manifest_state_authority(manifest)
    if any(getattr(state, key) != value for key, value in authority.items()):
        raise PhysicsBenchmarkCampaignStateError(
            "durable campaign state contradicts the frozen manifest"
        )
    entries = _read_campaign_journal(run_directory)
    if not entries:
        raise PhysicsBenchmarkCampaignStateError("campaign manifest lacks its origin transition")
    origin = entries[0]
    manifest_path = run_directory / MANIFEST_FILE
    manifest_file_sha256 = sha256_regular_file(manifest_path)
    if (
        origin.event_type != "campaign_initialized"
        or origin.action_id != _action_id("initialize", manifest.manifest_sha256)
        or origin.state_updates.get("campaign_id") != manifest.campaign_id
        or origin.state_updates.get("manifest_sha256") != manifest.manifest_sha256
        or origin.artifact_hashes != {str(manifest_path): manifest_file_sha256}
    ):
        raise PhysicsBenchmarkCampaignStateError(
            "campaign manifest is not bound to its original journal transition"
        )
    for key, expected in authority.items():
        if origin.state_updates.get(key) != expected:
            raise PhysicsBenchmarkCampaignStateError(
                "campaign origin authority contradicts the frozen manifest"
            )
        if any(
            key in entry.state_updates and entry.state_updates[key] != expected
            for entry in entries[1:]
        ):
            raise PhysicsBenchmarkCampaignStateError(
                "campaign journal attempted to replace frozen manifest authority"
            )


def _expected_scorer_action(
    context: _CampaignContext,
    expected: ExactBenchmarkExpectedRunsV1,
) -> CampaignScorerActionStartV1:
    payload: dict[str, object] = {
        "schema_version": 1,
        "campaign_id": context.manifest.campaign_id,
        "campaign_manifest_sha256": context.manifest.manifest_sha256,
        "repository_root": context.manifest.repository_root,
        "expected_child_set_sha256": context.state.expected_child_set_sha256,
        "expected_child_authority_sha256": sorted(
            item.canonical_sha256() for item in context.manifest.children
        ),
        "expected_pa5c2_input_identity_sha256": sorted(
            item.canonical_sha256() for item in expected.run_identities
        ),
        "expected_run_manifest_sha256": expected.manifest_sha256,
        "catalog_id": context.manifest.catalog_id,
        "catalog_sha256": context.manifest.catalog_sha256,
        "scorer_authority_sha256": context.state.scorer_authority_sha256,
    }
    digest = hashlib.sha256(canonical_json(payload)).hexdigest()
    return CampaignScorerActionStartV1.model_validate(
        {**payload, "action_id": f"score-{digest[:48]}"}
    )


def _verify_scorer_result_receipt(
    context: _CampaignContext,
    action: CampaignScorerActionStartV1,
    receipt: CampaignScorerResultReceiptV1,
) -> None:
    expected = (
        receipt.action_id == action.action_id,
        receipt.campaign_id == context.manifest.campaign_id,
        receipt.campaign_manifest_sha256 == context.manifest.manifest_sha256,
        receipt.scorer_action_start_sha256 == context.state.scorer_action_sha256,
        receipt.expected_run_manifest_sha256 == action.expected_run_manifest_sha256,
        receipt.scorer_authority_sha256 == context.state.scorer_authority_sha256,
        receipt.result_sha256 == context.state.scorer_result_semantic_sha256,
        receipt.result.catalog_sha256 == context.manifest.catalog_sha256,
    )
    if not all(expected):
        raise PhysicsBenchmarkCampaignStateError("scorer result receipt was substituted")


def _read_campaign_journal(
    run_directory: Path,
) -> tuple[PhysicsBenchmarkCampaignJournalEntryV1, ...]:
    raw_entries = read_hashed_journal(
        run_directory / JOURNAL_FILE,
        error_factory=PhysicsBenchmarkCampaignStateError,
        malformed_message="campaign journal is malformed",
    )
    entries: list[PhysicsBenchmarkCampaignJournalEntryV1] = []
    for value in raw_entries:
        try:
            entries.append(PhysicsBenchmarkCampaignJournalEntryV1.model_validate(value))
        except ValidationError as exc:
            raise PhysicsBenchmarkCampaignStateError("campaign journal entry is invalid") from exc
    return tuple(entries)


def _verify_durable_artifact_rebinding(context: _CampaignContext) -> None:
    """Reject every unexplained or stale scorer/finalization artifact."""
    entries = _read_campaign_journal(context.run_directory)
    manifest_path = context.run_directory / MANIFEST_FILE
    expected_path = context.run_directory / EXPECTED_RUNS_FILE
    scorer_action_path = context.run_directory / SCORER_ACTION_FILE
    scorer_result_path = context.run_directory / SCORER_RESULT_FILE
    aggregate_path = context.run_directory / AGGREGATE_FILE
    action_tree_path = context.run_directory / ACTION_TREE_FILE
    completion_path = context.run_directory / COMPLETION_RECEIPT_FILE
    paths = {
        "campaign_initialized": (manifest_path,),
        "campaign_scorer_action_started": (expected_path, scorer_action_path),
        "campaign_scorer_result_persisted": (scorer_result_path,),
        "campaign_aggregate_persisted": (aggregate_path,),
        "campaign_action_tree_persisted": (action_tree_path,),
        "campaign_completion_receipt_persisted": (completion_path,),
        "campaign_completed": (),
    }
    lifecycle_entries: dict[str, PhysicsBenchmarkCampaignJournalEntryV1] = {}
    for entry in entries:
        expected_paths = paths.get(entry.event_type)
        if expected_paths is None:
            continue
        if entry.event_type in lifecycle_entries:
            raise PhysicsBenchmarkCampaignStateError("campaign lifecycle transition was duplicated")
        lifecycle_entries[entry.event_type] = entry
        if set(entry.artifact_hashes) != {str(path) for path in expected_paths}:
            raise PhysicsBenchmarkCampaignStateError(
                "campaign lifecycle transition has unexplained artifact bindings"
            )

    state_presence = {
        expected_path: context.state.scorer_action_id is not None,
        scorer_action_path: context.state.scorer_action_id is not None,
        scorer_result_path: context.state.scorer_result_sha256 is not None,
        aggregate_path: context.state.aggregate_result_sha256 is not None,
        action_tree_path: context.state.action_tree_sha256 is not None,
        completion_path: context.state.completion_receipt_sha256 is not None,
    }
    event_for_path = {
        manifest_path: "campaign_initialized",
        expected_path: "campaign_scorer_action_started",
        scorer_action_path: "campaign_scorer_action_started",
        scorer_result_path: "campaign_scorer_result_persisted",
        aggregate_path: "campaign_aggregate_persisted",
        action_tree_path: "campaign_action_tree_persisted",
        completion_path: "campaign_completion_receipt_persisted",
    }
    for path, event_type in event_for_path.items():
        exists = path.exists()
        expected_exists = path == manifest_path or state_presence[path]
        lifecycle_entry = lifecycle_entries.get(event_type)
        if exists != expected_exists or (exists and lifecycle_entry is None):
            raise PhysicsBenchmarkCampaignStateError(
                f"campaign artifact is orphaned or missing: {path.name}"
            )
        if not exists:
            if lifecycle_entry is not None:
                raise PhysicsBenchmarkCampaignStateError(
                    f"campaign journal claims a missing artifact: {path.name}"
                )
            continue
        assert lifecycle_entry is not None
        current_sha256 = sha256_regular_file(path)
        if lifecycle_entry.artifact_hashes.get(str(path)) != current_sha256:
            raise PhysicsBenchmarkCampaignStateError(
                f"campaign artifact hash contradicts its journal transition: {path.name}"
            )

    scoring_directory = scorer_action_path.parent
    if scoring_directory.exists():
        try:
            metadata = scoring_directory.lstat()
            actual = set(scoring_directory.iterdir())
        except OSError as exc:
            raise PhysicsBenchmarkCampaignStateError(
                "scorer action directory is unavailable"
            ) from exc
        if scoring_directory.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
            raise PhysicsBenchmarkCampaignStateError("scorer action directory was substituted")
        allowed = {path for path in (scorer_action_path, scorer_result_path) if path.exists()}
        if actual != allowed:
            raise PhysicsBenchmarkCampaignStateError("unjournaled scorer action artifact exists")

    if context.state.scorer_action_id is not None:
        expected_manifest = _load_model(expected_path, ExactBenchmarkExpectedRunsV1)
        action = _load_model(scorer_action_path, CampaignScorerActionStartV1)
        if (
            context.state.expected_run_manifest_path != str(expected_path)
            or context.state.expected_run_manifest_sha256 != expected_manifest.manifest_sha256
            or context.state.scorer_action_path != str(scorer_action_path)
            or context.state.scorer_action_sha256 != sha256_regular_file(scorer_action_path)
            or context.state.scorer_action_id != action.action_id
            or action != _expected_scorer_action(context, expected_manifest)
        ):
            raise PhysicsBenchmarkCampaignStateError("scorer action did not rebind exactly")
        action_entry = lifecycle_entries["campaign_scorer_action_started"]
        if action_entry.action_id != action.action_id:
            raise PhysicsBenchmarkCampaignStateError("scorer action journal identity changed")
        if context.state.scorer_result_sha256 is not None:
            result = _load_model(scorer_result_path, CampaignScorerResultReceiptV1)
            _verify_scorer_result_receipt(context, action, result)
            if (
                context.state.scorer_result_path != str(scorer_result_path)
                or context.state.scorer_result_sha256 != sha256_regular_file(scorer_result_path)
                or lifecycle_entries["campaign_scorer_result_persisted"].action_id
                != action.action_id
            ):
                raise PhysicsBenchmarkCampaignStateError("scorer result did not rebind exactly")
            if context.state.aggregate_result_sha256 is not None:
                aggregate = _load_model(aggregate_path, CampaignAggregateReceiptV1)
                if (
                    context.state.aggregate_result_path != str(aggregate_path)
                    or context.state.aggregate_result_sha256
                    != sha256_regular_file(aggregate_path)
                    or aggregate.action_id != context.state.aggregate_action_id
                    or aggregate.campaign_id != context.manifest.campaign_id
                    or aggregate.campaign_manifest_sha256
                    != context.manifest.manifest_sha256
                    or aggregate.scorer_action_id != action.action_id
                    or aggregate.scorer_result_receipt_sha256
                    != context.state.scorer_result_sha256
                    or aggregate.scorer_result_semantic_sha256 != result.result_sha256
                    or aggregate.expected_run_manifest_sha256
                    != expected_manifest.manifest_sha256
                    or aggregate.result != result.result
                    or lifecycle_entries["campaign_aggregate_persisted"].action_id
                    != context.state.aggregate_action_id
                ):
                    raise PhysicsBenchmarkCampaignStateError("aggregate did not rebind exactly")

    if context.state.action_tree_sha256 is not None:
        tree = _load_json_object(action_tree_path)
        if (
            context.state.action_tree_path != str(action_tree_path)
            or context.state.action_tree_sha256 != sha256_regular_file(action_tree_path)
            or tree.get("campaign_id") != context.manifest.campaign_id
            or tree.get("campaign_manifest_sha256") != context.manifest.manifest_sha256
            or tree.get("scorer_action_id") != context.state.scorer_action_id
            or tree.get("scorer_result_sha256") != context.state.scorer_result_sha256
        ):
            raise PhysicsBenchmarkCampaignStateError("campaign action tree did not rebind exactly")
    if context.state.completion_receipt_sha256 is not None:
        completion = _load_json_object(completion_path)
        if (
            context.state.completion_receipt_path != str(completion_path)
            or context.state.completion_receipt_sha256 != sha256_regular_file(completion_path)
            or completion.get("campaign_id") != context.manifest.campaign_id
            or completion.get("campaign_manifest_sha256") != context.manifest.manifest_sha256
            or completion.get("scorer_action_id") != context.state.scorer_action_id
            or completion.get("scorer_result_sha256") != context.state.scorer_result_sha256
            or completion.get("aggregate_action_id") != context.state.aggregate_action_id
            or completion.get("aggregate_result_sha256")
            != context.state.aggregate_result_sha256
            or completion.get("action_tree_sha256") != context.state.action_tree_sha256
        ):
            raise PhysicsBenchmarkCampaignStateError(
                "campaign completion receipt did not rebind exactly"
            )


def _verify_frozen_manifest_authority(context: _CampaignContext) -> None:
    if context.catalog.canonical_sha256() != context.manifest.catalog_sha256:
        raise PhysicsBenchmarkCampaignInputError("campaign scorer catalog changed")
    for child in context.manifest.children:
        _verify_child_authority(child)


def _verify_child_authority(child: CampaignChildAuthorityV1) -> None:
    prepared = load_physics_substage_specification(
        Path(child.specification_path), require_clean=False
    )
    current = (
        prepared.specification.substage_id == child.substage_id,
        prepared.specification_sha256 == child.specification_sha256,
        str(prepared.workspace) == child.workspace,
        str(prepared.repository_root) == child.repository_root,
        prepared.software_prepared.baseline_commit == child.baseline_commit,
        str(prepared.physics_contract_path) == child.physics_contract_path,
        prepared.physics_contract_sha256 == child.physics_contract_sha256,
        str(prepared.oracle_catalog_path) == child.oracle_catalog_path,
        prepared.oracle_catalog_sha256 == child.oracle_catalog_sha256,
        str(prepared.auditor_config_path) == child.auditor_config_path,
        prepared.auditor_config_sha256 == child.auditor_config_sha256,
    )
    if not all(current):
        raise PhysicsBenchmarkCampaignStateError("child frozen authority changed")


def _route(
    context: _CampaignContext,
    status: PhysicsBenchmarkCampaignStatusV1,
    reason_code: str,
    child_run_id: str | None,
) -> PhysicsBenchmarkCampaignStateV1:
    if (
        context.state.status == status
        and context.state.reason_code == reason_code
        and context.state.blocking_child_run_id == child_run_id
    ):
        return context.state
    seed = hashlib.sha256(
        canonical_json(
            {
                "status": status,
                "reason_code": reason_code,
                "child_run_id": child_run_id,
                "journal_hash": context.state.journal_hash,
            }
        )
    ).hexdigest()
    context.state = _event(
        context,
        "campaign_status_routed",
        _action_id("route", seed),
        {
            "status": status,
            "reason_code": reason_code,
            "blocking_child_run_id": child_run_id,
        },
        {},
    )
    return context.state


def _build_action_tree(context: _CampaignContext) -> dict[str, object]:
    roots = (
        context.run_directory / ACTIONS_DIRECTORY,
        context.run_directory / TERMINAL_DIRECTORY,
        context.child_runs_directory / RECEIPT_ROOT,
    )
    records: list[dict[str, str]] = []
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            records.append(
                {
                    "path": str(path.relative_to(context.run_directory)),
                    "sha256": sha256_regular_file(path),
                }
            )
    return {
        "schema_version": 1,
        "campaign_id": context.manifest.campaign_id,
        "campaign_manifest_sha256": context.manifest.manifest_sha256,
        "expected_child_run_ids": [item.child_run_id for item in context.manifest.children],
        "terminal_child_identity_sha256": [
            item.canonical_sha256() for item in context.state.terminal_children
        ],
        "scorer_action_id": context.state.scorer_action_id,
        "scorer_action_sha256": context.state.scorer_action_sha256,
        "scorer_result_sha256": context.state.scorer_result_sha256,
        "scorer_result_semantic_sha256": context.state.scorer_result_semantic_sha256,
        "aggregate_action_id": context.state.aggregate_action_id,
        "aggregate_result_sha256": context.state.aggregate_result_sha256,
        "records": records,
    }


def _validate_completed(context: _CampaignContext) -> None:
    _verify_manifest_state_binding(context.run_directory, context.manifest, context.state)
    _verify_durable_artifact_rebinding(context)
    _verify_frozen_manifest_authority(context)
    rebound = _rebind_all_terminal_children(context)
    if len(rebound) != len(context.manifest.children):
        raise PhysicsBenchmarkCampaignStateError("completed child set is incomplete")
    paths = (
        context.state.expected_run_manifest_path,
        context.state.scorer_action_path,
        context.state.scorer_result_path,
        context.state.aggregate_result_path,
        context.state.action_tree_path,
        context.state.completion_receipt_path,
    )
    if any(path is None for path in paths):
        raise PhysicsBenchmarkCampaignStateError("completed campaign files are unavailable")
    expected = _load_model(Path(cast(str, paths[0])), ExactBenchmarkExpectedRunsV1)
    action = _load_model(Path(cast(str, paths[1])), CampaignScorerActionStartV1)
    result = _load_model(Path(cast(str, paths[2])), CampaignScorerResultReceiptV1)
    aggregate = _load_model(Path(cast(str, paths[3])), CampaignAggregateReceiptV1)
    report = aggregate.result
    _verify_scorer_result_receipt(context, action, result)
    if (
        expected.manifest_sha256 != context.state.expected_run_manifest_sha256
        or action.action_id != context.state.scorer_action_id
        or result.result != report
        or aggregate.campaign_manifest_sha256 != context.manifest.manifest_sha256
        or report.expected_run_manifest_sha256 != expected.manifest_sha256
        or sha256_regular_file(Path(cast(str, paths[3])))
        != context.state.aggregate_result_sha256
        or sha256_regular_file(Path(cast(str, paths[4]))) != context.state.action_tree_sha256
        or sha256_regular_file(Path(cast(str, paths[5])))
        != context.state.completion_receipt_sha256
    ):
        raise PhysicsBenchmarkCampaignStateError("completed campaign receipts changed")


def _event(
    context: _CampaignContext,
    event_type: str,
    action_id: str,
    updates: Mapping[str, object],
    artifact_hashes: Mapping[str, str],
) -> PhysicsBenchmarkCampaignStateV1:
    timestamp = _utc_string(context.services.utc_now())
    body: dict[str, object] = {
        "schema_version": 1,
        "sequence": context.state.journal_sequence + 1,
        "event_type": event_type,
        "action_id": action_id,
        "timestamp": timestamp,
        "state_updates": _json_value(dict(updates)),
        "artifact_hashes": dict(sorted(artifact_hashes.items())),
        "previous_hash": context.state.journal_hash,
    }
    entry, entry_hash = append_hashed_journal_entry(
        context.run_directory / JOURNAL_FILE,
        body,
        validate=_validate_journal_entry,
        error_factory=PhysicsBenchmarkCampaignStateError,
        error_message="campaign journal could not be appended",
    )
    values = context.state.model_dump(mode="json")
    values.update(cast(dict[str, object], entry["state_updates"]))
    values.update(
        {
            "updated_at": timestamp,
            "journal_sequence": entry["sequence"],
            "journal_hash": entry_hash,
        }
    )
    try:
        state = PhysicsBenchmarkCampaignStateV1.model_validate(values)
    except ValidationError as exc:
        raise PhysicsBenchmarkCampaignStateError("campaign transition is invalid") from exc
    context.services.checkpoint(f"state:before_replacement:{event_type}")
    _persist_state(context.run_directory, state)
    context.services.checkpoint(f"state:after_replacement:{event_type}")
    return state


def _load_reconciled_state(
    run_directory: Path, *, persist: bool
) -> PhysicsBenchmarkCampaignStateV1:
    state = _load_model(run_directory / STATE_FILE, PhysicsBenchmarkCampaignStateV1)
    typed_entries = _read_campaign_journal(run_directory)
    entries = [item.model_dump(mode="json") for item in typed_entries]
    reconciled = reconcile_model_snapshot(
        state,
        entries,
        model=PhysicsBenchmarkCampaignStateV1,
        error_factory=PhysicsBenchmarkCampaignStateError,
        error_message="campaign state cannot be reconciled",
    )
    if persist and reconciled != state:
        _persist_state(run_directory, reconciled)
    return reconciled


def _load_manifest(run_directory: Path) -> PhysicsBenchmarkCampaignManifestV1:
    manifest_path = run_directory / MANIFEST_FILE
    try:
        sha256_regular_file(manifest_path)
    except WorkflowStateError as exc:
        raise PhysicsBenchmarkCampaignStateError("campaign manifest was substituted") from exc
    manifest = _load_model(manifest_path, PhysicsBenchmarkCampaignManifestV1)
    if manifest.child_runs_directory != str(run_directory / CHILD_RUNS_DIRECTORY):
        raise PhysicsBenchmarkCampaignStateError("campaign child root was substituted")
    return manifest


def _load_current_catalog(
    manifest: PhysicsBenchmarkCampaignManifestV1,
) -> PhysicsBlindFixtureCatalogV1:
    try:
        catalog = load_blind_fixture_catalog(Path(manifest.scorer_catalog_path))
    except Exception as exc:
        raise PhysicsBenchmarkCampaignStateError("campaign scorer catalog is unavailable") from exc
    if (
        catalog.catalog_id != manifest.catalog_id
        or catalog.canonical_sha256() != manifest.catalog_sha256
    ):
        raise PhysicsBenchmarkCampaignStateError("campaign scorer catalog authority changed")
    return catalog


def _recovery_action_records(root: Path, plan_sha256: str) -> list[tuple[Path, dict[str, object]]]:
    if not root.exists():
        return []
    records: list[tuple[Path, dict[str, object]]] = []
    for path in sorted(root.glob("*.json")):
        if path.name.endswith(".outcome.json"):
            continue
        value = _load_json_object(path)
        if value.get("plan_sha256") == plan_sha256:
            records.append((path, value))
    return records


def _pa5a_receipts(child_runs_directory: Path, attempt_token: str) -> tuple[list[Path], list[Path]]:
    attempt_id = f"recovery-{attempt_token.replace('_', '-')}"
    root = child_runs_directory / RECEIPT_ROOT
    if not root.exists():
        return [], []
    plans = sorted(root.glob(f"*/{attempt_id}.plan.json"))
    outcomes = sorted(root.glob(f"*/{attempt_id}.outcome.json"))
    return plans, outcomes


def _terminal_artifact_hashes(children: tuple[CampaignTerminalChildV1, ...]) -> dict[str, str]:
    return {
        child.child_run_id: child.canonical_sha256()
        for child in sorted(children, key=lambda item: item.child_run_id)
    }


def _write_once_or_verify_json(path: Path, value: object, label: str) -> None:
    content = render_json_bytes(value)
    if path.exists():
        try:
            metadata = path.lstat()
            current = path.read_bytes()
        except OSError as exc:
            raise PhysicsBenchmarkCampaignStateError(f"{label} is unavailable") from exc
        if path.is_symlink() or not stat.S_ISREG(metadata.st_mode) or current != content:
            raise PhysicsBenchmarkCampaignStateError(f"{label} changed")
        return
    atomic_write_bytes(
        path,
        content,
        error_factory=PhysicsBenchmarkCampaignStateError,
        error_message=f"{label} could not be written",
    )


def _persist_state(run_directory: Path, state: PhysicsBenchmarkCampaignStateV1) -> None:
    atomic_write_json(
        run_directory / STATE_FILE,
        state.model_dump(mode="json"),
        error_factory=PhysicsBenchmarkCampaignStateError,
        error_message="campaign state could not be persisted",
    )


def _load_model(path: Path, model: type[ModelT]) -> ModelT:
    try:
        sha256_regular_file(path)
        value = json.loads(path.read_text(encoding="utf-8"), parse_constant=_reject_constant)
        return model.model_validate(value)
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValidationError,
        ValueError,
        WorkflowStateError,
    ) as exc:
        raise PhysicsBenchmarkCampaignStateError(f"invalid campaign artifact: {path.name}") from exc


def _validate_journal_entry(value: Mapping[str, object]) -> None:
    PhysicsBenchmarkCampaignJournalEntryV1.model_validate(value)


def _load_json_object(path: Path) -> dict[str, object]:
    try:
        sha256_regular_file(path)
        value = json.loads(path.read_text(encoding="utf-8"), parse_constant=_reject_constant)
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
        WorkflowStateError,
    ) as exc:
        raise PhysicsBenchmarkCampaignStateError("campaign action record is invalid") from exc
    if not isinstance(value, dict):
        raise PhysicsBenchmarkCampaignStateError("campaign action record is invalid")
    return cast(dict[str, object], value)


def _create_campaign_directory(path: Path) -> Path:
    try:
        resolved = path.resolve(strict=False)
        resolved.mkdir(parents=True, exist_ok=False)
        return resolved
    except FileExistsError as exc:
        raise PhysicsBenchmarkCampaignInputError("campaign directory already exists") from exc
    except (OSError, RuntimeError, ValueError) as exc:
        raise PhysicsBenchmarkCampaignInputError("campaign directory could not be created") from exc


def _resolve_campaign_directory(path: Path) -> Path:
    try:
        resolved = path.resolve(strict=True)
        metadata = resolved.lstat()
    except (OSError, RuntimeError, ValueError) as exc:
        raise PhysicsBenchmarkCampaignInputError("campaign directory is unavailable") from exc
    if resolved.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
        raise PhysicsBenchmarkCampaignInputError("campaign path is not a canonical directory")
    return resolved


def _canonical_directory(path: Path, label: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise PhysicsBenchmarkCampaignInputError(f"{label} is unavailable") from exc
    if resolved.is_symlink() or not resolved.is_dir():
        raise PhysicsBenchmarkCampaignInputError(f"{label} is not a canonical directory")
    return resolved


def _canonical_file(path: Path, label: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
        metadata = resolved.lstat()
    except (OSError, RuntimeError, ValueError) as exc:
        raise PhysicsBenchmarkCampaignInputError(f"{label} is unavailable") from exc
    if resolved.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        raise PhysicsBenchmarkCampaignInputError(f"{label} is not a regular file")
    return resolved


def _make_directory(path: Path) -> None:
    try:
        path.mkdir()
    except OSError as exc:
        raise PhysicsBenchmarkCampaignInputError("campaign child directory unavailable") from exc


@contextmanager
def _campaign_lock(run_directory: Path, utc_now: Callable[[], datetime]) -> Iterator[None]:
    path = run_directory / LOCK_FILE
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError("campaign lock is not regular")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise PhysicsBenchmarkCampaignLockError("campaign is already running") from exc
        content = render_json_bytes(
            {
                "schema_version": 1,
                "pid": os.getpid(),
                "started_at": _utc_string(utc_now()),
            }
        )
        os.ftruncate(descriptor, 0)
        os.write(descriptor, content)
        os.fsync(descriptor)
        yield
    except PhysicsBenchmarkCampaignLockError:
        raise
    except OSError as exc:
        raise PhysicsBenchmarkCampaignLockError("campaign lock is unavailable") from exc
    finally:
        if "descriptor" in locals():
            os.close(descriptor)


def _action_id(prefix: str, digest: str) -> str:
    return f"{prefix}-{digest[:48]}"


def _utc_string(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _json_value(value: object) -> object:
    if hasattr(value, "model_dump"):
        return cast(Any, value).model_dump(mode="json")
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")
