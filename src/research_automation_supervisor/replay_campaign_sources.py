"""Frozen input loading for Stage 5A historical replay campaigns."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]
from pydantic import ValidationError

from research_automation_supervisor.codex_adapter import build_subprocess_environment
from research_automation_supervisor.contract import (
    _format_validation_error,
    _UniqueKeySafeLoader,
)
from research_automation_supervisor.errors import (
    ReplayCampaignInputError,
    ShadowInputError,
    WorkflowInputError,
)
from research_automation_supervisor.replay_campaign_models import (
    HumanReplayDecision,
    ReplayCampaignSpecification,
    ReplayTaskSpecification,
)
from research_automation_supervisor.shadow_confidentiality import (
    preflight_shadow_confidentiality,
    preflight_shadow_locator,
)
from research_automation_supervisor.shadow_models import FrozenFileHash
from research_automation_supervisor.shadow_sources import (
    FrozenShadowFile,
    _absolute_locator,
    _join_locator,
    _load_shadow_file,
    _read_utf8,
    _resolve_exact_file,
)
from research_automation_supervisor.workflow_models import (
    PreparedSubstage,
    PreparedWorkflowTest,
    load_substage_specification,
)


@dataclass(frozen=True)
class PreparedReplayTask:
    """Resolved model-visible task authority and Stage 2 input."""

    specification: ReplayTaskSpecification
    stage2: PreparedSubstage
    contexts: tuple[FrozenShadowFile, ...]

    def authority_summary(self) -> dict[str, object]:
        stage2 = self.stage2.specification
        return {
            "task_id": self.specification.task_id,
            "title": self.specification.title,
            "contract_path": str(self.stage2.contract.path),
            "allowed_paths": list(stage2.allowed_paths),
            "protected_paths": list(stage2.protected_paths),
            "acceptance_tests": [
                {
                    "id": test.specification.id,
                    "argv": list(test.specification.argv),
                }
                for test in self.stage2.acceptance_tests
            ],
            "worker_model": stage2.worker_model,
            "worker_reasoning_effort": stage2.worker_reasoning_effort,
            "worker_timeout_seconds": stage2.worker_timeout_seconds,
            "auditor_model": stage2.auditor_model,
            "auditor_reasoning_effort": stage2.auditor_reasoning_effort,
            "auditor_timeout_seconds": stage2.auditor_timeout_seconds,
            "max_repair_rounds": stage2.max_repair_rounds,
            "production_profile": self.specification.production_profile.model_dump(
                mode="json"
            ),
        }


@dataclass(frozen=True)
class PreparedReplayEvaluator:
    """Engine-only hidden evaluator configuration for one replay task."""

    task_id: str
    evaluations: tuple[PreparedWorkflowTest, ...]
    gold_roots: tuple[Path, ...]

    def normalized_record(self) -> dict[str, object]:
        evaluations = []
        for prepared in self.evaluations:
            test = prepared.specification
            command = {
                "id": test.id,
                "argv": list(test.argv),
                "cwd": str(prepared.cwd),
                "timeout_seconds": test.timeout_seconds,
                "max_stdout_bytes": test.max_stdout_bytes,
                "max_stderr_bytes": test.max_stderr_bytes,
            }
            rendered = json.dumps(
                command,
                ensure_ascii=True,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("ascii")
            evaluations.append(
                {
                    "id": test.id,
                    "cwd_locator": str(prepared.cwd),
                    "cwd_locator_sha256": hashlib.sha256(
                        str(prepared.cwd).encode("utf-8")
                    ).hexdigest(),
                    "command_sha256": hashlib.sha256(rendered).hexdigest(),
                }
            )
        return {
            "schema_version": 1,
            "task_id": self.task_id,
            "gold_roots": [
                {
                    "locator": str(root),
                    "locator_sha256": hashlib.sha256(
                        str(root).encode("utf-8")
                    ).hexdigest(),
                }
                for root in self.gold_roots
            ],
            "evaluations": evaluations,
        }


@dataclass(frozen=True)
class PreparedReplayCampaign:
    """Resolved immutable campaign manifest."""

    specification_locator_path: Path
    specification_path: Path
    specification_bytes: bytes
    specification_sha256: str
    specification: ReplayCampaignSpecification
    supervisor_policy: FrozenShadowFile
    tasks: tuple[PreparedReplayTask, ...]
    evaluators: tuple[PreparedReplayEvaluator, ...]
    sensitive_values: tuple[str, ...]

    def evaluator_for(self, task_id: str) -> PreparedReplayEvaluator:
        matches = tuple(
            evaluator for evaluator in self.evaluators if evaluator.task_id == task_id
        )
        if len(matches) != 1:
            raise ReplayCampaignInputError(
                "engine-only replay evaluator identity is invalid"
            )
        return matches[0]

    def frozen_hashes(self) -> tuple[FrozenFileHash, ...]:
        values = [
            FrozenFileHash(
                path=str(self.specification_path),
                sha256=self.specification_sha256,
                byte_count=len(self.specification_bytes),
            ),
            self.supervisor_policy.manifest(),
        ]
        for task in self.tasks:
            values.extend(context.manifest() for context in task.contexts)
            values.append(
                FrozenFileHash(
                    path=str(task.stage2.contract.path),
                    sha256=task.stage2.contract.sha256,
                    byte_count=len(task.stage2.contract.content),
                )
            )
        return tuple(values)


def load_replay_campaign_specification(
    path: Path,
    *,
    environ: dict[str, str] | None = None,
    require_clean: bool = True,
) -> PreparedReplayCampaign:
    """Read once and validate all visible and hidden campaign authority."""
    _, _, sensitive_values = build_subprocess_environment(environ)
    try:
        raw_path = preflight_shadow_locator(
            path,
            sensitive_values,
            label="replay campaign specification locator",
        )
        lexical = Path(raw_path)
        locator = _absolute_locator(lexical)
        resolved = _resolve_exact_file(lexical, "replay campaign specification")
        content = _read_utf8(resolved, "replay campaign specification", limit=None)
    except ShadowInputError as exc:
        raise ReplayCampaignInputError(str(exc)) from exc
    try:
        parsed: Any = yaml.load(content.decode("utf-8"), Loader=_UniqueKeySafeLoader)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        location = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        problem = getattr(exc, "problem", None) or "invalid YAML"
        raise ReplayCampaignInputError(
            f"malformed replay campaign YAML{location}: {problem}"
        ) from exc
    if not isinstance(parsed, dict):
        raise ReplayCampaignInputError(
            "replay campaign specification root must be a YAML mapping"
        )
    try:
        specification = ReplayCampaignSpecification.model_validate(parsed)
    except ValidationError as exc:
        details = "; ".join(_format_validation_error(error) for error in exc.errors())
        raise ReplayCampaignInputError(
            f"replay campaign specification validation failed: {details}"
        ) from exc

    parent = locator.parent
    try:
        policy = _load_shadow_file(
            parent,
            specification.supervisor_policy_path,
            "replay supervisor policy",
        )
        prepared_pairs = tuple(
            _prepare_task(
                parent,
                task,
                sensitive_values=sensitive_values,
                require_clean=require_clean,
            )
            for task in specification.tasks
        )
        tasks = tuple(pair[0] for pair in prepared_pairs)
        evaluators = tuple(pair[1] for pair in prepared_pairs)
    except (ShadowInputError, WorkflowInputError) as exc:
        raise ReplayCampaignInputError(str(exc)) from exc
    prepared = PreparedReplayCampaign(
        specification_locator_path=locator,
        specification_path=resolved,
        specification_bytes=content,
        specification_sha256=hashlib.sha256(content).hexdigest(),
        specification=specification,
        supervisor_policy=policy,
        tasks=tasks,
        evaluators=evaluators,
        sensitive_values=sensitive_values,
    )
    _validate_gold_confinement(prepared)
    visible = (
        raw_path,
        content,
        policy.content,
        tuple(
            (
                task.stage2.contract.content,
                tuple(context.content for context in task.contexts),
                task.authority_summary(),
            )
            for task in tasks
        ),
    )
    try:
        preflight_shadow_confidentiality(
            visible,
            sensitive_values,
            label="replay campaign visible inputs",
        )
    except ShadowInputError as exc:
        raise ReplayCampaignInputError(str(exc)) from exc
    return prepared


def load_human_replay_decision(path: Path) -> tuple[HumanReplayDecision, bytes, Path]:
    """Load one exact schema-version-1 human decision without applying it."""
    resolved = _resolve_exact_file(path, "replay campaign decision")
    content = _read_utf8(resolved, "replay campaign decision", limit=64 * 1024)
    try:
        parsed: Any = yaml.load(content.decode("utf-8"), Loader=_UniqueKeySafeLoader)
    except yaml.YAMLError as exc:
        raise ReplayCampaignInputError("malformed replay campaign decision YAML") from exc
    if not isinstance(parsed, dict):
        raise ReplayCampaignInputError("replay campaign decision must be a mapping")
    try:
        decision = HumanReplayDecision.model_validate(parsed)
    except ValidationError as exc:
        details = "; ".join(_format_validation_error(error) for error in exc.errors())
        raise ReplayCampaignInputError(
            f"replay campaign decision validation failed: {details}"
        ) from exc
    return decision, content, resolved


def _prepare_task(
    parent: Path,
    task: ReplayTaskSpecification,
    *,
    sensitive_values: tuple[str, ...],
    require_clean: bool,
) -> tuple[PreparedReplayTask, PreparedReplayEvaluator]:
    stage2 = load_substage_specification(
        _join_locator(parent, task.stage2_specification_path),
        sensitive_values=sensitive_values,
        require_clean=require_clean,
    )
    if stage2.specification.substage_id != task.task_id:
        raise ReplayCampaignInputError(
            "task_id must equal the referenced Stage 2 substage_id"
        )
    if stage2.specification.checkpoint_after:
        raise ReplayCampaignInputError(
            "replay Stage 2 specifications must set checkpoint_after to false"
        )
    contexts = tuple(
        _load_shadow_file(
            parent,
            value,
            f"replay task {task.task_id} project context {index + 1}",
        )
        for index, value in enumerate(task.project_context_paths)
    )
    gold_roots = tuple(
        _resolve_directory(parent, value, f"gold artifact root for {task.task_id}")
        for value in task.gold_artifact_roots
    )
    evaluations = tuple(
        PreparedWorkflowTest(
            specification=test,
            cwd=_resolve_directory(
                parent,
                test.cwd,
                f"gold evaluation cwd {test.id}",
            ),
        )
        for test in task.gold_evaluations
    )
    return (
        PreparedReplayTask(
            specification=task,
            stage2=stage2,
            contexts=contexts,
        ),
        PreparedReplayEvaluator(
            task_id=task.task_id,
            evaluations=evaluations,
            gold_roots=gold_roots,
        ),
    )


def _validate_gold_confinement(campaign: PreparedReplayCampaign) -> None:
    """Require hidden evaluator material to be disjoint from every model-visible root."""
    workspaces = tuple(task.stage2.workspace for task in campaign.tasks)
    visible_files = (
        campaign.specification_path,
        campaign.supervisor_policy.path,
        *(
            path
            for task in campaign.tasks
            for path in (
                task.stage2.specification_path,
                task.stage2.contract.path,
                task.stage2.worker_initial_prompt.path,
                task.stage2.worker_repair_prompt.path,
                task.stage2.auditor_prompt.path,
                *(context.path for context in task.contexts),
            )
        ),
    )
    for workspace in workspaces:
        if _contains(workspace, campaign.specification_path):
            raise ReplayCampaignInputError(
                "campaign manifest and gold configuration must be outside every replay workspace"
            )
    all_gold_roots = tuple(
        root for evaluator in campaign.evaluators for root in evaluator.gold_roots
    )
    for root in all_gold_roots:
        for workspace in workspaces:
            if _contains(root, workspace) or _contains(workspace, root):
                raise ReplayCampaignInputError(
                    "gold artifact roots must be disjoint from every replay workspace"
                )
        if any(_contains(root, path) for path in visible_files):
            raise ReplayCampaignInputError(
                "gold roots must exclude campaign, policy, contract, prompt, and context files"
            )
    for evaluator in campaign.evaluators:
        for evaluation in evaluator.evaluations:
            for workspace in workspaces:
                if _contains(workspace, evaluation.cwd) or _contains(
                    evaluation.cwd, workspace
                ):
                    raise ReplayCampaignInputError(
                        "hidden evaluator roots and gold commands must be outside every "
                        "replay workspace"
                    )
            if not any(_contains(root, evaluation.cwd) for root in evaluator.gold_roots):
                raise ReplayCampaignInputError(
                    "hidden evaluator cwd must be contained by its engine-only gold root"
                )
            for argument in evaluation.specification.argv:
                candidate = Path(argument)
                if not candidate.is_absolute() or not candidate.is_file():
                    continue
                try:
                    resolved = candidate.resolve(strict=False)
                except (OSError, RuntimeError, ValueError) as exc:
                    raise ReplayCampaignInputError(
                        "gold command locator could not be normalized"
                    ) from exc
                if any(
                    _contains(workspace, resolved) or _contains(resolved, workspace)
                    for workspace in workspaces
                ):
                    raise ReplayCampaignInputError(
                        "gold command locators must be outside every replay workspace"
                    )


def _resolve_directory(parent: Path, value: str, label: str) -> Path:
    candidate = _join_locator(parent, value)
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ReplayCampaignInputError(f"{label} could not be resolved") from exc
    if not resolved.is_dir():
        raise ReplayCampaignInputError(f"{label} must be a directory")
    return resolved


def _contains(parent: Path, child: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True
