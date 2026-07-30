"""Prepare and verify private historical replay evaluation packages.

This module is deliberately independent from campaign execution and model
adapters.  It accepts a preserved, pre-split prepared campaign as host-side
authority and produces a sealed input for ``evaluate-historical-replay``.
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import errno
import hashlib
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import yaml  # type: ignore[import-untyped]

PACKAGE_MANIFEST_NAME = "evaluation-package-manifest.json"
PACKAGE_CONFIG_PATH = Path("evaluation-config/offline-evaluation.json")
PACKAGE_SCHEMA_VERSION = 1
TASK_IDS = (
    "reduced-vars-gp",
    "hidden-cleanup",
    "cell-storage",
    "hat-gamma-x",
    "stage4ao-b",
)
_DEPENDENCY_NAMES = ("chombo-dependency", "grchombo-dependency")
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
_GIT_ENV = {
    "GIT_OPTIONAL_LOCKS": "0",
    "LC_ALL": "C",
    "PATH": "/usr/bin:/bin",
}


class EvaluationPackageError(RuntimeError):
    """Raised when historical package authority is incomplete or unsafe."""


@dataclass(frozen=True)
class _TaskSource:
    task_id: str
    workspace: Path
    baseline_commit: str
    source_commit: str
    source_tree: str
    gold_root: Path
    evaluator: Path
    allowed_paths: tuple[str, ...]
    production_profile: dict[str, list[str]]
    stage2_specification: Path
    stage2_specification_sha256: str


@dataclass(frozen=True)
class _DependencySource:
    name: str
    repository: Path
    commit: str
    tree: str
    namespace_path: str


@dataclass(frozen=True)
class _SourceAuthority:
    root: Path
    declared_root: Path
    root_fd: int
    campaign_id: str
    campaign_manifest: Path
    campaign_manifest_sha256: str
    preparation_report: Path
    preparation_report_sha256: str
    source_state: Path
    source_state_sha256: str
    tasks: tuple[_TaskSource, ...]
    dependencies: tuple[_DependencySource, ...]


@dataclass(frozen=True)
class _OutputTarget:
    destination: Path
    parent_fd: int
    parent_identity: tuple[int, int, int]
    created_parent: Path | None


def prepare_historical_replay_evaluation_package(
    source_prepared_campaign: Path,
    output: Path,
) -> tuple[Path, str]:
    """Materialize a sealed package without invoking a campaign or model."""
    source = _load_source_authority(source_prepared_campaign)
    try:
        return _prepare_loaded_source(source, output)
    finally:
        os.close(source.root_fd)


def _prepare_loaded_source(
    source: _SourceAuthority,
    output: Path,
) -> tuple[Path, str]:
    target = _new_output_path(output, source.declared_root)
    destination = target.destination
    before = _source_fingerprint(source)
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{destination.name}.staging-",
            dir=destination.parent,
        )
    )
    staging_identity = _path_identity(staging)[:2]
    published = False
    try:
        config_tasks: list[dict[str, object]] = []
        evaluator_destination = staging / "evaluators/historical-functional.py"
        evaluator_destination.parent.mkdir(parents=True)
        _copy_regular_file(source.tasks[0].evaluator, evaluator_destination)
        evaluator_sha256 = _sha256_file(evaluator_destination)
        for task in source.tasks:
            if _sha256_file(task.evaluator) != _sha256_file(
                source.tasks[0].evaluator
            ):
                raise EvaluationPackageError(
                    "historical tasks do not share one evaluator authority"
                )
            config_tasks.append(
                _materialize_task(
                    task,
                    staging,
                    evaluator_sha256=evaluator_sha256,
                )
            )
        runtime = _materialize_dependencies(source.dependencies, staging)
        provenance = _provenance_record(source)
        provenance_path = staging / "provenance/source-authority.json"
        provenance_path.parent.mkdir(parents=True)
        _write_json(provenance_path, provenance)
        config = {
            "schema_version": PACKAGE_SCHEMA_VERSION,
            "package_id": f"{source.campaign_id}-offline-v1",
            "runtime": runtime,
            "tasks": config_tasks,
        }
        config_path = staging / PACKAGE_CONFIG_PATH
        config_path.parent.mkdir(parents=True)
        _write_json(config_path, config)
        _make_payload_read_only(staging)
        staging.chmod(0o700)
        manifest_payload = {
            "schema_version": PACKAGE_SCHEMA_VERSION,
            "snapshot_id": f"{source.campaign_id}:offline-evaluation-v1",
            "package_id": config["package_id"],
            "entries": _package_entries(staging),
        }
        digest = _sha256_json(manifest_payload)
        manifest_path = staging / PACKAGE_MANIFEST_NAME
        _write_json(
            manifest_path,
            {
                **manifest_payload,
                "package_manifest_sha256": digest,
            },
        )
        manifest_path.chmod(0o400)
        staging.chmod(0o500)
        if before != _source_fingerprint(source):
            raise EvaluationPackageError(
                "historical source authority changed during package preparation"
            )
        verify_evaluation_package(staging)
        _fsync_tree(staging)
        _verify_output_target(target, source.declared_root)
        if (
            _path_identity_at(target.parent_fd, staging.name)[:2]
            != staging_identity
        ):
            raise EvaluationPackageError(
                "package staging identity changed before publication"
            )
        _rename_noreplace(
            staging.name,
            destination.name,
            source_dir_fd=target.parent_fd,
            destination_dir_fd=target.parent_fd,
            forbidden_roots=(source.declared_root,),
        )
        published = True
        _verify_output_target(target, source.declared_root)
        verify_evaluation_package(destination)
        _fsync_directory(destination.parent)
        return destination, digest
    except Exception:
        cleanup_name = destination.name if published else staging.name
        _remove_staging(
            Path(f"/proc/self/fd/{target.parent_fd}") / cleanup_name
        )
        if target.created_parent is not None:
            with contextlib.suppress(OSError):
                target.created_parent.rmdir()
        raise
    finally:
        os.close(target.parent_fd)


def verify_evaluation_package(
    root: Path,
    *,
    require_production: bool = True,
) -> dict[str, object]:
    """Verify a package-level canonical manifest and every declared entry."""
    package = _exact_directory(root, "evaluation package")
    manifest_path = package / PACKAGE_MANIFEST_NAME
    try:
        status = manifest_path.lstat()
    except OSError as exc:
        raise EvaluationPackageError(
            "evaluation package manifest is unavailable"
        ) from exc
    if (
        stat.S_ISLNK(status.st_mode)
        or not stat.S_ISREG(status.st_mode)
        or status.st_nlink != 1
        or stat.S_IMODE(status.st_mode) & 0o111
    ):
        raise EvaluationPackageError(
            "evaluation package manifest is not an independent regular file"
        )
    manifest_bytes = manifest_path.read_bytes()
    manifest = _read_json(manifest_path)
    required = {
        "schema_version",
        "snapshot_id",
        "package_id",
        "entries",
        "package_manifest_sha256",
    }
    if set(manifest) != required or manifest["schema_version"] != 1:
        raise EvaluationPackageError("evaluation package manifest fields are invalid")
    claimed = manifest["package_manifest_sha256"]
    payload = {
        key: value
        for key, value in manifest.items()
        if key != "package_manifest_sha256"
    }
    if not isinstance(claimed, str) or claimed != _sha256_json(payload):
        raise EvaluationPackageError("evaluation package manifest digest is invalid")
    if require_production:
        root_status = package.lstat()
        if (
            stat.S_IMODE(root_status.st_mode) != 0o500
            or root_status.st_uid != os.geteuid()
            or root_status.st_gid != os.getegid()
            or stat.S_IMODE(status.st_mode) != 0o400
            or status.st_uid != os.geteuid()
            or status.st_gid != os.getegid()
            or manifest_bytes != _json_bytes(manifest)
        ):
            raise EvaluationPackageError(
                "evaluation package top-level seal is not canonical"
            )
    actual_entries = _package_entries(package)
    if manifest["entries"] != actual_entries:
        raise EvaluationPackageError(
            "evaluation package content does not match its manifest"
        )
    config = _read_json(package / PACKAGE_CONFIG_PATH)
    if config.get("package_id") != manifest["package_id"]:
        raise EvaluationPackageError(
            "evaluation package identity is internally inconsistent"
        )
    _validate_package_configuration(package, config)
    provenance_path = package / "provenance/source-authority.json"
    if require_production:
        if not provenance_path.is_file() or provenance_path.is_symlink():
            raise EvaluationPackageError(
                "prepared historical package provenance is missing"
            )
        provenance = _read_json(provenance_path)
        _validate_production_package(package, manifest, config, provenance)
        for entry in actual_entries:
            entry_path = _source_path(
                package,
                str(entry["path"]),
                "evaluation package entry",
            )
            entry_status = entry_path.lstat()
            expected_mode = (
                0o500 if entry["object_type"] == "directory" else 0o400
            )
            if (
                entry["mode"] != expected_mode
                or entry_status.st_uid != os.geteuid()
                or entry_status.st_gid != os.getegid()
            ):
                raise EvaluationPackageError(
                    "prepared historical package ownership is not canonical"
                )
    return manifest


def _validate_production_package(
    root: Path,
    manifest: Mapping[str, object],
    config: Mapping[str, object],
    provenance: Mapping[str, object],
) -> None:
    if set(config) != {"schema_version", "package_id", "runtime", "tasks"}:
        raise EvaluationPackageError(
            "prepared historical package configuration is incomplete"
        )
    campaign_id = provenance.get("campaign_id")
    if (
        set(provenance)
        != {
            "schema_version",
            "campaign_id",
            "source_campaign_manifest_sha256",
            "source_preparation_report_sha256",
            "source_state_sha256",
            "tasks",
            "dependencies",
        }
        or provenance.get("schema_version") != PACKAGE_SCHEMA_VERSION
        or not isinstance(campaign_id, str)
        or not campaign_id
        or config.get("package_id") != f"{campaign_id}-offline-v1"
        or manifest.get("snapshot_id")
        != f"{campaign_id}:offline-evaluation-v1"
        or not all(
            _is_sha256(provenance.get(key))
            for key in (
                "source_campaign_manifest_sha256",
                "source_preparation_report_sha256",
                "source_state_sha256",
            )
        )
    ):
        raise EvaluationPackageError(
            "prepared historical package provenance is inconsistent"
        )
    tasks = _required_object_list(config.get("tasks"), "evaluation tasks")
    provenance_tasks = _required_object_list(
        provenance.get("tasks"),
        "provenance tasks",
    )
    task_ids = tuple(_required_string(task, "task_id") for task in tasks)
    provenance_ids = tuple(
        _required_string(task, "task_id") for task in provenance_tasks
    )
    if task_ids != TASK_IDS or provenance_ids != TASK_IDS:
        raise EvaluationPackageError(
            "prepared historical package does not contain all five tasks"
        )
    for task, source_record in zip(tasks, provenance_tasks, strict=True):
        task_id = _required_string(task, "task_id")
        if (
            set(task)
            != {
                "task_id",
                "baseline_archive",
                "baseline_archive_sha256",
                "source_commit",
                "source_tree",
                "expected_changed_paths",
                "production_profile",
                "tests",
                "exact_reference_archive",
                "exact_reference_archive_sha256",
            }
            or set(source_record)
            != {
                "task_id",
                "source_commit",
                "source_tree",
                "baseline_commit",
                "stage2_specification_sha256",
            }
            or task.get("source_commit") != source_record.get("source_commit")
            or task.get("source_tree") != source_record.get("source_tree")
            or not _is_git_oid(task.get("source_commit"))
            or not _is_git_oid(task.get("source_tree"))
            or not _is_git_oid(source_record.get("baseline_commit"))
            or not _is_sha256(
                source_record.get("stage2_specification_sha256")
            )
        ):
            raise EvaluationPackageError(
                f"{task_id} package provenance is inconsistent"
            )
        changed_paths = _string_list(
            task.get("expected_changed_paths"),
            f"{task_id} expected changed paths",
        )
        if not changed_paths or len(set(changed_paths)) != len(changed_paths):
            raise EvaluationPackageError(
                f"{task_id} expected changed paths are incomplete"
            )
        for relative in changed_paths:
            _relative_path(relative, f"{task_id} changed path")
            fixture = _source_path(
                root,
                f"protected-fixtures/{task_id}/{relative}",
                f"{task_id} protected fixture",
            )
            if not fixture.is_file() or fixture.is_symlink():
                raise EvaluationPackageError(
                    f"{task_id} protected fixture is incomplete"
                )
        profile = task.get("production_profile")
        if not isinstance(profile, dict) or set(profile) != {
            "hot_path",
            "post_update",
            "validation_only",
        }:
            raise EvaluationPackageError(
                f"{task_id} production profile is incomplete"
            )
        for role in ("hot_path", "post_update", "validation_only"):
            _string_list(profile[role], f"{task_id} production profile")
        tests = _required_object_list(task.get("tests"), f"{task_id} tests")
        if len(tests) != 1 or set(tests[0]) != {
            "id",
            "runner",
            "script",
            "script_sha256",
            "arguments",
            "timeout_seconds",
            "max_stdout_bytes",
            "max_stderr_bytes",
        }:
            raise EvaluationPackageError(
                f"{task_id} functional evaluator is incomplete"
            )
        selected_test = tests[0]
        if (
            selected_test.get("id") != "historical-functional"
            or selected_test.get("runner") != "python_script_v1"
            or selected_test.get("script")
            != "evaluators/historical-functional.py"
            or _string_list(
                selected_test.get("arguments"),
                f"{task_id} evaluator arguments",
            )
            != [
                "functional",
                task_id,
                "{workspace}",
                (
                    "{evaluation_package}/protected-fixtures/"
                    f"{task_id}"
                ),
            ]
        ):
            raise EvaluationPackageError(
                f"{task_id} functional evaluator mapping is inconsistent"
            )
        _validate_task_archive_authority(root, task)
    runtime = config.get("runtime")
    if not isinstance(runtime, dict) or set(runtime) != {
        "profile",
        "dependency_roots",
    }:
        raise EvaluationPackageError(
            "prepared historical dependency runtime is incomplete"
        )
    dependency_records = _required_object_list(
        runtime.get("dependency_roots"),
        "dependency roots",
    )
    provenance_dependencies = _required_object_list(
        provenance.get("dependencies"),
        "provenance dependencies",
    )
    if (
        runtime.get("profile") != "gl_historical_replay_v1"
        or tuple(record.get("role") for record in dependency_records)
        != _DEPENDENCY_NAMES
        or tuple(record.get("name") for record in provenance_dependencies)
        != _DEPENDENCY_NAMES
    ):
        raise EvaluationPackageError(
            "prepared historical dependency mapping is incomplete"
        )
    for dependency, source_record in zip(
        dependency_records,
        provenance_dependencies,
        strict=True,
    ):
        name = dependency.get("role")
        if (
            set(dependency)
            != {
                "role",
                "path",
                "source_commit",
                "source_tree",
                "namespace_path",
            }
            or set(source_record)
            != {"name", "source_commit", "source_tree"}
            or name != source_record.get("name")
            or dependency.get("source_commit")
            != source_record.get("source_commit")
            or dependency.get("source_tree") != source_record.get("source_tree")
            or not _is_git_oid(dependency.get("source_commit"))
            or not _is_git_oid(dependency.get("source_tree"))
            or dependency.get("path") != f"dependencies/{name}"
        ):
            raise EvaluationPackageError(
                "prepared historical dependency provenance is inconsistent"
            )
        dependency_root = _source_path(
            root,
            str(dependency["path"]),
            "dependency snapshot",
        )
        if _git_tree_oid(dependency_root) != dependency["source_tree"]:
            raise EvaluationPackageError(
                "dependency snapshot tree contradicts its provenance"
            )
    required_roots = {
        "baseline-archives",
        "dependencies",
        "evaluation-config",
        "evaluators",
        "exact-reference",
        "protected-fixtures",
        "provenance",
    }
    actual_roots = {
        path.name
        for path in root.iterdir()
        if path.name != PACKAGE_MANIFEST_NAME
    }
    if actual_roots != required_roots:
        raise EvaluationPackageError(
            "prepared historical package authority is incomplete"
        )


def _validate_task_archive_authority(
    root: Path,
    task: Mapping[str, object],
) -> None:
    task_id = _required_string(task, "task_id")
    baseline = _verified_package_file(
        root,
        task.get("baseline_archive"),
        task.get("baseline_archive_sha256"),
        f"{task_id} baseline archive",
    )
    exact = _verified_package_file(
        root,
        task.get("exact_reference_archive"),
        task.get("exact_reference_archive_sha256"),
        f"{task_id} exact reference archive",
    )
    changed_paths = _string_list(
        task.get("expected_changed_paths"),
        f"{task_id} expected changed paths",
    )
    with tempfile.TemporaryDirectory(
        prefix=f"verify-offline-{task_id}-"
    ) as temporary:
        temporary_root = Path(temporary)
        baseline_root = temporary_root / "baseline"
        expected_root = temporary_root / "expected"
        exact_root = temporary_root / "exact"
        baseline_root.mkdir()
        expected_root.mkdir()
        exact_root.mkdir()
        _extract_regular_archive(baseline, baseline_root)
        _extract_regular_archive(baseline, expected_root)
        if _git_tree_oid(baseline_root) != task.get("source_tree"):
            raise EvaluationPackageError(
                f"{task_id} baseline archive contradicts source provenance"
            )
        for relative in changed_paths:
            fixture = _source_path(
                root,
                f"protected-fixtures/{task_id}/{relative}",
                f"{task_id} protected fixture",
            )
            destination = expected_root.joinpath(
                *PurePosixPath(relative).parts
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists() or destination.is_symlink():
                if destination.is_dir() and not destination.is_symlink():
                    raise EvaluationPackageError(
                        f"{task_id} exact reference path is ambiguous"
                    )
                destination.unlink()
            _copy_regular_file(fixture, destination)
            destination.chmod(0o644)
        _extract_regular_archive(exact, exact_root)
        if _git_tree_oid(expected_root) != _git_tree_oid(exact_root):
            raise EvaluationPackageError(
                f"{task_id} exact reference archive is inconsistent"
            )


def write_evaluation_package_manifest(
    root: Path,
    *,
    package_id: str,
    snapshot_id: str | None = None,
) -> str:
    """Seal a synthetic package manifest for tests and host tooling."""
    package = _exact_directory(root, "evaluation package")
    manifest_path = package / PACKAGE_MANIFEST_NAME
    if manifest_path.exists() or manifest_path.is_symlink():
        manifest_path.unlink()
    payload = {
        "schema_version": PACKAGE_SCHEMA_VERSION,
        "snapshot_id": snapshot_id or f"{package_id}:offline-evaluation-v1",
        "package_id": package_id,
        "entries": _package_entries(package),
    }
    digest = _sha256_json(payload)
    _write_json(
        manifest_path,
        {**payload, "package_manifest_sha256": digest},
    )
    return digest


def historical_replay_command_report(
    *,
    candidate: Path,
    source_prepared_campaign: Path,
    evaluation_package: Path,
    output: Path,
    prepare_executable: str = "prepare-historical-replay-evaluation-package",
    evaluate_executable: str = "evaluate-historical-replay",
) -> dict[str, object]:
    """Return exact next commands without claiming a missing package is ready."""
    prepare_command = shlex.join(
        (
            prepare_executable,
            "--source-prepared-campaign",
            str(source_prepared_campaign),
            "--output",
            str(evaluation_package),
        )
    )
    evaluation_command = shlex.join(
        (
            evaluate_executable,
            "--candidate",
            str(candidate),
            "--evaluation-package",
            str(evaluation_package),
            "--output",
            str(output),
        )
    )
    if evaluation_package.exists() or evaluation_package.is_symlink():
        manifest = verify_evaluation_package(evaluation_package)
        return {
            "schema_version": 1,
            "evaluation_package_status": "validated",
            "evaluation_package_manifest_sha256": manifest[
                "package_manifest_sha256"
            ],
            "package_preparation_command": None,
            "evaluation_command": evaluation_command,
        }
    return {
        "schema_version": 1,
        "evaluation_package_status": "missing",
        "evaluation_package_manifest_sha256": None,
        "package_preparation_command": prepare_command,
        "evaluation_command": evaluation_command,
    }


def _load_source_authority(root: Path) -> _SourceAuthority:
    declared_source = _exact_directory(root, "source prepared campaign")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        source_fd = os.open(declared_source, flags)
    except OSError as exc:
        raise EvaluationPackageError(
            "source prepared campaign could not be pinned"
        ) from exc
    source_identity = _descriptor_identity(source_fd)
    if source_identity != _path_identity(declared_source):
        os.close(source_fd)
        raise EvaluationPackageError(
            "source prepared campaign changed while it was pinned"
        )
    source = Path(f"/proc/self/fd/{source_fd}")
    try:
        return _load_pinned_source_authority(
            source,
            declared_source,
            source_fd,
        )
    except Exception:
        os.close(source_fd)
        raise


def _load_pinned_source_authority(
    source: Path,
    declared_source: Path,
    source_fd: int,
) -> _SourceAuthority:
    campaign_path = source / "campaign.yaml"
    preparation_path = source / "preparation-report.json"
    source_state_path = source / "engine-only/historical-audits/source-state.json"
    campaign, campaign_sha256 = _read_yaml_pinned(campaign_path)
    preparation, preparation_sha256 = _read_json_pinned(preparation_path)
    source_state, source_state_sha256 = _read_json_pinned(source_state_path)
    campaign_id = campaign.get("campaign_id")
    preflight = preparation.get("preflight")
    required_preflight_checks = {
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
        campaign.get("schema_version") != 1
        or not isinstance(campaign_id, str)
        or not campaign_id
        or preparation.get("schema_version") != 1
        or preparation.get("campaign_id") != campaign_id
        or preparation.get("campaign_started") is not False
        or preparation.get("real_model_invoked") is not False
        or not (successful_preflight or notification_only_preflight)
        or not required_preflight_checks <= passed_checks
    ):
        raise EvaluationPackageError(
            "preserved campaign preparation authority is incomplete"
        )
    raw_tasks = campaign.get("tasks")
    preparation_tasks = preparation.get("tasks")
    if not isinstance(raw_tasks, list) or not isinstance(preparation_tasks, list):
        raise EvaluationPackageError("historical task authority is invalid")
    preparation_by_id = _objects_by_id(preparation_tasks, "task_id")
    task_ids = tuple(_required_string(item, "task_id") for item in raw_tasks)
    if task_ids != TASK_IDS or tuple(preparation_by_id) != TASK_IDS:
        raise EvaluationPackageError(
            "historical campaign does not contain the exact five-task mapping"
        )
    tasks = tuple(
        _load_task_source(
            source,
            declared_source,
            item,
            preparation_by_id[_required_string(item, "task_id")],
            preparation_cutoff_ns=preparation_path.lstat().st_mtime_ns,
        )
        for item in raw_tasks
    )
    dependencies = _load_dependencies(source_state)
    return _SourceAuthority(
        root=source,
        declared_root=declared_source,
        root_fd=source_fd,
        campaign_id=campaign_id,
        campaign_manifest=campaign_path,
        campaign_manifest_sha256=campaign_sha256,
        preparation_report=preparation_path,
        preparation_report_sha256=preparation_sha256,
        source_state=source_state_path,
        source_state_sha256=source_state_sha256,
        tasks=tasks,
        dependencies=dependencies,
    )


def _load_task_source(
    root: Path,
    declared_root: Path,
    task: Mapping[str, object],
    preparation: Mapping[str, object],
    *,
    preparation_cutoff_ns: int,
) -> _TaskSource:
    task_id = _required_string(task, "task_id")
    workspace = _source_path(
        root,
        f"visible/tasks/{task_id}/workspace",
        f"{task_id} workspace",
    )
    if not workspace.is_dir() or workspace.is_symlink():
        raise EvaluationPackageError(f"{task_id} workspace is invalid")
    stage2_relative = _required_string(task, "stage2_specification_path")
    stage2_path = _source_path(root, stage2_relative, f"{task_id} Stage 2")
    stage2, stage2_sha256 = _read_yaml_pinned(stage2_path)
    stage2_status = stage2_path.lstat()
    if stage2_status.st_mtime_ns > preparation_cutoff_ns:
        raise EvaluationPackageError(
            f"{task_id} Stage 2 is newer than preparation authority"
        )
    if stage2.get("substage_id") != task_id:
        raise EvaluationPackageError(f"{task_id} Stage 2 identity is invalid")
    allowed_paths = tuple(
        _relative_path(value, f"{task_id} allowed path")
        for value in _string_list(stage2.get("allowed_paths"), "allowed paths")
    )
    if not allowed_paths or len(set(allowed_paths)) != len(allowed_paths):
        raise EvaluationPackageError(f"{task_id} allowed paths are ambiguous")
    gold_roots = task.get("gold_artifact_roots")
    if gold_roots != [f"engine-only/gold/{task_id}"]:
        raise EvaluationPackageError(f"{task_id} gold root is ambiguous")
    gold_root = _source_path(
        root,
        f"engine-only/gold/{task_id}",
        f"{task_id} historical fixture",
    )
    _validate_source_tree(gold_root, allow_executable=False)
    evaluations = task.get("gold_evaluations")
    if not isinstance(evaluations, list) or len(evaluations) != 2:
        raise EvaluationPackageError(f"{task_id} evaluator mapping is incomplete")
    evaluator: Path | None = None
    modes: set[str] = set()
    for evaluation in evaluations:
        if not isinstance(evaluation, dict):
            raise EvaluationPackageError(f"{task_id} evaluator mapping is invalid")
        argv = evaluation.get("argv")
        if (
            not isinstance(argv, list)
            or len(argv) != 6
            or argv[0] != "/usr/bin/python3"
            or argv[3] != task_id
        ):
            raise EvaluationPackageError(
                f"{task_id} evaluator command is not structurally recognized"
            )
        mode = argv[2]
        if mode not in {"functional", "exact"}:
            raise EvaluationPackageError(f"{task_id} evaluator mode is invalid")
        modes.add(mode)
        selected = _exact_source_absolute(
            root,
            declared_root,
            argv[1],
            f"{task_id} evaluator",
        )
        expected_workspace = _exact_source_absolute(
            root,
            declared_root,
            argv[4],
            f"{task_id} evaluator workspace",
        )
        expected_gold = _exact_source_absolute(
            root,
            declared_root,
            argv[5],
            f"{task_id} evaluator fixture",
        )
        if expected_workspace != workspace or expected_gold != gold_root:
            raise EvaluationPackageError(
                f"{task_id} evaluator authority is inconsistent"
            )
        if evaluator is not None and selected != evaluator:
            raise EvaluationPackageError(f"{task_id} evaluator is ambiguous")
        evaluator = selected
    if modes != {"functional", "exact"} or evaluator is None:
        raise EvaluationPackageError(f"{task_id} evaluator modes are incomplete")
    evaluator_status = evaluator.lstat()
    if (
        not stat.S_ISREG(evaluator_status.st_mode)
        or stat.S_ISLNK(evaluator_status.st_mode)
        or evaluator_status.st_nlink != 1
    ):
        raise EvaluationPackageError(f"{task_id} evaluator is not a regular file")
    head = _git(workspace, "rev-parse", "HEAD")
    local_baseline = preparation.get("local_baseline_commit")
    if local_baseline != head:
        raise EvaluationPackageError(f"{task_id} baseline commit changed")
    if _git(workspace, "status", "--porcelain=v1", "--untracked-files=all"):
        raise EvaluationPackageError(f"{task_id} workspace is contaminated")
    if _git(workspace, "rev-list", "--count", "--all") != "1":
        raise EvaluationPackageError(f"{task_id} workspace history is ambiguous")
    if _git(workspace, "remote"):
        raise EvaluationPackageError(f"{task_id} workspace has a remote")
    source_tree = _git(workspace, "rev-parse", "HEAD^{tree}")
    recorded_tree = preparation.get("source_tree")
    if (
        recorded_tree != source_tree
        or preparation.get("target_tree") != source_tree
        or preparation.get("functional_evaluator_proof") is not True
        or preparation.get("exact_evaluator_proof") is not True
    ):
        raise EvaluationPackageError(f"{task_id} source tree changed")
    source_commit = preparation.get("source_workspace_head")
    if not isinstance(source_commit, str) or len(source_commit) != 40:
        raise EvaluationPackageError(f"{task_id} source commit is invalid")
    profile = task.get("production_profile")
    if not isinstance(profile, dict) or set(profile) != {
        "hot_path",
        "post_update",
        "validation_only",
    }:
        raise EvaluationPackageError(f"{task_id} production profile is invalid")
    production_profile = {
        key: _string_list(profile[key], f"{task_id} production profile")
        for key in ("hot_path", "post_update", "validation_only")
    }
    for allowed in allowed_paths:
        fixture = _source_path(gold_root, allowed, f"{task_id} exact reference")
        status = fixture.lstat()
        if (
            not stat.S_ISREG(status.st_mode)
            or stat.S_ISLNK(status.st_mode)
            or status.st_nlink != 1
        ):
            raise EvaluationPackageError(
                f"{task_id} exact reference is incomplete"
            )
    return _TaskSource(
        task_id=task_id,
        workspace=workspace,
        baseline_commit=head,
        source_commit=source_commit,
        source_tree=source_tree,
        gold_root=gold_root,
        evaluator=evaluator,
        allowed_paths=allowed_paths,
        production_profile=production_profile,
        stage2_specification=stage2_path,
        stage2_specification_sha256=stage2_sha256,
    )


def _load_dependencies(source_state: Mapping[str, object]) -> tuple[_DependencySource, ...]:
    repositories = source_state.get("repositories")
    if source_state.get("schema_version") != 1 or not isinstance(repositories, list):
        raise EvaluationPackageError("source provenance authority is invalid")
    selected: dict[str, _DependencySource] = {}
    for raw in repositories:
        if not isinstance(raw, dict):
            raise EvaluationPackageError("source repository record is invalid")
        name = raw.get("name")
        if name not in _DEPENDENCY_NAMES:
            continue
        if name in selected:
            raise EvaluationPackageError(
                f"{name} provenance authority is duplicated"
            )
        path_value = raw.get("path")
        commit = raw.get("head")
        if not isinstance(path_value, str) or not isinstance(commit, str):
            raise EvaluationPackageError(f"{name} provenance is invalid")
        repository = _exact_directory(Path(path_value), f"{name} repository")
        if _git(repository, "rev-parse", "HEAD") != commit:
            raise EvaluationPackageError(f"{name} commit changed")
        if _git(repository, "status", "--porcelain=v1", "--untracked-files=all"):
            raise EvaluationPackageError(f"{name} repository is contaminated")
        tree = _git(repository, "rev-parse", f"{commit}^{{tree}}")
        selected[name] = _DependencySource(
            name=name,
            repository=repository,
            commit=commit,
            tree=tree,
            namespace_path=str(repository),
        )
    if tuple(sorted(selected)) != _DEPENDENCY_NAMES:
        raise EvaluationPackageError("historical dependency authority is incomplete")
    return tuple(selected[name] for name in _DEPENDENCY_NAMES)


def _materialize_task(
    task: _TaskSource,
    staging: Path,
    *,
    evaluator_sha256: str,
) -> dict[str, object]:
    archive = staging / "baseline-archives" / f"{task.task_id}.tar"
    archive.parent.mkdir(parents=True, exist_ok=True)
    _git_archive(task.workspace, task.baseline_commit, archive)
    fixture = staging / "protected-fixtures" / task.task_id
    _copy_tree(task.gold_root, fixture)
    with tempfile.TemporaryDirectory(prefix=f"exact-{task.task_id}-") as temporary:
        reference_root = Path(temporary) / "reference"
        reference_root.mkdir()
        _extract_regular_archive(archive, reference_root)
        for relative in task.allowed_paths:
            source = _source_path(task.gold_root, relative, "exact reference")
            destination = reference_root.joinpath(*PurePosixPath(relative).parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists() or destination.is_symlink():
                destination.unlink()
            _copy_regular_file(source, destination)
            destination.chmod(0o644)
        reference_archive = (
            staging / "exact-reference" / f"{task.task_id}.tar"
        )
        reference_archive.parent.mkdir(parents=True, exist_ok=True)
        _write_canonical_tar(reference_root, reference_archive)
    return {
        "task_id": task.task_id,
        "baseline_archive": archive.relative_to(staging).as_posix(),
        "baseline_archive_sha256": _sha256_file(archive),
        "source_commit": task.source_commit,
        "source_tree": task.source_tree,
        "expected_changed_paths": list(task.allowed_paths),
        "production_profile": task.production_profile,
        "tests": [
            {
                "id": "historical-functional",
                "runner": "python_script_v1",
                "script": "evaluators/historical-functional.py",
                "script_sha256": evaluator_sha256,
                "arguments": [
                    "functional",
                    task.task_id,
                    "{workspace}",
                    (
                        "{evaluation_package}/protected-fixtures/"
                        f"{task.task_id}"
                    ),
                ],
                "timeout_seconds": 1800,
                "max_stdout_bytes": 1_048_576,
                "max_stderr_bytes": 1_048_576,
            }
        ],
        "exact_reference_archive": (
            reference_archive.relative_to(staging).as_posix()
        ),
        "exact_reference_archive_sha256": _sha256_file(reference_archive),
    }


def _materialize_dependencies(
    dependencies: tuple[_DependencySource, ...],
    staging: Path,
) -> dict[str, object]:
    records: list[dict[str, object]] = []
    for dependency in dependencies:
        with tempfile.TemporaryDirectory(
            prefix=f"{dependency.name}-archive-"
        ) as temporary:
            archive = Path(temporary) / "dependency.tar"
            _git_archive(dependency.repository, dependency.commit, archive)
            destination = staging / "dependencies" / dependency.name
            destination.mkdir(parents=True)
            _extract_regular_archive(archive, destination)
        records.append(
            {
                "role": dependency.name,
                "path": destination.relative_to(staging).as_posix(),
                "source_commit": dependency.commit,
                "source_tree": dependency.tree,
                "namespace_path": dependency.namespace_path,
            }
        )
    return {
        "profile": "gl_historical_replay_v1",
        "dependency_roots": records,
    }


def _provenance_record(source: _SourceAuthority) -> dict[str, object]:
    return {
        "schema_version": 1,
        "campaign_id": source.campaign_id,
        "source_campaign_manifest_sha256": source.campaign_manifest_sha256,
        "source_preparation_report_sha256": (
            source.preparation_report_sha256
        ),
        "source_state_sha256": source.source_state_sha256,
        "tasks": [
            {
                "task_id": task.task_id,
                "source_commit": task.source_commit,
                "source_tree": task.source_tree,
                "baseline_commit": task.baseline_commit,
                "stage2_specification_sha256": (
                    task.stage2_specification_sha256
                ),
            }
            for task in source.tasks
        ],
        "dependencies": [
            {
                "name": dependency.name,
                "source_commit": dependency.commit,
                "source_tree": dependency.tree,
            }
            for dependency in source.dependencies
        ],
    }


def _source_fingerprint(source: _SourceAuthority) -> str:
    records: list[dict[str, object]] = [
        {
            "role": "source_campaign",
            "device": _descriptor_identity(source.root_fd)[0],
            "inode": _descriptor_identity(source.root_fd)[1],
            "mode": _descriptor_identity(source.root_fd)[2],
        }
    ]
    if _path_identity(source.declared_root) != _descriptor_identity(
        source.root_fd
    ):
        raise EvaluationPackageError(
            "source prepared campaign pathname identity changed"
        )
    for path, expected_sha256 in (
        (source.campaign_manifest, source.campaign_manifest_sha256),
        (source.preparation_report, source.preparation_report_sha256),
        (source.source_state, source.source_state_sha256),
    ):
        records.append(
            _source_file_record(
                source.root,
                path,
                expected_sha256=expected_sha256,
            )
        )
    for task in source.tasks:
        records.append(
            _source_file_record(
                source.root,
                task.stage2_specification,
                expected_sha256=task.stage2_specification_sha256,
            )
        )
        records.append(
            _directory_identity(
                task.workspace,
                role=f"{task.task_id}_workspace",
            )
        )
        records.append(
            _directory_identity(
                task.workspace / ".git",
                role=f"{task.task_id}_git",
            )
        )
        records.append(
            _directory_identity(
                task.gold_root,
                role=f"{task.task_id}_protected_fixture",
            )
        )
        records.extend(_source_tree_records(task.gold_root))
        records.append(_source_file_record(source.root, task.evaluator))
        records.append(
            {
                "role": "workspace",
                "task_id": task.task_id,
                "head": _git(task.workspace, "rev-parse", "HEAD"),
                "tree": _git(task.workspace, "rev-parse", "HEAD^{tree}"),
                "status": _git(
                    task.workspace,
                    "status",
                    "--porcelain=v1",
                    "--untracked-files=all",
                ),
            }
        )
    for dependency in source.dependencies:
        records.append(
            {
                "role": "dependency",
                "name": dependency.name,
                "head": _git(dependency.repository, "rev-parse", "HEAD"),
                "tree": _git(
                    dependency.repository,
                    "rev-parse",
                    f"{dependency.commit}^{{tree}}",
                ),
                "status": _git(
                    dependency.repository,
                    "status",
                    "--porcelain=v1",
                    "--untracked-files=all",
                ),
            }
        )
        records.append(
            _directory_identity(
                dependency.repository,
                role=f"{dependency.name}_repository",
            )
        )
        records.append(
            _directory_identity(
                dependency.repository / ".git",
                role=f"{dependency.name}_git",
            )
        )
    return _sha256_json(records)


def _source_file_record(
    root: Path,
    path: Path,
    *,
    expected_sha256: str | None = None,
) -> dict[str, object]:
    status = path.lstat()
    if (
        not stat.S_ISREG(status.st_mode)
        or stat.S_ISLNK(status.st_mode)
        or status.st_nlink != 1
    ):
        raise EvaluationPackageError("source authority contains an unsafe file")
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError:
        relative = str(path)
    digest = _sha256_file(path)
    if expected_sha256 is not None and digest != expected_sha256:
        raise EvaluationPackageError(
            "historical source authority changed after metadata loading"
        )
    return {
        "path": relative,
        "device": status.st_dev,
        "inode": status.st_ino,
        "mode": stat.S_IMODE(status.st_mode),
        "size": status.st_size,
        "mtime_ns": status.st_mtime_ns,
        "ctime_ns": status.st_ctime_ns,
        "sha256": digest,
    }


def _directory_identity(path: Path, *, role: str) -> dict[str, object]:
    status = path.lstat()
    if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
        raise EvaluationPackageError("source authority directory is invalid")
    return {
        "role": role,
        "device": status.st_dev,
        "inode": status.st_ino,
        "mode": stat.S_IMODE(status.st_mode),
    }


def _source_tree_records(root: Path) -> list[dict[str, object]]:
    _validate_source_tree(root, allow_executable=False)
    records: list[dict[str, object]] = []
    for path in sorted(root.rglob("*")):
        status = path.lstat()
        relative = path.relative_to(root).as_posix()
        if stat.S_ISDIR(status.st_mode):
            records.append(
                {
                    "path": relative,
                    "type": "directory",
                    "device": status.st_dev,
                    "inode": status.st_ino,
                    "mode": stat.S_IMODE(status.st_mode),
                }
            )
        else:
            records.append(_source_file_record(root, path))
    return records


def _validate_source_tree(root: Path, *, allow_executable: bool) -> None:
    directory = root
    root_status = directory.lstat()
    if stat.S_ISLNK(root_status.st_mode) or not stat.S_ISDIR(root_status.st_mode):
        raise EvaluationPackageError("historical source tree is not a directory")
    for path in directory.rglob("*"):
        status = path.lstat()
        if status.st_dev != root_status.st_dev:
            raise EvaluationPackageError(
                "historical source tree crosses a filesystem boundary"
            )
        if stat.S_ISLNK(status.st_mode) or not (
            stat.S_ISDIR(status.st_mode) or stat.S_ISREG(status.st_mode)
        ):
            raise EvaluationPackageError(
                "historical source tree contains an unsupported object"
            )
        if stat.S_ISREG(status.st_mode):
            if status.st_nlink != 1:
                raise EvaluationPackageError(
                    "historical source tree contains a hard-linked file"
                )
            if not allow_executable and stat.S_IMODE(status.st_mode) & 0o111:
                raise EvaluationPackageError(
                    "historical source tree contains an executable file"
                )


def _copy_tree(source: Path, destination: Path) -> None:
    _validate_source_tree(source, allow_executable=False)
    before = _canonical_tree_content_manifest(source)
    destination.mkdir(parents=True)
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        target = destination / relative
        status = path.lstat()
        if stat.S_ISDIR(status.st_mode):
            target.mkdir()
            target.chmod(stat.S_IMODE(status.st_mode))
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            _copy_regular_file(path, target)
            target.chmod(stat.S_IMODE(status.st_mode))
    after = _canonical_tree_content_manifest(source)
    copied = _canonical_tree_content_manifest(destination)
    if before != after or before != copied:
        raise EvaluationPackageError(
            "historical source tree changed while it was copied"
        )


def _canonical_tree_content_manifest(root: Path) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for path in sorted(root.rglob("*")):
        status = path.lstat()
        relative = path.relative_to(root).as_posix()
        if stat.S_ISDIR(status.st_mode):
            object_type = "directory"
            byte_length = 0
            digest = _EMPTY_SHA256
        elif stat.S_ISREG(status.st_mode) and status.st_nlink == 1:
            object_type = "regular"
            byte_length = status.st_size
            digest = _sha256_file(path)
        else:
            raise EvaluationPackageError(
                "historical source tree contains an unsupported object"
            )
        records.append(
            {
                "path_length": len(os.fsencode(relative)),
                "path": relative,
                "object_type": object_type,
                "mode": stat.S_IMODE(status.st_mode),
                "byte_length": byte_length,
                "content_sha256": digest,
            }
        )
    return records


def _copy_regular_file(source: Path, destination: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(
        os, "O_NOFOLLOW", 0
    )
    source_fd = os.open(source, flags)
    try:
        before = os.fstat(source_fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise EvaluationPackageError("source file is not independently owned")
        before_digest = _sha256_fd(source_fd)
        os.lseek(source_fd, 0, os.SEEK_SET)
        destination.parent.mkdir(parents=True, exist_ok=True)
        output_flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
        )
        output_fd = os.open(destination, output_flags, 0o600)
        copied_digest = hashlib.sha256()
        try:
            while True:
                chunk = os.read(source_fd, 1024 * 1024)
                if not chunk:
                    break
                copied_digest.update(chunk)
                view = memoryview(chunk)
                while view:
                    written = os.write(output_fd, view)
                    view = view[written:]
            os.fsync(output_fd)
        finally:
            os.close(output_fd)
        after_digest = _sha256_fd(source_fd)
        after = os.fstat(source_fd)
        identity = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        if identity != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ) or not (
            before_digest
            == copied_digest.hexdigest()
            == _sha256_file(destination)
            == after_digest
        ):
            raise EvaluationPackageError("source file changed while it was copied")
        if destination.stat().st_ino == before.st_ino:
            raise EvaluationPackageError("package copy reused a source inode")
    finally:
        os.close(source_fd)


def _sha256_fd(descriptor: int) -> str:
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
    return digest.hexdigest()


def _git_archive(repository: Path, commit: str, output: Path) -> None:
    completed = subprocess.run(
        (
            "/usr/bin/git",
            "-C",
            str(repository),
            "archive",
            "--format=tar",
            "--output",
            str(output),
            commit,
        ),
        cwd=None,
        env=_GIT_ENV,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        check=False,
        close_fds=True,
        pass_fds=_proc_path_fds(repository),
        timeout=300,
    )
    if completed.returncode != 0:
        raise EvaluationPackageError("committed source archive could not be created")


def _extract_regular_archive(archive: Path, destination: Path) -> None:
    try:
        with tarfile.open(archive, mode="r:*") as opened:
            for member in opened.getmembers():
                relative = PurePosixPath(member.name)
                if (
                    relative.is_absolute()
                    or not relative.parts
                    or any(part in {"", ".", ".."} for part in relative.parts)
                    or not (member.isdir() or member.isreg())
                ):
                    raise EvaluationPackageError(
                        "committed archive contains an unsafe entry"
                    )
                target = destination.joinpath(*relative.parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    target.chmod(stat.S_IMODE(member.mode) & 0o755)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                source = opened.extractfile(member)
                if source is None:
                    raise EvaluationPackageError(
                        "committed archive entry is unavailable"
                    )
                with source, target.open("xb") as output:
                    shutil.copyfileobj(source, output)
                target.chmod(stat.S_IMODE(member.mode) & 0o755)
    except (OSError, tarfile.TarError) as exc:
        raise EvaluationPackageError(
            "committed archive could not be materialized"
        ) from exc


def _write_canonical_tar(source: Path, output: Path) -> None:
    with tarfile.open(output, mode="w", format=tarfile.GNU_FORMAT) as archive:
        for path in sorted(source.rglob("*")):
            relative = path.relative_to(source).as_posix()
            status = path.lstat()
            info = tarfile.TarInfo(relative + ("/" if path.is_dir() else ""))
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            info.mtime = 0
            info.mode = stat.S_IMODE(status.st_mode)
            if stat.S_ISDIR(status.st_mode):
                info.type = tarfile.DIRTYPE
                info.size = 0
                archive.addfile(info)
            elif stat.S_ISREG(status.st_mode):
                info.type = tarfile.REGTYPE
                info.size = status.st_size
                with path.open("rb") as content:
                    archive.addfile(info, content)
            else:
                raise EvaluationPackageError(
                    "exact reference contains an unsupported object"
                )


def _make_payload_read_only(root: Path) -> None:
    for path in sorted(root.rglob("*"), reverse=True):
        status = path.lstat()
        if stat.S_ISDIR(status.st_mode):
            path.chmod(0o500)
        elif stat.S_ISREG(status.st_mode):
            path.chmod(0o400)
        else:
            raise EvaluationPackageError(
                "evaluation package contains an unsupported object"
            )


def _package_entries(root: Path) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    for path in sorted(root.rglob("*")):
        if path.parent == root and path.name == PACKAGE_MANIFEST_NAME:
            continue
        status = path.lstat()
        relative = path.relative_to(root).as_posix()
        if stat.S_ISDIR(status.st_mode):
            object_type = "directory"
            byte_length = 0
            digest = _EMPTY_SHA256
        elif stat.S_ISREG(status.st_mode):
            if status.st_nlink != 1:
                raise EvaluationPackageError(
                    "evaluation package contains a hard-linked file"
                )
            object_type = "regular"
            byte_length = status.st_size
            digest = _sha256_file(path)
        else:
            raise EvaluationPackageError(
                "evaluation package contains an unsupported object"
            )
        entries.append(
            {
                "path_length": len(os.fsencode(relative)),
                "path": relative,
                "object_type": object_type,
                "mode": stat.S_IMODE(status.st_mode),
                "byte_length": byte_length,
                "content_sha256": digest,
                "file_role": _file_role(relative),
            }
        )
    return entries


def _file_role(relative: str) -> str:
    prefix = relative.split("/", 1)[0]
    roles = {
        "baseline-archives": "committed_baseline",
        "dependencies": "dependency_snapshot",
        "evaluation-config": "evaluation_configuration",
        "evaluators": "hidden_functional_evaluator",
        "exact-reference": "exact_historical_reference",
        "protected-fixtures": "hidden_functional_fixture",
        "provenance": "source_provenance",
    }
    try:
        return roles[prefix]
    except KeyError as exc:
        raise EvaluationPackageError(
            "evaluation package contains an undeclared file role"
        ) from exc


def _validate_package_configuration(
    root: Path,
    config: Mapping[str, object],
) -> None:
    if (
        set(config) - {"schema_version", "package_id", "runtime", "tasks"}
        or config.get("schema_version") != PACKAGE_SCHEMA_VERSION
        or not isinstance(config.get("package_id"), str)
    ):
        raise EvaluationPackageError(
            "evaluation package configuration is invalid"
        )
    tasks = _required_object_list(config.get("tasks"), "evaluation tasks")
    seen: set[str] = set()
    for task in tasks:
        task_id = _required_string(task, "task_id")
        if task_id in seen:
            raise EvaluationPackageError("evaluation task identity is duplicated")
        seen.add(task_id)
        for path_key, digest_key in (
            ("baseline_archive", "baseline_archive_sha256"),
            (
                "exact_reference_archive",
                "exact_reference_archive_sha256",
            ),
        ):
            path_value = task.get(path_key)
            digest = task.get(digest_key)
            if path_value is None and digest is None and path_key.startswith("exact"):
                continue
            _verified_package_file(
                root,
                path_value,
                digest,
                path_key.replace("_", " "),
            )
        tests = _required_object_list(task.get("tests"), f"{task_id} tests")
        if not tests:
            raise EvaluationPackageError(f"{task_id} functional tests are missing")
        for test in tests:
            if test.get("runner") != "python_script_v1":
                raise EvaluationPackageError(
                    f"{task_id} contains an unregistered evaluator"
                )
            _verified_package_file(
                root,
                test.get("script"),
                test.get("script_sha256"),
                f"{task_id} evaluator",
            )
    runtime = config.get("runtime")
    if runtime is not None:
        if not isinstance(runtime, dict) or set(runtime) != {
            "profile",
            "dependency_roots",
        }:
            raise EvaluationPackageError("evaluation runtime is invalid")
        dependencies = _required_object_list(
            runtime.get("dependency_roots"),
            "dependency roots",
        )
        for dependency in dependencies:
            relative = dependency.get("path")
            if not isinstance(relative, str):
                raise EvaluationPackageError("dependency root path is invalid")
            path = _source_path(root, relative, "dependency root")
            if not path.is_dir() or path.is_symlink():
                raise EvaluationPackageError("dependency root is not a directory")


def _verified_package_file(
    root: Path,
    relative: object,
    expected_sha256: object,
    label: str,
) -> Path:
    if not isinstance(relative, str) or not isinstance(expected_sha256, str):
        raise EvaluationPackageError(f"{label} identity is invalid")
    path = _source_path(root, relative, label)
    status = path.lstat()
    if (
        not stat.S_ISREG(status.st_mode)
        or stat.S_ISLNK(status.st_mode)
        or status.st_nlink != 1
        or _sha256_file(path) != expected_sha256
    ):
        raise EvaluationPackageError(f"{label} identity does not match")
    return path


def _new_output_path(
    output: Path,
    source: Path,
) -> _OutputTarget:
    absolute = Path(os.path.abspath(output))
    if output != absolute:
        raise EvaluationPackageError("output must be an exact absolute path")
    if absolute.exists() or absolute.is_symlink():
        raise EvaluationPackageError("output already exists")
    if _overlaps(absolute, source):
        raise EvaluationPackageError(
            "offline package must be outside its source campaign"
        )
    parts = absolute.parts
    if "runs" in parts and "prepared-campaigns" in parts:
        raise EvaluationPackageError(
            "offline package must be outside every visible campaign tree"
        )
    parent_path = absolute.parent
    created_parent: Path | None = None
    if not parent_path.exists() and not parent_path.is_symlink():
        grandparent = _exact_directory(parent_path.parent, "output grandparent")
        try:
            parent_path.mkdir(mode=0o700)
            _fsync_directory(grandparent)
            created_parent = parent_path
        except OSError as exc:
            raise EvaluationPackageError(
                "output parent could not be created"
            ) from exc
    parent = _exact_directory(parent_path, "output parent")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(
        os,
        "O_CLOEXEC",
        0,
    )
    try:
        parent_fd = os.open(parent, flags)
    except OSError as exc:
        raise EvaluationPackageError("output parent could not be pinned") from exc
    identity = _descriptor_identity(parent_fd)
    return _OutputTarget(
        destination=parent / absolute.name,
        parent_fd=parent_fd,
        parent_identity=identity,
        created_parent=created_parent,
    )


def _verify_output_target(target: _OutputTarget, source: Path) -> None:
    parent = _exact_directory(
        target.destination.parent,
        "output parent",
    )
    if (
        _path_identity(parent) != target.parent_identity
        or _descriptor_identity(target.parent_fd) != target.parent_identity
        or _overlaps(parent / target.destination.name, source)
    ):
        raise EvaluationPackageError(
            "output parent identity or separation changed"
        )


def _descriptor_identity(descriptor: int) -> tuple[int, int, int]:
    status = os.fstat(descriptor)
    if not stat.S_ISDIR(status.st_mode):
        raise EvaluationPackageError("pinned output parent is not a directory")
    return status.st_dev, status.st_ino, stat.S_IMODE(status.st_mode)


def _path_identity(path: Path) -> tuple[int, int, int]:
    status = path.lstat()
    if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
        raise EvaluationPackageError("filesystem path identity is invalid")
    return status.st_dev, status.st_ino, stat.S_IMODE(status.st_mode)


def _path_identity_at(descriptor: int, name: str) -> tuple[int, int, int]:
    status = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
    if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
        raise EvaluationPackageError("package staging identity is invalid")
    return status.st_dev, status.st_ino, stat.S_IMODE(status.st_mode)


def _source_path(root: Path, relative: str, label: str) -> Path:
    value = PurePosixPath(relative)
    if (
        value.is_absolute()
        or not value.parts
        or any(part in {"", ".", ".."} for part in value.parts)
    ):
        raise EvaluationPackageError(f"{label} path is invalid")
    try:
        candidate = root.joinpath(*value.parts)
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
    except (OSError, RuntimeError, ValueError) as exc:
        raise EvaluationPackageError(f"{label} escapes source authority") from exc
    return candidate


def _exact_source_absolute(
    root: Path,
    declared_root: Path,
    value: object,
    label: str,
) -> Path:
    if not isinstance(value, str):
        raise EvaluationPackageError(f"{label} path is invalid")
    path = Path(value)
    try:
        absolute = Path(os.path.abspath(path))
        relative = absolute.relative_to(declared_root)
        selected = _source_path(root, relative.as_posix(), label)
    except (OSError, RuntimeError, ValueError, EvaluationPackageError) as exc:
        raise EvaluationPackageError(f"{label} escapes source authority") from exc
    if path != absolute:
        raise EvaluationPackageError(f"{label} path is not canonical")
    return selected


def _relative_path(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise EvaluationPackageError(f"{label} is invalid")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise EvaluationPackageError(f"{label} is invalid")
    return path.as_posix()


def _exact_directory(path: Path, label: str) -> Path:
    try:
        absolute = Path(os.path.abspath(path))
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise EvaluationPackageError(f"{label} could not be resolved") from exc
    if absolute != resolved or not resolved.is_dir() or resolved.is_symlink():
        raise EvaluationPackageError(f"{label} is not an exact directory")
    return resolved


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ("/usr/bin/git", "-C", str(repository), *arguments),
        cwd=None,
        env=_GIT_ENV,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
        close_fds=True,
        pass_fds=_proc_path_fds(repository),
        timeout=60,
    )
    if completed.returncode != 0:
        raise EvaluationPackageError("Git provenance inspection failed")
    return completed.stdout.decode("utf-8", errors="strict").strip()


def _proc_path_fds(path: Path) -> tuple[int, ...]:
    parts = path.parts
    if len(parts) >= 5 and parts[:4] == ("/", "proc", "self", "fd"):
        try:
            return (int(parts[4]),)
        except ValueError:
            return ()
    return ()


def _objects_by_id(
    values: list[object],
    key: str,
) -> dict[str, Mapping[str, object]]:
    result: dict[str, Mapping[str, object]] = {}
    for value in values:
        if not isinstance(value, dict):
            raise EvaluationPackageError("historical metadata record is invalid")
        identifier = _required_string(value, key)
        if identifier in result:
            raise EvaluationPackageError("historical metadata identity is duplicated")
        result[identifier] = value
    return result


def _required_string(value: Mapping[str, object], key: str) -> str:
    selected = value.get(key)
    if not isinstance(selected, str) or not selected:
        raise EvaluationPackageError(f"historical {key} is invalid")
    return selected


def _string_list(value: object, label: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise EvaluationPackageError(f"{label} is invalid")
    return list(value)


def _required_object_list(
    value: object,
    label: str,
) -> list[Mapping[str, object]]:
    if not isinstance(value, list) or any(
        not isinstance(item, dict) for item in value
    ):
        raise EvaluationPackageError(f"{label} are invalid")
    return value


def _read_pinned_bytes(path: Path) -> tuple[bytes, str]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(
        os,
        "O_NOFOLLOW",
        0,
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise EvaluationPackageError(
            "historical metadata is unavailable"
        ) from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise EvaluationPackageError(
                "historical metadata is not independently owned"
            )
        chunks: list[bytes] = []
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
            digest.update(chunk)
        after = os.fstat(descriptor)
        if (
            (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            )
            != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            )
            or sum(len(chunk) for chunk in chunks) != before.st_size
        ):
            raise EvaluationPackageError(
                "historical metadata changed while it was loaded"
            )
        return b"".join(chunks), digest.hexdigest()
    finally:
        os.close(descriptor)


def _read_json_pinned(path: Path) -> tuple[dict[str, Any], str]:
    encoded, digest = _read_pinned_bytes(path)
    try:
        value = json.loads(encoded.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise EvaluationPackageError("historical JSON metadata is invalid") from exc
    if not isinstance(value, dict):
        raise EvaluationPackageError("historical JSON metadata must be an object")
    return value, digest


def _read_yaml_pinned(path: Path) -> tuple[dict[str, Any], str]:
    encoded, digest = _read_pinned_bytes(path)
    try:
        value = yaml.safe_load(encoded.decode("utf-8"))
    except (UnicodeError, yaml.YAMLError) as exc:
        raise EvaluationPackageError("historical YAML metadata is invalid") from exc
    if not isinstance(value, dict):
        raise EvaluationPackageError("historical YAML metadata must be an object")
    return value, digest


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EvaluationPackageError("historical JSON metadata is invalid") from exc
    if not isinstance(value, dict):
        raise EvaluationPackageError("historical JSON metadata must be an object")
    return value


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise EvaluationPackageError("historical YAML metadata is invalid") from exc
    if not isinstance(value, dict):
        raise EvaluationPackageError("historical YAML metadata must be an object")
    return value


def _write_json(path: Path, value: object) -> None:
    path.write_bytes(_json_bytes(value))
    with path.open("rb") as stream:
        os.fsync(stream.fileno())


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("ascii")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_json(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    ).hexdigest()


def _git_tree_oid(root: Path) -> str:
    """Derive a Git tree identity from regular files without Git metadata."""

    def object_oid(kind: bytes, content: bytes) -> bytes:
        header = kind + b" " + str(len(content)).encode("ascii") + b"\0"
        return hashlib.sha1(header + content).digest()  # noqa: S324

    def directory_oid(directory: Path) -> tuple[bytes, bool]:
        entries: list[tuple[bytes, bytes]] = []
        try:
            children = tuple(directory.iterdir())
        except OSError as exc:
            raise EvaluationPackageError(
                "packaged source tree could not be read"
            ) from exc
        for child in children:
            name = os.fsencode(child.name)
            if b"\0" in name or b"/" in name:
                raise EvaluationPackageError(
                    "packaged source tree contains an invalid name"
                )
            status = child.lstat()
            if stat.S_ISDIR(status.st_mode):
                nested, has_entries = directory_oid(child)
                if not has_entries:
                    continue
                mode = b"40000"
                oid = nested
                sort_key = name + b"/"
            elif stat.S_ISREG(status.st_mode):
                content = child.read_bytes()
                mode = (
                    b"100755"
                    if stat.S_IMODE(status.st_mode) & 0o111
                    else b"100644"
                )
                oid = object_oid(b"blob", content)
                sort_key = name
            else:
                raise EvaluationPackageError(
                    "packaged source tree contains an unsupported object"
                )
            entries.append(
                (sort_key, mode + b" " + name + b"\0" + oid)
            )
        payload = b"".join(entry for _key, entry in sorted(entries))
        return object_oid(b"tree", payload), bool(entries)

    oid, _has_entries = directory_oid(root)
    return oid.hex()


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _is_git_oid(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 40
        and all(character in "0123456789abcdef" for character in value)
    )


def _overlaps(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_tree(root: Path) -> None:
    for path in sorted(root.rglob("*")):
        status = path.lstat()
        if stat.S_ISREG(status.st_mode):
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    for path in sorted(
        (item for item in root.rglob("*") if item.is_dir()),
        key=lambda item: len(item.parts),
        reverse=True,
    ):
        _fsync_directory(path)
    _fsync_directory(root)


def _rename_noreplace(
    source: Path | str,
    destination: Path | str,
    *,
    source_dir_fd: int = -100,
    destination_dir_fd: int = -100,
    forbidden_roots: tuple[Path, ...] = (),
) -> None:
    if destination_dir_fd != -100 and forbidden_roots:
        destination_parent = Path(
            os.path.realpath(f"/proc/self/fd/{destination_dir_fd}")
        )
        if any(
            destination_parent == root
            or root in destination_parent.parents
            for root in forbidden_roots
        ):
            raise EvaluationPackageError(
                "atomic package publication entered forbidden authority"
            )
    library = ctypes.CDLL(None, use_errno=True)
    try:
        renameat2 = library.renameat2
    except AttributeError as exc:
        raise EvaluationPackageError(
            "atomic no-replace publication is unavailable"
        ) from exc
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    result = renameat2(
        source_dir_fd,
        os.fsencode(source),
        destination_dir_fd,
        os.fsencode(destination),
        1,
    )
    if result == 0:
        return
    selected_errno = ctypes.get_errno()
    if selected_errno == errno.EEXIST:
        raise EvaluationPackageError(
            "output appeared during atomic package publication"
        )
    raise EvaluationPackageError(
        f"atomic package publication failed with errno {selected_errno}"
    )


def _remove_staging(path: Path) -> None:
    if not path.exists():
        return
    for item in path.rglob("*"):
        with contextlib.suppress(OSError):
            item.chmod(0o700 if item.is_dir() else 0o600)
    path.chmod(0o700)
    shutil.rmtree(path)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point that prints only package identity, never protected content."""
    parser = argparse.ArgumentParser(
        prog="prepare-historical-replay-evaluation-package",
        description="Prepare a sealed non-model historical replay package.",
    )
    parser.add_argument("--source-prepared-campaign", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args(argv)
    try:
        output, digest = prepare_historical_replay_evaluation_package(
            arguments.source_prepared_campaign,
            arguments.output,
        )
    except EvaluationPackageError as exc:
        print(f"offline package preparation failed: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "output": str(output),
                "package_manifest_sha256": digest,
            },
            sort_keys=True,
        )
    )
    return 0


def command_report_main(argv: Sequence[str] | None = None) -> int:
    """Report one or two exact commands according to package availability."""
    parser = argparse.ArgumentParser(
        prog="report-historical-replay-evaluation-commands",
        description="Report deterministic offline replay next commands.",
    )
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--source-prepared-campaign", required=True, type=Path)
    parser.add_argument("--evaluation-package", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args(argv)
    try:
        report = historical_replay_command_report(
            candidate=arguments.candidate,
            source_prepared_campaign=arguments.source_prepared_campaign,
            evaluation_package=arguments.evaluation_package,
            output=arguments.output,
        )
    except EvaluationPackageError as exc:
        print(f"offline evaluation command reporting failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
