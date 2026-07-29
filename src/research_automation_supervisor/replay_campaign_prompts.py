"""Model-visible Stage 5A supervisor requests with no gold-derived evidence."""

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
        "gold_evidence": "withheld_until_terminal_and_never_model_visible",
    }
    policy = campaign.supervisor_policy.content.decode("utf-8")
    content = (
        "You are the one persistent historical-replay supervisor.\n"
        "The manifest and Stage 2 engine are authoritative and immutable. "
        "Do not request contract, scope, permission, acceptance-test, or convention changes.\n"
        f"Return action {request.action!r}, unless judgment is genuinely required, in which "
        "case return 'human_pause'. Prompt actions contain only an advisory task body; "
        "the Stage 2 engine supplies the complete authoritative worker or auditor wrapper. "
        "Terminal actions must use an empty prompt.\n"
        "In referenced_paths, include only concrete paths permitted by the output schema; "
        "omit contextual files outside frozen allowed/protected path authority.\n"
        "Never mention, infer, or request hidden/gold evaluation material.\n\n"
        "[BEGIN SUPERVISOR POLICY]\n"
        + policy
        + "\n[END SUPERVISOR POLICY]\n"
        "[BEGIN VISIBLE REPLAY EVIDENCE]\n"
        + json.dumps(
            evidence,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n[END VISIBLE REPLAY EVIDENCE]\n"
        "Return only one JSON object satisfying the engine-owned output schema.\n"
    ).encode("utf-8")
    return RenderedSupervisorRequest(
        content=content,
        sha256=hashlib.sha256(content).hexdigest(),
        byte_count=len(content),
        output_schema=output_schema,
        visible_evidence=evidence,
    )


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
