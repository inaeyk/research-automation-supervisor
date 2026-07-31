"""Authoritative host-side replay through a prepared campaign's original evaluator.

This module is intentionally independent of campaign engines and model adapters.  It
never starts a Supervisor, Worker, Auditor, Codex process, or model integration.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal

import yaml  # type: ignore[import-untyped]

from research_automation_supervisor import __version__

REPORT_SCHEMA_VERSION = 1
DEFAULT_TIMEOUT_SECONDS = 3600
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_GIT_ENVIRONMENT = {
    "GIT_ATTR_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_NO_REPLACE_OBJECTS": "1",
    "GIT_OPTIONAL_LOCKS": "0",
    "LC_ALL": "C",
}
_MODEL_STOP_WARNING = (
    "Stop and confirm that every Supervisor, Worker, Auditor, Codex, and other "
    "model process has exited before using real protected evaluation material."
)


class DirectReplayError(RuntimeError):
    """Base error for direct historical replay."""


class DirectReplayInputError(DirectReplayError):
    """Raised when replay inputs or output authority are invalid."""


class DirectReplayInfrastructureError(DirectReplayError):
    """Raised when a disposable replay workspace cannot be prepared."""


ReplayStatus = Literal[
    "passed",
    "functional_failure",
    "evaluator_infrastructure_failure",
    "no_structured_result",
]


@dataclass(frozen=True)
class CandidateAuthority:
    """Verified immutable visible-campaign candidate metadata."""

    root: Path
    campaign_id: str
    snapshot_id: str
    manifest_sha256: str
    tasks: Mapping[str, Mapping[str, object]]
    task_order: tuple[str, ...]


@dataclass(frozen=True)
class PreparedTaskAuthority:
    """One original historical functional evaluator mapping."""

    task_id: str
    workspace: Path
    baseline_commit: str
    baseline_tree: str
    source_commit: str
    evaluator: Path
    python: Path
    fixture_root: Path
    evaluator_cwd: Path
    candidate_task: Mapping[str, object]


@dataclass(frozen=True)
class RepositoryAuthority:
    """One qualified source/dependency repository named by preparation evidence."""

    name: str
    root: Path
    head: str
    tree: str


@dataclass(frozen=True)
class PreparedCampaignAuthority:
    """Validated host-side authority from a preserved prepared campaign."""

    root: Path
    campaign_id: str
    campaign_manifest_sha256: str
    preparation_report_sha256: str
    source_state_sha256: str
    evaluator_sha256: str
    tasks: tuple[PreparedTaskAuthority, ...]
    repositories: tuple[RepositoryAuthority, ...]


def run_direct_historical_replay(
    candidate: Path,
    prepared_campaign: Path,
    output: Path,
    *,
    keep_workspaces: bool = False,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> tuple[Path, dict[str, object]]:
    """Run the original functional evaluator against disposable task workspaces."""
    if not 1 <= timeout_seconds <= 86_400:
        raise DirectReplayInputError("timeout must be between 1 and 86400 seconds")
    candidate_authority = _verify_candidate(candidate)
    prepared_authority = _load_prepared_campaign(
        prepared_campaign,
        candidate_authority,
    )
    destination = _new_output_directory(
        output,
        forbidden=(
            candidate_authority.root,
            prepared_authority.root,
            *(repository.root for repository in prepared_authority.repositories),
        ),
    )
    before_candidate = _candidate_fingerprint(candidate_authority)
    before_prepared = _prepared_fingerprint(prepared_authority)
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{destination.name}.staging-",
            dir=destination.parent,
        )
    )
    published = False
    try:
        staging.chmod(0o700)
        tasks_output = staging / "tasks"
        tasks_output.mkdir(mode=0o700)
        workspaces = staging / ("workspaces" if keep_workspaces else ".workspaces")
        workspaces.mkdir(mode=0o700)
        task_results = [
            _run_task(
                task,
                candidate_authority,
                tasks_output,
                workspaces,
                timeout_seconds=timeout_seconds,
                keep_workspace=keep_workspaces,
            )
            for task in prepared_authority.tasks
        ]
        if not keep_workspaces:
            _remove_disposable_tree(workspaces)

        input_immutability_verified = _inputs_unchanged(
            candidate_authority,
            prepared_authority,
            before_candidate,
            before_prepared,
        )
        report = _build_report(
            candidate_authority,
            prepared_authority,
            task_results,
            keep_workspaces=keep_workspaces,
            input_immutability_verified=input_immutability_verified,
        )
        _write_json(staging / "report.json", report)
        _write_bytes(staging / "summary.md", _markdown_summary(report).encode("utf-8"))
        _make_report_private(staging)
        if destination.exists() or destination.is_symlink():
            raise DirectReplayInputError("output directory was created during replay")
        os.replace(staging, destination)
        published = True
        return destination, report
    finally:
        if not published and staging.exists():
            _remove_disposable_tree(staging)


def report_exit_code(report: Mapping[str, object]) -> int:
    """Map a completed report to a stable process exit status."""
    status = report.get("evaluation_status")
    if status == "passed":
        return 0
    if status == "functional_failure":
        return 1
    if status == "no_structured_result":
        return 4
    return 3


def _verify_candidate(path: Path) -> CandidateAuthority:
    root = _exact_directory(path, "candidate")
    manifest_path = root / "candidate-manifest.json"
    manifest = _read_json(manifest_path, "candidate manifest")
    required = {
        "schema_version",
        "snapshot_id",
        "campaign_id",
        "entries",
        "candidate_manifest_sha256",
    }
    campaign_id = manifest.get("campaign_id")
    snapshot_id = manifest.get("snapshot_id")
    claimed = manifest.get("candidate_manifest_sha256")
    if (
        set(manifest) != required
        or manifest.get("schema_version") != 1
        or not isinstance(campaign_id, str)
        or not campaign_id
        or snapshot_id != f"{campaign_id}:candidate-v1"
        or not isinstance(claimed, str)
        or not re.fullmatch(r"[0-9a-f]{64}", claimed)
    ):
        raise DirectReplayInputError("candidate manifest schema or identity is invalid")
    if manifest_path.read_bytes() != _json_bytes(manifest):
        raise DirectReplayInputError("candidate manifest encoding is not canonical")
    payload = {
        key: value
        for key, value in manifest.items()
        if key != "candidate_manifest_sha256"
    }
    if _sha256_json(payload) != claimed:
        raise DirectReplayInputError("candidate manifest digest is invalid")
    if manifest.get("entries") != _package_entries(
        root,
        excluded_manifest="candidate-manifest.json",
    ):
        raise DirectReplayInputError("candidate package changed after finalization")
    _require_sealed_candidate_modes(root)
    record = _read_json(root / "candidate.json", "candidate provenance")
    raw_tasks = record.get("tasks")
    task_order = record.get("task_order")
    if (
        record.get("schema_version") != 1
        or record.get("campaign_id") != campaign_id
        or not isinstance(raw_tasks, list)
        or not isinstance(task_order, list)
    ):
        raise DirectReplayInputError("candidate provenance is invalid")
    tasks: dict[str, Mapping[str, object]] = {}
    for raw in raw_tasks:
        if not isinstance(raw, dict):
            raise DirectReplayInputError("candidate task provenance is invalid")
        task_id = raw.get("task_id")
        if not isinstance(task_id, str) or task_id in tasks:
            raise DirectReplayInputError("candidate task identity is invalid")
        tasks[task_id] = raw
    if task_order != list(tasks) or not tasks:
        raise DirectReplayInputError("candidate task order is invalid")
    return CandidateAuthority(
        root=root,
        campaign_id=campaign_id,
        snapshot_id=str(snapshot_id),
        manifest_sha256=claimed,
        tasks=tasks,
        task_order=tuple(tasks),
    )


def _load_prepared_campaign(
    path: Path,
    candidate: CandidateAuthority,
) -> PreparedCampaignAuthority:
    root = _exact_directory(path, "prepared campaign")
    campaign_path = root / "campaign.yaml"
    preparation_path = root / "preparation-report.json"
    source_state_path = root / "engine-only/historical-audits/source-state.json"
    campaign = _read_yaml(campaign_path, "prepared campaign manifest")
    preparation = _read_json(preparation_path, "prepared campaign report")
    source_state = _read_json(source_state_path, "prepared source state")
    campaign_id = campaign.get("campaign_id")
    raw_tasks = campaign.get("tasks")
    preparation_tasks = preparation.get("tasks")
    if (
        campaign.get("schema_version") != 1
        or not isinstance(campaign_id, str)
        or not campaign_id
        or preparation.get("schema_version") != 1
        or preparation.get("campaign_id") != campaign_id
        or not isinstance(raw_tasks, list)
        or not raw_tasks
        or not isinstance(preparation_tasks, list)
    ):
        raise DirectReplayInputError("prepared campaign authority is incomplete")
    preflight = preparation.get("preflight")
    required_checks = {
        "source_matching_policies_before_and_after",
        "implemented_loader",
        "five_clean_isolated_workspaces",
        "bounded_visible_gold_and_evaluator_leak_scan",
        "all_five_functional_evaluator_proofs",
        "all_five_exact_evaluator_proofs",
    }
    successful_preflight = (
        preparation.get("status") == "prepared_and_preflight_passed"
        and isinstance(preflight, dict)
        and preflight.get("passed") is True
        and preflight.get("blocked_checks") == []
    )
    notification_only_preflight = (
        preparation.get("status")
        == "prepared_with_managed_shell_notification_blocker"
        and isinstance(preflight, dict)
        and preflight.get("passed") is False
        and isinstance(preflight.get("blocked_checks"), list)
        and len(preflight["blocked_checks"]) == 1
        and isinstance(preflight["blocked_checks"][0], dict)
        and preflight["blocked_checks"][0].get("check")
        == "windows_notification_capability"
    )
    passed_checks = (
        set(preflight.get("passed_checks", []))
        if isinstance(preflight, dict)
        and isinstance(preflight.get("passed_checks"), list)
        else set()
    )
    if (
        preparation.get("campaign_started") is not False
        or preparation.get("real_model_invoked") is not False
        or not (successful_preflight or notification_only_preflight)
        or not required_checks <= passed_checks
    ):
        raise DirectReplayInputError(
            "prepared campaign qualification evidence is incomplete"
        )
    preparation_by_id = _records_by_task_id(preparation_tasks, "preparation report")
    task_ids = tuple(_task_id(raw, "campaign task") for raw in raw_tasks)
    if (
        task_ids != tuple(preparation_by_id)
        or task_ids != candidate.task_order
    ):
        raise DirectReplayInputError(
            "prepared campaign and candidate task mappings do not match"
        )
    tasks = tuple(
        _load_prepared_task(
            root,
            raw,
            preparation_by_id[task_id],
            candidate.tasks[task_id],
        )
        for task_id, raw in zip(task_ids, raw_tasks, strict=True)
    )
    evaluator_hashes = {_sha256_file(task.evaluator) for task in tasks}
    evaluator_paths = {task.evaluator for task in tasks}
    if len(evaluator_hashes) != 1 or len(evaluator_paths) != 1:
        raise DirectReplayInputError(
            "historical tasks do not share one original functional evaluator"
        )
    repositories = _load_repositories(source_state)
    return PreparedCampaignAuthority(
        root=root,
        campaign_id=campaign_id,
        campaign_manifest_sha256=_sha256_file(campaign_path),
        preparation_report_sha256=_sha256_file(preparation_path),
        source_state_sha256=_sha256_file(source_state_path),
        evaluator_sha256=next(iter(evaluator_hashes)),
        tasks=tasks,
        repositories=repositories,
    )


def _load_prepared_task(
    root: Path,
    raw: object,
    preparation: Mapping[str, object],
    candidate_task: Mapping[str, object],
) -> PreparedTaskAuthority:
    if not isinstance(raw, dict):
        raise DirectReplayInputError("prepared campaign task is invalid")
    task_id = _task_id(raw, "campaign task")
    if preparation.get("task_id") != task_id or candidate_task.get("task_id") != task_id:
        raise DirectReplayInputError(f"{task_id} task identity is inconsistent")
    stage2_relative = _relative_path(
        raw.get("stage2_specification_path"),
        f"{task_id} Stage 2 specification",
    )
    stage2_path = _beneath(root, stage2_relative, f"{task_id} Stage 2 specification")
    stage2 = _read_yaml(stage2_path, f"{task_id} Stage 2 specification")
    if stage2.get("substage_id") != task_id:
        raise DirectReplayInputError(f"{task_id} Stage 2 identity is invalid")
    workspace = _beneath(
        root,
        f"visible/tasks/{task_id}/workspace",
        f"{task_id} source workspace",
    )
    if not workspace.is_dir() or workspace.is_symlink():
        raise DirectReplayInputError(f"{task_id} source workspace is invalid")
    evaluations = raw.get("gold_evaluations")
    if not isinstance(evaluations, list):
        raise DirectReplayInputError(f"{task_id} historical evaluator mapping is absent")
    functional = [
        value
        for value in evaluations
        if isinstance(value, dict)
        and isinstance(value.get("argv"), list)
        and len(value["argv"]) >= 3
        and value["argv"][2] == "functional"
    ]
    if len(functional) != 1:
        raise DirectReplayInputError(
            f"{task_id} must declare exactly one functional evaluator"
        )
    evaluation = functional[0]
    argv = evaluation["argv"]
    assert isinstance(argv, list)
    if (
        len(argv) != 6
        or not all(isinstance(item, str) and item for item in argv)
        or argv[2] != "functional"
        or argv[3] != task_id
    ):
        raise DirectReplayInputError(f"{task_id} functional evaluator command is invalid")
    python = _executable(Path(argv[0]), f"{task_id} historical Python")
    evaluator = _source_absolute(root, argv[1], f"{task_id} historical evaluator")
    evaluator_root = _beneath(root, "engine-only/evaluators", "evaluator root")
    if (
        not evaluator.is_file()
        or evaluator.is_symlink()
        or not _is_within(evaluator, evaluator_root)
    ):
        raise DirectReplayInputError(f"{task_id} historical evaluator is invalid")
    declared_workspace = _source_absolute(
        root,
        argv[4],
        f"{task_id} declared evaluator workspace",
    )
    fixture_root = _source_absolute(
        root,
        argv[5],
        f"{task_id} historical fixture root",
    )
    expected_fixture = _beneath(
        root,
        f"engine-only/gold/{task_id}",
        f"{task_id} historical fixture root",
    )
    cwd_value = evaluation.get("cwd")
    evaluator_cwd = _source_absolute(
        root,
        cwd_value,
        f"{task_id} evaluator working directory",
    )
    if (
        declared_workspace != workspace
        or fixture_root != expected_fixture
        or evaluator_cwd != fixture_root
        or not fixture_root.is_dir()
        or fixture_root.is_symlink()
    ):
        raise DirectReplayInputError(f"{task_id} evaluator authority is inconsistent")
    baseline_commit = preparation.get("local_baseline_commit")
    source_commit = preparation.get("source_workspace_head")
    if (
        not isinstance(baseline_commit, str)
        or not re.fullmatch(r"[0-9a-f]{40,64}", baseline_commit)
        or not isinstance(source_commit, str)
        or not re.fullmatch(r"[0-9a-f]{40,64}", source_commit)
        or preparation.get("functional_evaluator_proof") is not True
    ):
        raise DirectReplayInputError(f"{task_id} baseline authority is incomplete")
    head = _git(workspace, "rev-parse", "HEAD")
    tree = _git(workspace, "rev-parse", f"{baseline_commit}^{{tree}}")
    if head != baseline_commit:
        raise DirectReplayInputError(f"{task_id} prepared baseline commit changed")
    if _git(workspace, "status", "--porcelain=v1", "-z", "--untracked-files=all"):
        raise DirectReplayInputError(f"{task_id} prepared workspace is not clean")
    if _git(workspace, "rev-list", "--count", "--all") != "1":
        raise DirectReplayInputError(f"{task_id} prepared workspace history is ambiguous")
    provenance = candidate_task.get("source_provenance")
    changes_sha256 = candidate_task.get("changes_sha256")
    if (
        not isinstance(provenance, dict)
        or provenance.get("source_commit") != source_commit
        or provenance.get("source_tree") != tree
        or candidate_task.get("execution_baseline_tree") != tree
        or not isinstance(changes_sha256, str)
        or not re.fullmatch(r"[0-9a-f]{64}", changes_sha256)
    ):
        raise DirectReplayInputError(
            f"{task_id} candidate and prepared baseline provenance do not match"
        )
    return PreparedTaskAuthority(
        task_id=task_id,
        workspace=workspace,
        baseline_commit=baseline_commit,
        baseline_tree=tree,
        source_commit=source_commit,
        evaluator=evaluator,
        python=python,
        fixture_root=fixture_root,
        evaluator_cwd=evaluator_cwd,
        candidate_task=candidate_task,
    )


def _load_repositories(source_state: Mapping[str, object]) -> tuple[RepositoryAuthority, ...]:
    raw_repositories = source_state.get("repositories")
    if source_state.get("schema_version") != 1 or not isinstance(raw_repositories, list):
        raise DirectReplayInputError("prepared source repository authority is invalid")
    repositories: list[RepositoryAuthority] = []
    names: set[str] = set()
    for raw in raw_repositories:
        if not isinstance(raw, dict):
            raise DirectReplayInputError("prepared source repository record is invalid")
        name = raw.get("name")
        path_value = raw.get("path")
        head = raw.get("head")
        if (
            not isinstance(name, str)
            or _IDENTIFIER.fullmatch(name) is None
            or name in names
            or not isinstance(path_value, str)
            or not isinstance(head, str)
            or not re.fullmatch(r"[0-9a-f]{40,64}", head)
            or raw.get("status") != []
        ):
            raise DirectReplayInputError("prepared source repository record is invalid")
        repository = _exact_directory(Path(path_value), f"{name} repository")
        if _git(repository, "rev-parse", "HEAD") != head:
            raise DirectReplayInputError(f"{name} repository commit changed")
        if _git(repository, "status", "--porcelain=v1", "-z", "--untracked-files=all"):
            raise DirectReplayInputError(f"{name} repository is not clean")
        tree = _git(repository, "rev-parse", f"{head}^{{tree}}")
        repositories.append(
            RepositoryAuthority(name=name, root=repository, head=head, tree=tree)
        )
        names.add(name)
    return tuple(repositories)
def _run_task(
    task: PreparedTaskAuthority,
    candidate: CandidateAuthority,
    tasks_output: Path,
    workspaces_root: Path,
    *,
    timeout_seconds: int,
    keep_workspace: bool,
) -> dict[str, object]:
    task_output = tasks_output / task.task_id
    task_output.mkdir(mode=0o700)
    stdout_path = task_output / "stdout.log"
    stderr_path = task_output / "stderr.log"
    workspace_root = workspaces_root / task.task_id
    workspace = workspace_root / "workspace"
    exit_code: int | None = None
    timed_out = False
    setup_error: str | None = None
    try:
        workspace_root.mkdir(mode=0o700)
        workspace.mkdir(mode=0o700)
        _export_baseline(task, workspace, workspace_root)
        _initialize_ephemeral_git_baseline(
            workspace,
            workspace_root / "git-scratch",
            task.baseline_tree,
        )
        _apply_candidate_changes(
            workspace,
            candidate.root / "tasks" / task.task_id,
            task.candidate_task,
        )
        _make_workspace_user_writable(workspace)
        exit_code, timed_out = _execute_evaluator(
            task,
            workspace,
            workspace_root,
            stdout_path,
            stderr_path,
            timeout_seconds,
        )
    except (DirectReplayError, OSError, subprocess.SubprocessError, tarfile.TarError):
        setup_error = "task_setup_or_evaluator_launch_failed"
        if not stdout_path.exists():
            _write_bytes(stdout_path, b"")
        if not stderr_path.exists():
            _write_bytes(stderr_path, b"")
    parse_status, structured = _parse_functional_result(stdout_path, task.task_id)
    status, reason = _classify_task(
        exit_code=exit_code,
        timed_out=timed_out,
        parse_status=parse_status,
        structured=structured,
        setup_error=setup_error,
    )
    structured_artifact = {
        "schema_version": 1,
        "contract": "historical-functional-json-v1",
        "parse_status": parse_status,
        "result": structured,
    }
    _write_json(task_output / "structured-result.json", structured_artifact)
    stdout_record = _stream_record(stdout_path, task_output)
    stderr_record = _stream_record(stderr_path, task_output)
    if not keep_workspace and workspace_root.exists():
        _remove_disposable_tree(workspace_root)
    return {
        "task_id": task.task_id,
        "status": status,
        "reason_code": reason,
        "functional_passed": status == "passed",
        "hidden_acceptance_passed": (
            None if structured is None else structured["hidden_tests_passed"]
        ),
        "visible_acceptance_passed": (
            None if structured is None else structured["visible_tests_passed"]
        ),
        "changed_path_match": (
            None if structured is None else structured["changed_path_match"]
        ),
        "process_exit_code": exit_code,
        "timed_out": timed_out,
        "structured_result_status": parse_status,
        "structured_result": structured,
        "baseline_commit": task.baseline_commit,
        "baseline_tree": task.baseline_tree,
        "source_commit": task.source_commit,
        "candidate_changes_sha256": str(task.candidate_task.get("changes_sha256")),
        "stdout": stdout_record,
        "stderr": stderr_record,
        "structured_result_artifact": (
            f"tasks/{task.task_id}/structured-result.json"
        ),
        "workspace_retained": keep_workspace,
        "workspace_artifact": (
            f"workspaces/{task.task_id}/workspace" if keep_workspace else None
        ),
    }


def _export_baseline(
    task: PreparedTaskAuthority,
    workspace: Path,
    scratch: Path,
) -> None:
    archive = scratch / "baseline.tar"
    environment = {**os.environ, **_GIT_ENVIRONMENT}
    completed = subprocess.run(
        (
            "git",
            "-C",
            str(task.workspace),
            "archive",
            "--format=tar",
            f"--output={archive}",
            task.baseline_commit,
        ),
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        close_fds=True,
        timeout=300,
    )
    if completed.returncode != 0:
        raise DirectReplayInfrastructureError("committed baseline export failed")
    _extract_git_archive(archive, workspace)
    archive.unlink()


def _extract_git_archive(archive: Path, destination: Path) -> None:
    with tarfile.open(archive, mode="r:") as opened:
        members = opened.getmembers()
        for member in members:
            relative = _relative_path(member.name.rstrip("/"), "baseline archive entry")
            target = _change_target(destination, relative)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                target.chmod(stat.S_IMODE(member.mode) & 0o755)
            elif member.isreg():
                target.parent.mkdir(parents=True, exist_ok=True)
                source = opened.extractfile(member)
                if source is None:
                    raise DirectReplayInfrastructureError(
                        "baseline archive entry could not be read"
                    )
                with source, target.open("xb") as output:
                    shutil.copyfileobj(source, output)
                target.chmod(stat.S_IMODE(member.mode) & 0o755)
            elif member.issym():
                target.parent.mkdir(parents=True, exist_ok=True)
                _validate_link_target(destination, target, member.linkname)
                target.symlink_to(member.linkname)
            else:
                raise DirectReplayInfrastructureError(
                    "baseline archive contains an unsupported object"
                )


def _initialize_ephemeral_git_baseline(
    workspace: Path,
    scratch: Path,
    expected_tree: str,
) -> None:
    home = scratch / "home"
    template = scratch / "template"
    xdg = scratch / "xdg"
    for directory in (scratch, home, template, xdg):
        directory.mkdir(mode=0o700, parents=directory == scratch, exist_ok=False)
    environment = {
        **_GIT_ENVIRONMENT,
        "GIT_AUTHOR_DATE": "1970-01-01T00:00:00Z",
        "GIT_AUTHOR_EMAIL": "direct-replay@example.invalid",
        "GIT_AUTHOR_NAME": "Direct Historical Replay",
        "GIT_COMMITTER_DATE": "1970-01-01T00:00:00Z",
        "GIT_COMMITTER_EMAIL": "direct-replay@example.invalid",
        "GIT_COMMITTER_NAME": "Direct Historical Replay",
        "HOME": str(home),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "XDG_CONFIG_HOME": str(xdg),
    }

    def run(*arguments: str, input_bytes: bytes | None = None) -> bytes:
        completed = subprocess.run(
            ("git", "-C", str(workspace), *arguments),
            env=environment,
            input=b"" if input_bytes is None else input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            close_fds=True,
            timeout=120,
        )
        if completed.returncode != 0:
            raise DirectReplayInfrastructureError(
                "ephemeral Git baseline could not be created"
            )
        return completed.stdout

    run("init", "--quiet", "--initial-branch=direct-replay", f"--template={template}")
    attributes = workspace / ".git/info/attributes"
    attributes.parent.mkdir(parents=True, exist_ok=True)
    attributes.write_text(
        "* -crlf -eol -filter -ident -text -working-tree-encoding\n",
        encoding="ascii",
    )
    index_entries = bytearray()
    for path in _walk_objects(workspace):
        relative = path.relative_to(workspace)
        if relative.parts[:1] == (".git",):
            continue
        status = path.lstat()
        if stat.S_ISDIR(status.st_mode):
            continue
        if stat.S_ISREG(status.st_mode):
            content = path.read_bytes()
            mode = "100755" if stat.S_IMODE(status.st_mode) & 0o111 else "100644"
        elif stat.S_ISLNK(status.st_mode):
            content = os.fsencode(os.readlink(path))
            mode = "120000"
        else:
            raise DirectReplayInfrastructureError(
                "baseline contains an unsupported Git object"
            )
        object_id = run(
            "hash-object",
            "-w",
            "--stdin",
            input_bytes=content,
        ).decode("ascii", errors="strict").strip()
        if not re.fullmatch(r"[0-9a-f]{40}", object_id):
            raise DirectReplayInfrastructureError("ephemeral Git object is invalid")
        index_entries.extend(
            f"{mode} {object_id}\t{relative.as_posix()}".encode(
                "utf-8",
                errors="surrogateescape",
            )
        )
        index_entries.append(0)
    run(
        "-c",
        "core.quotePath=false",
        "update-index",
        "-z",
        "--index-info",
        input_bytes=bytes(index_entries),
    )
    if run("write-tree").decode("ascii").strip() != expected_tree:
        raise DirectReplayInfrastructureError("ephemeral Git tree does not match baseline")
    run("commit", "--quiet", "--no-gpg-sign", "--no-verify", "-m", "direct replay baseline")
    if (
        run("rev-parse", "HEAD^{tree}").decode("ascii").strip() != expected_tree
        or run("status", "--porcelain=v1", "-z", "--untracked-files=all")
    ):
        raise DirectReplayInfrastructureError("ephemeral Git baseline is inconsistent")


def _apply_candidate_changes(
    workspace: Path,
    task_root: Path,
    candidate_task: Mapping[str, object],
) -> None:
    record = _read_json(task_root / "changes.json", "candidate changes")
    if record.get("schema_version") != 1 or not isinstance(record.get("entries"), list):
        raise DirectReplayInfrastructureError("candidate changes are invalid")
    if candidate_task.get("changes_sha256") != _sha256_json(record):
        raise DirectReplayInfrastructureError("candidate changes provenance is invalid")
    entries = record["entries"]
    assert isinstance(entries, list)
    for raw in entries:
        if not isinstance(raw, dict):
            raise DirectReplayInfrastructureError("candidate change is invalid")
        relative = _relative_path(raw.get("path"), "candidate change path")
        if PurePosixPath(relative).parts[:1] == (".git",):
            raise DirectReplayInfrastructureError("candidate change targets Git metadata")
        target = _change_target(workspace, relative)
        operation = raw.get("operation")
        if operation == "delete":
            _remove_change_target(target)
            continue
        if operation != "upsert":
            raise DirectReplayInfrastructureError("candidate change operation is invalid")
        target.parent.mkdir(parents=True, exist_ok=True)
        _remove_change_target(target)
        object_type = raw.get("object_type")
        if object_type == "regular":
            source = _change_target(task_root / "changed-files", relative)
            content = source.read_bytes()
            if (
                raw.get("byte_length") != len(content)
                or raw.get("content_sha256") != hashlib.sha256(content).hexdigest()
            ):
                raise DirectReplayInfrastructureError("candidate changed file digest is invalid")
            mode = raw.get("mode")
            if not isinstance(mode, int) or isinstance(mode, bool) or not 0 <= mode <= 0o777:
                raise DirectReplayInfrastructureError("candidate changed file mode is invalid")
            target.write_bytes(content)
            target.chmod(mode)
        elif object_type == "symlink":
            target_value = raw.get("target")
            if not isinstance(target_value, str):
                raise DirectReplayInfrastructureError("candidate symlink target is invalid")
            encoded = os.fsencode(target_value)
            if (
                raw.get("byte_length") != len(encoded)
                or raw.get("content_sha256") != hashlib.sha256(encoded).hexdigest()
            ):
                raise DirectReplayInfrastructureError("candidate symlink digest is invalid")
            _validate_link_target(workspace, target, target_value)
            target.symlink_to(target_value)
        else:
            raise DirectReplayInfrastructureError("candidate changed object type is invalid")


def _make_workspace_user_writable(workspace: Path) -> None:
    """Add owner write access only to the disposable replay workspace."""
    for path in reversed(_walk_objects(workspace)):
        status = path.lstat()
        if stat.S_ISDIR(status.st_mode):
            path.chmod(stat.S_IMODE(status.st_mode) | 0o700)
        elif stat.S_ISREG(status.st_mode):
            path.chmod(stat.S_IMODE(status.st_mode) | 0o600)
    workspace.chmod(stat.S_IMODE(workspace.lstat().st_mode) | 0o700)


def _execute_evaluator(
    task: PreparedTaskAuthority,
    workspace: Path,
    scratch: Path,
    stdout_path: Path,
    stderr_path: Path,
    timeout_seconds: int,
) -> tuple[int | None, bool]:
    home = scratch / "evaluator-home"
    xdg = scratch / "evaluator-xdg"
    home.mkdir(mode=0o700)
    xdg.mkdir(mode=0o700)
    environment = {
        **os.environ,
        **_GIT_ENVIRONMENT,
        "HOME": str(home),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "XDG_CONFIG_HOME": str(xdg),
    }
    command = (
        str(task.python),
        "-B",
        str(task.evaluator),
        "functional",
        task.task_id,
        str(workspace),
        str(task.fixture_root),
    )
    with stdout_path.open("xb") as stdout, stderr_path.open("xb") as stderr:
        stdout_path.chmod(0o600)
        stderr_path.chmod(0o600)
        process = subprocess.Popen(
            command,
            cwd=task.evaluator_cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            close_fds=True,
            start_new_session=True,
        )
        try:
            return process.wait(timeout=timeout_seconds), False
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            return None, True


def _parse_functional_result(
    stdout_path: Path,
    expected_task_id: str,
) -> tuple[str, dict[str, object] | None]:
    final_line = _last_nonempty_line(stdout_path)
    if final_line is None:
        return "absent", None
    try:
        value = json.loads(final_line)
    except json.JSONDecodeError:
        return (
            ("malformed", None)
            if final_line.lstrip().startswith(("{", "["))
            else ("absent", None)
        )
    if not isinstance(value, dict):
        return "malformed", None
    required = {
        "schema_version",
        "evaluation",
        "task_id",
        "passed",
        "hidden_tests_passed",
        "visible_tests_passed",
        "changed_path_match",
        "hidden_runner",
        "visible_runner",
    }
    booleans = (
        "passed",
        "hidden_tests_passed",
        "visible_tests_passed",
        "changed_path_match",
    )
    hidden_runner = value.get("hidden_runner")
    visible_runner = value.get("visible_runner")
    if (
        not required <= set(value)
        or value.get("schema_version") != 1
        or value.get("evaluation") != "functional"
        or value.get("task_id") != expected_task_id
        or not all(isinstance(value.get(field), bool) for field in booleans)
        or not isinstance(hidden_runner, dict)
        or not isinstance(visible_runner, dict)
        or not isinstance(hidden_runner.get("passed"), bool)
        or not isinstance(visible_runner.get("passed"), bool)
        or hidden_runner["passed"] != value["hidden_tests_passed"]
        or visible_runner["passed"] != value["visible_tests_passed"]
        or value["passed"]
        != (
            value["hidden_tests_passed"]
            and value["visible_tests_passed"]
            and value["changed_path_match"]
        )
    ):
        return "malformed", None
    return "parsed", {
        "schema_version": 1,
        "evaluation": "functional",
        "task_id": expected_task_id,
        "passed": value["passed"],
        "hidden_tests_passed": value["hidden_tests_passed"],
        "visible_tests_passed": value["visible_tests_passed"],
        "changed_path_match": value["changed_path_match"],
    }


def _classify_task(
    *,
    exit_code: int | None,
    timed_out: bool,
    parse_status: str,
    structured: Mapping[str, object] | None,
    setup_error: str | None,
) -> tuple[ReplayStatus, str]:
    if setup_error is not None:
        return "evaluator_infrastructure_failure", setup_error
    if timed_out:
        return "evaluator_infrastructure_failure", "evaluator_timeout"
    if parse_status == "absent":
        return "no_structured_result", "historical_functional_contract_absent"
    if parse_status != "parsed" or structured is None:
        return "evaluator_infrastructure_failure", "malformed_structured_result"
    passed = structured.get("passed") is True
    if passed != (exit_code == 0):
        return "evaluator_infrastructure_failure", "contract_exit_inconsistent"
    if passed:
        return "passed", "all_functional_checks_passed"
    return "functional_failure", "one_or_more_functional_checks_failed"


def _build_report(
    candidate: CandidateAuthority,
    prepared: PreparedCampaignAuthority,
    task_results: Sequence[Mapping[str, object]],
    *,
    keep_workspaces: bool,
    input_immutability_verified: bool,
) -> dict[str, object]:
    status_counts = {
        status: sum(result.get("status") == status for result in task_results)
        for status in (
            "passed",
            "functional_failure",
            "evaluator_infrastructure_failure",
            "no_structured_result",
        )
    }
    if not input_immutability_verified or status_counts["evaluator_infrastructure_failure"]:
        evaluation_status = "evaluator_infrastructure_failure"
    elif status_counts["no_structured_result"]:
        evaluation_status = "no_structured_result"
    elif status_counts["functional_failure"]:
        evaluation_status = "functional_failure"
    else:
        evaluation_status = "passed"
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "report_type": "direct_historical_replay",
        "tool": {
            "name": "research-automation-supervisor",
            "version": __version__,
            "command": "run-direct-historical-replay",
        },
        "authority": (
            "Original prepared-campaign historical functional evaluator run directly "
            "on the qualified host after all model processes stop."
        ),
        "experimental_packaged_evaluator_used": False,
        "model_process_warning": _MODEL_STOP_WARNING,
        "candidate": {
            "campaign_id": candidate.campaign_id,
            "snapshot_id": candidate.snapshot_id,
            "manifest_sha256": candidate.manifest_sha256,
        },
        "prepared_campaign": {
            "campaign_id": prepared.campaign_id,
            "campaign_manifest_sha256": prepared.campaign_manifest_sha256,
            "preparation_report_sha256": prepared.preparation_report_sha256,
            "source_state_sha256": prepared.source_state_sha256,
            "evaluator_sha256": prepared.evaluator_sha256,
            "repositories": [
                {
                    "name": repository.name,
                    "head": repository.head,
                    "tree": repository.tree,
                }
                for repository in prepared.repositories
            ],
        },
        "evaluation_status": evaluation_status,
        "input_immutability_verified": input_immutability_verified,
        "workspaces_retained": keep_workspaces,
        "task_order": [task.task_id for task in prepared.tasks],
        "totals": {
            "tasks": len(task_results),
            "functional_passed": status_counts["passed"],
            "functional_failed": status_counts["functional_failure"],
            "evaluator_infrastructure_failed": status_counts[
                "evaluator_infrastructure_failure"
            ],
            "no_structured_result": status_counts["no_structured_result"],
            "exact_match": "not_evaluated_by_this_command",
        },
        "tasks": list(task_results),
    }


def _markdown_summary(report: Mapping[str, object]) -> str:
    tasks = report["tasks"]
    totals = report["totals"]
    assert isinstance(tasks, list)
    assert isinstance(totals, dict)
    lines = [
        "# Direct historical replay",
        "",
        f"Status: `{report['evaluation_status']}`",
        "",
        f"> WARNING: {report['model_process_warning']}",
        "",
        "This report uses the original prepared-campaign functional evaluator directly. ",
        "It does not use the experimental packaged Bubblewrap evaluator and does not ",
        "perform exact historical identity comparison.",
        "",
        "| Task | Status | Hidden | Visible | Paths | Exit |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for raw in tasks:
        assert isinstance(raw, dict)
        lines.append(
            "| "
            + " | ".join(
                (
                    str(raw["task_id"]),
                    str(raw["status"]),
                    _markdown_bool(raw["hidden_acceptance_passed"]),
                    _markdown_bool(raw["visible_acceptance_passed"]),
                    _markdown_bool(raw["changed_path_match"]),
                    str(raw["process_exit_code"]),
                )
            )
            + " |"
        )
    lines.extend(
        (
            "",
            f"Functional total: {totals['functional_passed']}/{totals['tasks']}",
            "",
            f"Input immutability verified: {report['input_immutability_verified']}",
            "",
            "Raw stdout and stderr are private per-task artifacts and may contain untrusted ",
            "evaluator output. Summaries intentionally exclude their contents.",
            "",
        )
    )
    return "\n".join(lines)


def _inputs_unchanged(
    candidate: CandidateAuthority,
    prepared: PreparedCampaignAuthority,
    before_candidate: Mapping[str, object],
    before_prepared: Mapping[str, object],
) -> bool:
    try:
        after_candidate_authority = _verify_candidate(candidate.root)
        after_prepared_authority = _load_prepared_campaign(
            prepared.root,
            after_candidate_authority,
        )
        return (
            before_candidate == _candidate_fingerprint(after_candidate_authority)
            and before_prepared == _prepared_fingerprint(after_prepared_authority)
        )
    except DirectReplayError:
        return False


def _candidate_fingerprint(candidate: CandidateAuthority) -> dict[str, object]:
    return {
        "manifest_sha256": candidate.manifest_sha256,
        "tree_sha256": _tree_fingerprint(candidate.root),
    }


def _prepared_fingerprint(prepared: PreparedCampaignAuthority) -> dict[str, object]:
    return {
        "prepared_tree_sha256": _tree_fingerprint(prepared.root),
        "campaign_manifest_sha256": prepared.campaign_manifest_sha256,
        "preparation_report_sha256": prepared.preparation_report_sha256,
        "source_state_sha256": prepared.source_state_sha256,
        "evaluator_sha256": prepared.evaluator_sha256,
        "tasks": [
            {
                "task_id": task.task_id,
                "baseline_commit": _git(task.workspace, "rev-parse", "HEAD"),
                "baseline_tree": _git(task.workspace, "rev-parse", "HEAD^{tree}"),
                "status_sha256": hashlib.sha256(
                    _git_bytes(
                        task.workspace,
                        "status",
                        "--porcelain=v1",
                        "-z",
                        "--untracked-files=all",
                    )
                ).hexdigest(),
                "fixture_tree_sha256": _tree_fingerprint(task.fixture_root),
            }
            for task in prepared.tasks
        ],
        "repositories": [
            {
                "name": repository.name,
                "head": _git(repository.root, "rev-parse", "HEAD"),
                "tree": _git(repository.root, "rev-parse", "HEAD^{tree}"),
                "status_sha256": hashlib.sha256(
                    _git_bytes(
                        repository.root,
                        "status",
                        "--porcelain=v1",
                        "-z",
                        "--untracked-files=all",
                    )
                ).hexdigest(),
            }
            for repository in prepared.repositories
        ],
    }


def _stream_record(path: Path, task_output: Path) -> dict[str, object]:
    return {
        "artifact": f"tasks/{task_output.name}/{path.name}",
        "byte_length": path.stat().st_size,
        "sha256": _sha256_file(path),
        "content_policy": "private_untrusted_evaluator_output",
    }


def _last_nonempty_line(path: Path, *, limit: int = 1_048_576) -> str | None:
    size = path.stat().st_size
    with path.open("rb") as stream:
        stream.seek(max(0, size - limit))
        tail = stream.read(limit)
    if b"\x00" in tail:
        return None
    lines = [line for line in tail.splitlines() if line.strip()]
    if not lines:
        return None
    try:
        return lines[-1].decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return None


def _records_by_task_id(
    values: Sequence[object],
    label: str,
) -> dict[str, Mapping[str, object]]:
    result: dict[str, Mapping[str, object]] = {}
    for raw in values:
        if not isinstance(raw, dict):
            raise DirectReplayInputError(f"{label} task is invalid")
        task_id = _task_id(raw, f"{label} task")
        if task_id in result:
            raise DirectReplayInputError(f"{label} task identity is duplicated")
        result[task_id] = raw
    return result


def _task_id(raw: object, label: str) -> str:
    if not isinstance(raw, dict):
        raise DirectReplayInputError(f"{label} is invalid")
    value = raw.get("task_id")
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise DirectReplayInputError(f"{label} identity is invalid")
    return value


def _new_output_directory(output: Path, *, forbidden: Sequence[Path]) -> Path:
    if output.exists() or output.is_symlink():
        raise DirectReplayInputError("output directory must not already exist")
    parent = _exact_directory(output.parent, "output parent")
    destination = parent / output.name
    if not output.name or output.name in {".", ".."}:
        raise DirectReplayInputError("output directory name is invalid")
    for root in forbidden:
        if _is_within(destination, root) or _is_within(root, destination):
            raise DirectReplayInputError(
                "output must be disjoint from candidate, prepared campaign, "
                "and qualified source repositories"
            )
    return destination


def _exact_directory(path: Path, label: str) -> Path:
    try:
        status = path.lstat()
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise DirectReplayInputError(f"{label} directory is unavailable") from exc
    if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
        raise DirectReplayInputError(f"{label} must be a non-symlink directory")
    return resolved


def _source_absolute(root: Path, value: object, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise DirectReplayInputError(f"{label} path is invalid")
    raw = Path(value)
    selected = raw if raw.is_absolute() else root / raw
    try:
        resolved = selected.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise DirectReplayInputError(f"{label} is unavailable") from exc
    if not _is_within(resolved, root):
        raise DirectReplayInputError(f"{label} escapes the prepared campaign")
    return resolved


def _beneath(root: Path, relative: str, label: str) -> Path:
    canonical = _relative_path(relative, label)
    return _source_absolute(root, canonical, label)


def _relative_path(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise DirectReplayInputError(f"{label} path is invalid")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.as_posix() != value
    ):
        raise DirectReplayInputError(f"{label} path is not canonical")
    return value


def _change_target(root: Path, relative: str) -> Path:
    canonical = _relative_path(relative, "workspace change")
    canonical_root = root.resolve(strict=True)
    target = canonical_root.joinpath(*PurePosixPath(canonical).parts)
    current = canonical_root
    for part in PurePosixPath(canonical).parts[:-1]:
        current = current / part
        if current.exists() and current.is_symlink():
            raise DirectReplayInfrastructureError("workspace change parent is a symlink")
    if not _is_within(Path(os.path.abspath(target)), canonical_root):
        raise DirectReplayInfrastructureError("workspace change escapes replay workspace")
    return target


def _validate_link_target(root: Path, link: Path, target: str) -> None:
    value = Path(target)
    if value.is_absolute() or "\x00" in target:
        raise DirectReplayInfrastructureError("symlink target escapes replay workspace")
    resolved = Path(os.path.abspath(link.parent / value))
    if not _is_within(resolved, root.resolve(strict=True)):
        raise DirectReplayInfrastructureError("symlink target escapes replay workspace")


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _executable(path: Path, label: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
        status = resolved.stat()
    except OSError as exc:
        raise DirectReplayInputError(f"{label} is unavailable") from exc
    if not stat.S_ISREG(status.st_mode) or not os.access(resolved, os.X_OK):
        raise DirectReplayInputError(f"{label} is not executable")
    return resolved


def _read_json(path: Path, label: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DirectReplayInputError(f"{label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise DirectReplayInputError(f"{label} must be a JSON object")
    return value


def _read_yaml(path: Path, label: str) -> dict[str, object]:
    try:
        value: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise DirectReplayInputError(f"{label} is not valid YAML") from exc
    if not isinstance(value, dict):
        raise DirectReplayInputError(f"{label} must be a YAML mapping")
    return value


def _git(repository: Path, *arguments: str) -> str:
    return _git_bytes(repository, *arguments).decode("utf-8", errors="strict").strip("\x00\n")


def _git_bytes(repository: Path, *arguments: str) -> bytes:
    completed = subprocess.run(
        ("git", "-C", str(repository), *arguments),
        env={**os.environ, **_GIT_ENVIRONMENT},
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
        close_fds=True,
        timeout=120,
    )
    if completed.returncode != 0:
        raise DirectReplayInputError("prepared Git authority could not be read")
    return completed.stdout


def _tree_fingerprint(root: Path) -> str:
    digest = hashlib.sha256()
    for path in _walk_objects(root):
        relative = path.relative_to(root).as_posix().encode("utf-8", errors="surrogateescape")
        status = path.lstat()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(stat.S_IMODE(status.st_mode).to_bytes(4, "big"))
        if stat.S_ISDIR(status.st_mode):
            digest.update(b"directory\0")
        elif stat.S_ISREG(status.st_mode):
            digest.update(b"regular\0")
            digest.update(bytes.fromhex(_sha256_file(path)))
        elif stat.S_ISLNK(status.st_mode):
            digest.update(b"symlink\0")
            digest.update(hashlib.sha256(os.fsencode(os.readlink(path))).digest())
        else:
            raise DirectReplayInputError("input tree contains an unsupported object")
    return digest.hexdigest()


def _package_entries(root: Path, *, excluded_manifest: str) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    for path in _walk_objects(root):
        if path.parent == root and path.name == excluded_manifest:
            continue
        relative = path.relative_to(root).as_posix()
        status = path.lstat()
        if stat.S_ISDIR(status.st_mode):
            object_type = "directory"
            byte_length = 0
            content_sha256 = _EMPTY_SHA256
        elif stat.S_ISREG(status.st_mode):
            if status.st_nlink != 1:
                raise DirectReplayInputError("candidate contains a hard-linked file")
            object_type = "regular"
            byte_length = status.st_size
            content_sha256 = _sha256_file(path)
        else:
            raise DirectReplayInputError("candidate contains an unsupported object")
        entries.append(
            {
                "path_length": len(os.fsencode(relative)),
                "path": relative,
                "object_type": object_type,
                "mode": stat.S_IMODE(status.st_mode),
                "byte_length": byte_length,
                "content_sha256": content_sha256,
                "tree_role": relative.split("/", 1)[0],
            }
        )
    return entries


def _require_sealed_candidate_modes(root: Path) -> None:
    root_status = root.lstat()
    if stat.S_IMODE(root_status.st_mode) != 0o500:
        raise DirectReplayInputError("candidate root mode is not canonical")
    for path in _walk_objects(root):
        status = path.lstat()
        expected = 0o500 if stat.S_ISDIR(status.st_mode) else 0o400
        if stat.S_IMODE(status.st_mode) != expected:
            raise DirectReplayInputError("candidate entry mode is not canonical")


def _walk_objects(root: Path) -> list[Path]:
    result: list[Path] = []
    for directory, names, files in os.walk(root, topdown=True, followlinks=False):
        names.sort()
        files.sort()
        current = Path(directory)
        for name in names:
            result.append(current / name)
        for name in files:
            result.append(current / name)
    return sorted(result, key=lambda path: path.relative_to(root).as_posix())


def _remove_change_target(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _remove_disposable_tree(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    if path.is_symlink() or not path.is_dir():
        raise DirectReplayInfrastructureError("disposable workspace root is unsafe")
    for item in reversed(_walk_objects(path)):
        status = item.lstat()
        if stat.S_ISDIR(status.st_mode):
            item.chmod(stat.S_IMODE(status.st_mode) | 0o700)
        elif stat.S_ISREG(status.st_mode):
            item.chmod(stat.S_IMODE(status.st_mode) | 0o600)
    path.chmod(stat.S_IMODE(path.lstat().st_mode) | 0o700)
    shutil.rmtree(path)


def _make_report_private(root: Path) -> None:
    for path in _walk_objects(root):
        status = path.lstat()
        if stat.S_ISDIR(status.st_mode):
            path.chmod(0o700)
        elif stat.S_ISREG(status.st_mode):
            path.chmod(0o600)
    root.chmod(0o700)


def _write_json(path: Path, value: Mapping[str, object]) -> None:
    _write_bytes(path, _json_bytes(value))


def _write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    path.chmod(0o600)


def _json_bytes(value: Mapping[str, object]) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _sha256_json(value: Mapping[str, object]) -> str:
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _markdown_bool(value: object) -> str:
    if value is True:
        return "pass"
    if value is False:
        return "fail"
    return "n/a"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run-direct-historical-replay",
        description=(
            "Run a prepared campaign's original historical functional evaluator "
            "directly on disposable host-side workspaces. No model process is started."
        ),
    )
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--prepared-campaign", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--keep-workspaces",
        action="store_true",
        help="Retain disposable reconstructed workspaces inside the private report.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=DEFAULT_TIMEOUT_SECONDS,
        help="Per-task evaluator timeout (default: 3600).",
    )
    parser.add_argument("--version", action="version", version=__version__)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Console entry point for authoritative direct historical replay."""
    arguments = _build_parser().parse_args(argv)
    print(f"WARNING: {_MODEL_STOP_WARNING}", file=sys.stderr)
    try:
        destination, report = run_direct_historical_replay(
            arguments.candidate,
            arguments.prepared_campaign,
            arguments.output,
            keep_workspaces=arguments.keep_workspaces,
            timeout_seconds=arguments.timeout_seconds,
        )
    except DirectReplayInputError as exc:
        print(f"input error: {exc}", file=sys.stderr)
        return 2
    except DirectReplayError as exc:
        print(f"evaluator infrastructure error: {exc}", file=sys.stderr)
        return 3
    print(f"Report: {destination}")
    print(f"Status: {report['evaluation_status']}")
    return report_exit_code(report)


if __name__ == "__main__":
    raise SystemExit(main())
