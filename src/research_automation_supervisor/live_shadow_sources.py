"""Frozen Stage 4 inputs and point-in-time Stage 2 envelope construction."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import yaml  # type: ignore[import-untyped]
from pydantic import ValidationError

from research_automation_supervisor.codex_adapter import build_subprocess_environment
from research_automation_supervisor.contract import (
    _format_validation_error,
    _UniqueKeySafeLoader,
)
from research_automation_supervisor.errors import (
    LiveShadowInputError,
    LiveShadowIntegrityError,
    ShadowInputError,
    WorkflowInputError,
    WorkflowStateError,
)
from research_automation_supervisor.git_evidence import GitEvidence
from research_automation_supervisor.live_shadow_models import (
    AuthoritativeRunRecord,
    FrozenEvidenceArtifact,
    LiveAcceptanceTest,
    LiveDecisionEnvelope,
    LiveShadowSpecification,
    PriorAuthoritativeActionSummary,
)
from research_automation_supervisor.shadow_confidentiality import (
    preflight_shadow_confidentiality,
    preflight_shadow_locator,
)
from research_automation_supervisor.shadow_models import FrozenFileHash, ProposalKind
from research_automation_supervisor.shadow_sources import (
    FrozenShadowFile,
    _absolute_locator,
    _join_locator,
    _load_shadow_file,
    _read_json,
    _read_utf8,
    _resolve_exact_file,
)
from research_automation_supervisor.test_runner import TestSuiteResult
from research_automation_supervisor.workflow_integrity import (
    JournalEntry,
    PromptHandoff,
    sha256_regular_file,
)
from research_automation_supervisor.workflow_models import (
    AuditorModelResult,
    PendingAction,
    PreparedSubstage,
    WorkerModelResult,
    load_substage_specification,
)

MAX_FROZEN_EVIDENCE_ITEM_BYTES = 256 * 1024
MAX_FROZEN_EVIDENCE_TOTAL_BYTES = 1024 * 1024


@dataclass(frozen=True)
class PreparedLiveShadowSpecification:
    """Resolved immutable Stage 4 specification and validated Stage 2 input."""

    specification_locator_path: Path
    specification_path: Path
    specification_bytes: bytes
    specification_sha256: str
    specification: LiveShadowSpecification
    policy: FrozenShadowFile
    contexts: tuple[FrozenShadowFile, ...]
    stage2: PreparedSubstage
    sensitive_values: tuple[str, ...]

    def normalized_dict(self) -> dict[str, object]:
        value = self.specification.model_dump(mode="json")
        value.update(
            {
                "specification_path": str(self.specification_path),
                "stage2_specification_path": str(self.stage2.specification_path),
                "supervisor_policy_path": str(self.policy.path),
                "project_context_paths": [
                    str(context.path) for context in self.contexts
                ],
            }
        )
        return value

    def context_manifests(self) -> tuple[FrozenFileHash, ...]:
        return tuple(context.manifest() for context in self.contexts)

    def blind_source_summary(self) -> dict[str, object]:
        """Return source policy without live-repository or prompt locators."""
        specification = self.stage2.specification
        return {
            "schema_version": 1,
            "substage_id": specification.substage_id,
            "title": specification.title,
            "baseline_commit": self.stage2.baseline_commit,
            "baseline_branch": self.stage2.baseline_branch,
            "worker_model": specification.worker_model,
            "worker_reasoning_effort": specification.worker_reasoning_effort,
            "worker_timeout_seconds": specification.worker_timeout_seconds,
            "auditor_model": specification.auditor_model,
            "auditor_reasoning_effort": specification.auditor_reasoning_effort,
            "auditor_timeout_seconds": specification.auditor_timeout_seconds,
            "allowed_paths": list(specification.allowed_paths),
            "protected_paths": list(specification.protected_paths),
            "acceptance_tests": [
                {
                    "id": test.specification.id,
                    "argv": list(test.specification.argv),
                    "timeout_seconds": test.specification.timeout_seconds,
                }
                for test in self.stage2.acceptance_tests
            ],
            "max_repair_rounds": specification.max_repair_rounds,
            "checkpoint_after": specification.checkpoint_after,
        }


def load_live_shadow_specification(
    path: Path,
    *,
    environ: Mapping[str, str] | None = None,
    require_clean: bool = True,
) -> PreparedLiveShadowSpecification:
    """Validate every Stage 4 input without writes or process launches."""
    _, _, sensitive_values = build_subprocess_environment(environ)
    try:
        raw_path = preflight_shadow_locator(
            path,
            sensitive_values,
            label="live-shadow specification locator",
        )
        lexical_path = Path(raw_path)
        locator = _absolute_locator(lexical_path)
        resolved = _resolve_exact_file(lexical_path, "live-shadow specification")
        content = _read_utf8(resolved, "live-shadow specification", limit=None)
    except ShadowInputError as exc:
        raise LiveShadowInputError(str(exc)) from exc
    try:
        parsed: Any = yaml.load(content.decode("utf-8"), Loader=_UniqueKeySafeLoader)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        location = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        problem = getattr(exc, "problem", None) or "invalid YAML"
        raise LiveShadowInputError(
            f"malformed live-shadow YAML{location}: {problem}"
        ) from exc
    if not isinstance(parsed, dict):
        raise LiveShadowInputError("live-shadow specification root must be a YAML mapping")
    try:
        specification = LiveShadowSpecification.model_validate(parsed)
    except ValidationError as exc:
        details = "; ".join(_format_validation_error(error) for error in exc.errors())
        raise LiveShadowInputError(
            f"live-shadow specification validation failed: {details}"
        ) from exc

    parent = locator.parent
    stage2_locator = _join_locator(parent, specification.stage2_specification_path)
    try:
        policy = _load_shadow_file(
            parent,
            specification.supervisor_policy_path,
            "live-shadow supervisor policy",
        )
        contexts = tuple(
            _load_shadow_file(
                parent,
                value,
                f"live-shadow project context {index + 1}",
            )
            for index, value in enumerate(specification.project_context_paths)
        )
    except ShadowInputError as exc:
        raise LiveShadowInputError(str(exc)) from exc
    if len({context.path for context in contexts}) != len(contexts):
        raise LiveShadowInputError("project_context_paths resolve to duplicate files")
    try:
        stage2 = load_substage_specification(
            stage2_locator,
            sensitive_values=sensitive_values,
            require_clean=require_clean,
        )
    except WorkflowInputError as exc:
        raise LiveShadowInputError(str(exc)) from exc
    prepared = PreparedLiveShadowSpecification(
        specification_locator_path=locator,
        specification_path=resolved,
        specification_bytes=content,
        specification_sha256=hashlib.sha256(content).hexdigest(),
        specification=specification,
        policy=policy,
        contexts=contexts,
        stage2=stage2,
        sensitive_values=sensitive_values,
    )
    try:
        preflight_shadow_confidentiality(
            (
                raw_path,
                prepared.specification_bytes,
                prepared.normalized_dict(),
                prepared.policy.manifest(),
                prepared.context_manifests(),
                prepared.policy.content,
                tuple(context.content for context in prepared.contexts),
                prepared.stage2.contract.content,
                prepared.blind_source_summary(),
            ),
            sensitive_values,
            label="live-shadow source or input",
        )
    except ShadowInputError as exc:
        raise LiveShadowInputError(str(exc)) from exc
    return prepared


def replay_stage2_prefix(entries: Sequence[JournalEntry]) -> dict[str, object]:
    """Replay only typed Stage 2 state updates in a verified prefix."""
    replay: dict[str, object] = {
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
    for entry in entries:
        replay.update(entry.state_updates)
        replay["status"] = entry.new_state
    return replay


def proposal_kind_for_intent(
    pending: PendingAction,
    handoff: PromptHandoff,
    replay: Mapping[str, object],
) -> ProposalKind:
    """Derive the six live decision kinds only from typed durable evidence."""
    if pending.kind == "auditor":
        if handoff.kind != "auditor":
            raise LiveShadowIntegrityError("auditor intent has a non-auditor handoff")
        return "auditor"
    direct: dict[str, ProposalKind] = {
        "initial_worker": "worker_initial",
        "audit_repair": "worker_audit_repair",
        "human_continuation": "worker_human_continuation",
    }
    if handoff.kind in direct:
        return direct[handoff.kind]
    if handoff.kind != "fixed_test_or_scope_repair":
        raise LiveShadowIntegrityError("worker intent has an unsupported handoff")
    trigger = replay.get("repair_trigger")
    if trigger == "scope":
        return "worker_scope_repair"
    if trigger == "test":
        return "worker_test_repair"
    raise LiveShadowIntegrityError("combined repair intent lacks a typed trigger")


def build_live_decision_envelope(
    prepared: PreparedLiveShadowSpecification,
    authoritative_run: AuthoritativeRunRecord,
    entries: Sequence[JournalEntry],
    journal_prefix_bytes: bytes,
    *,
    live_shadow_run_id: str,
    sensitive_values: Sequence[str] = (),
) -> LiveDecisionEnvelope:
    """Freeze one immutable envelope from a verified prefix ending at an intent."""
    if not entries:
        raise LiveShadowIntegrityError("cannot build an envelope from an empty journal")
    intent = entries[-1]
    if intent.event_type != "action_intent" or intent.action_kind not in {
        "worker",
        "auditor",
    }:
        raise LiveShadowIntegrityError("envelope prefix does not end at a Codex intent")
    try:
        pending = PendingAction.model_validate(intent.state_updates["pending_action"])
        handoff = PromptHandoff.model_validate(
            _read_json(Path(cast(str, pending.handoff_path)))
        )
    except (KeyError, TypeError, ValidationError, WorkflowStateError) as exc:
        raise LiveShadowIntegrityError("intent handoff is malformed or unavailable") from exc
    replay = replay_stage2_prefix(entries[:-1])
    proposal_kind = proposal_kind_for_intent(pending, handoff, replay)
    ordinal = 1 + sum(
        entry.event_type == "action_intent"
        and entry.action_kind in {"worker", "auditor"}
        for entry in entries[:-1]
    )
    decision_id = f"{proposal_kind}-r{pending.repair_round:03d}-a{ordinal:03d}"
    evidence = _triggering_evidence(
        prepared,
        proposal_kind,
        pending,
        replay,
        authoritative_run.run_directory,
    )
    artifacts = _freeze_evidence_artifacts(
        replay,
        Path(authoritative_run.run_directory),
    )
    summaries = _prior_action_summaries(replay)
    repository_identity = {
        "substage_id": authoritative_run.substage_id,
        "run_token": authoritative_run.run_token,
        "baseline_commit": authoritative_run.baseline_commit,
        "baseline_branch": prepared.stage2.baseline_branch,
    }
    body: dict[str, object] = {
        "schema_version": 1,
        "live_shadow_id": prepared.specification.live_shadow_id,
        "live_shadow_run_id": live_shadow_run_id,
        "authoritative_stage2_run": (
            f"stage2-run/{authoritative_run.run_token}"
        ),
        "authoritative_stage2_run_id": authoritative_run.run_token,
        "authoritative_substage_id": authoritative_run.substage_id,
        "decision_id": decision_id,
        "proposal_kind": proposal_kind,
        "ordinal": ordinal,
        "repair_round": pending.repair_round,
        "source_action_id": pending.action_id,
        "journal_intent_sequence": intent.sequence,
        "journal_intent_hash": intent.entry_hash,
        "journal_prefix_sha256": hashlib.sha256(journal_prefix_bytes).hexdigest(),
        "baseline_commit": authoritative_run.baseline_commit,
        "baseline_branch": prepared.stage2.baseline_branch,
        "repository_identity_sha256": hashlib.sha256(
            _canonical_json(repository_identity)
        ).hexdigest(),
        "allowed_paths": list(prepared.stage2.specification.allowed_paths),
        "protected_paths": list(prepared.stage2.specification.protected_paths),
        "acceptance_tests": [
            LiveAcceptanceTest(
                id=test.specification.id,
                argv=test.specification.argv,
            ).model_dump(mode="json")
            for test in prepared.stage2.acceptance_tests
        ],
        "triggering_evidence": evidence,
        "evidence_artifacts": [
            artifact.model_dump(mode="json") for artifact in artifacts
        ],
        "prior_authoritative_action_summaries": [
            summary.model_dump(mode="json") for summary in summaries
        ],
        "comparison_available": False,
        "comparison_unavailable_reason": "authoritative_action_pending",
        "envelope_timestamp": intent.timestamp,
    }
    body["envelope_sha256"] = hashlib.sha256(_canonical_json(body)).hexdigest()
    try:
        preflight_shadow_confidentiality(
            body,
            sensitive_values,
            label="live decision envelope",
        )
    except ShadowInputError as exc:
        raise LiveShadowInputError(str(exc)) from exc
    try:
        return LiveDecisionEnvelope.model_validate(body)
    except ValidationError as exc:
        raise LiveShadowIntegrityError("constructed live decision envelope is invalid") from exc


def _triggering_evidence(
    prepared: PreparedLiveShadowSpecification,
    proposal_kind: ProposalKind,
    pending: PendingAction,
    replay: Mapping[str, object],
    authoritative_run_directory: str,
) -> dict[str, object]:
    common: dict[str, object] = {
        "repair_round": pending.repair_round,
        "authoritative_state_before_intent": replay.get("status"),
        "scope": {
            "allowed_paths": list(prepared.stage2.specification.allowed_paths),
            "protected_paths": list(prepared.stage2.specification.protected_paths),
        },
        "acceptance_tests": [
            {
                "id": test.specification.id,
                "argv": list(test.specification.argv),
            }
            for test in prepared.stage2.acceptance_tests
        ],
    }
    if proposal_kind == "worker_initial":
        common["baseline"] = {
            "head": prepared.stage2.baseline_commit,
            "branch": prepared.stage2.baseline_branch,
        }
        return common
    common["latest_worker_result"] = _optional_typed_json(
        replay.get("latest_worker_result_path"),
        WorkerModelResult,
    )
    common["latest_git_evidence"] = _safe_git_evidence(
        replay.get("latest_git_evidence_path"),
        Path(authoritative_run_directory),
    )
    common["latest_tests"] = _safe_test_suite(
        replay.get("latest_tests_path"),
        Path(authoritative_run_directory),
    )
    common["latest_audit"] = _optional_typed_json(
        replay.get("latest_audit_result_path"),
        AuditorModelResult,
    )
    prior_paths = replay.get("prior_audit_result_paths")
    common["prior_audits"] = (
        [
            AuditorModelResult.model_validate(_read_json(Path(path))).model_dump(mode="json")
            for path in cast(list[str], prior_paths)
        ]
        if isinstance(prior_paths, list)
        and all(isinstance(path, str) for path in prior_paths)
        else []
    )
    if proposal_kind == "worker_human_continuation":
        common["human_continuation"] = {
            "source_content_withheld": True,
            "origin_state": replay.get("status"),
        }
    if proposal_kind == "auditor":
        common["instruction"] = (
            "Plan from this frozen envelope only. Direct inspection of the live "
            "repository is prohibited."
        )
    return common


def _optional_typed_json(
    value: object,
    model: type[WorkerModelResult] | type[AuditorModelResult] | type[TestSuiteResult],
) -> dict[str, object] | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = model.model_validate(_read_json(Path(value)))
    except (ValidationError, WorkflowStateError) as exc:
        raise LiveShadowIntegrityError("prior typed evidence is invalid") from exc
    return cast(dict[str, object], parsed.model_dump(mode="json"))


def _safe_git_evidence(
    value: object,
    authoritative_run_directory: Path,
) -> dict[str, object] | None:
    if not isinstance(value, str):
        return None
    try:
        evidence = GitEvidence.model_validate(_read_json(Path(value)))
    except (ValidationError, WorkflowStateError) as exc:
        raise LiveShadowIntegrityError("prior Git evidence is invalid") from exc
    rendered = evidence.model_dump(mode="json")
    rendered["repository_root"] = (
        "repository-identity/"
        + hashlib.sha256(evidence.repository_root.encode("utf-8")).hexdigest()
    )
    rendered["patch_artifact"] = _logical_locator(
        Path(evidence.patch_artifact), authoritative_run_directory
    )
    return cast(dict[str, object], rendered)


def _safe_test_suite(
    value: object,
    authoritative_run_directory: Path,
) -> dict[str, object] | None:
    if not isinstance(value, str):
        return None
    try:
        suite = TestSuiteResult.model_validate(_read_json(Path(value)))
    except (ValidationError, WorkflowStateError) as exc:
        raise LiveShadowIntegrityError("prior test suite is invalid") from exc
    rendered = suite.model_dump(mode="json")
    results = cast(list[dict[str, object]], rendered["results"])
    for result in results:
        result["cwd"] = f"configured-test-workspace/{result['test_id']}"
        for field in ("stdout_artifact", "stderr_artifact"):
            locator = result[field]
            if isinstance(locator, str):
                result[field] = _logical_locator(
                    Path(locator),
                    authoritative_run_directory,
                )
    return cast(dict[str, object], rendered)


def _prior_action_summaries(
    replay: Mapping[str, object],
) -> tuple[PriorAuthoritativeActionSummary, ...]:
    summaries: list[PriorAuthoritativeActionSummary] = []
    worker_path = replay.get("latest_worker_result_path")
    worker_id = replay.get("latest_worker_action_id")
    if isinstance(worker_path, str) and isinstance(worker_id, str):
        try:
            worker = WorkerModelResult.model_validate(_read_json(Path(worker_path)))
        except (ValidationError, WorkflowStateError) as exc:
            raise LiveShadowIntegrityError("prior worker summary is invalid") from exc
        summaries.append(
            PriorAuthoritativeActionSummary(
                action_id=worker_id,
                kind="worker",
                repair_round=_round_from_action_id(worker_id),
                summary=worker.summary,
                status=worker.status,
            )
        )
    audit_path = replay.get("latest_audit_result_path")
    audit_id = replay.get("latest_audit_action_id")
    if isinstance(audit_path, str) and isinstance(audit_id, str):
        try:
            audit = AuditorModelResult.model_validate(_read_json(Path(audit_path)))
        except (ValidationError, WorkflowStateError) as exc:
            raise LiveShadowIntegrityError("prior auditor summary is invalid") from exc
        summaries.append(
            PriorAuthoritativeActionSummary(
                action_id=audit_id,
                kind="auditor",
                repair_round=_round_from_action_id(audit_id),
                summary=audit.summary,
                status=audit.verdict,
            )
        )
    return tuple(summaries)


def _round_from_action_id(action_id: str) -> int:
    marker = "-r"
    try:
        return int(action_id.split(marker, 1)[1][:3])
    except (IndexError, ValueError) as exc:
        raise LiveShadowIntegrityError("prior action ID has no deterministic round") from exc


def _freeze_evidence_artifacts(
    replay: Mapping[str, object],
    authoritative_run_directory: Path,
) -> tuple[FrozenEvidenceArtifact, ...]:
    locators: list[Path] = []
    for field in (
        "latest_worker_result_path",
        "latest_audit_result_path",
        "latest_git_evidence_path",
        "latest_tests_path",
    ):
        value = replay.get(field)
        if isinstance(value, str):
            locators.append(Path(value))
    prior = replay.get("prior_audit_result_paths")
    if isinstance(prior, list):
        locators.extend(Path(value) for value in prior if isinstance(value, str))
    structured_locators = set(locators)
    git_path = replay.get("latest_git_evidence_path")
    if isinstance(git_path, str):
        try:
            git_evidence = GitEvidence.model_validate(_read_json(Path(git_path)))
            locators.append(Path(git_evidence.patch_artifact))
        except (ValidationError, WorkflowStateError) as exc:
            raise LiveShadowIntegrityError("prior Git artifact is invalid") from exc
    tests_path = replay.get("latest_tests_path")
    if isinstance(tests_path, str):
        try:
            suite = TestSuiteResult.model_validate(_read_json(Path(tests_path)))
        except (ValidationError, WorkflowStateError) as exc:
            raise LiveShadowIntegrityError("prior test suite is invalid") from exc
        for result in suite.results:
            if result.status != "skipped":
                locators.extend(
                    (
                        Path(cast(str, result.stdout_artifact)),
                        Path(cast(str, result.stderr_artifact)),
                    )
                )

    frozen: list[FrozenEvidenceArtifact] = []
    remaining = MAX_FROZEN_EVIDENCE_TOTAL_BYTES
    for path in dict.fromkeys(locators):
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise LiveShadowIntegrityError("prior evidence artifact is unreadable") from exc
        if sha256_regular_file(path) != hashlib.sha256(raw).hexdigest():
            raise LiveShadowIntegrityError("prior evidence artifact changed while freezing")
        if path in structured_locators:
            stored = b""
            safe_text = ""
        else:
            stored_limit = min(MAX_FROZEN_EVIDENCE_ITEM_BYTES, remaining)
            stored = raw[:stored_limit]
            while stored:
                try:
                    safe_text = stored.decode("utf-8")
                    break
                except UnicodeDecodeError:
                    stored = stored[:-1]
            else:
                safe_text = ""
        remaining -= len(stored)
        frozen.append(
            FrozenEvidenceArtifact(
                locator=_logical_locator(path, authoritative_run_directory),
                sha256=hashlib.sha256(raw).hexdigest(),
                byte_count=len(raw),
                stored_byte_count=len(stored),
                content_truncated=len(stored) < len(raw),
                safe_content=safe_text,
            )
        )
    return tuple(frozen)


def _logical_locator(path: Path, authoritative_run_directory: Path) -> str:
    try:
        return path.relative_to(authoritative_run_directory).as_posix()
    except ValueError:
        return f"external-evidence/{hashlib.sha256(str(path).encode()).hexdigest()[:16]}"


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
