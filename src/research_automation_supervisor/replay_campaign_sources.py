"""Frozen input loading for visible-only replay campaigns."""

from __future__ import annotations

import hashlib
import os
import stat
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
    load_substage_specification,
)

_OFFLINE_ONLY_COMPONENTS = frozenset(
    {
        "engine-only",
        "evaluation-config",
        "evaluators",
        "exact-reference",
        "gold",
        "hidden-evaluators",
        "hidden-tests",
        "offline-evaluation",
    }
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
class PreparedReplayCampaign:
    """Resolved immutable visible-campaign manifest."""

    specification_locator_path: Path
    specification_path: Path
    specification_bytes: bytes
    specification_sha256: str
    specification: ReplayCampaignSpecification
    visible_package_root: Path
    supervisor_policy: FrozenShadowFile
    tasks: tuple[PreparedReplayTask, ...]
    sensitive_values: tuple[str, ...]

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
    """Read once and validate only visible campaign authority."""
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
    visible_package_root = _visible_authority_path(
        parent,
        specification.visible_package_root,
        parent,
        "visible package root",
        expected="directory",
    )
    _require_beneath_visible_root(
        resolved,
        visible_package_root,
        "campaign specification",
        expected="file",
    )
    try:
        _visible_authority_path(
            parent,
            specification.supervisor_policy_path,
            visible_package_root,
            "replay supervisor policy",
            expected="file",
        )
        for task_specification in specification.tasks:
            _visible_authority_path(
                parent,
                task_specification.stage2_specification_path,
                visible_package_root,
                f"task {task_specification.task_id} Stage 2 specification",
                expected="file",
            )
            for index, context_path in enumerate(
                task_specification.project_context_paths
            ):
                _visible_authority_path(
                    parent,
                    context_path,
                    visible_package_root,
                    f"task {task_specification.task_id} project context {index + 1}",
                    expected="file",
                )
        policy = _load_shadow_file(
            parent,
            specification.supervisor_policy_path,
            "replay supervisor policy",
        )
        tasks = tuple(
            _prepare_task(
                parent,
                task,
                visible_package_root=visible_package_root,
                sensitive_values=sensitive_values,
                require_clean=require_clean,
            )
            for task in specification.tasks
        )
    except (ShadowInputError, WorkflowInputError) as exc:
        raise ReplayCampaignInputError(str(exc)) from exc
    prepared = PreparedReplayCampaign(
        specification_locator_path=locator,
        specification_path=resolved,
        specification_bytes=content,
        specification_sha256=hashlib.sha256(content).hexdigest(),
        specification=specification,
        visible_package_root=visible_package_root,
        supervisor_policy=policy,
        tasks=tasks,
        sensitive_values=sensitive_values,
    )
    for prepared_task in prepared.tasks:
        if _contains(
            prepared_task.stage2.repository_root,
            prepared.specification_path,
        ):
            raise ReplayCampaignInputError(
                "campaign manifest must remain outside every task workspace"
            )
    _validate_visible_campaign_layout(prepared)
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
    visible_package_root: Path,
    sensitive_values: tuple[str, ...],
    require_clean: bool,
) -> PreparedReplayTask:
    stage2 = load_substage_specification(
        _join_locator(parent, task.stage2_specification_path),
        sensitive_values=sensitive_values,
        require_clean=require_clean,
        visible_authority_root=visible_package_root,
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
    return PreparedReplayTask(
        specification=task,
        stage2=stage2,
        contexts=contexts,
    )


def _validate_visible_campaign_layout(campaign: PreparedReplayCampaign) -> None:
    """Reject any offline-evaluation authority inside the visible package."""
    campaign_root = campaign.visible_package_root
    try:
        for _current, directories, files in os.walk(campaign_root, followlinks=False):
            names = (*directories, *files)
            for name in names:
                normalized = name.casefold().replace("_", "-")
                if normalized in _OFFLINE_ONLY_COMPONENTS:
                    raise ReplayCampaignInputError(
                        "visible campaign package contains offline-evaluation material"
                    )
    except ReplayCampaignInputError:
        raise
    except OSError as exc:
        raise ReplayCampaignInputError(
            "visible campaign package could not be leak-scanned"
        ) from exc


def _visible_authority_path(
    parent: Path,
    value: str,
    root: Path,
    label: str,
    *,
    expected: str,
) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = parent / candidate
    return _require_beneath_visible_root(
        candidate,
        root,
        label,
        expected=expected,
    )


def _require_beneath_visible_root(
    candidate: Path,
    root: Path,
    label: str,
    *,
    expected: str,
) -> Path:
    try:
        absolute = Path(os.path.abspath(candidate))
        resolved = candidate.resolve(strict=True)
        canonical_root = root.resolve(strict=True)
        resolved.relative_to(canonical_root)
        status = resolved.lstat()
    except (OSError, RuntimeError, ValueError) as exc:
        raise ReplayCampaignInputError(
            f"{label} must remain inside visible campaign authority"
        ) from exc
    if absolute != resolved:
        raise ReplayCampaignInputError(
            f"{label} must not traverse a symlink or alternate path"
        )
    if expected == "file" and not stat.S_ISREG(status.st_mode):
        raise ReplayCampaignInputError(f"{label} must be a regular file")
    if expected == "directory" and not stat.S_ISDIR(status.st_mode):
        raise ReplayCampaignInputError(f"{label} must be a directory")
    return resolved


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
