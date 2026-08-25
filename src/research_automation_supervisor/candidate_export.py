"""Deterministic visible-campaign candidate export."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

from research_automation_supervisor.errors import ReplayCampaignStateError
from research_automation_supervisor.replay_campaign_sources import (
    PreparedReplayCampaign,
    PreparedReplayTask,
)

CANDIDATE_SCHEMA_VERSION = 1
CANDIDATE_DIRECTORY_NAME = "final-candidate"
CANDIDATE_MANIFEST_NAME = "candidate-manifest.json"
CANDIDATE_STAGING_NAME = ".final-candidate.staging"
TASK_INPUT_DIRECTORY_NAME = "candidate-input"
TASK_INPUT_MANIFEST_NAME = "candidate-input-manifest.json"
TASK_INPUT_STAGING_NAME = ".candidate-input.staging"


def _candidate_checkpoint(_name: str) -> None:
    """Fault-injection seam for durable publication tests."""


def export_visible_candidate(
    prepared: PreparedReplayCampaign,
    run_directory: Path,
    *,
    run_token: str,
    completed_task_ids: Sequence[str],
    human_decision_count: int,
    model_turn_count: int,
) -> tuple[Path, str]:
    """Export or verify one immutable candidate after every task is terminal."""
    del run_token
    expected_ids = tuple(
        task.specification.task_id for task in prepared.tasks
    )
    if tuple(completed_task_ids) != expected_ids:
        raise ReplayCampaignStateError(
            "candidate export requires every visible task to be terminal"
        )
    root = run_directory / CANDIDATE_DIRECTORY_NAME
    manifest_path = root / CANDIDATE_MANIFEST_NAME
    if root.exists():
        return _verify_existing_candidate(root, manifest_path)
    staging = run_directory / CANDIDATE_STAGING_NAME
    try:
        _discard_stale_tree(
            run_directory,
            staging,
            expected_name=CANDIDATE_STAGING_NAME,
        )
        staging.mkdir(mode=0o700)
        _candidate_checkpoint("after_candidate_staging_creation")
        tasks_root = staging / "tasks"
        tasks_root.mkdir(mode=0o700)
        task_records = [
            _export_task(task, run_directory, tasks_root)
            for task in prepared.tasks
        ]
        provenance = {
            "schema_version": CANDIDATE_SCHEMA_VERSION,
            "campaign_id": prepared.specification.campaign_id,
            "campaign_specification_sha256": prepared.specification_sha256,
            "task_order": list(expected_ids),
            "human_decision_count": human_decision_count,
            "model_terminal_task_ids": list(expected_ids),
            "finalized_model_turn_count": model_turn_count,
            "tasks": task_records,
        }
        _write_json(staging / "candidate.json", provenance)
        _candidate_checkpoint("after_candidate_payload_write")
        _make_payload_read_only(staging)
        payload = _candidate_manifest_payload(staging, prepared)
        digest = _sha256_json(payload)
        staging_manifest = staging / CANDIDATE_MANIFEST_NAME
        _write_json(
            staging_manifest,
            {
                **payload,
                "candidate_manifest_sha256": digest,
            },
        )
        staging_manifest.chmod(0o400)
        staging.chmod(0o500)
        _fsync_tree(staging)
        _verify_existing_candidate(staging, staging_manifest)
        _candidate_checkpoint("before_candidate_atomic_publish")
        os.replace(staging, root)
        _fsync_directory(run_directory)
        _candidate_checkpoint("after_candidate_atomic_publish")
    except ReplayCampaignStateError:
        raise
    except OSError as exc:
        raise ReplayCampaignStateError(
            "visible candidate package could not be exported"
        ) from exc
    return _verify_existing_candidate(root, manifest_path)


def _discard_stale_tree(
    parent: Path,
    staging: Path,
    *,
    expected_name: str,
) -> None:
    try:
        canonical_parent = parent.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ReplayCampaignStateError(
            "candidate staging parent could not be resolved"
        ) from exc
    if staging.parent != parent or staging.name != expected_name:
        raise ReplayCampaignStateError("candidate staging path is invalid")
    if not staging.exists() and not staging.is_symlink():
        return
    try:
        if staging.is_symlink() or not staging.is_dir():
            raise ReplayCampaignStateError(
                "candidate staging path has an unsafe object type"
            )
        if staging.resolve(strict=True).parent != canonical_parent:
            raise ReplayCampaignStateError("candidate staging path escaped")
        for path in staging.rglob("*"):
            status = path.lstat()
            if not (stat.S_ISDIR(status.st_mode) or stat.S_ISREG(status.st_mode)):
                raise ReplayCampaignStateError(
                    "candidate staging tree contains an unsafe object"
                )
        staging.chmod(0o700)
        for path in staging.rglob("*"):
            if path.is_dir():
                path.chmod(0o700)
            else:
                path.chmod(0o600)
        shutil.rmtree(staging)
        _fsync_directory(canonical_parent)
    except ReplayCampaignStateError:
        raise
    except OSError as exc:
        raise ReplayCampaignStateError(
            "stale candidate staging could not be discarded"
        ) from exc


def verify_visible_candidate(candidate: Path) -> dict[str, object]:
    """Validate a standalone candidate without campaign state access."""
    root = candidate.resolve(strict=True)
    manifest_path = root / CANDIDATE_MANIFEST_NAME
    _path, _digest = _verify_existing_candidate(root, manifest_path)
    value = _read_json(manifest_path)
    return value


def capture_terminal_task_candidate_input(
    task: PreparedReplayTask,
    run_directory: Path,
) -> Path:
    """Seal one task's candidate bytes before its terminal journal transition."""
    task_run_root = run_directory / "tasks" / task.specification.task_id
    root = task_run_root / TASK_INPUT_DIRECTORY_NAME
    manifest_path = root / TASK_INPUT_MANIFEST_NAME
    if root.exists():
        _verify_snapshot(
            root,
            manifest_path,
            digest_field="candidate_input_manifest_sha256",
            excluded_manifest=TASK_INPUT_MANIFEST_NAME,
        )
        return root
    staging = task_run_root / TASK_INPUT_STAGING_NAME
    try:
        _discard_stale_tree(
            task_run_root,
            staging,
            expected_name=TASK_INPUT_STAGING_NAME,
        )
        staging.mkdir(mode=0o700)
        _candidate_checkpoint("after_task_input_staging_creation")
        record = _build_task_candidate(task, run_directory, staging)
        _write_json(staging / "candidate-task.json", record)
        _candidate_checkpoint("after_task_input_payload_write")
        _make_payload_read_only(staging)
        payload = {
            "schema_version": CANDIDATE_SCHEMA_VERSION,
            "snapshot_id": (
                f"{task.specification.task_id}:terminal-candidate-input-v1"
            ),
            "task_id": task.specification.task_id,
            "entries": _package_entries(
                staging,
                excluded_manifest=TASK_INPUT_MANIFEST_NAME,
            ),
        }
        digest = _sha256_json(payload)
        staging_manifest = staging / TASK_INPUT_MANIFEST_NAME
        _write_json(
            staging_manifest,
            {
                **payload,
                "candidate_input_manifest_sha256": digest,
            },
        )
        staging_manifest.chmod(0o400)
        staging.chmod(0o500)
        _fsync_tree(staging)
        _verify_snapshot(
            staging,
            staging_manifest,
            digest_field="candidate_input_manifest_sha256",
            excluded_manifest=TASK_INPUT_MANIFEST_NAME,
        )
        _candidate_checkpoint("before_task_input_atomic_publish")
        os.replace(staging, root)
        _fsync_directory(task_run_root)
        _candidate_checkpoint("after_task_input_atomic_publish")
    except ReplayCampaignStateError:
        raise
    except OSError as exc:
        raise ReplayCampaignStateError(
            "terminal candidate input could not be sealed"
        ) from exc
    _verify_snapshot(
        root,
        manifest_path,
        digest_field="candidate_input_manifest_sha256",
        excluded_manifest=TASK_INPUT_MANIFEST_NAME,
    )
    return root


def _export_task(
    task: PreparedReplayTask,
    run_directory: Path,
    tasks_root: Path,
) -> dict[str, object]:
    task_id = task.specification.task_id
    source = (
        run_directory
        / "tasks"
        / task_id
        / TASK_INPUT_DIRECTORY_NAME
    )
    _verify_snapshot(
        source,
        source / TASK_INPUT_MANIFEST_NAME,
        digest_field="candidate_input_manifest_sha256",
        excluded_manifest=TASK_INPUT_MANIFEST_NAME,
    )
    destination = tasks_root / task_id
    try:
        shutil.copytree(source, destination, copy_function=shutil.copy2)
    except OSError as exc:
        raise ReplayCampaignStateError(
            "sealed terminal candidate input could not be copied"
        ) from exc
    return _read_json(destination / "candidate-task.json")


def _build_task_candidate(
    task: PreparedReplayTask,
    run_directory: Path,
    task_root: Path,
) -> dict[str, object]:
    task_id = task.specification.task_id
    source_root = run_directory / "tasks" / task_id
    terminal = _read_json(source_root / "model-terminal.json")
    report = _read_json(source_root / "task-report.json")
    terminal_result = terminal.get("stage2_result")
    if not isinstance(terminal_result, dict):
        raise ReplayCampaignStateError("terminal task result is invalid")
    artifact_directory = terminal_result.get("artifact_directory")
    if not isinstance(artifact_directory, str):
        raise ReplayCampaignStateError(
            "terminal task artifact directory is invalid"
        )
    stage2 = Path(artifact_directory)
    stage2_state = _read_json(stage2 / "state.json")
    evidence_path = stage2_state.get("latest_git_evidence_path")
    tests_path = stage2_state.get("latest_tests_path")
    if not isinstance(evidence_path, str) or not isinstance(tests_path, str):
        raise ReplayCampaignStateError(
            "terminal task lacks visible Git or acceptance-test evidence"
        )
    evidence = _read_json(Path(evidence_path))
    if (
        evidence.get("scope_compliant") is not True
        or evidence.get("patch_complete") is not True
    ):
        raise ReplayCampaignStateError(
            "terminal task evidence is incomplete or outside visible scope"
        )
    patch_path = Path(str(evidence["patch_artifact"]))
    shutil.copyfile(patch_path, task_root / "candidate.patch")
    changes = _export_changed_files(
        task.stage2.workspace,
        evidence,
        task_root,
    )
    _write_json(task_root / "changes.json", changes)
    _write_json(task_root / "git-evidence.json", _portable_git_evidence(evidence))
    _write_json(
        task_root / "visible-tests.json",
        _portable_visible_tests(_read_json(Path(tests_path))),
    )
    _write_json(
        task_root / "terminal-summary.json",
        {
            "schema_version": 1,
            "task_id": task_id,
            "verdict": report["verdict"],
            "repair_rounds": report["repair_rounds"],
            "worker_reports": report["worker_reports"],
            "auditor_reports": report["auditor_reports"],
            "stage2_status": terminal_result["status"],
            "model_turn_count": terminal["model_turn_count"],
        },
    )
    baseline_tree = _git_value(
        task.stage2.workspace,
        ("rev-parse", f"{task.stage2.baseline_commit}^{{tree}}"),
    )
    provenance = task.specification.source_provenance
    if provenance.source_tree != baseline_tree:
        raise ReplayCampaignStateError(
            "visible source provenance does not match the Git baseline tree"
        )
    final_manifest = _working_tree_manifest(task.stage2.workspace)
    final_tree_sha256 = _sha256_json(
        {
            "schema_version": 1,
            "entries": final_manifest,
        }
    )
    changed = evidence.get("changed_paths")
    report_result = report.get("stage2_result")
    if not isinstance(changed, list) or not isinstance(report_result, dict):
        raise ReplayCampaignStateError(
            "terminal task summary is invalid"
        )
    return {
        "task_id": task_id,
        "source_provenance": provenance.model_dump(mode="json"),
        "execution_baseline_commit": task.stage2.baseline_commit,
        "execution_baseline_tree": baseline_tree,
        "final_visible_tree_sha256": final_tree_sha256,
        "changed_paths": [
            item["path"]
            for item in changed
            if isinstance(item, dict) and isinstance(item.get("path"), str)
        ],
        "patch_sha256": evidence["patch_sha256"],
        "changes_sha256": _sha256_json(changes),
        "visible_tests_passed": report_result["tests_passed"],
        "scope_compliant": evidence["scope_compliant"],
    }


def _export_changed_files(
    workspace: Path,
    evidence: Mapping[str, object],
    task_root: Path,
) -> dict[str, object]:
    raw_paths = evidence.get("changed_paths")
    if not isinstance(raw_paths, list):
        raise ReplayCampaignStateError("candidate changed paths are invalid")
    files_root = task_root / "changed-files"
    files_root.mkdir(mode=0o700)
    entries: list[dict[str, object]] = []
    deletions: set[str] = set()
    for raw in raw_paths:
        if not isinstance(raw, dict):
            raise ReplayCampaignStateError("candidate changed path is invalid")
        path = _candidate_relative_path(raw.get("path"))
        old_path_value = raw.get("old_path")
        if isinstance(old_path_value, str) and old_path_value != path:
            deletions.add(_candidate_relative_path(old_path_value))
        source = workspace / path
        try:
            status = source.lstat()
        except FileNotFoundError:
            deletions.add(path)
            continue
        if stat.S_ISREG(status.st_mode):
            content = source.read_bytes()
            destination = files_root / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
            entries.append(
                {
                    "path": path,
                    "operation": "upsert",
                    "object_type": "regular",
                    "mode": stat.S_IMODE(status.st_mode),
                    "byte_length": len(content),
                    "content_sha256": hashlib.sha256(content).hexdigest(),
                }
            )
        elif stat.S_ISLNK(status.st_mode):
            target = os.readlink(source)
            target_bytes = os.fsencode(target)
            entries.append(
                {
                    "path": path,
                    "operation": "upsert",
                    "object_type": "symlink",
                    "mode": stat.S_IMODE(status.st_mode),
                    "byte_length": len(target_bytes),
                    "content_sha256": hashlib.sha256(target_bytes).hexdigest(),
                    "target": target,
                }
            )
        else:
            raise ReplayCampaignStateError(
                "candidate changed path has an unsupported object type"
            )
    upserts = {str(entry["path"]) for entry in entries}
    entries.extend(
        {
            "path": path,
            "operation": "delete",
            "object_type": "absent",
            "mode": 0,
            "byte_length": 0,
            "content_sha256": hashlib.sha256(b"").hexdigest(),
        }
        for path in sorted(deletions - upserts)
    )
    entries.sort(key=lambda entry: str(entry["path"]))
    return {
        "schema_version": 1,
        "entries": entries,
    }


def _candidate_relative_path(value: object) -> str:
    if not isinstance(value, str):
        raise ReplayCampaignStateError("candidate changed path is invalid")
    path = Path(value)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ReplayCampaignStateError("candidate changed path escapes workspace")
    return path.as_posix()


def _portable_git_evidence(
    evidence: Mapping[str, object],
) -> dict[str, object]:
    keys = (
        "baseline_commit",
        "changed_paths",
        "head_commit",
        "patch_complete",
        "patch_sha256",
        "scope_compliant",
        "status_porcelain_v2",
    )
    return {key: evidence.get(key) for key in keys}


def _portable_visible_tests(
    suite: Mapping[str, object],
) -> dict[str, object]:
    raw_results = suite.get("results")
    if not isinstance(raw_results, list):
        raise ReplayCampaignStateError("visible test suite is invalid")
    results: list[dict[str, object]] = []
    for raw in raw_results:
        if not isinstance(raw, dict):
            raise ReplayCampaignStateError("visible test result is invalid")
        results.append(
            {
                key: raw.get(key)
                for key in (
                    "test_id",
                    "argv",
                    "status",
                    "exit_code",
                    "passed",
                    "stdout_sha256",
                    "stderr_sha256",
                )
            }
        )
    return {
        "schema_version": 1,
        "passed": suite.get("passed"),
        "results": results,
    }


def _candidate_manifest_payload(
    root: Path,
    prepared: PreparedReplayCampaign,
) -> dict[str, object]:
    return {
        "schema_version": CANDIDATE_SCHEMA_VERSION,
        "snapshot_id": f"{prepared.specification.campaign_id}:candidate-v1",
        "campaign_id": prepared.specification.campaign_id,
        "entries": _package_entries(
            root,
            excluded_manifest=CANDIDATE_MANIFEST_NAME,
        ),
    }


def _verify_existing_candidate(
    root: Path,
    manifest_path: Path,
) -> tuple[Path, str]:
    return _verify_snapshot(
        root,
        manifest_path,
        digest_field="candidate_manifest_sha256",
        excluded_manifest=CANDIDATE_MANIFEST_NAME,
    )


def _verify_snapshot(
    root: Path,
    manifest_path: Path,
    *,
    digest_field: str,
    excluded_manifest: str,
) -> tuple[Path, str]:
    try:
        root_status = root.lstat()
        manifest_status = manifest_path.lstat()
        if (
            stat.S_ISLNK(root_status.st_mode)
            or not stat.S_ISDIR(root_status.st_mode)
            or stat.S_ISLNK(manifest_status.st_mode)
            or not stat.S_ISREG(manifest_status.st_mode)
            or manifest_status.st_nlink != 1
        ):
            raise ReplayCampaignStateError(
                "candidate publication has an unsafe object type"
            )
        manifest = _read_json(manifest_path)
        claimed = manifest.pop(digest_field)
    except (KeyError, OSError, TypeError) as exc:
        raise ReplayCampaignStateError(
            "candidate manifest is missing or invalid"
        ) from exc
    if not isinstance(claimed, str) or claimed != _sha256_json(manifest):
        raise ReplayCampaignStateError("candidate manifest digest is invalid")
    if manifest.get("entries") != _package_entries(
        root,
        excluded_manifest=excluded_manifest,
    ):
        raise ReplayCampaignStateError("candidate package contents changed")
    return root, claimed


def _package_entries(
    root: Path,
    *,
    excluded_manifest: str,
) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    for path in sorted(root.rglob("*")):
        if path.parent == root and path.name == excluded_manifest:
            continue
        relative = path.relative_to(root).as_posix()
        status = path.lstat()
        if stat.S_ISDIR(status.st_mode):
            object_type = "directory"
            byte_length = 0
            content_sha256 = hashlib.sha256(b"").hexdigest()
        elif stat.S_ISREG(status.st_mode):
            if status.st_nlink != 1:
                raise ReplayCampaignStateError(
                    "candidate package contains a hard-linked regular file"
                )
            object_type = "regular"
            content = path.read_bytes()
            byte_length = len(content)
            content_sha256 = hashlib.sha256(content).hexdigest()
        else:
            raise ReplayCampaignStateError(
                "candidate package contains an unsupported entry"
            )
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


def _working_tree_manifest(root: Path) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    for path in sorted(root.rglob("*")):
        relative_path = path.relative_to(root)
        if ".git" in relative_path.parts:
            continue
        relative = relative_path.as_posix()
        status = path.lstat()
        if stat.S_ISDIR(status.st_mode):
            object_type = "directory"
            byte_length = 0
            digest = hashlib.sha256(b"").hexdigest()
        elif stat.S_ISREG(status.st_mode):
            content = path.read_bytes()
            object_type = "regular"
            byte_length = len(content)
            digest = hashlib.sha256(content).hexdigest()
        elif stat.S_ISLNK(status.st_mode):
            target = os.fsencode(os.readlink(path))
            object_type = "symlink"
            byte_length = len(target)
            digest = hashlib.sha256(target).hexdigest()
        else:
            raise ReplayCampaignStateError(
                "visible workspace contains an unsupported entry"
            )
        entries.append(
            {
                "path_length": len(os.fsencode(relative)),
                "path": relative,
                "object_type": object_type,
                "mode": stat.S_IMODE(status.st_mode),
                "byte_length": byte_length,
                "content_sha256": digest,
            }
        )
    return entries


def _git_value(workspace: Path, arguments: Sequence[str]) -> str:
    qualified_workspace = Path(os.path.abspath(workspace))
    try:
        completed = subprocess.run(
            (
                "git",
                "--no-pager",
                "-c",
                f"safe.directory={qualified_workspace}",
                "-C",
                str(qualified_workspace),
                *arguments,
            ),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            text=True,
            timeout=30,
            close_fds=True,
            env={
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "LC_ALL": "C",
                "PATH": "/usr/bin:/bin",
            },
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ReplayCampaignStateError(
            "candidate source provenance could not be resolved"
        ) from exc
    value = completed.stdout.strip()
    if completed.returncode != 0 or not value:
        raise ReplayCampaignStateError(
            "candidate source provenance is invalid"
        )
    return value


def _make_payload_read_only(root: Path) -> None:
    for current, directories, files in os.walk(root, topdown=False):
        current_path = Path(current)
        for name in files:
            path = current_path / name
            mode = stat.S_IMODE(path.stat().st_mode)
            os.chmod(path, 0o500 if mode & 0o111 else 0o400)
        for name in directories:
            path = current_path / name
            os.chmod(path, 0o500)
        if current_path != root:
            os.chmod(current_path, 0o500)


def _fsync_tree(root: Path) -> None:
    for current, directories, files in os.walk(root, topdown=False):
        current_path = Path(current)
        for name in files:
            descriptor = os.open(current_path / name, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        for name in directories:
            _fsync_directory(current_path / name)
        _fsync_directory(current_path)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sha256_json(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _write_json(path: Path, value: object) -> None:
    content = (
        json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("ascii")
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("wb") as output:
        output.write(content)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)
    _fsync_directory(path.parent)


def _read_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReplayCampaignStateError(
            "candidate source artifact is invalid"
        ) from exc
    if not isinstance(value, dict):
        raise ReplayCampaignStateError(
            "candidate source artifact must be an object"
        )
    return value
