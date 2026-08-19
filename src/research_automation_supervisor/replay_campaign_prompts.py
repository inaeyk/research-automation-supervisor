"""Model-visible supervisor requests built only from campaign authority."""

from __future__ import annotations

import glob
import hashlib
import json
import shlex
import stat
from dataclasses import dataclass
from pathlib import Path

from research_automation_supervisor.replay_campaign_models import SupervisorAction
from research_automation_supervisor.replay_campaign_sources import (
    PreparedReplayCampaign,
    PreparedReplayTask,
)
from research_automation_supervisor.structured_outputs import normalize_production_schema
from research_automation_supervisor.workflow_engine import WorkflowPromptRequest
from research_automation_supervisor.workflow_models import path_matches_any


@dataclass(frozen=True)
class RenderedSupervisorRequest:
    """Exact in-memory supervisor request and safe durable metadata."""

    content: bytes
    sha256: str
    byte_count: int
    output_schema: dict[str, object]
    visible_evidence: dict[str, object]
    already_sent_authority_ledger: dict[str, str]
    repeated_material_block_count: int


def material_prompt_delta(
    authority_blocks: dict[str, object],
    already_sent_authority: dict[str, str],
) -> tuple[dict[str, object], list[dict[str, str]], dict[str, str], int]:
    """Partition canonical authority into changed bodies and stable path/hash references."""
    ledger = dict(already_sent_authority)
    delta_blocks: dict[str, object] = {}
    refs: list[dict[str, str]] = []
    repeated = 0
    for path, value in authority_blocks.items():
        rendered_value = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        digest = hashlib.sha256(rendered_value.encode("utf-8")).hexdigest()
        refs.append({"path": path, "sha256": digest})
        if already_sent_authority.get(path) == digest:
            repeated += 1
        else:
            delta_blocks[path] = value
        ledger[path] = digest
    return delta_blocks, refs, dict(sorted(ledger.items())), repeated


def load_already_sent_authority_ledger(
    decisions_root: Path,
    *,
    exclude_action_id: str,
) -> dict[str, str]:
    """Recover the exact sent-block ledger without depending on session memory."""
    ledger: dict[str, str] = {}
    if not decisions_root.is_dir():
        return ledger
    for request_path in sorted(decisions_root.glob("*/request.json")):
        if request_path.parent.name == exclude_action_id:
            continue
        try:
            value = json.loads(request_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        candidate = value.get("already_sent_authority_ledger") if isinstance(value, dict) else None
        if not isinstance(candidate, dict):
            continue
        for path, digest in candidate.items():
            if (
                isinstance(path, str)
                and isinstance(digest, str)
                and len(digest) == 64
                and all(character in "0123456789abcdef" for character in digest)
            ):
                ledger[path] = digest
    return ledger


def build_supervisor_action_schema(
    task: PreparedReplayTask,
) -> dict[str, object]:
    """Constrain model-owned checks to the complete frozen acceptance authority."""
    schema = SupervisorAction.model_json_schema()
    properties = schema["properties"]
    assert isinstance(properties, dict)
    required_checks = properties["required_checks"]
    assert isinstance(required_checks, dict)
    referenced_paths = properties["referenced_paths"]
    assert isinstance(referenced_paths, dict)
    tests = task.stage2.acceptance_tests
    allowed_values = list(
        dict.fromkeys(
            (
                *(test.specification.id for test in tests),
                *(shlex.join(test.specification.argv) for test in tests),
            )
        )
    )
    required_checks.update(
        {
            "minItems": len(tests),
            "maxItems": len(tests),
            "items": {
                "type": "string",
                "enum": allowed_values,
            },
        }
    )
    reference_candidates = _supervisor_reference_candidates(task)
    if reference_candidates:
        referenced_paths["items"] = {
            "type": "string",
            "enum": list(reference_candidates),
        }
    return normalize_production_schema(schema)


def _supervisor_reference_candidates(
    task: PreparedReplayTask,
) -> tuple[str, ...]:
    """Enumerate only concrete references accepted by frozen task authority."""
    specification = task.stage2.specification
    candidates = [
        path
        for path in (
            *specification.allowed_paths,
            *specification.protected_paths,
        )
        if not glob.has_magic(path)
    ]
    workspace = task.stage2.workspace
    for candidate in sorted(workspace.rglob("*")):
        try:
            relative = candidate.relative_to(workspace).as_posix()
            status = candidate.lstat()
        except (OSError, ValueError):
            continue
        if (
            stat.S_ISREG(status.st_mode)
            and not stat.S_ISLNK(status.st_mode)
            and path_matches_any(relative, specification.protected_paths)
            and _has_no_symlink_parent(workspace, candidate)
        ):
            candidates.append(relative)
    return tuple(dict.fromkeys(candidates))
def _has_no_symlink_parent(workspace: Path, candidate: Path) -> bool:
    current = workspace
    try:
        for part in candidate.relative_to(workspace).parts[:-1]:
            current = current / part
            if stat.S_ISLNK(current.lstat().st_mode):
                return False
    except OSError:
        return False
    return True


def build_supervisor_request(
    campaign: PreparedReplayCampaign,
    task: PreparedReplayTask,
    request: WorkflowPromptRequest,
    *,
    already_sent_authority: dict[str, str] | None = None,
) -> RenderedSupervisorRequest:
    """Build one supervisor turn from visible authority and Stage 2 evidence."""
    output_schema = build_supervisor_action_schema(task)
    evidence: dict[str, object] = {
        "campaign": {
            "campaign_id": campaign.specification.campaign_id,
            "title": campaign.specification.title,
        },
        "requested_action": request.action,
        "task_authority": task.authority_summary(),
        "persistent_sessions": {
            "supervisor": True,
            "worker_thread_id": request.worker_thread_id,
            "auditor": "fresh_ephemeral_each_round",
        },
        "repair_round": request.repair_round,
        "repair_trigger": request.repair_trigger,
        "contract": task.stage2.contract.content.decode("utf-8"),
        "project_context": [
            {
                "path": str(context.path),
                "content": context.content.decode("utf-8"),
            }
            for context in task.contexts
        ],
        "stage2_evidence": {
            "worker_result": _read_optional_json(request.latest_worker_result_path),
            "auditor_result": _read_optional_json(request.latest_audit_result_path),
            "git_scope": _read_optional_json(request.latest_git_evidence_path),
            "fixed_tests": _read_optional_json(request.latest_tests_path),
            "final_diff": _read_patch(request.latest_git_evidence_path),
        },
    }
    evidence = _portable_supervisor_evidence(evidence, campaign, request)
    authority_blocks: dict[str, object] = {
        "supervisor-policy": campaign.supervisor_policy.content.decode("utf-8"),
        f"task:{task.specification.task_id}:contract": evidence["contract"],
        f"task:{task.specification.task_id}:authority": evidence["task_authority"],
    }
    contexts = evidence["project_context"]
    assert isinstance(contexts, list)
    for index, context in enumerate(contexts):
        assert isinstance(context, dict)
        authority_blocks[f"task:{task.specification.task_id}:context:{index}"] = context
    stage2_evidence = evidence["stage2_evidence"]
    assert isinstance(stage2_evidence, dict)
    for name, value in stage2_evidence.items():
        if value is not None:
            authority_blocks[f"task:{task.specification.task_id}:evidence:{name}"] = value
    delta_blocks, refs, ledger, repeated = material_prompt_delta(
        authority_blocks,
        already_sent_authority or {},
    )
    dynamic_delta = {
        "campaign": evidence["campaign"],
        "requested_action": evidence["requested_action"],
        "persistent_sessions": evidence["persistent_sessions"],
        "repair_round": evidence["repair_round"],
        "repair_trigger": evidence["repair_trigger"],
        "stage2_evidence": "see authority refs and new_or_changed_authority",
        "new_or_changed_authority": delta_blocks,
    }
    content = (
        "Goal\n"
        f"Return {request.action!r}; use human_pause only when judgment is genuinely required.\n\n"
        "Delta\n"
        + json.dumps(
            dynamic_delta,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n\nAuthority refs\n"
        + json.dumps(refs, separators=(",", ":"), sort_keys=True)
        + "\nUnchanged hashes were already sent in this persistent session and are not repeated. "
        "The manifest and Stage 2 engine remain immutable authority.\n\n"
        "Validation\nReturn one JSON object satisfying the engine-owned schema. Referenced paths "
        "must "
        "be concrete schema-permitted paths. Prompt bodies are advisory; Stage 2 supplies the "
        "complete authoritative wrapper.\n\n"
        "Stop\nTerminal actions use an empty prompt. At a task boundary or budget exhaustion, stop "
        "with a compact durable handoff; qualified recovery retains its original session "
        "identity.\n"
    ).encode("utf-8")
    return RenderedSupervisorRequest(
        content=content,
        sha256=hashlib.sha256(content).hexdigest(),
        byte_count=len(content),
        output_schema=output_schema,
        visible_evidence=evidence,
        already_sent_authority_ledger=ledger,
        repeated_material_block_count=repeated,
    )


def _portable_supervisor_evidence(
    value: object,
    campaign: PreparedReplayCampaign,
    request: WorkflowPromptRequest,
) -> dict[str, object]:
    """Keep persistent Codex rollout state free of host workspace/run locators."""
    replacements = [
        (
            str(prepared_task.stage2.repository_root),
            f"<TASK_WORKSPACE:{prepared_task.specification.task_id}>",
        )
        for prepared_task in campaign.tasks
    ]
    replacements.append(
        (str(campaign.visible_package_root), "<VISIBLE_CAMPAIGN>")
    )
    replacements.append((str(request.run_directory), "<STAGE2_RUN>"))
    replacements.sort(key=lambda item: len(item[0]), reverse=True)

    def normalize(item: object) -> object:
        if isinstance(item, dict):
            return {str(key): normalize(child) for key, child in item.items()}
        if isinstance(item, list):
            return [normalize(child) for child in item]
        if isinstance(item, str):
            for locator, replacement in replacements:
                item = item.replace(locator, replacement)
        return item

    normalized = normalize(value)
    assert isinstance(normalized, dict)
    return normalized


def _read_optional_json(path: Path | None) -> object:
    if path is None:
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {"unavailable": True}
    return value


def _read_patch(git_evidence_path: Path | None) -> str | None:
    evidence = _read_optional_json(git_evidence_path)
    if not isinstance(evidence, dict):
        return None
    artifact = evidence.get("patch_artifact")
    if not isinstance(artifact, str):
        return None
    try:
        return Path(artifact).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
