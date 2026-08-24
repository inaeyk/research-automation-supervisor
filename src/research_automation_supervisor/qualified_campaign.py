"""Qualified core ingress used by the low-privilege Campaign Custodian.

This module, not the Custodian, translates a frozen input bundle into existing
visible-campaign authority, invokes PA-5A recovery, validates human responses, and
projects verified durable outcomes into operator-safe language.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import zipfile
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from research_automation_supervisor.core_authority_client import CoreAuthorityClient
from research_automation_supervisor.custodian_errors import (
    CustodianEnvironmentError,
    QualifiedCampaignInputError,
    QualifiedCampaignStateError,
)
from research_automation_supervisor.custodian_exchange import (
    load_human_action_request,
    load_human_action_response,
    prepare_operator_exchange,
    publish_human_action_request,
)
from research_automation_supervisor.custodian_models import (
    CampaignInputBundleV1,
    CampaignResultSummaryV1,
    DurableStateAuthorityV1,
    HumanActionOptionV1,
    HumanActionRequestV1,
    OperatorCampaignProjectionV1,
    SafeEvidenceLinkV1,
    UploadedResponseFileV1,
)
from research_automation_supervisor.durable_state import (
    atomic_write_json,
    render_json_bytes,
)
from research_automation_supervisor.errors import (
    ReplayCampaignError,
    WorkflowError,
)
from research_automation_supervisor.gitless_repository import verify_operator_campaign_workspace
from research_automation_supervisor.managed_codex import (
    MANAGED_CODEX_EXECUTABLE,
    ManagedCodexIdentity,
    ManagedCodexSecurityError,
    verified_managed_codex_home,
    verify_managed_codex_installation,
)
from research_automation_supervisor.replay_campaign_engine import (
    ReplayCampaignServices,
    replay_campaign_status,
    resume_replay_campaign,
    run_replay_campaign,
)
from research_automation_supervisor.replay_campaign_models import ReplayCampaignState
from research_automation_supervisor.safe_git import safe_git_archive_sha256, safe_git_text
from research_automation_supervisor.workflow_engine import substage_status
from research_automation_supervisor.workflow_integrity import sha256_regular_file
from research_automation_supervisor.workflow_recovery import (
    build_recovery_plan,
    execute_recovery_plan,
)

BUNDLE_FILE = "campaign-input-bundle-v1.json"
PROJECTION_FILE = "operator-projection-v1.json"
FAILURE_FILE = "qualified-failure-v1.json"
RESULTS_DIRECTORY = "operator-results"
MAX_ARTIFACT_BYTES = 32 * 1024 * 1024


def start_qualified_launch(
    launch_intent_id: str,
    core: CoreAuthorityClient,
    *,
    expected_campaign_public_id: str,
    authority_directory: Path,
    exchange_root: Path,
) -> OperatorCampaignProjectionV1:
    """Consume only core-frozen launch material, then enter unchanged PA-5C3."""
    _verify_qualified_runtime_pair(str(MANAGED_CODEX_EXECUTABLE))
    material = core.consume_start_intent_for_qualified_launch(
        launch_intent_id,
        expected_campaign_public_id=expected_campaign_public_id,
    )
    expected_authority = authority_directory.parent / material.campaign_public_id
    if authority_directory != expected_authority:
        raise QualifiedCampaignInputError("qualified campaign authority belongs elsewhere")
    return _start_qualified_bundle(
        material.input_bundle,
        authority_directory=authority_directory,
        exchange_root=exchange_root,
    )


def run_qualified_authentication() -> None:
    """Start the approved Codex authentication flow outside Custodian authority."""
    identity = _verified_managed_codex_identity()
    executable = identity.executable
    codex_home = _managed_codex_home()
    try:
        completed = subprocess.run(
            [str(executable), "login"],
            check=False,
            timeout=600,
            cwd="/",
            env={
                "PATH": "/usr/bin:/bin",
                "HOME": "/nonexistent",
                "CODEX_HOME": str(codex_home),
                "LANG": "C.UTF-8",
            },
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise QualifiedCampaignStateError("Codex authentication could not be completed") from exc
    if completed.returncode != 0:
        raise QualifiedCampaignStateError("Codex authentication was not completed")


def _managed_codex_home() -> Path:
    try:
        return verified_managed_codex_home()
    except CustodianEnvironmentError as exc:
        raise QualifiedCampaignInputError(
            "Managed Codex credential storage is unavailable"
        ) from exc


def _verified_managed_codex_identity() -> ManagedCodexIdentity:
    try:
        return verify_managed_codex_installation()
    except ManagedCodexSecurityError as exc:
        raise QualifiedCampaignInputError(
            "Managed Codex installation identity is unavailable"
        ) from exc


def _verify_qualified_runtime_pair(executable: str) -> None:
    identity = _verified_managed_codex_identity()
    if Path(executable) != identity.executable:
        raise QualifiedCampaignInputError("Qualified Codex executable identity changed")
    _managed_codex_home()


def _qualified_replay_services(*, deterministic_token: str | None = None) -> ReplayCampaignServices:
    identity = _verified_managed_codex_identity()
    _managed_codex_home()
    if deterministic_token is None:
        return ReplayCampaignServices(
            codex_executable=str(identity.executable),
            codex_identity_verifier=_verify_qualified_runtime_pair,
        )
    return ReplayCampaignServices(
        codex_executable=str(identity.executable),
        codex_identity_verifier=_verify_qualified_runtime_pair,
        token_factory=lambda: deterministic_token,
    )


class QualifiedCampaignLocatorV1(BaseModel):
    """Core-owned locator that never appears in the browser projection."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    campaign_public_id: str
    bundle_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    visible_manifest: str
    core_run_directory: str
    prepared_workspace: str


def _start_qualified_bundle(
    bundle: CampaignInputBundleV1,
    *,
    authority_directory: Path,
    exchange_root: Path,
) -> OperatorCampaignProjectionV1:
    """Internal bundle ingress; the installed runner never exposes a pathname."""
    authority = _authority_path(authority_directory)
    if authority.exists():
        existing = load_campaign_input_bundle(authority / BUNDLE_FILE)
        if (
            existing.campaign_public_id != bundle.campaign_public_id
            or existing.bundle_sha256 != bundle.bundle_sha256
        ):
            raise QualifiedCampaignInputError("existing campaign Start binding was substituted")
        return qualified_campaign_status(
            authority_directory=authority,
            exchange_root=exchange_root,
        )
    try:
        authority.mkdir(parents=True, exist_ok=False, mode=0o700)
    except OSError as exc:
        raise QualifiedCampaignInputError(
            "qualified campaign directory could not be created"
        ) from exc
    try:
        _write_once(authority / BUNDLE_FILE, render_json_bytes(bundle.model_dump(mode="json")))
        manifest = _materialize_visible_authority(bundle, authority)
        run_directory = _expected_run_directory(bundle, authority)
        locator = QualifiedCampaignLocatorV1(
            campaign_public_id=bundle.campaign_public_id,
            bundle_sha256=bundle.bundle_sha256,
            visible_manifest=str(manifest),
            core_run_directory=str(run_directory),
            prepared_workspace=bundle.repository.prepared_workspace,
        )
        _write_once(
            authority / "qualified-locator-v1.json",
            render_json_bytes(locator.model_dump(mode="json")),
        )
        preparing = _preparing_projection(bundle)
        _persist_projection(authority, preparing)
        services = _qualified_replay_services(
            deterministic_token=bundle.bundle_sha256[:32]
        )
        run_replay_campaign(manifest, runs_dir=authority / "runs", services=services)
        return qualified_campaign_status(
            authority_directory=authority,
            exchange_root=exchange_root,
        )
    except BaseException as exc:
        _persist_failure(authority, exc)
        if isinstance(exc, (QualifiedCampaignInputError, QualifiedCampaignStateError)):
            raise
        if isinstance(exc, (ReplayCampaignError, WorkflowError)):
            raise QualifiedCampaignStateError(str(exc)) from exc
        raise


def verify_qualified_campaign_binding(
    authority_directory: Path,
    *,
    expected_campaign_public_id: str,
    expected_bundle_sha256: str,
) -> None:
    """Bind every later operation back to the core-owned immutable Start."""
    authority = _existing_authority_path(authority_directory)
    bundle = load_campaign_input_bundle(authority / BUNDLE_FILE)
    if (
        bundle.campaign_public_id != expected_campaign_public_id
        or bundle.bundle_sha256 != expected_bundle_sha256
    ):
        raise QualifiedCampaignInputError("qualified campaign Start binding was substituted")


def qualified_campaign_status(
    *,
    authority_directory: Path,
    exchange_root: Path,
) -> OperatorCampaignProjectionV1:
    """Rebuild a UI projection only from verified bundle and durable core state."""
    authority = _existing_authority_path(authority_directory)
    bundle = load_campaign_input_bundle(authority / BUNDLE_FILE)
    locator = _load_locator(authority, bundle)
    run_directory = Path(locator.core_run_directory)
    if not run_directory.exists():
        failure = _load_failure(authority)
        projection = (
            _blocked_projection(bundle, failure[0], failure[1])
            if failure is not None
            else _preparing_projection(bundle)
        )
        _persist_projection(authority, projection)
        return projection
    try:
        state = replay_campaign_status(run_directory)
    except (ReplayCampaignError, WorkflowError):
        projection = _blocked_projection(
            bundle,
            "verified_status_unavailable",
            "Campaign evidence could not be verified. No recovery was attempted.",
        )
        _persist_projection(authority, projection)
        return projection
    projection = _project_verified_state(bundle, authority, state, exchange_root)
    _persist_projection(authority, projection)
    return projection


def qualified_campaign_repository(
    *,
    authority_directory: Path,
    exchange_root: Path,
) -> Path:
    """Return the verified prepared repository without trusting a Custodian locator."""
    del exchange_root
    authority = _existing_authority_path(authority_directory)
    bundle = load_campaign_input_bundle(authority / BUNDLE_FILE)
    locator = _load_locator(authority, bundle)
    return _verified_workspace(bundle) if locator.prepared_workspace else Path()


def resume_qualified_campaign(
    *,
    authority_directory: Path,
    exchange_root: Path,
) -> OperatorCampaignProjectionV1:
    """Delegate interrupted child recovery to PA-5A, then resume the outer campaign."""
    authority = _existing_authority_path(authority_directory)
    bundle = load_campaign_input_bundle(authority / BUNDLE_FILE)
    locator = _load_locator(authority, bundle)
    state = replay_campaign_status(Path(locator.core_run_directory))
    if state.status == "human_paused":
        return _project_verified_state(bundle, authority, state, exchange_root)
    if state.status != "running":
        return _project_verified_state(bundle, authority, state, exchange_root)
    if state.current_task_run is not None:
        plan = build_recovery_plan(Path(state.current_task_run))
        if plan.disposition in {"auto_resume", "finish_finalization"}:
            outcome = execute_recovery_plan(plan).outcome
            if outcome.status in {"blocked", "failed"}:
                projection = _blocked_projection(
                    bundle,
                    outcome.reason_code,
                    (
                        "Qualified recovery stopped safely. Review the requested "
                        "action before continuing."
                    ),
                )
                _persist_projection(authority, projection)
                return projection
        elif plan.disposition == "blocked":
            if plan.process_reconciliation == "active_matching":
                return _running_projection(bundle, state)
            projection = _blocked_projection(
                bundle,
                plan.reason_code,
                "Qualified recovery could not prove that continuing is safe.",
            )
            _persist_projection(authority, projection)
            return projection
        elif plan.disposition == "reopen_pause":
            projection = _blocked_projection(
                bundle,
                plan.reason_code,
                "The qualified workflow pause must be reopened by its existing core path.",
            )
            _persist_projection(authority, projection)
            return projection
    try:
        resume_replay_campaign(
            Path(locator.core_run_directory),
            services=_qualified_replay_services(),
        )
    except (ReplayCampaignError, WorkflowError) as exc:
        _persist_failure(authority, exc)
    return qualified_campaign_status(authority_directory=authority, exchange_root=exchange_root)


def apply_qualified_human_response(
    *,
    authority_directory: Path,
    exchange_root: Path,
    request_sha256: str,
) -> OperatorCampaignProjectionV1:
    """Validate an exchange response and pass it through the existing core ingress."""
    authority = _existing_authority_path(authority_directory)
    bundle = load_campaign_input_bundle(authority / BUNDLE_FILE)
    locator = _load_locator(authority, bundle)
    run_directory = Path(locator.core_run_directory)
    state = replay_campaign_status(run_directory)
    if state.status != "human_paused":
        raise QualifiedCampaignInputError("campaign is no longer waiting for human input")
    paths = prepare_operator_exchange(exchange_root, bundle.campaign_public_id)
    request = load_human_action_request(paths, request_sha256)
    response = load_human_action_response(paths, request)
    current = _durable_authority(state, run_directory)
    if (
        response.campaign_public_id != bundle.campaign_public_id
        or request.campaign_public_id != bundle.campaign_public_id
        or response.request_id != request.request_id
        or response.request_sha256 != request.request_sha256
        or response.input_bundle_sha256 != bundle.bundle_sha256
        or request.input_bundle_sha256 != bundle.bundle_sha256
        or response.durable_authority != current
        or request.durable_authority != current
    ):
        raise QualifiedCampaignInputError("human response binding is stale or belongs elsewhere")
    allowed = {item.option_id for item in request.allowed_options}
    if allowed:
        if response.selected_option_id not in allowed:
            raise QualifiedCampaignInputError("human response selection is not allowed")
    elif response.selected_option_id is not None:
        raise QualifiedCampaignInputError("this human response does not accept a choice")
    if request.response_type == "free_text" and not response.response_text.strip():
        raise QualifiedCampaignInputError("this human response requires text")
    if request.response_type == "file_upload" and not response.uploaded_files:
        raise QualifiedCampaignInputError("this human response requires a file")
    upload_manifest = _verify_response_uploads(paths.root, response.uploaded_files)
    decision = "abort" if response.selected_option_id == "stop_safely" else "resume"
    note = response.response_text.strip()
    if response.selected_option_id == "request_more_evidence":
        note = (
            "Provide additional relevant evidence before asking for a scientific decision.\n\n"
            + note
        )
    if not note:
        note = (
            "Stop this campaign safely."
            if decision == "abort"
            else "Continue under the existing frozen contract and plan."
        )
    if upload_manifest:
        note = note + "\n\nValidated operator uploads:\n" + "\n".join(upload_manifest)
    if len(note) > 16_384:
        raise QualifiedCampaignInputError("human response and upload manifest are too large")
    decision_path = authority / "accepted-operator-decisions" / f"{request.request_sha256}.json"
    _write_once(
        decision_path,
        render_json_bytes({"schema_version": 1, "decision": decision, "note": note}),
    )
    resume_replay_campaign(
        run_directory,
        decision_path=decision_path,
        services=_qualified_replay_services(),
    )
    return qualified_campaign_status(authority_directory=authority, exchange_root=exchange_root)


def read_qualified_safe_artifact(
    *,
    authority_directory: Path,
    exchange_root: Path,
    token: str,
) -> tuple[str, bytes]:
    """Return only a currently allowlisted operator-safe file after status verification."""
    projection = qualified_campaign_status(
        authority_directory=authority_directory,
        exchange_root=exchange_root,
    )
    authority = _existing_authority_path(authority_directory)
    bundle = load_campaign_input_bundle(authority / BUNDLE_FILE)
    locator = _load_locator(authority, bundle)
    state = replay_campaign_status(Path(locator.core_run_directory))
    registry = _artifact_registry(bundle, authority, state)
    entry = registry.get(token)
    if entry is None or token not in {
        item.token
        for item in (*projection.result_links, *(_request_links(exchange_root, projection)))
    }:
        raise QualifiedCampaignInputError("safe result link is unavailable")
    media_type, path = entry
    content = _read_regular_file(path, "safe result", max_bytes=MAX_ARTIFACT_BYTES)
    return media_type, content


def export_qualified_campaign_bundle(
    *,
    authority_directory: Path,
    exchange_root: Path,
    destination: Path,
) -> Path:
    """Export verified results without logs, sessions, scorer authority, or hidden files."""
    projection = qualified_campaign_status(
        authority_directory=authority_directory,
        exchange_root=exchange_root,
    )
    if not projection.completion_verified:
        raise QualifiedCampaignInputError("only a verified completed campaign can be exported")
    authority = _existing_authority_path(authority_directory)
    bundle = load_campaign_input_bundle(authority / BUNDLE_FILE)
    locator = _load_locator(authority, bundle)
    run_directory = Path(locator.core_run_directory)
    replay_campaign_status(run_directory)
    destination = Path(os.path.abspath(destination))
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise QualifiedCampaignInputError("campaign export destination already exists")
    sources: list[tuple[Path, str]] = [(authority / BUNDLE_FILE, BUNDLE_FILE)]
    results = authority / RESULTS_DIRECTORY
    sources.extend((path, f"results/{path.name}") for path in _regular_files(results))
    for name in ("campaign-report.json",):
        path = run_directory / name
        if path.is_file() and not path.is_symlink():
            sources.append((path, f"verified-campaign/{name}"))
    candidate = run_directory / "final-candidate"
    for path in _regular_files_recursive(candidate):
        relative = path.relative_to(candidate).as_posix()
        sources.append((path, f"verified-campaign/final-candidate/{relative}"))
    try:
        with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_DEFLATED) as archive:
            for path, archive_name in sources:
                archive.writestr(
                    archive_name,
                    _read_regular_file(path, "export artifact", max_bytes=MAX_ARTIFACT_BYTES),
                )
    except OSError as exc:
        raise QualifiedCampaignStateError("campaign bundle could not be exported") from exc
    return destination


def load_campaign_input_bundle(path: Path) -> CampaignInputBundleV1:
    """Load one regular, self-hashed bundle without accepting symlink substitution."""
    content = _read_regular_file(path, "campaign input bundle", max_bytes=64 * 1024 * 1024)
    try:
        value = json.loads(content.decode("utf-8"))
        return CampaignInputBundleV1.model_validate(value)
    except (UnicodeDecodeError, json.JSONDecodeError, ValidationError, ValueError) as exc:
        raise QualifiedCampaignInputError("campaign input bundle is invalid") from exc


def _materialize_visible_authority(bundle: CampaignInputBundleV1, authority: Path) -> Path:
    workspace = _verified_workspace(bundle)
    visible_root = workspace.parent
    control = visible_root / ".research-supervisor-control"
    if control.exists():
        raise QualifiedCampaignInputError("qualified visible-control directory already exists")
    control.mkdir(mode=0o700)
    _write_once(control / "contract.md", bundle.research_contract.content_bytes())
    _write_once(control / "research-plan.md", bundle.research_plan.content_bytes())
    _write_once(control / "initial-task.md", bundle.initial_task.content_bytes())
    supporting_directory = control / "supporting"
    supporting_directory.mkdir(mode=0o700)
    for item in bundle.supporting_files:
        _write_once(supporting_directory / item.display_name, item.content_bytes())
    _write_once(control / "worker-repair.md", _worker_repair_policy())
    _write_once(control / "auditor.md", _auditor_policy())
    _write_once(control / "supervisor-policy.md", _supervisor_policy())
    acceptance_name = _acceptance_profile(bundle, workspace)

    archive_sha256 = _git_archive_sha256(workspace, authority)
    source_commit = _git(workspace, "rev-parse", "HEAD")
    source_tree = _git(workspace, "rev-parse", "HEAD^{tree}")
    task_id = _task_identifier(bundle.campaign_public_id)
    initial_prompt = (
        bundle.initial_task.content_bytes()
        + b"\n\nThe frozen research plan is available to the campaign supervisor. "
        + b"Work only under the frozen contract and editable-area authority.\n"
    )
    _replace_exact(control / "initial-task.md", initial_prompt)
    substage = {
        "schema_version": 1,
        "substage_id": task_id,
        "title": bundle.human_name,
        "workspace": "../repository",
        "contract_path": "contract.md",
        "worker_initial_prompt_path": "initial-task.md",
        "worker_repair_prompt_path": "worker-repair.md",
        "auditor_prompt_path": "auditor.md",
        "worker_model": bundle.requested_settings.worker_model,
        "worker_reasoning_effort": bundle.requested_settings.worker_reasoning_effort,
        "worker_timeout_seconds": 3600,
        "auditor_model": bundle.requested_settings.auditor_model,
        "auditor_reasoning_effort": bundle.requested_settings.auditor_reasoning_effort,
        "auditor_timeout_seconds": 3600,
        "acceptance_tests": [
            {
                "id": "repository-acceptance",
                "argv": [
                    "/usr/bin/python3",
                    ".research-supervisor/acceptance.py",
                    acceptance_name,
                ],
                "cwd": "../repository",
                "timeout_seconds": 3600,
                "max_stdout_bytes": 10 * 1024 * 1024,
                "max_stderr_bytes": 10 * 1024 * 1024,
            }
        ],
        "allowed_paths": list(bundle.requested_settings.editable_areas),
        "protected_paths": [".research-supervisor/**"],
        "max_repair_rounds": bundle.requested_settings.max_repair_rounds,
        "checkpoint_after": False,
    }
    _write_once(control / "substage.json", render_json_bytes(substage))
    control_name = control.name
    contexts = [f"{control_name}/research-plan.md"] + [
        f"{control_name}/supporting/{item.display_name}" for item in bundle.supporting_files
    ]
    manifest = {
        "schema_version": 1,
        "campaign_id": bundle.campaign_public_id,
        "title": bundle.human_name,
        "visible_package_root": ".",
        "supervisor_policy_path": f"{control_name}/supervisor-policy.md",
        "supervisor_model": bundle.requested_settings.supervisor_model,
        "supervisor_reasoning_effort": bundle.requested_settings.supervisor_reasoning_effort,
        "supervisor_timeout_seconds": 3600,
        "tasks": [
            {
                "task_id": task_id,
                "title": bundle.human_name,
                "stage2_specification_path": f"{control_name}/substage.json",
                "project_context_paths": contexts,
                "source_provenance": {
                    "repository_id": bundle.repository.repository_id,
                    "source_commit": source_commit,
                    "source_tree": source_tree,
                    "baseline_archive_sha256": archive_sha256,
                },
                "production_profile": {
                    "hot_path": list(bundle.requested_settings.editable_areas),
                    "post_update": [],
                    "validation_only": [],
                },
            }
        ],
    }
    manifest_path = visible_root / "campaign.json"
    _write_once(manifest_path, render_json_bytes(manifest))
    for path in control.rglob("*"):
        if path.is_file():
            path.chmod(0o400)
    manifest_path.chmod(0o400)
    return manifest_path


def _verified_workspace(bundle: CampaignInputBundleV1) -> Path:
    path = Path(bundle.repository.prepared_workspace)
    try:
        absolute = Path(os.path.abspath(path))
        resolved = path.resolve(strict=True)
        status = resolved.lstat()
    except (OSError, RuntimeError, ValueError) as exc:
        raise QualifiedCampaignInputError("prepared repository is unavailable") from exc
    if absolute != resolved or stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
        raise QualifiedCampaignInputError("prepared repository path is unsafe")
    binding = _read_json_file(
        resolved.parent / "snapshot-binding-v1.json", "sanitized snapshot binding"
    )
    if (
        binding.get("schema_version") != 1
        or binding.get("campaign_public_id") != bundle.campaign_public_id
        or binding.get("bundle_sha256") != bundle.bundle_sha256
        or binding.get("baseline_commit") != bundle.repository.baseline_commit
        or binding.get("baseline_tree") != bundle.repository.baseline_tree
        or not isinstance(binding.get("snapshot_id"), str)
        or re.fullmatch(r"[0-9a-f]{64}", str(binding.get("snapshot_id"))) is None
    ):
        raise QualifiedCampaignInputError("prepared repository snapshot binding is invalid")
    verified_binding = verify_operator_campaign_workspace(
        resolved,
    )
    if (
        verified_binding.get("campaign_public_id") != bundle.campaign_public_id
        or verified_binding.get("bundle_sha256") != bundle.bundle_sha256
        or verified_binding.get("snapshot_id") != binding["snapshot_id"]
        or verified_binding.get("baseline_commit") != bundle.repository.baseline_commit
        or verified_binding.get("baseline_tree") != bundle.repository.baseline_tree
    ):
        raise QualifiedCampaignInputError("prepared repository snapshot binding is invalid")
    if _git(resolved, "rev-parse", "HEAD") != bundle.repository.baseline_commit:
        raise QualifiedCampaignInputError("prepared repository commit changed before Start")
    if _git(resolved, "rev-parse", "HEAD^{tree}") != bundle.repository.baseline_tree:
        raise QualifiedCampaignInputError("prepared repository tree changed before Start")
    if _git(resolved, "status", "--porcelain", "--untracked-files=normal"):
        raise QualifiedCampaignInputError("prepared repository changed before Start")
    return resolved


def _acceptance_profile(bundle: CampaignInputBundleV1, workspace: Path) -> str:
    selected = bundle.requested_settings.profile
    if selected != "standard":
        return selected
    if (workspace / "tests").is_dir() and any(
        (workspace / name).is_file() for name in ("pyproject.toml", "pytest.ini", "setup.cfg")
    ):
        return "python_pytest"
    return "repository_integrity"


def _worker_repair_policy() -> bytes:
    return (
        b"Repair only the validated findings supplied by the qualified workflow. "
        b"Do not change frozen contract, scope, acceptance, or scientific conventions.\n"
    )


def _auditor_policy() -> bytes:
    return (
        b"Independently audit the Worker changes against the exact frozen contract, plan, "
        b"scope, and deterministic evidence. Escalate uncertainty to the human; never infer "
        b"a scientific convention.\n"
    )


def _supervisor_policy() -> bytes:
    return (
        b"Sequence the frozen visible campaign through the qualified Worker and fresh Auditor. "
        b"Do not alter authority, answer human gates, access hidden evaluation, or declare "
        b"completion without the deterministic engine.\n"
    )


def _project_verified_state(
    bundle: CampaignInputBundleV1,
    authority: Path,
    state: ReplayCampaignState,
    exchange_root: Path,
) -> OperatorCampaignProjectionV1:
    if state.status == "completed":
        results, links = _materialize_operator_results(bundle, authority, state)
        return OperatorCampaignProjectionV1(
            campaign_public_id=bundle.campaign_public_id,
            human_name=bundle.human_name,
            repository=bundle.repository.source_display,
            status="completed",
            stage="Complete",
            last_activity="Qualified completion evidence and final candidate were verified",
            human_input_needed=False,
            campaign_state_safe=True,
            result=results,
            result_links=links,
            completion_verified=True,
        )
    if state.status == "human_paused":
        request = _issue_request(bundle, authority, state, exchange_root)
        return OperatorCampaignProjectionV1(
            campaign_public_id=bundle.campaign_public_id,
            human_name=bundle.human_name,
            repository=bundle.repository.source_display,
            status="needs_input",
            stage=_stage_label(bundle, state),
            last_activity="Campaign paused at a verified human decision boundary",
            human_input_needed=True,
            campaign_state_safe=True,
            action_title="Research Supervisor needs input",
            action_message=request.reason,
            technical_code=_safe_identifier(state.pause_reason or "human_review_required"),
            active_request_sha256=request.request_sha256,
        )
    if state.status == "running":
        return _running_projection(bundle, state)
    return _blocked_projection(
        bundle,
        _safe_identifier(state.pause_reason or f"campaign_{state.status}"),
        "Campaign stopped safely before verified completion.",
    )


def _running_projection(
    bundle: CampaignInputBundleV1,
    state: ReplayCampaignState,
) -> OperatorCampaignProjectionV1:
    activity = "Qualified campaign is preparing its next action"
    if state.current_task_run:
        try:
            child = substage_status(Path(state.current_task_run))
            activity = {
                "initialized": "Preparing the Worker",
                "worker_running": "Worker implementing the current research task",
                "scope_checking": "Checking changed files against frozen scope",
                "tests_running": "Running the frozen acceptance checks",
                "auditor_running": "Auditor reviewing Worker changes",
                "repair_pending": "Worker preparing a bounded repair",
                "human_paused": "Waiting at a verified human decision boundary",
                "repair_limit_paused": "Repair limit reached; human review is required",
                "checkpoint_paused": "Campaign reached its qualified checkpoint",
                "completed": "Current task passed its qualified checks",
                "failed": "Current task stopped safely",
                "aborted": "Current task was stopped by the operator",
            }.get(child.status, activity)
        except WorkflowError:
            activity = "Verifying the current campaign boundary"
    return OperatorCampaignProjectionV1(
        campaign_public_id=bundle.campaign_public_id,
        human_name=bundle.human_name,
        repository=bundle.repository.source_display,
        status="running",
        stage=_stage_label(bundle, state),
        last_activity=activity,
        human_input_needed=False,
        campaign_state_safe=True,
    )


def _preparing_projection(bundle: CampaignInputBundleV1) -> OperatorCampaignProjectionV1:
    return OperatorCampaignProjectionV1(
        campaign_public_id=bundle.campaign_public_id,
        human_name=bundle.human_name,
        repository=bundle.repository.source_display,
        status="preparing",
        stage="Preparing campaign",
        last_activity="Verifying the repository and frozen inputs",
        human_input_needed=False,
        campaign_state_safe=True,
    )


def _blocked_projection(
    bundle: CampaignInputBundleV1,
    technical_code: str,
    message: str,
) -> OperatorCampaignProjectionV1:
    return OperatorCampaignProjectionV1(
        campaign_public_id=bundle.campaign_public_id,
        human_name=bundle.human_name,
        repository=bundle.repository.source_display,
        status="blocked",
        stage="Paused safely",
        last_activity="Qualified core stopped before taking another campaign action",
        human_input_needed=False,
        campaign_state_safe=True,
        action_title="Campaign paused safely",
        action_message=message,
        technical_code=_safe_identifier(technical_code),
    )


def _issue_request(
    bundle: CampaignInputBundleV1,
    authority: Path,
    state: ReplayCampaignState,
    exchange_root: Path,
) -> HumanActionRequestV1:
    run_directory = _load_locator(authority, bundle).core_run_directory
    durable = _durable_authority(state, Path(run_directory))
    links = _human_evidence_links(bundle, authority, state)
    reason = {
        "worker_requires_human": (
            "The Worker found a question that the frozen inputs do not answer."
        ),
        "auditor_requires_human": (
            "The Auditor found a decision that requires human scientific authority."
        ),
        "supervisor_requires_human": (
            "The campaign cannot continue without an authorized human decision."
        ),
        "repair_limit": "The bounded repair limit was reached without a verified pass.",
    }.get(state.pause_reason or "", "The qualified workflow reached a human decision boundary.")
    request = HumanActionRequestV1.issue(
        campaign_public_id=bundle.campaign_public_id,
        input_bundle_sha256=bundle.bundle_sha256,
        stage=_stage_label(bundle, state),
        substage=_task_identifier(bundle.campaign_public_id),
        request_id=f"human-{state.journal_sequence:06d}",
        reason=reason,
        question=_exact_human_question(state),
        response_type="contract_decision",
        allowed_options=[
            HumanActionOptionV1(
                option_id="continue_existing",
                label="Continue under the existing frozen authority",
                consequence=(
                    "The exact note is passed to the qualified core; frozen inputs do not change."
                ),
            ).model_dump(mode="json"),
            HumanActionOptionV1(
                option_id="request_more_evidence",
                label="Request additional evidence",
                consequence=(
                    "The qualified workflow is asked to gather evidence before another decision."
                ),
            ).model_dump(mode="json"),
            HumanActionOptionV1(
                option_id="stop_safely",
                label="Stop this campaign safely",
                consequence=(
                    "The campaign is aborted through the existing core human-decision ingress."
                ),
            ).model_dump(mode="json"),
        ],
        evidence_links=[item.model_dump(mode="json") for item in links],
        campaign_state_safe=True,
        durable_authority=durable.model_dump(mode="json"),
    )
    paths = prepare_operator_exchange(exchange_root, bundle.campaign_public_id)
    destination = paths.requests / f"{request.request_sha256}.json"
    if destination.exists():
        existing = load_human_action_request(paths, request.request_sha256)
        if existing != request:
            raise QualifiedCampaignStateError("human-action request was replaced")
    else:
        publish_human_action_request(paths, request)
    return request


def _durable_authority(state: ReplayCampaignState, run_directory: Path) -> DurableStateAuthorityV1:
    return DurableStateAuthorityV1(
        authority_kind="visible_campaign",
        state_sha256=sha256_regular_file(run_directory / "state.json"),
        journal_sha256=sha256_regular_file(run_directory / "journal.jsonl"),
        journal_sequence=state.journal_sequence,
        journal_hash=state.journal_hash,
        frozen_policy_sha256=state.specification_sha256,
    )


def _exact_human_question(state: ReplayCampaignState) -> str:
    """Project an exact validated Worker/Auditor question when one is available."""
    if state.current_task_run is None:
        return "What should the qualified campaign do at this boundary?"
    run = Path(state.current_task_run)
    questions: list[str] = []
    for pattern, field in (
        ("worker/*.structured.json", "questions"),
        ("audits/*.structured.json", "human_questions"),
    ):
        for path in sorted(run.glob(pattern), reverse=True):
            value = _read_json_file(path, "validated human question")
            candidates = value.get(field)
            if isinstance(candidates, list):
                questions.extend(
                    item.strip() for item in candidates if isinstance(item, str) and item.strip()
                )
            if questions:
                break
        if questions:
            break
    if not questions:
        return "What should the qualified campaign do at this boundary?"
    return "\n\n".join(dict.fromkeys(questions))[:16_384]


def _verify_response_uploads(
    exchange_directory: Path,
    uploads: tuple[UploadedResponseFileV1, ...],
) -> tuple[str, ...]:
    """Re-verify exchange upload bytes in qualified core before response ingress."""
    manifest: list[str] = []
    root = exchange_directory.resolve(strict=True)
    upload_root = (root / "uploads").resolve(strict=True)
    for uploaded in uploads:
        path = root / uploaded.exchange_path
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(upload_root)
        except (OSError, RuntimeError, ValueError) as exc:
            raise QualifiedCampaignInputError("human response upload escaped its exchange") from exc
        content = _read_regular_file(
            resolved,
            "human response upload",
            max_bytes=MAX_ARTIFACT_BYTES,
        )
        if (
            len(content) != uploaded.byte_count
            or hashlib.sha256(content).hexdigest() != uploaded.sha256
        ):
            raise QualifiedCampaignInputError("human response upload changed")
        manifest.append(
            f"- {uploaded.display_name}: sha256 {uploaded.sha256}, {uploaded.byte_count} bytes"
        )
    return tuple(manifest)


def _human_evidence_links(
    bundle: CampaignInputBundleV1,
    authority: Path,
    state: ReplayCampaignState,
) -> tuple[SafeEvidenceLinkV1, ...]:
    registry = _artifact_registry(bundle, authority, state)
    labels = {
        "human-review": ("Review packet", "Safe campaign explanation and relevant evidence"),
        "worker-reports": ("Worker report", "Validated structured Worker reports"),
        "auditor-reports": ("Auditor report", "Validated structured Auditor reports"),
    }
    links: list[SafeEvidenceLinkV1] = []
    for kind, (label, description) in labels.items():
        token = _artifact_token(bundle, kind)
        if token in registry:
            links.append(SafeEvidenceLinkV1(token=token, label=label, description=description))
    return tuple(links)


def _materialize_operator_results(
    bundle: CampaignInputBundleV1,
    authority: Path,
    state: ReplayCampaignState,
) -> tuple[CampaignResultSummaryV1, tuple[SafeEvidenceLinkV1, ...]]:
    locator = _load_locator(authority, bundle)
    run_directory = Path(locator.core_run_directory)
    replay_campaign_status(run_directory)
    results = authority / RESULTS_DIRECTORY
    results.mkdir(exist_ok=True, mode=0o700)
    task_reports = tuple(sorted((run_directory / "tasks").glob("*/task-report.json")))
    reports = [_read_json_file(path, "task report") for path in task_reports]
    worker_reports = [item for report in reports for item in _list_value(report, "worker_reports")]
    auditor_reports = [
        item for report in reports for item in _list_value(report, "auditor_reports")
    ]
    repair_count = sum(_integer_value(report, "repair_rounds") for report in reports)
    final_diffs = [report.get("final_diff") for report in reports if report.get("final_diff")]
    _replace_json(results / "worker-reports.json", worker_reports)
    _replace_json(results / "auditor-reports.json", auditor_reports)
    _replace_text(
        results / "changed-files-and-diff.txt",
        "\n\n".join(str(item) for item in final_diffs) or "No changed-file diff was reported.\n",
    )
    provenance = {
        "schema_version": 1,
        "campaign": bundle.campaign_public_id,
        "input_bundle_sha256": bundle.bundle_sha256,
        "repository_baseline_commit": bundle.repository.baseline_commit,
        "candidate_manifest_sha256": state.candidate_manifest_sha256,
        "completion_verified": True,
        "note": "Detailed journal and proof internals remain in the qualified campaign directory.",
    }
    _replace_json(results / "provenance.json", provenance)
    summary_text = (
        f"# {bundle.human_name}\n\n"
        "Outcome: Completed with verified durable campaign evidence.\n\n"
        f"Repository: {bundle.repository.source_display}\n\n"
        "The final candidate was published only after the qualified Worker/Auditor workflow "
        "and completion evidence passed core verification.\n"
    )
    _replace_text(results / "scientific-report.md", summary_text)
    final_commit = _final_commit_if_clean(Path(bundle.repository.prepared_workspace))
    result = CampaignResultSummaryV1(
        outcome="Completed with verified durable evidence",
        final_stage="Qualified completion",
        final_commit=final_commit,
        worker_run_count=len(worker_reports),
        auditor_run_count=len(auditor_reports),
        repair_count=repair_count,
        human_decision_count=state.human_decision_count,
        executive_summary=(
            "The qualified campaign completed under its frozen contract, plan, task, scope, "
            "acceptance profile, and bounded repair authority."
        ),
    )
    link_specs = (
        ("scientific-report", "Scientific Report", "Concise verified campaign outcome"),
        ("worker-reports", "Worker Reports", "Validated structured Worker results"),
        ("auditor-reports", "Auditor Reports", "Validated structured Auditor results"),
        ("changed-diff", "Changed Files / Diff", "Final changed-file evidence"),
        ("provenance", "Provenance", "Frozen input and candidate identities"),
    )
    links = tuple(
        SafeEvidenceLinkV1(
            token=_artifact_token(bundle, kind),
            label=label,
            description=description,
        )
        for kind, label, description in link_specs
    )
    return result, links


def _artifact_registry(
    bundle: CampaignInputBundleV1,
    authority: Path,
    state: ReplayCampaignState,
) -> dict[str, tuple[str, Path]]:
    locator = _load_locator(authority, bundle)
    run = Path(locator.core_run_directory)
    results = authority / RESULTS_DIRECTORY
    registry: dict[str, tuple[str, Path]] = {}
    entries = {
        "scientific-report": ("text/markdown; charset=utf-8", results / "scientific-report.md"),
        "worker-reports": ("application/json; charset=utf-8", results / "worker-reports.json"),
        "auditor-reports": ("application/json; charset=utf-8", results / "auditor-reports.json"),
        "changed-diff": ("text/plain; charset=utf-8", results / "changed-files-and-diff.txt"),
        "provenance": ("application/json; charset=utf-8", results / "provenance.json"),
        "human-review": ("text/markdown; charset=utf-8", run / "human-review-packet.md"),
    }
    for kind, entry in entries.items():
        path = entry[1]
        if path.is_file() and not path.is_symlink():
            registry[_artifact_token(bundle, kind)] = entry
    if state.status == "human_paused" and state.current_task_run:
        child = Path(state.current_task_run)
        paused_reports = {
            "worker-reports": child / "operator-worker-reports.json",
            "auditor-reports": child / "operator-auditor-reports.json",
        }
        for kind, destination in paused_reports.items():
            pattern = (
                "worker/*.structured.json"
                if kind == "worker-reports"
                else "audits/*.structured.json"
            )
            values = [_read_json_file(path, kind) for path in sorted(child.glob(pattern))]
            _replace_json(destination, values)
            registry[_artifact_token(bundle, kind)] = (
                "application/json; charset=utf-8",
                destination,
            )
    return registry


def _request_links(
    exchange_root: Path, projection: OperatorCampaignProjectionV1
) -> tuple[SafeEvidenceLinkV1, ...]:
    if projection.active_request_sha256 is None:
        return ()
    paths = prepare_operator_exchange(exchange_root, projection.campaign_public_id)
    request = load_human_action_request(paths, projection.active_request_sha256)
    return request.evidence_links


def _stage_label(bundle: CampaignInputBundleV1, state: ReplayCampaignState) -> str:
    task = _human_milestone(bundle)
    if state.current_task_index == 0:
        return f"Implementing {task}"
    return "Finalizing verified results"


def _human_milestone(bundle: CampaignInputBundleV1) -> str:
    task = bundle.initial_task.content_bytes().decode("utf-8", errors="replace")
    match = re.search(r"\b[A-Za-z]+\d+(?:-[A-Za-z0-9]+)+\b", task)
    if match is not None:
        return match.group(0)
    return bundle.human_name[:200]


def _expected_run_directory(bundle: CampaignInputBundleV1, authority: Path) -> Path:
    return authority / "runs" / f"{bundle.campaign_public_id}-{bundle.bundle_sha256[:32]}"


def _task_identifier(campaign_public_id: str) -> str:
    suffix = campaign_public_id.removeprefix("campaign-")
    return f"task-{suffix}"[:80]


def _artifact_token(bundle: CampaignInputBundleV1, kind: str) -> str:
    return hashlib.sha256(f"{bundle.bundle_sha256}:{kind}".encode("ascii")).hexdigest()[:32]


def _safe_identifier(value: str) -> str:
    normalized = "".join(
        character if character.isalnum() or character in "_-" else "_" for character in value
    )
    normalized = normalized.strip("_") or "campaign_blocked"
    if not normalized[0].isalnum():
        normalized = f"status_{normalized}"
    return normalized[:80]


def _authority_path(path: Path) -> Path:
    return Path(os.path.abspath(path))


def _existing_authority_path(path: Path) -> Path:
    absolute = _authority_path(path)
    try:
        resolved = path.resolve(strict=True)
        status = resolved.lstat()
    except (OSError, RuntimeError, ValueError) as exc:
        raise QualifiedCampaignInputError("qualified campaign directory is unavailable") from exc
    if absolute != resolved or stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
        raise QualifiedCampaignInputError("qualified campaign directory is unsafe")
    return resolved


def _load_locator(authority: Path, bundle: CampaignInputBundleV1) -> QualifiedCampaignLocatorV1:
    value = _read_json_file(authority / "qualified-locator-v1.json", "qualified campaign locator")
    try:
        locator = QualifiedCampaignLocatorV1.model_validate(value)
    except ValidationError as exc:
        raise QualifiedCampaignStateError("qualified campaign locator is invalid") from exc
    expected_workspace = Path(bundle.repository.prepared_workspace)
    expected_manifest = expected_workspace.parent / "campaign.json"
    if (
        locator.campaign_public_id != bundle.campaign_public_id
        or locator.bundle_sha256 != bundle.bundle_sha256
        or Path(locator.core_run_directory) != _expected_run_directory(bundle, authority)
        or Path(locator.visible_manifest) != expected_manifest
        or Path(locator.prepared_workspace) != expected_workspace
    ):
        raise QualifiedCampaignStateError("qualified campaign locator was substituted")
    return locator


def _git(workspace: Path, *arguments: str) -> str:
    return safe_git_text(workspace, *arguments)


def _git_archive_sha256(workspace: Path, authority: Path) -> str:
    return safe_git_archive_sha256(workspace, authority)


def _write_once(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
        try:
            offset = 0
            while offset < len(content):
                offset += os.write(descriptor, content[offset:])
            os.fchmod(descriptor, 0o400)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except FileExistsError as exc:
        raise QualifiedCampaignStateError("qualified create-once artifact already exists") from exc
    except OSError as exc:
        raise QualifiedCampaignStateError(
            "qualified create-once artifact could not be written"
        ) from exc


def _replace_exact(path: Path, content: bytes) -> None:
    if not path.is_file() or path.is_symlink():
        raise QualifiedCampaignStateError("qualified input materialization is inconsistent")
    path.chmod(0o600)
    try:
        path.write_bytes(content)
    except OSError as exc:
        raise QualifiedCampaignStateError("qualified input could not be materialized") from exc


def _replace_json(path: Path, value: object) -> None:
    atomic_write_json(
        path,
        value,
        error_factory=QualifiedCampaignStateError,
        error_message="operator result could not be written",
    )


def _replace_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(value, encoding="utf-8")
        os.replace(temporary, path)
    except OSError as exc:
        raise QualifiedCampaignStateError("operator result could not be written") from exc
    finally:
        temporary.unlink(missing_ok=True)


def _persist_projection(authority: Path, projection: OperatorCampaignProjectionV1) -> None:
    atomic_write_json(
        authority / PROJECTION_FILE,
        projection.model_dump(mode="json"),
        error_factory=QualifiedCampaignStateError,
        error_message="operator projection could not be written",
    )


def _persist_failure(authority: Path, exc: BaseException) -> None:
    if not authority.is_dir():
        return
    reason = _safe_identifier(type(exc).__name__)
    message = (
        str(exc)[:1024]
        if isinstance(
            exc,
            (
                QualifiedCampaignInputError,
                QualifiedCampaignStateError,
                ReplayCampaignError,
                WorkflowError,
            ),
        )
        else "Qualified campaign operation failed before verified completion."
    )
    _replace_json(
        authority / FAILURE_FILE, {"schema_version": 1, "reason_code": reason, "message": message}
    )


def _load_failure(authority: Path) -> tuple[str, str] | None:
    path = authority / FAILURE_FILE
    if not path.exists():
        return None
    value = _read_json_file(path, "qualified failure record")
    code = value.get("reason_code")
    message = value.get("message")
    if not isinstance(code, str) or not isinstance(message, str):
        raise QualifiedCampaignStateError("qualified failure record is invalid")
    return code, message


def _read_regular_file(path: Path, label: str, *, max_bytes: int) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or opened.st_size > max_bytes:
                raise OSError
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(descriptor, 64 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise OSError
                chunks.append(chunk)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise QualifiedCampaignStateError(f"{label} is unavailable or unsafe") from exc
    return b"".join(chunks)


def _read_json_file(path: Path, label: str) -> dict[str, object]:
    try:
        value = json.loads(
            _read_regular_file(path, label, max_bytes=MAX_ARTIFACT_BYTES).decode("utf-8")
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise QualifiedCampaignStateError(f"{label} is malformed") from exc
    if not isinstance(value, dict):
        raise QualifiedCampaignStateError(f"{label} is malformed")
    return value


def _regular_files(directory: Path) -> tuple[Path, ...]:
    if not directory.is_dir() or directory.is_symlink():
        return ()
    return tuple(
        sorted(path for path in directory.iterdir() if path.is_file() and not path.is_symlink())
    )


def _regular_files_recursive(directory: Path) -> tuple[Path, ...]:
    if not directory.is_dir() or directory.is_symlink():
        return ()
    values: list[Path] = []
    for root, directories, files in os.walk(directory, followlinks=False):
        root_path = Path(root)
        directories[:] = [name for name in directories if not (root_path / name).is_symlink()]
        for name in files:
            path = root_path / name
            if path.is_file() and not path.is_symlink():
                values.append(path)
    return tuple(sorted(values))


def _list_value(value: dict[str, object], key: str) -> list[object]:
    item = value.get(key)
    return item if isinstance(item, list) else []


def _integer_value(value: dict[str, object], key: str) -> int:
    item = value.get(key)
    return item if isinstance(item, int) and not isinstance(item, bool) and item >= 0 else 0


def _final_commit_if_clean(workspace: Path) -> str | None:
    return (
        _git(workspace, "rev-parse", "HEAD")
        if not _git(workspace, "status", "--porcelain")
        else None
    )
