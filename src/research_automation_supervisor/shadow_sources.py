"""Frozen Stage 3 inputs and retrospective Stage 2 decision reconstruction."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import yaml  # type: ignore[import-untyped]
from pydantic import ValidationError

from research_automation_supervisor.codex_adapter import (
    build_subprocess_environment,
)
from research_automation_supervisor.contract import (
    _format_validation_error,
    _UniqueKeySafeLoader,
)
from research_automation_supervisor.errors import (
    ShadowInputError,
    ShadowIntegrityError,
    ShadowStateError,
    WorkflowDependencyError,
    WorkflowInputError,
    WorkflowLockError,
    WorkflowStateError,
)
from research_automation_supervisor.git_evidence import (
    GitBaseline,
    GitEvidence,
    record_git_baseline,
)
from research_automation_supervisor.shadow_confidentiality import (
    preflight_shadow_confidentiality,
    preflight_shadow_locator,
)
from research_automation_supervisor.shadow_models import (
    DecisionPoint,
    FrozenFileHash,
    ProposalKind,
    ShadowSpecification,
)
from research_automation_supervisor.test_runner import (
    TestAttemptResult,
    TestSuiteResult,
)
from research_automation_supervisor.workflow_engine import (
    read_stage2_source_for_shadow,
)
from research_automation_supervisor.workflow_integrity import (
    JournalEntry,
    PromptHandoff,
    sha256_regular_file,
)
from research_automation_supervisor.workflow_models import (
    AuditorModelResult,
    HumanFile,
    PendingAction,
    PreparedSubstage,
    WorkerModelResult,
    WorkflowResult,
    WorkflowState,
    load_continuation_instruction,
    load_substage_specification,
)
from research_automation_supervisor.workflow_prompts import (
    RenderedWorkflowPrompt,
    build_audit_repair_prompt,
    build_auditor_prompt,
    build_fixed_test_repair_prompt,
    build_human_continuation_prompt,
    build_initial_worker_prompt,
)

MAX_SHADOW_INPUT_FILE_BYTES = 2 * 1024 * 1024
ALLOWED_SOURCE_STATUSES = frozenset(
    {
        "completed",
        "checkpoint_paused",
        "human_paused",
        "repair_limit_paused",
        "failed",
        "aborted",
    }
)


@dataclass(frozen=True)
class FrozenShadowFile:
    """One exact human-written Stage 3 file read once."""

    locator_path: Path
    path: Path
    content: bytes
    sha256: str

    def manifest(self) -> FrozenFileHash:
        return FrozenFileHash(
            path=str(self.path),
            sha256=self.sha256,
            byte_count=len(self.content),
        )


@dataclass(frozen=True)
class DecisionReconstruction:
    """One decision point plus in-memory blind and comparison evidence."""

    point: DecisionPoint
    blind_evidence: dict[str, object]
    authoritative_source: HumanFile | None
    authoritative_rendered: RenderedWorkflowPrompt | None


@dataclass(frozen=True)
class VerifiedStage2Source:
    """A fully verified Stage 2 run and its frozen source material."""

    run_directory: Path
    result: WorkflowResult
    state: WorkflowState
    prepared: PreparedSubstage
    baseline: GitBaseline
    normalized_specification: dict[str, Any]
    journal: tuple[JournalEntry, ...]
    decisions: tuple[DecisionReconstruction, ...]

    def blind_source_summary(self) -> dict[str, object]:
        """Return only source identity and the frozen normalized specification."""
        normalized = dict(self.normalized_specification)
        for field in (
            "worker_initial_prompt_path",
            "worker_repair_prompt_path",
            "auditor_prompt_path",
        ):
            normalized.pop(field, None)
        return {
            "schema_version": 1,
            "source_stage2_run": str(self.run_directory),
            "substage_id": self.state.substage_id,
            "workspace": self.state.workspace,
            "repository_root": self.state.repository_root,
            "baseline_commit": self.state.baseline_commit,
            "baseline_branch": self.state.baseline_branch,
            "normalized_stage2_specification": normalized,
        }

    def identity_record(self) -> dict[str, object]:
        """Return hash-only source identity for the Stage 3 run artifact."""
        return {
            "schema_version": 1,
            "run_directory": str(self.run_directory),
            "substage_id": self.state.substage_id,
            "status": self.state.status,
            "state_sha256": sha256_regular_file(
                self.run_directory / "state.json"
            ),
            "result_sha256": sha256_regular_file(
                self.run_directory / "result.json"
            ),
            "journal_sha256": sha256_regular_file(
                self.run_directory / "journal.jsonl"
            ),
            "journal_head": self.state.journal_hash,
            "journal_sequence": self.state.journal_sequence,
            "repository_root": self.state.repository_root,
            "baseline_commit": self.state.baseline_commit,
            "baseline_branch": self.state.baseline_branch,
            "decision_count": len(self.decisions),
        }

    def model_session_uuids(self) -> frozenset[str]:
        """Return canonical forms of verified source worker/auditor UUIDs."""
        identifiers: set[str] = set()
        for action_id in self.state.completed_action_ids:
            path = self.run_directory / "actions" / f"{action_id}.json"
            value = _read_json(path)
            if value.get("kind") not in {"worker", "auditor"}:
                continue
            thread_ids = value.get("thread_started_ids")
            if isinstance(thread_ids, list):
                for item in thread_ids:
                    if not isinstance(item, str):
                        continue
                    try:
                        parsed = UUID(item)
                    except ValueError:
                        continue
                    if parsed.int != 0:
                        identifiers.add(str(parsed))
        return frozenset(identifiers)


@dataclass(frozen=True)
class PreparedShadowSpecification:
    """Resolved immutable Stage 3 specification and verified source run."""

    specification_locator_path: Path
    specification_path: Path
    specification_bytes: bytes
    specification_sha256: str
    specification: ShadowSpecification
    policy: FrozenShadowFile
    contexts: tuple[FrozenShadowFile, ...]
    source: VerifiedStage2Source

    def normalized_dict(self) -> dict[str, object]:
        value = self.specification.model_dump(mode="json")
        value.update(
            {
                "specification_path": str(self.specification_path),
                "source_stage2_run": str(self.source.run_directory),
                "supervisor_policy_path": str(self.policy.path),
                "project_context_paths": [
                    str(context.path) for context in self.contexts
                ],
                "supervisor_workspace": self.source.state.workspace,
            }
        )
        return value


def load_shadow_specification(
    path: Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> PreparedShadowSpecification:
    """Read, strictly validate, and fully verify one Stage 3 specification."""
    _, _, sensitive_values = build_subprocess_environment(environ)
    raw_path = preflight_shadow_locator(
        path,
        sensitive_values,
        label="shadow specification locator",
    )
    lexical_path = Path(raw_path)
    specification_locator = _absolute_locator(lexical_path)
    specification_path = _resolve_exact_file(
        lexical_path, "shadow specification"
    )
    specification_bytes = _read_utf8(
        specification_path,
        "shadow specification",
        limit=None,
    )
    try:
        parsed: Any = yaml.load(
            specification_bytes.decode("utf-8"),
            Loader=_UniqueKeySafeLoader,
        )
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        location = (
            f" at line {mark.line + 1}, column {mark.column + 1}"
            if mark
            else ""
        )
        problem = getattr(exc, "problem", None) or "invalid YAML"
        raise ShadowInputError(
            f"malformed shadow YAML{location}: {problem}"
        ) from exc
    if not isinstance(parsed, dict):
        raise ShadowInputError(
            "shadow specification root must be a YAML mapping"
        )
    try:
        specification = ShadowSpecification.model_validate(parsed)
    except ValidationError as exc:
        details = "; ".join(
            _format_validation_error(error) for error in exc.errors()
        )
        raise ShadowInputError(
            f"shadow specification validation failed: {details}"
        ) from exc

    locator_parent = specification_locator.parent
    source_run = _resolve_exact_directory(
        _join_locator(locator_parent, specification.source_stage2_run),
        "source Stage 2 run",
    )
    policy = _load_shadow_file(
        locator_parent,
        specification.supervisor_policy_path,
        "supervisor policy",
    )
    contexts = tuple(
        _load_shadow_file(
            locator_parent,
            value,
            f"project context {index + 1}",
        )
        for index, value in enumerate(specification.project_context_paths)
    )
    context_paths = [context.path for context in contexts]
    if len(set(context_paths)) != len(context_paths):
        raise ShadowInputError(
            "project_context_paths resolve to duplicate files"
        )
    source = verify_stage2_source(source_run, environ=environ)
    prepared = PreparedShadowSpecification(
        specification_locator_path=specification_locator,
        specification_path=specification_path,
        specification_bytes=specification_bytes,
        specification_sha256=hashlib.sha256(
            specification_bytes
        ).hexdigest(),
        specification=specification,
        policy=policy,
        contexts=contexts,
        source=source,
    )
    preflight_shadow_confidentiality(
        (
            prepared.specification_bytes,
            prepared.normalized_dict(),
            prepared.policy.manifest(),
            tuple(context.manifest() for context in prepared.contexts),
            prepared.source.identity_record(),
            prepared.policy.content,
            tuple(context.content for context in prepared.contexts),
            prepared.source.prepared.contract.content,
            prepared.source.blind_source_summary(),
            tuple(
                (
                    decision.point.model_dump(mode="json"),
                    decision.blind_evidence,
                )
                for decision in prepared.source.decisions
            ),
        ),
        sensitive_values,
        label="shadow source or input",
    )
    # Import locally to keep source reconstruction independent of prompt
    # assembly while still proving the complete prompt before run creation.
    from research_automation_supervisor.shadow_prompts import (
        build_blind_supervisor_prompt,
    )

    for decision in prepared.source.decisions:
        build_blind_supervisor_prompt(
            prepared,
            decision,
            sensitive_values=sensitive_values,
        )
    return prepared


def verify_stage2_source(
    run_directory: Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> VerifiedStage2Source:
    """Use trusted Stage 2 readers, then reconstruct verified decisions."""
    resolved = _resolve_exact_directory(
        run_directory, "source Stage 2 run"
    )
    _assert_source_unlocked(resolved)
    try:
        result, state, journal = read_stage2_source_for_shadow(resolved)
    except WorkflowLockError as exc:
        raise ShadowInputError("source Stage 2 run is actively locked") from exc
    except WorkflowStateError as exc:
        raise ShadowIntegrityError(
            "source Stage 2 run failed trusted integrity validation"
        ) from exc
    if result.status not in ALLOWED_SOURCE_STATUSES:
        raise ShadowInputError(
            "source Stage 2 run is still active or not in an allowed state"
        )
    try:
        prepared = load_substage_specification(
            Path(state.specification_path),
            require_clean=False,
        )
        baseline = GitBaseline.model_validate(
            _read_json(resolved / "baseline.json")
        )
        normalized = _read_json(resolved / "spec.normalized.json")
        current_repository = record_git_baseline(
            Path(state.workspace), environ=environ
        )
    except WorkflowDependencyError:
        raise
    except (WorkflowInputError, WorkflowStateError, ValidationError) as exc:
        raise ShadowIntegrityError(
            "source Stage 2 frozen inputs could not be verified"
        ) from exc
    if (
        current_repository.repository_root != state.repository_root
        or current_repository.head != state.baseline_commit
        or current_repository.branch != state.baseline_branch
        or baseline.repository_root != state.repository_root
        or baseline.head != state.baseline_commit
        or baseline.branch != state.baseline_branch
    ):
        raise ShadowIntegrityError(
            "source Stage 2 repository identity no longer matches"
        )
    try:
        decisions = reconstruct_decision_points(
            resolved,
            prepared,
            baseline,
            journal,
        )
    except (ShadowStateError, WorkflowInputError, WorkflowStateError) as exc:
        raise ShadowIntegrityError(
            "source Stage 2 decision reconstruction failed integrity checks"
        ) from exc
    return VerifiedStage2Source(
        run_directory=resolved,
        result=result,
        state=state,
        prepared=prepared,
        baseline=baseline,
        normalized_specification=normalized,
        journal=journal,
        decisions=decisions,
    )


def reconstruct_decision_points(
    run_directory: Path,
    prepared: PreparedSubstage,
    baseline: GitBaseline,
    journal: Sequence[JournalEntry],
) -> tuple[DecisionReconstruction, ...]:
    """Enumerate Stage 2 Codex intents in verified journal/action order."""
    replay = _initial_replay()
    continuation_origins: dict[int, str] = {}
    decisions: list[DecisionReconstruction] = []
    for entry in journal:
        if entry.reason == "human_continuation_requested":
            next_round = entry.state_updates.get("repair_round")
            if isinstance(next_round, int) and entry.previous_state is not None:
                continuation_origins[next_round] = entry.previous_state
        if entry.event_type == "action_intent" and entry.action_kind in {
            "worker",
            "auditor",
        }:
            try:
                pending = PendingAction.model_validate(
                    entry.state_updates["pending_action"]
                )
                handoff = PromptHandoff.model_validate(
                    _read_json(Path(cast(str, pending.handoff_path)))
                )
            except (
                KeyError,
                TypeError,
                ValidationError,
                WorkflowStateError,
            ) as exc:
                raise ShadowStateError(
                    "verified Stage 2 decision intent is malformed"
                ) from exc
            proposal_kind = _proposal_kind(
                pending,
                handoff,
                replay,
            )
            ordinal = len(decisions) + 1
            decision_id = (
                f"{proposal_kind}-r{pending.repair_round:03d}"
                f"-a{ordinal:03d}"
            )
            evidence: dict[str, object]
            authoritative_source: HumanFile | None = None
            authoritative_rendered: RenderedWorkflowPrompt | None = None
            unavailable_reason: str | None = None
            if (
                proposal_kind == "worker_human_continuation"
                and _continuation_source_is_missing(replay)
            ):
                evidence = _continuation_evidence_from_replay(
                    prepared,
                    pending,
                    handoff,
                    replay,
                    continuation_origins,
                )
                unavailable_reason = "continuation_source_unavailable"
            else:
                (
                    evidence,
                    authoritative_source,
                    authoritative_rendered,
                ) = _reconstruct_one(
                    prepared,
                    baseline,
                    proposal_kind,
                    pending,
                    handoff,
                    replay,
                    continuation_origins,
                )
            evidence_sha256 = hashlib.sha256(
                _canonical_json(evidence)
            ).hexdigest()
            point = DecisionPoint(
                decision_id=decision_id,
                proposal_kind=proposal_kind,
                source_action_id=pending.action_id,
                repair_round=pending.repair_round,
                ordinal=ordinal,
                journal_sequence=entry.sequence,
                evidence_sha256=evidence_sha256,
                comparison_available=unavailable_reason is None,
                comparison_unavailable_reason=unavailable_reason,
            )
            decisions.append(
                DecisionReconstruction(
                    point=point,
                    blind_evidence=evidence,
                    authoritative_source=authoritative_source,
                    authoritative_rendered=authoritative_rendered,
                )
            )
        replay.update(entry.state_updates)
        replay["status"] = entry.new_state
    return tuple(decisions)


def decision_points_artifact(
    decisions: Sequence[DecisionReconstruction],
) -> dict[str, object]:
    """Build the comparison-free durable decision-point artifact."""
    return {
        "schema_version": 1,
        "decision_points": [
            decision.point.model_dump(mode="json") for decision in decisions
        ],
    }


def _proposal_kind(
    pending: PendingAction,
    handoff: PromptHandoff,
    replay: Mapping[str, object],
) -> ProposalKind:
    if pending.kind == "auditor":
        if handoff.kind != "auditor":
            raise ShadowStateError(
                "Stage 2 auditor intent has a non-auditor handoff"
            )
        return "auditor"
    mapping: dict[str, ProposalKind] = {
        "initial_worker": "worker_initial",
        "audit_repair": "worker_audit_repair",
        "human_continuation": "worker_human_continuation",
    }
    if handoff.kind in mapping:
        return mapping[handoff.kind]
    if handoff.kind != "fixed_test_or_scope_repair":
        raise ShadowStateError("Stage 2 worker handoff kind is unsupported")
    trigger = replay.get("repair_trigger")
    if trigger == "scope":
        return "worker_scope_repair"
    if trigger == "test":
        return "worker_test_repair"
    raise ShadowStateError(
        "combined Stage 2 repair handoff has no deterministic trigger"
    )


def _reconstruct_one(
    prepared: PreparedSubstage,
    baseline: GitBaseline,
    proposal_kind: ProposalKind,
    pending: PendingAction,
    handoff: PromptHandoff,
    replay: Mapping[str, object],
    continuation_origins: Mapping[int, str],
) -> tuple[dict[str, object], HumanFile, RenderedWorkflowPrompt]:
    rendered: RenderedWorkflowPrompt
    evidence: dict[str, object]
    if proposal_kind == "worker_initial":
        evidence = _initial_worker_evidence(prepared, baseline)
        rendered = build_initial_worker_prompt(prepared, baseline)
        source = prepared.worker_initial_prompt
    elif proposal_kind in {
        "worker_scope_repair",
        "worker_test_repair",
    }:
        git_evidence = _git_from_replay(replay)
        tests = (
            _optional_tests_from_replay(replay)
            if proposal_kind == "worker_scope_repair"
            else _tests_from_replay(replay)
        )
        evidence = _fixed_repair_evidence(
            pending.repair_round,
            tests,
            git_evidence,
        )
        rendered = build_fixed_test_repair_prompt(
            prepared,
            pending.repair_round,
            tests,
            git_evidence,
        )
        source = prepared.worker_repair_prompt
    elif proposal_kind == "worker_audit_repair":
        git_evidence = _git_from_replay(replay)
        tests = _tests_from_replay(replay)
        audit = _audit_from_replay(replay)
        evidence = _audit_repair_evidence(
            pending.repair_round,
            audit,
            tests,
            git_evidence,
        )
        rendered = build_audit_repair_prompt(
            prepared,
            pending.repair_round,
            audit,
            tests,
            git_evidence,
        )
        source = prepared.worker_repair_prompt
    elif proposal_kind == "worker_human_continuation":
        instruction_path = replay.get("continuation_path")
        instruction_sha256 = replay.get("continuation_sha256")
        if not isinstance(instruction_path, str) or not isinstance(
            instruction_sha256, str
        ):
            raise ShadowStateError(
                "continuation decision lacks its original instruction"
            )
        instruction = load_continuation_instruction(
            Path(instruction_path),
            workspace=prepared.workspace,
            protected_paths=prepared.specification.protected_paths,
        )
        if instruction.sha256 != instruction_sha256:
            raise ShadowStateError(
                "continuation instruction hash no longer matches"
            )
        tests = _optional_tests_from_replay(replay)
        optional_git_evidence = _optional_git_from_replay(replay)
        optional_audit = _optional_audit_from_replay(replay)
        origin = continuation_origins.get(pending.repair_round)
        if origin is None:
            raise ShadowStateError(
                "continuation origin state cannot be proven"
            )
        evidence = _human_continuation_evidence(
            origin,
            pending.repair_round,
            tests,
            optional_git_evidence,
            optional_audit,
        )
        rendered = build_human_continuation_prompt(
            prepared,
            instruction,
            origin,
            pending.repair_round,
            tests,
            optional_git_evidence,
            optional_audit,
        )
        source = instruction
    else:
        git_evidence = _git_from_replay(replay)
        tests = _tests_from_replay(replay)
        worker = _worker_from_replay(replay)
        prior_audits = _prior_audits_from_replay(replay)
        try:
            patch_bytes = Path(git_evidence.patch_artifact).read_bytes()
        except OSError as exc:
            raise ShadowStateError(
                "auditor patch evidence is unavailable"
            ) from exc
        evidence = _auditor_evidence(
            prepared,
            baseline,
            git_evidence,
            patch_bytes,
            worker,
            tests,
            prior_audits,
        )
        rendered = build_auditor_prompt(
            prepared,
            baseline,
            git_evidence,
            patch_bytes,
            worker,
            tests,
            prior_audits,
        )
        source = prepared.auditor_prompt
    if (
        handoff.kind != rendered.kind
        or handoff.source_path != str(source.path)
        or handoff.source_sha256 != source.sha256
        or handoff.contract_sha256 != prepared.contract.sha256
        or handoff.rendered_prompt_sha256 != rendered.rendered_sha256
        or handoff.rendered_prompt_byte_count != rendered.byte_count
        or handoff.evidence_sha256 != rendered.evidence_sha256
        or pending.prompt_sha256 != rendered.rendered_sha256
    ):
        raise ShadowStateError(
            "authoritative Stage 2 prompt reconstruction does not match handoff"
        )
    return evidence, source, rendered


def _initial_worker_evidence(
    prepared: PreparedSubstage,
    baseline: GitBaseline,
) -> dict[str, object]:
    specification = prepared.specification
    return {
        "substage": {
            "schema_version": specification.schema_version,
            "substage_id": specification.substage_id,
            "title": specification.title,
            "workspace": str(prepared.workspace),
            "worker_model": specification.worker_model,
            "worker_reasoning_effort": specification.worker_reasoning_effort,
            "max_repair_rounds": specification.max_repair_rounds,
            "checkpoint_after": specification.checkpoint_after,
        },
        "scope": {
            "allowed_paths": list(specification.allowed_paths),
            "protected_paths": list(specification.protected_paths),
        },
        "acceptance_tests": [
            {
                "id": test.specification.id,
                "argv": list(test.specification.argv),
            }
            for test in prepared.acceptance_tests
        ],
        "baseline": baseline.to_dict(),
    }


def _fixed_repair_evidence(
    repair_round: int,
    tests: tuple[TestAttemptResult, ...],
    git_evidence: GitEvidence,
) -> dict[str, object]:
    return {
        "repair_round": repair_round,
        "test_results": [result.to_dict() for result in tests],
        "scope": {
            "scope_compliant": git_evidence.scope_compliant,
            "findings": [
                item.model_dump(mode="json")
                for item in git_evidence.scope_findings
            ],
            "changed_paths": [
                item.model_dump(mode="json")
                for item in git_evidence.changed_paths
            ],
        },
        "bounded_logs": [
            {
                "test_id": result.test_id,
                "stdout_artifact": result.stdout_artifact,
                "stdout_sha256": result.stdout_sha256,
                "stderr_artifact": result.stderr_artifact,
                "stderr_sha256": result.stderr_sha256,
            }
            for result in tests
            if result.status != "skipped"
        ],
    }


def _audit_repair_evidence(
    repair_round: int,
    audit: AuditorModelResult,
    tests: tuple[TestAttemptResult, ...],
    git_evidence: GitEvidence,
) -> dict[str, object]:
    return {
        "repair_round": repair_round,
        "auditor_result": audit.model_dump(mode="json"),
        "tests": [result.to_dict() for result in tests],
        "scope": {
            "scope_compliant": git_evidence.scope_compliant,
            "findings": [
                item.model_dump(mode="json")
                for item in git_evidence.scope_findings
            ],
        },
    }


def _human_continuation_evidence(
    current_state: str,
    repair_round: int,
    tests: tuple[TestAttemptResult, ...],
    git_evidence: GitEvidence | None,
    audit: AuditorModelResult | None,
) -> dict[str, object]:
    return {
        "current_state": current_state,
        "repair_round": repair_round,
        "tests": [result.to_dict() for result in tests],
        "scope": (
            None
            if git_evidence is None
            else {
                "scope_compliant": git_evidence.scope_compliant,
                "findings": [
                    item.model_dump(mode="json")
                    for item in git_evidence.scope_findings
                ],
            }
        ),
        "audit": (
            None if audit is None else audit.model_dump(mode="json")
        ),
    }


def _auditor_evidence(
    prepared: PreparedSubstage,
    baseline: GitBaseline,
    git_evidence: GitEvidence,
    patch_bytes: bytes,
    worker: WorkerModelResult,
    tests: tuple[TestAttemptResult, ...],
    prior_audits: tuple[AuditorModelResult, ...],
) -> dict[str, object]:
    return {
        "instruction": (
            "Inspect the current workspace directly; this evidence is not a "
            "substitute for direct inspection."
        ),
        "substage": {
            "substage_id": prepared.specification.substage_id,
            "title": prepared.specification.title,
            "workspace": str(prepared.workspace),
            "allowed_paths": list(
                prepared.specification.allowed_paths
            ),
            "protected_paths": list(
                prepared.specification.protected_paths
            ),
        },
        "baseline": baseline.to_dict(),
        "current_git": git_evidence.to_dict(),
        "patch_evidence": {
            "complete": git_evidence.patch_complete,
            "sha256": git_evidence.patch_sha256,
            "byte_count": git_evidence.patch_byte_count,
            "content": patch_bytes.decode("utf-8", errors="replace"),
        },
        "worker_result": worker.model_dump(mode="json"),
        "tests": [result.to_dict() for result in tests],
        "prior_audits": [
            audit.model_dump(mode="json") for audit in prior_audits
        ],
    }


def _continuation_source_is_missing(
    replay: Mapping[str, object],
) -> bool:
    path = replay.get("continuation_path")
    digest = replay.get("continuation_sha256")
    if not isinstance(path, str) or not isinstance(digest, str):
        raise ShadowStateError(
            "continuation decision lacks its trusted source anchor"
        )
    try:
        Path(path).lstat()
    except FileNotFoundError:
        return True
    except OSError as exc:
        raise ShadowStateError(
            "continuation source locator could not be inspected"
        ) from exc
    return False


def _continuation_evidence_from_replay(
    prepared: PreparedSubstage,
    pending: PendingAction,
    handoff: PromptHandoff,
    replay: Mapping[str, object],
    continuation_origins: Mapping[int, str],
) -> dict[str, object]:
    path = replay.get("continuation_path")
    digest = replay.get("continuation_sha256")
    if (
        not isinstance(path, str)
        or not isinstance(digest, str)
        or handoff.kind != "human_continuation"
        or handoff.source_path != path
        or handoff.source_sha256 != digest
        or handoff.contract_sha256 != prepared.contract.sha256
        or pending.prompt_sha256 != handoff.rendered_prompt_sha256
    ):
        raise ShadowStateError(
            "missing continuation source anchor contradicts its handoff"
        )
    origin = continuation_origins.get(pending.repair_round)
    if origin is None:
        raise ShadowStateError(
            "continuation origin state cannot be proven"
        )
    return _human_continuation_evidence(
        origin,
        pending.repair_round,
        _optional_tests_from_replay(replay),
        _optional_git_from_replay(replay),
        _optional_audit_from_replay(replay),
    )


def _initial_replay() -> dict[str, object]:
    return {
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


def _git_from_replay(replay: Mapping[str, object]) -> GitEvidence:
    value = replay.get("latest_git_evidence_path")
    if not isinstance(value, str):
        raise ShadowStateError("decision lacks prior Git evidence")
    try:
        return GitEvidence.model_validate(_read_json(Path(value)))
    except ValidationError as exc:
        raise ShadowStateError("prior Git evidence is invalid") from exc


def _optional_git_from_replay(
    replay: Mapping[str, object],
) -> GitEvidence | None:
    return (
        None
        if replay.get("latest_git_evidence_path") is None
        else _git_from_replay(replay)
    )


def _tests_from_replay(
    replay: Mapping[str, object],
) -> tuple[TestAttemptResult, ...]:
    value = replay.get("latest_tests_path")
    if not isinstance(value, str):
        raise ShadowStateError("decision lacks prior fixed-test evidence")
    try:
        return TestSuiteResult.model_validate(
            _read_json(Path(value))
        ).results
    except ValidationError as exc:
        raise ShadowStateError(
            "prior fixed-test evidence is invalid"
        ) from exc


def _optional_tests_from_replay(
    replay: Mapping[str, object],
) -> tuple[TestAttemptResult, ...]:
    return (
        ()
        if replay.get("latest_tests_path") is None
        else _tests_from_replay(replay)
    )


def _audit_from_replay(
    replay: Mapping[str, object],
) -> AuditorModelResult:
    value = replay.get("latest_audit_result_path")
    if not isinstance(value, str):
        raise ShadowStateError("decision lacks prior auditor evidence")
    try:
        return AuditorModelResult.model_validate(_read_json(Path(value)))
    except ValidationError as exc:
        raise ShadowStateError("prior auditor evidence is invalid") from exc


def _optional_audit_from_replay(
    replay: Mapping[str, object],
) -> AuditorModelResult | None:
    return (
        None
        if replay.get("latest_audit_result_path") is None
        else _audit_from_replay(replay)
    )


def _worker_from_replay(
    replay: Mapping[str, object],
) -> WorkerModelResult:
    value = replay.get("latest_worker_result_path")
    if not isinstance(value, str):
        raise ShadowStateError("decision lacks prior worker evidence")
    try:
        return WorkerModelResult.model_validate(_read_json(Path(value)))
    except ValidationError as exc:
        raise ShadowStateError("prior worker evidence is invalid") from exc


def _prior_audits_from_replay(
    replay: Mapping[str, object],
) -> tuple[AuditorModelResult, ...]:
    paths = replay.get("prior_audit_result_paths")
    if not isinstance(paths, list) or any(
        not isinstance(path, str) for path in paths
    ):
        raise ShadowStateError("prior auditor history is invalid")
    try:
        return tuple(
            AuditorModelResult.model_validate(_read_json(Path(path)))
            for path in cast(list[str], paths)
        )
    except ValidationError as exc:
        raise ShadowStateError("prior auditor history is invalid") from exc


def _load_shadow_file(
    parent: Path,
    value: str,
    label: str,
) -> FrozenShadowFile:
    locator = _join_locator(parent, value)
    path = _resolve_exact_file(locator, label)
    content = _read_utf8(
        path,
        label,
        limit=MAX_SHADOW_INPUT_FILE_BYTES,
    )
    return FrozenShadowFile(
        locator_path=_absolute_locator(locator),
        path=path,
        content=content,
        sha256=hashlib.sha256(content).hexdigest(),
    )


def _join_locator(parent: Path, value: str) -> Path:
    candidate = Path(value)
    return candidate if candidate.is_absolute() else parent / candidate


def _resolve_exact_file(path: Path, label: str) -> Path:
    return _resolve_exact_path(path, label, expect_directory=False)


def _resolve_exact_directory(path: Path, label: str) -> Path:
    return _resolve_exact_path(path, label, expect_directory=True)


def _resolve_exact_path(
    path: Path,
    label: str,
    *,
    expect_directory: bool,
) -> Path:
    invalid = (
        f"{label} path is invalid and must contain no symbolic-link "
        "component"
    )
    try:
        supplied = os.fspath(path)
        if (
            not expect_directory
            and supplied.endswith(os.sep)
            and supplied not in {os.sep, Path(path).anchor}
        ):
            raise OSError
        candidate = Path(path)
        if candidate.is_absolute():
            current = Path(candidate.anchor)
            components = candidate.parts[1:]
        else:
            current = Path.cwd()
            _inspect_directory_chain(current)
            components = candidate.parts
        final_status: os.stat_result | None = None
        for index, component in enumerate(components):
            if component in {"", "."}:
                continue
            if component == "..":
                current = current.parent
                final_status = current.lstat()
                if stat.S_ISLNK(final_status.st_mode):
                    raise OSError
                continue
            current = current / component
            final_status = current.lstat()
            if stat.S_ISLNK(final_status.st_mode):
                raise OSError
            is_final = index == len(components) - 1
            if not is_final and not stat.S_ISDIR(final_status.st_mode):
                raise OSError
        if final_status is None:
            final_status = current.lstat()
        valid_final = (
            stat.S_ISDIR(final_status.st_mode)
            if expect_directory
            else stat.S_ISREG(final_status.st_mode)
        )
        if not valid_final:
            raise OSError
        locator = _absolute_locator(path)
        resolved = locator.resolve(strict=True)
        resolved_status = resolved.lstat()
        if (
            resolved != locator
            or stat.S_ISLNK(resolved_status.st_mode)
            or (resolved_status.st_dev, resolved_status.st_ino)
            != (final_status.st_dev, final_status.st_ino)
        ):
            raise OSError
    except (OSError, RuntimeError, ValueError) as exc:
        raise ShadowInputError(invalid) from exc
    return resolved


def _inspect_directory_chain(path: Path) -> None:
    if not path.is_absolute():
        raise OSError
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current = current / component
        status = current.lstat()
        if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(
            status.st_mode
        ):
            raise OSError


def _absolute_locator(path: Path) -> Path:
    try:
        return Path(os.path.abspath(os.fspath(path)))
    except (OSError, TypeError, ValueError) as exc:
        raise ShadowInputError(
            "shadow input path could not be normalized"
        ) from exc


def _read_utf8(path: Path, label: str, limit: int | None) -> bytes:
    try:
        size = path.stat().st_size
        if limit is not None and size > limit:
            raise ShadowInputError(
                f"{label} exceeds the {limit}-byte limit"
            )
        content = path.read_bytes()
    except ShadowInputError:
        raise
    except OSError as exc:
        raise ShadowInputError(f"{label} could not be read") from exc
    if limit is not None and len(content) > limit:
        raise ShadowInputError(
            f"{label} exceeds the {limit}-byte limit"
        )
    if not content:
        raise ShadowInputError(f"{label} must not be empty")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ShadowInputError(f"{label} is not valid UTF-8") from exc
    if not text.strip():
        raise ShadowInputError(
            f"{label} must not be empty after trimming"
        )
    return content


def _assert_source_unlocked(run_directory: Path) -> None:
    lock_path = run_directory / "workflow.lock"
    try:
        status = lock_path.lstat()
        if stat.S_ISLNK(status.st_mode) or not stat.S_ISREG(
            status.st_mode
        ):
            raise OSError
        with lock_path.open("r", encoding="utf-8") as handle:
            try:
                fcntl.flock(
                    handle.fileno(),
                    fcntl.LOCK_EX | fcntl.LOCK_NB,
                )
            except BlockingIOError as exc:
                raise ShadowInputError(
                    "source Stage 2 run is actively locked"
                ) from exc
            finally:
                with suppress(OSError):
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except ShadowInputError:
        raise
    except OSError as exc:
        raise ShadowIntegrityError(
            "source Stage 2 lock could not be inspected"
        ) from exc


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=_reject_json_constant,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise WorkflowStateError(
            "durable Stage 2 JSON artifact is invalid"
        ) from exc
    if not isinstance(value, dict):
        raise WorkflowStateError(
            "durable Stage 2 JSON artifact is not an object"
        )
    return cast(dict[str, Any], value)


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


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")
