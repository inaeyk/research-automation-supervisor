"""Deterministic, non-model evaluation of an exported campaign candidate."""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import errno
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
from typing import Any

from research_automation_supervisor.offline_evaluation_package import (
    EvaluationPackageError,
    verify_evaluation_package,
)

CONFIG_RELATIVE_PATH = Path("evaluation-config/offline-evaluation.json")
REPORT_NAME = "historical-replay-report.json"
SCHEMA_VERSION = 1
REPORT_SCHEMA_VERSION = 3
MAX_CAPTURE_BYTES = 1_048_576
_BUBBLEWRAP = Path("/usr/bin/bwrap")
_GIT = Path("/usr/bin/git")
_PYTHON = Path("/usr/bin/python3").resolve(strict=True)
_CPP = Path("/usr/bin/g++").resolve(strict=True)
_FORTRAN = Path("/usr/bin/x86_64-linux-gnu-gfortran-15").resolve(strict=True)
_MAKE = Path("/usr/bin/make").resolve(strict=True)
_ASSEMBLER = Path("/usr/bin/as").resolve(strict=True)
_LINKER = Path("/usr/bin/ld").resolve(strict=True)
_SHELL = Path("/bin/sh").resolve(strict=True)
_DYNAMIC_LOADER = Path("/lib64/ld-linux-x86-64.so.2").resolve(strict=True)
_SYSTEM_RUNTIME_DIRECTORIES = (
    Path("/usr/include"),
    Path("/usr/lib/python3.14"),
    Path("/usr/lib/x86_64-linux-gnu"),
    Path("/usr/lib/gcc"),
    Path("/usr/libexec/gcc"),
)
_GIT_ENVIRONMENT = {
    "GIT_ASKPASS": "/nonexistent",
    "GIT_ATTR_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_DISCOVERY_ACROSS_FILESYSTEM": "0",
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_PROTOCOL_FROM_USER": "0",
    "GIT_SSH_COMMAND": "/nonexistent",
    "GIT_TERMINAL_PROMPT": "0",
    "SSH_ASKPASS": "/nonexistent",
    "XDG_CONFIG_HOME": "/tmp/home/.config",
}
_SAFE_DIAGNOSTIC_EXCEPTION_TYPES = {
    "CalledProcessError",
    "FileNotFoundError",
    "OSError",
    "PermissionError",
    "RuntimeError",
    "TimeoutError",
}
_SAFE_DIAGNOSTIC_EXECUTABLES = {
    "/usr/bin/as",
    "/usr/bin/g++",
    "/usr/bin/git",
    "/usr/bin/ld",
    "/usr/bin/make",
    "/usr/bin/python3",
    "/usr/bin/x86_64-linux-gnu-gfortran-15",
}
_SAFE_DIAGNOSTIC_ERRNOS = {
    str(value): value
    for value in (
        errno.EACCES,
        errno.ENOENT,
        errno.ENOEXEC,
        errno.EPERM,
    )
}
_DIAGNOSTIC_SIGNATURES = {
    "changed_path_failure": (
        b"changed path mismatch",
        b"changed paths differ",
        b"unexpected changed path",
    ),
    "compile_failure": (
        b"cc1plus:",
        b"compilation failed",
        b"compilation terminated",
        b"compile failed",
        b"g++: error:",
    ),
    "evaluator_runtime_failure": (
        b"runtimeerror",
        b"traceback (most recent call last)",
    ),
    "functional_assertion_or_mismatch": (
        b"assertion failed",
        b"assertionerror",
        b"functional mismatch",
        b"mismatch detected",
        b"tests failed",
        b"the following tests failed",
    ),
    "link_failure": (
        b"collect2: error:",
        b"ld returned",
        b"link failed",
        b"linker command failed",
        b"undefined reference",
    ),
    "missing_runtime_dependency": (
        b"command not found",
        b"filenotfounderror",
        b"no such file or directory",
    ),
    "sandbox_or_permission_failure": (
        b"operation not permitted",
        b"permission denied",
        b"permissionerror",
    ),
}


class OfflineEvaluationError(RuntimeError):
    """Raised when standalone evaluation authority or evidence is invalid."""


@dataclass
class _OutputDirectory:
    path: Path
    descriptor: int
    identity: tuple[int, int, int]
    staging_path: Path
    staging_name: str
    parent: Path
    parent_descriptor: int
    parent_identity: tuple[int, int, int]
    candidate: Path
    package: Path
    published: bool = False


def evaluate_historical_replay(
    candidate: Path,
    evaluation_package: Path,
    output: Path,
) -> Path:
    """Evaluate a finalized candidate without importing campaign/model runtime code."""
    source_candidate = _exact_directory(candidate, "candidate")
    source_package = _exact_directory(
        evaluation_package,
        "evaluation package",
    )
    candidate_fd = _pin_directory(source_candidate, "candidate")
    package_fd = _pin_directory(source_package, "evaluation package")
    output_target: _OutputDirectory | None = None
    try:
        initial_candidate_manifest = _verify_candidate(source_candidate)
        initial_package_manifest = _validate_evaluation_package(source_package)
        _require_pinned_path(source_candidate, candidate_fd, "candidate")
        _require_pinned_path(
            source_package,
            package_fd,
            "evaluation package",
        )
        output_target = _new_output_directory(
            output,
            source_candidate,
            source_package,
        )
        return _evaluate_pinned_authority(
            source_candidate,
            source_package,
            candidate_fd,
            package_fd,
            initial_candidate_manifest,
            initial_package_manifest,
            output_target,
        )
    finally:
        if output_target is not None:
            _cleanup_output_staging(output_target)
            os.close(output_target.descriptor)
            os.close(output_target.parent_descriptor)
        os.close(package_fd)
        os.close(candidate_fd)


def _evaluate_pinned_authority(
    source_candidate: Path,
    source_package: Path,
    candidate_fd: int,
    package_fd: int,
    initial_candidate_manifest: Mapping[str, object],
    initial_package_manifest: Mapping[str, object],
    output_target: _OutputDirectory,
) -> Path:
    with tempfile.TemporaryDirectory(
        prefix="historical-replay-evaluation-"
    ) as temporary:
        temporary_root = Path(temporary)
        candidate_root = temporary_root / "candidate"
        package_root = temporary_root / "evaluation-package"
        _copy_authority_tree(
            Path(f"/proc/self/fd/{candidate_fd}"),
            candidate_root,
        )
        _copy_authority_tree(
            Path(f"/proc/self/fd/{package_fd}"),
            package_root,
        )
        candidate_manifest = _verify_candidate(candidate_root)
        package_manifest = _validate_evaluation_package(package_root)
        if (
            candidate_manifest != initial_candidate_manifest
            or package_manifest != initial_package_manifest
        ):
            raise OfflineEvaluationError(
                "offline authority changed while it was snapshotted"
            )
        candidate_record = _read_object(candidate_root / "candidate.json")
        config_path = package_root / CONFIG_RELATIVE_PATH
        config = _read_object(config_path)
        _require_exact_keys(
            config,
            required={"schema_version", "package_id", "tasks"},
            optional={"runtime"},
            label="evaluation configuration",
        )
        if config["schema_version"] != SCHEMA_VERSION:
            raise OfflineEvaluationError(
                "unsupported evaluation configuration schema"
            )
        raw_tasks = config["tasks"]
        if not isinstance(raw_tasks, list) or not raw_tasks:
            raise OfflineEvaluationError(
                "evaluation configuration has no tasks"
            )
        candidate_tasks = _candidate_tasks(candidate_record)
        configured_ids = tuple(_task_id(item) for item in raw_tasks)
        if configured_ids != tuple(candidate_tasks):
            raise OfflineEvaluationError(
                "evaluation task order does not match the candidate"
            )
        runtime_mounts = _runtime_dependency_mounts(
            config.get("runtime"),
            package_root,
        )
        results: list[dict[str, object]] = []
        artifacts: dict[str, bytes] = {}
        scratch = temporary_root / "scratch"
        scratch.mkdir()
        for raw_task in raw_tasks:
            assert isinstance(raw_task, dict)
            results.append(
                _evaluate_task(
                    raw_task,
                    candidate_tasks[_task_id(raw_task)],
                    candidate_root,
                    package_root,
                    scratch,
                    runtime_mounts,
                    artifacts,
                )
            )
        functional_passed_tasks = sum(
            result["functional_passed"] is True for result in results
        )
        exact_match_tasks = sum(
            result["exact_match"] is True for result in results
        )
        strict_combined_passed_tasks = sum(
            result["strict_combined_passed"] is True
            for result in results
        )
        total_tasks = len(results)
        all_functional_passed = functional_passed_tasks == total_tasks
        all_exact_matched = exact_match_tasks == total_tasks
        report: dict[str, object] = {
            "schema_version": REPORT_SCHEMA_VERSION,
            "package_id": config["package_id"],
            "candidate_manifest_sha256": candidate_manifest[
                "candidate_manifest_sha256"
            ],
            "evaluation_configuration_sha256": _sha256_file(config_path),
            "evaluation_package_manifest_sha256": package_manifest[
                "package_manifest_sha256"
            ],
            "passed": all_functional_passed,
            "functional_passed_tasks": functional_passed_tasks,
            "exact_match_tasks": exact_match_tasks,
            "total_tasks": total_tasks,
            "all_functional_passed": all_functional_passed,
            "all_exact_matched": all_exact_matched,
            "strict_combined_passed": (
                all_functional_passed and all_exact_matched
            ),
            "tasks": results,
            "score": {
                "passed_tasks": functional_passed_tasks,
                "functional_passed_tasks": functional_passed_tasks,
                "exact_match_tasks": exact_match_tasks,
                "strict_combined_passed_tasks": (
                    strict_combined_passed_tasks
                ),
                "total_tasks": total_tasks,
            },
        }
        report["report_sha256"] = _sha256_json(report)
        _require_pinned_path(source_candidate, candidate_fd, "candidate")
        _require_pinned_path(
            source_package,
            package_fd,
            "evaluation package",
        )
        if (
            _verify_candidate(source_candidate) != initial_candidate_manifest
            or _validate_evaluation_package(source_package)
            != initial_package_manifest
        ):
            raise OfflineEvaluationError(
                "offline authority changed during evaluation"
            )
        report_path = _write_output_report(
            output_target,
            report,
            artifacts,
        )
    return report_path


def _copy_authority_tree(source: Path, destination: Path) -> None:
    try:
        shutil.copytree(
            source,
            destination,
            symlinks=True,
            copy_function=shutil.copy2,
        )
    except OSError as exc:
        raise OfflineEvaluationError(
            "offline evaluation authority could not be snapshotted"
        ) from exc


def _evaluate_task(
    task: Mapping[str, object],
    candidate_task: object,
    candidate_root: Path,
    package_root: Path,
    scratch: Path,
    runtime_mounts: tuple[tuple[Path, Path], ...],
    artifacts: dict[str, bytes],
) -> dict[str, object]:
    _require_exact_keys(
        task,
        required={
            "task_id",
            "baseline_archive",
            "baseline_archive_sha256",
            "source_commit",
            "source_tree",
            "tests",
        },
        optional={
            "exact_reference_archive",
            "exact_reference_archive_sha256",
            "expected_changed_paths",
            "production_profile",
        },
        label="evaluation task",
    )
    task_id = _task_id(task)
    if not isinstance(candidate_task, dict):
        raise OfflineEvaluationError("candidate task provenance is invalid")
    provenance = candidate_task.get("source_provenance")
    if not isinstance(provenance, dict):
        raise OfflineEvaluationError("candidate source provenance is missing")
    if (
        task["source_commit"] != provenance.get("source_commit")
        or task["source_tree"] != provenance.get("source_tree")
        or candidate_task.get("execution_baseline_tree")
        != provenance.get("source_tree")
    ):
        raise OfflineEvaluationError(
            "evaluation source identity does not match candidate provenance"
        )
    raw_tests = task["tests"]
    if not isinstance(raw_tests, list):
        raise OfflineEvaluationError("evaluation tests must be a list")
    for raw_test in raw_tests:
        _validate_test_definition(raw_test, package_root)
    task_scratch = scratch / task_id
    workspace = task_scratch / "workspace"
    workspace.mkdir(parents=True)
    baseline = _package_file(
        package_root,
        task["baseline_archive"],
        task["baseline_archive_sha256"],
        "baseline archive",
    )
    _extract_regular_archive(baseline, workspace)
    if _git_tree_oid(workspace) != provenance.get("source_tree"):
        raise OfflineEvaluationError(
            "evaluation baseline tree does not match candidate source provenance"
        )
    _initialize_ephemeral_git_baseline(
        workspace,
        task_scratch,
        str(provenance["source_tree"]),
    )
    _apply_candidate_changes(
        workspace,
        candidate_root / "tasks" / task_id,
    )
    changed_paths = _candidate_changed_paths(
        candidate_root / "tasks" / task_id / "git-evidence.json"
    )
    expected_changed = task.get("expected_changed_paths")
    changed_paths_passed = (
        True
        if expected_changed is None
        else _canonical_changed_paths(
            _string_list(expected_changed, "expected changed paths"),
            "expected changed paths",
        )
        == changed_paths
    )
    production_profile = _production_profile_analysis(
        task.get("production_profile"),
        changed_paths,
    )
    exact_result: dict[str, object] | None = None
    reference_name = task.get("exact_reference_archive")
    reference_sha = task.get("exact_reference_archive_sha256")
    if reference_name is not None or reference_sha is not None:
        if reference_name is None or reference_sha is None:
            raise OfflineEvaluationError(
                "exact reference archive identity is incomplete"
            )
        reference_archive = _package_file(
            package_root,
            reference_name,
            reference_sha,
            "exact reference archive",
        )
        reference = task_scratch / "exact-reference"
        reference.mkdir()
        _extract_regular_archive(reference_archive, reference)
        actual_manifest = _tree_manifest(workspace)
        expected_manifest = _tree_manifest(reference)
        exact_result = {
            "passed": actual_manifest == expected_manifest,
            "candidate_tree_sha256": _sha256_json(actual_manifest),
            "reference_tree_sha256": _sha256_json(expected_manifest),
        }
    tests = []
    for index, item in enumerate(raw_tests):
        tests.append(
            _run_test(
                item,
                workspace,
                package_root,
                task_scratch,
                runtime_mounts,
                artifacts,
                f"{task_id}-{index:02d}",
            )
        )
    functional_tests_passed = all(
        test["passed"] is True for test in tests
    )
    functional_passed = (
        changed_paths_passed
        and production_profile["passed"] is True
        and functional_tests_passed
    )
    exact_match = (
        None if exact_result is None else exact_result["passed"] is True
    )
    strict_combined_passed = functional_passed and exact_match is not False
    return {
        "task_id": task_id,
        "passed": functional_passed,
        "changed_paths": list(changed_paths),
        "changed_paths_passed": changed_paths_passed,
        "functional_tests_passed": functional_tests_passed,
        "production_profile": production_profile,
        "production_profile_passed": production_profile["passed"],
        "functional_passed": functional_passed,
        "exact_match": exact_match,
        "strict_combined_passed": strict_combined_passed,
        "tests": tests,
        "exact_comparison": exact_result,
    }


def _initialize_ephemeral_git_baseline(
    workspace: Path,
    scratch: Path,
    expected_tree: str,
) -> None:
    if (
        not _GIT.is_file()
        or _GIT.is_symlink()
        or not os.access(_GIT, os.X_OK)
    ):
        raise OfflineEvaluationError(
            "Git is required for sealed offline evaluation"
        )
    home = scratch / "git-home"
    template = scratch / "git-template"
    xdg_config = scratch / "git-xdg"
    home.mkdir()
    template.mkdir()
    xdg_config.mkdir()
    environment = {
        **_GIT_ENVIRONMENT,
        "GIT_AUTHOR_DATE": "1970-01-01T00:00:00Z",
        "GIT_AUTHOR_EMAIL": "offline-evaluator@example.invalid",
        "GIT_AUTHOR_NAME": "Offline Replay Evaluator",
        "GIT_COMMITTER_DATE": "1970-01-01T00:00:00Z",
        "GIT_COMMITTER_EMAIL": "offline-evaluator@example.invalid",
        "GIT_COMMITTER_NAME": "Offline Replay Evaluator",
        "HOME": str(home),
        "LC_ALL": "C",
        "PATH": "/usr/bin",
        "XDG_CONFIG_HOME": str(xdg_config),
    }

    def run(
        *arguments: str,
        input_bytes: bytes | None = None,
    ) -> bytes:
        completed = subprocess.run(
            (str(_GIT), "-C", str(workspace), *arguments),
            cwd=None,
            env=environment,
            input=b"" if input_bytes is None else input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            close_fds=True,
            timeout=120,
        )
        if completed.returncode != 0:
            raise OfflineEvaluationError(
                "ephemeral evaluation Git baseline could not be created"
            )
        return completed.stdout

    run(
        "init",
        "--quiet",
        "--initial-branch=offline-baseline",
        f"--template={template}",
    )
    private_attributes = workspace / ".git/info/attributes"
    private_attributes.parent.mkdir()
    private_attributes.write_text(
        "* -crlf -eol -filter -ident -text -working-tree-encoding\n",
        encoding="ascii",
    )
    index_entries = bytearray()
    for path in sorted(workspace.rglob("*")):
        relative = path.relative_to(workspace)
        if relative.parts[:1] == (".git",) or path.is_dir():
            continue
        status = path.lstat()
        if not stat.S_ISREG(status.st_mode):
            raise OfflineEvaluationError(
                "evaluation baseline contains an unsupported Git object"
            )
        relative_text = relative.as_posix()
        object_id = run(
            "hash-object",
            "-w",
            "--no-filters",
            "--",
            relative_text,
        ).decode("ascii", errors="strict").strip()
        if not re.fullmatch(r"[0-9a-f]{40}", object_id):
            raise OfflineEvaluationError(
                "evaluation baseline Git object identity is invalid"
            )
        mode = (
            "100755"
            if stat.S_IMODE(status.st_mode) & 0o111
            else "100644"
        )
        index_entries.extend(
            f"{mode} {object_id}\t{relative_text}".encode(
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
    if run("write-tree").decode("ascii", errors="strict").strip() != expected_tree:
        raise OfflineEvaluationError(
            "ephemeral evaluation Git baseline is inconsistent"
        )
    run(
        "commit",
        "--quiet",
        "--no-gpg-sign",
        "--no-verify",
        "-m",
        "sealed offline baseline",
    )
    selected_tree = run("rev-parse", "HEAD^{tree}").decode(
        "ascii",
        errors="strict",
    ).strip()
    if selected_tree != expected_tree or run(
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
    ):
        raise OfflineEvaluationError(
            "ephemeral evaluation Git baseline is inconsistent"
        )


def _run_test(
    raw: object,
    workspace: Path,
    package_root: Path,
    scratch: Path,
    runtime_mounts: tuple[tuple[Path, Path], ...],
    artifacts: dict[str, bytes],
    artifact_stem: str,
) -> dict[str, object]:
    _validate_test_definition(raw, package_root)
    assert isinstance(raw, dict)
    test_id = raw["id"]
    assert isinstance(test_id, str)
    script = _package_file(
        package_root,
        raw["script"],
        raw["script_sha256"],
        "evaluation script",
    )
    arguments = _string_list(
        raw.get("arguments", []),
        "evaluation test arguments",
    )
    timeout = _bounded_int(raw.get("timeout_seconds", 300), 1, 3600, "timeout")
    stdout_limit = _bounded_int(
        raw.get("max_stdout_bytes", MAX_CAPTURE_BYTES),
        0,
        MAX_CAPTURE_BYTES,
        "stdout limit",
    )
    stderr_limit = _bounded_int(
        raw.get("max_stderr_bytes", MAX_CAPTURE_BYTES),
        0,
        MAX_CAPTURE_BYTES,
        "stderr limit",
    )
    canonical_arguments = tuple(
        argument.replace("{workspace}", "/workspace").replace(
            "{evaluation_package}",
            "/evaluation",
        )
        for argument in arguments
    )
    script_relative = script.relative_to(package_root).as_posix()
    inner = (
        "/usr/bin/python3",
        "-I",
        "-S",
        "-B",
        f"/evaluation/{script_relative}",
        *canonical_arguments,
    )
    command = _offline_bubblewrap_command(
        workspace,
        package_root,
        inner,
        runtime_mounts,
    )
    timed_out = False
    with (
        tempfile.TemporaryFile(dir=scratch) as stdout_file,
        tempfile.TemporaryFile(dir=scratch) as stderr_file,
    ):
        process = subprocess.Popen(
            command,
            cwd=None,
            env={"LC_ALL": "C", "PATH": "/usr/bin:/bin"},
            stdin=subprocess.DEVNULL,
            stdout=stdout_file,
            stderr=stderr_file,
            close_fds=True,
            start_new_session=True,
        )
        try:
            return_code: int | None = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            return_code = None
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        stdout = _bounded_stream_record(stdout_file, stdout_limit)
        stderr = _bounded_stream_record(stderr_file, stderr_limit)
    stdout_artifact = f"artifacts/{artifact_stem}.stdout"
    stderr_artifact = f"artifacts/{artifact_stem}.stderr"
    stdout_artifact_bytes = _diagnostic_artifact_bytes("stdout", stdout)
    stderr_artifact_bytes = _diagnostic_artifact_bytes("stderr", stderr)
    artifacts[stdout_artifact] = stdout_artifact_bytes
    artifacts[stderr_artifact] = stderr_artifact_bytes
    diagnostic = _classify_test_diagnostic(
        return_code=return_code,
        timed_out=timed_out,
        stdout=stdout,
        stderr=stderr,
    )
    return {
        "id": test_id,
        "runner": "python_script_v1",
        "script": script_relative,
        "script_sha256": raw["script_sha256"],
        "arguments": list(canonical_arguments),
        "passed": not timed_out and return_code == 0,
        "exit_code": return_code,
        "timed_out": timed_out,
        "stdout_sha256": stdout["captured_prefix_sha256"],
        "stderr_sha256": stderr["captured_prefix_sha256"],
        "stdout_observed_sha256": stdout["observed_sha256"],
        "stderr_observed_sha256": stderr["observed_sha256"],
        "stdout_artifact": stdout_artifact,
        "stderr_artifact": stderr_artifact,
        "stdout_artifact_sha256": hashlib.sha256(
            stdout_artifact_bytes
        ).hexdigest(),
        "stderr_artifact_sha256": hashlib.sha256(
            stderr_artifact_bytes
        ).hexdigest(),
        "stdout_byte_length": stdout["captured_prefix_byte_length"],
        "stderr_byte_length": stderr["captured_prefix_byte_length"],
        "stdout_observed_byte_length": stdout["observed_byte_length"],
        "stderr_observed_byte_length": stderr["observed_byte_length"],
        "stdout_truncated": stdout["truncated"],
        "stderr_truncated": stderr["truncated"],
        "diagnostic": diagnostic,
    }


def _bounded_stream_record(
    stream: Any,
    limit: int,
) -> dict[str, object]:
    stream.flush()
    stream.seek(0)
    digest = hashlib.sha256()
    prefix = bytearray()
    observed_length = 0
    while True:
        chunk = stream.read(65_536)
        if not chunk:
            break
        digest.update(chunk)
        observed_length += len(chunk)
        if len(prefix) < limit:
            prefix.extend(chunk[: limit - len(prefix)])
    captured = bytes(prefix)
    return {
        "captured_prefix": captured,
        "captured_prefix_sha256": hashlib.sha256(captured).hexdigest(),
        "captured_prefix_byte_length": len(captured),
        "observed_sha256": digest.hexdigest(),
        "observed_byte_length": observed_length,
        "truncated": observed_length > len(captured),
    }


def _diagnostic_artifact_bytes(
    stream_name: str,
    record: Mapping[str, object],
) -> bytes:
    captured = record["captured_prefix"]
    assert isinstance(captured, bytes)
    text = captured.decode("utf-8", errors="replace")
    exception_type: str | None = None
    error_number: int | None = None
    qualified_executable: str | None = None
    for line in text.splitlines():
        match = re.match(
            r"^(?:[A-Za-z_][A-Za-z0-9_.]*\.)?"
            r"([A-Za-z_][A-Za-z0-9_]*(?:Error|Exception))(?::|$)",
            line,
        )
        if (
            match is not None
            and match.group(1) in _SAFE_DIAGNOSTIC_EXCEPTION_TYPES
        ):
            exception_type = match.group(1)
            errno_match = re.search(r"\[Errno ([0-9]+)\]", line)
            if errno_match is not None:
                error_number = _SAFE_DIAGNOSTIC_ERRNOS.get(
                    errno_match.group(1)
                )
            executable_match = re.search(
                r"['\"](/usr/bin/[A-Za-z0-9_.+-]+)['\"]",
                line,
            )
            if (
                executable_match is not None
                and executable_match.group(1)
                in _SAFE_DIAGNOSTIC_EXECUTABLES
            ):
                qualified_executable = executable_match.group(1)
    artifact = {
        "schema_version": 1,
        "artifact_role": "bounded_process_stream_diagnostic",
        "stream": stream_name,
        "content_policy": "untrusted_output_digest_only",
        "captured_prefix_sha256": record["captured_prefix_sha256"],
        "captured_prefix_byte_length": record[
            "captured_prefix_byte_length"
        ],
        "observed_sha256": record["observed_sha256"],
        "observed_byte_length": record["observed_byte_length"],
        "truncated": record["truncated"],
        "python_exception": (
            None
            if exception_type is None
            else {
                "type": exception_type,
                "errno": error_number,
                "qualified_executable": qualified_executable,
            }
        ),
    }
    return _json_bytes(artifact)


def _classify_test_diagnostic(
    *,
    return_code: int | None,
    timed_out: bool,
    stdout: Mapping[str, object],
    stderr: Mapping[str, object],
) -> dict[str, object]:
    stdout_bytes = stdout["captured_prefix"]
    stderr_bytes = stderr["captured_prefix"]
    assert isinstance(stdout_bytes, bytes)
    assert isinstance(stderr_bytes, bytes)
    stdout_length = stdout["observed_byte_length"]
    stderr_length = stderr["observed_byte_length"]
    assert isinstance(stdout_length, int)
    assert isinstance(stderr_length, int)
    signals = {
        "stdout_empty" if stdout_length == 0 else "stdout_nonempty",
        "stderr_empty" if stderr_length == 0 else "stderr_nonempty",
    }
    if stdout["truncated"] is True:
        signals.add("stdout_truncated")
    if stderr["truncated"] is True:
        signals.add("stderr_truncated")
    if timed_out:
        category = "evaluator_runtime_failure"
        signals.update({"exit_unavailable", "timeout"})
    elif return_code == 0:
        category = "passed"
        signals.add("exit_zero")
    else:
        signals.add("exit_nonzero")
        malformed = b"\x00" in stdout_bytes or b"\x00" in stderr_bytes
        truncated_decode_failure = False
        decoded_streams: list[str] = []
        for raw_bytes, record in (
            (stdout_bytes, stdout),
            (stderr_bytes, stderr),
        ):
            try:
                decoded_streams.append(
                    raw_bytes.decode("utf-8", errors="strict")
                )
            except UnicodeDecodeError:
                decoded_streams.append("")
                if record["truncated"] is True:
                    truncated_decode_failure = True
                else:
                    malformed = True
        if malformed:
            category = "malformed_evaluator_output"
            signals.add("malformed_or_binary_output")
        elif truncated_decode_failure:
            category = "unknown_nonzero_exit"
            signals.add("truncated_unclassifiable_output")
        else:
            normalized = "\n".join(decoded_streams).casefold().encode(
                "utf-8"
            )
            matches = {
                candidate
                for candidate, signatures in _DIAGNOSTIC_SIGNATURES.items()
                if any(signature in normalized for signature in signatures)
            }
            if (
                "evaluator_runtime_failure" in matches
                and len(matches) > 1
            ):
                matches.remove("evaluator_runtime_failure")
            signals.update(f"{match}_signature" for match in matches)
            if (
                stdout["truncated"] is True
                or stderr["truncated"] is True
            ):
                category = "unknown_nonzero_exit"
                signals.add("truncated_unclassifiable_output")
            elif len(matches) == 1:
                category = next(iter(matches))
            elif len(matches) > 1:
                category = "unknown_nonzero_exit"
                signals.add("ambiguous_failure_signatures")
            else:
                category = "unknown_nonzero_exit"
                signals.add("no_allowlisted_failure_signature")
    return {
        "diagnostic_category": category,
        "diagnostic_signals": sorted(signals),
        "stdout_observed_byte_length": stdout["observed_byte_length"],
        "stderr_observed_byte_length": stderr["observed_byte_length"],
        "stdout_observed_sha256": stdout["observed_sha256"],
        "stderr_observed_sha256": stderr["observed_sha256"],
        "process_exit_code": return_code,
        "timed_out": timed_out,
    }


def _validate_test_definition(
    raw: object,
    package_root: Path,
) -> None:
    if not isinstance(raw, dict):
        raise OfflineEvaluationError("evaluation test must be an object")
    _require_exact_keys(
        raw,
        required={"id", "runner", "script", "script_sha256"},
        optional={
            "arguments",
            "timeout_seconds",
            "max_stdout_bytes",
            "max_stderr_bytes",
        },
        label="evaluation test",
    )
    test_id = raw["id"]
    if not isinstance(test_id, str) or not test_id:
        raise OfflineEvaluationError("evaluation test id is invalid")
    if raw["runner"] != "python_script_v1":
        raise OfflineEvaluationError("evaluation test runner is not registered")
    script = _package_file(
        package_root,
        raw["script"],
        raw["script_sha256"],
        "evaluation script",
    )
    arguments = _string_list(
        raw.get("arguments", []),
        "evaluation test arguments",
    )
    del arguments, script, test_id
    _bounded_int(raw.get("timeout_seconds", 300), 1, 3600, "timeout")
    _bounded_int(
        raw.get("max_stdout_bytes", MAX_CAPTURE_BYTES),
        0,
        MAX_CAPTURE_BYTES,
        "stdout limit",
    )
    _bounded_int(
        raw.get("max_stderr_bytes", MAX_CAPTURE_BYTES),
        0,
        MAX_CAPTURE_BYTES,
        "stderr limit",
    )


def _offline_bubblewrap_command(
    workspace: Path,
    package_root: Path,
    inner: Sequence[str],
    runtime_mounts: tuple[tuple[Path, Path], ...] = (),
) -> tuple[str, ...]:
    git_metadata = workspace / ".git"
    for executable, label in (
        (_BUBBLEWRAP, "Bubblewrap"),
        (_GIT, "Git"),
        (_PYTHON, "Python"),
        (_CPP, "C++ compiler"),
        (_FORTRAN, "Fortran compiler"),
        (_MAKE, "make"),
        (_ASSEMBLER, "assembler"),
        (_LINKER, "linker"),
        (_SHELL, "POSIX shell"),
        (_DYNAMIC_LOADER, "dynamic loader"),
    ):
        if (
            not executable.is_file()
            or executable.is_symlink()
            or not os.access(executable, os.X_OK)
        ):
            raise OfflineEvaluationError(
                f"{label} is required for sealed offline evaluation"
            )
    for directory in _SYSTEM_RUNTIME_DIRECTORIES:
        if not directory.is_dir() or directory.is_symlink():
            raise OfflineEvaluationError(
                "audited offline runtime directory is unavailable"
            )
    if not git_metadata.is_dir() or git_metadata.is_symlink():
        raise OfflineEvaluationError(
            "ephemeral evaluation Git metadata is unavailable"
        )
    command: list[str] = [
        str(_BUBBLEWRAP),
        "--unshare-all",
        "--die-with-parent",
        "--new-session",
        "--clearenv",
        "--setenv",
        "HOME",
        "/tmp/home",
        "--setenv",
        "LANG",
        "C.UTF-8",
        "--setenv",
        "LC_ALL",
        "C.UTF-8",
        "--setenv",
        "PATH",
        "/usr/bin",
        "--setenv",
        "PYTHONDONTWRITEBYTECODE",
        "1",
        "--setenv",
        "TMPDIR",
        "/tmp",
        "--tmpfs",
        "/usr",
        "--dir",
        "/usr/bin",
        "--dir",
        "/usr/lib",
        "--dir",
        "/usr/libexec",
        "--dir",
        "/usr/include",
        "--dir",
        "/usr/lib64",
        "--dir",
        "/lib64",
        "--dir",
        "/bin",
    ]
    for source, destination in (
        (_PYTHON, Path("/usr/bin/python3")),
        (_GIT, Path("/usr/bin/git")),
        (_CPP, Path("/usr/bin/g++")),
        (_FORTRAN, Path("/usr/bin/x86_64-linux-gnu-gfortran-15")),
        (_MAKE, Path("/usr/bin/make")),
        (_ASSEMBLER, Path("/usr/bin/as")),
        (_LINKER, Path("/usr/bin/ld")),
        (_SHELL, Path("/bin/sh")),
        (_DYNAMIC_LOADER, Path("/lib64/ld-linux-x86-64.so.2")),
    ):
        command.extend(("--ro-bind", str(source), str(destination)))
    for directory in _SYSTEM_RUNTIME_DIRECTORIES:
        command.extend(("--ro-bind", str(directory), str(directory)))
    command.extend(
        (
            "--dev",
            "/dev",
            "--proc",
            "/proc",
            "--tmpfs",
            "/tmp",
            "--dir",
            "/tmp/home",
            "--bind",
            str(workspace),
            "/workspace",
            "--ro-bind",
            str(git_metadata),
            "/workspace/.git",
            "--ro-bind",
            str(package_root),
            "/evaluation",
        )
    )
    created: set[Path] = {
        Path("/"),
        Path("/dev"),
        Path("/proc"),
        Path("/tmp"),
        Path("/tmp/home"),
        Path("/usr"),
        Path("/usr/bin"),
        Path("/usr/bin/git"),
        Path("/usr/lib"),
        Path("/usr/libexec"),
        Path("/usr/include"),
        Path("/usr/lib64"),
        Path("/lib64"),
        Path("/bin"),
        Path("/bin/sh"),
        Path("/workspace"),
        Path("/workspace/.git"),
        Path("/evaluation"),
    }
    for _source, destination in runtime_mounts:
        for parent in reversed(destination.parents):
            if parent not in created:
                command.extend(("--dir", str(parent)))
                created.add(parent)
        if destination not in created:
            command.extend(("--dir", str(destination)))
            created.add(destination)
    for source, destination in runtime_mounts:
        command.extend(("--ro-bind", str(source), str(destination)))
    for name, value in sorted(_GIT_ENVIRONMENT.items()):
        command.extend(("--setenv", name, value))
    command.extend(("--chdir", "/workspace", "--", *inner))
    return tuple(command)


def _verify_candidate(root: Path) -> dict[str, object]:
    manifest_path = root / "candidate-manifest.json"
    try:
        root_status = root.lstat()
        manifest_status = manifest_path.lstat()
    except OSError as exc:
        raise OfflineEvaluationError(
            "candidate root or manifest is unavailable"
        ) from exc
    if (
        stat.S_ISLNK(root_status.st_mode)
        or not stat.S_ISDIR(root_status.st_mode)
        or stat.S_IMODE(root_status.st_mode) != 0o500
        or root_status.st_uid != os.geteuid()
        or root_status.st_gid != os.getegid()
        or stat.S_ISLNK(manifest_status.st_mode)
        or not stat.S_ISREG(manifest_status.st_mode)
        or manifest_status.st_nlink != 1
        or stat.S_IMODE(manifest_status.st_mode) != 0o400
        or manifest_status.st_uid != os.geteuid()
        or manifest_status.st_gid != os.getegid()
    ):
        raise OfflineEvaluationError(
            "candidate root and manifest must be exact non-symlink objects"
        )
    manifest = _read_object(manifest_path)
    if (
        set(manifest)
        != {
            "schema_version",
            "snapshot_id",
            "campaign_id",
            "entries",
            "candidate_manifest_sha256",
        }
        or manifest.get("schema_version") != SCHEMA_VERSION
        or not isinstance(manifest.get("campaign_id"), str)
        or not manifest["campaign_id"]
        or manifest.get("snapshot_id")
        != f"{manifest['campaign_id']}:candidate-v1"
    ):
        raise OfflineEvaluationError(
            "candidate manifest schema or identity is invalid"
        )
    if manifest_path.read_bytes() != _json_bytes(manifest):
        raise OfflineEvaluationError(
            "candidate manifest encoding is not canonical"
        )
    claimed = manifest.get("candidate_manifest_sha256")
    payload = {
        key: value
        for key, value in manifest.items()
        if key != "candidate_manifest_sha256"
    }
    if not isinstance(claimed, str) or claimed != _sha256_json(payload):
        raise OfflineEvaluationError("candidate manifest digest is invalid")
    if payload.get("entries") != _package_entries(root):
        raise OfflineEvaluationError("candidate package changed after finalization")
    candidate_record = _read_object(root / "candidate.json")
    if (
        candidate_record.get("schema_version") != SCHEMA_VERSION
        or candidate_record.get("campaign_id") != manifest["campaign_id"]
    ):
        raise OfflineEvaluationError(
            "candidate manifest does not match candidate identity"
        )
    for path in root.rglob("*"):
        status = path.lstat()
        expected_mode = 0o500 if stat.S_ISDIR(status.st_mode) else 0o400
        if (
            stat.S_IMODE(status.st_mode) != expected_mode
            or status.st_uid != os.geteuid()
            or status.st_gid != os.getegid()
        ):
            raise OfflineEvaluationError(
                "candidate package ownership or mode is not canonical"
            )
    return manifest


def _candidate_tasks(record: Mapping[str, object]) -> dict[str, object]:
    raw = record.get("tasks")
    if not isinstance(raw, list):
        raise OfflineEvaluationError("candidate task records are invalid")
    result: dict[str, object] = {}
    for item in raw:
        if not isinstance(item, dict):
            raise OfflineEvaluationError("candidate task record is invalid")
        task_id = item.get("task_id")
        if not isinstance(task_id, str) or task_id in result:
            raise OfflineEvaluationError("candidate task id is invalid")
        result[task_id] = item
    return result


def _candidate_changed_paths(path: Path) -> tuple[str, ...]:
    evidence = _read_object(path)
    raw = evidence.get("changed_paths")
    if not isinstance(raw, list):
        raise OfflineEvaluationError("candidate changed-path evidence is invalid")
    paths: list[str] = []
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise OfflineEvaluationError("candidate changed path is invalid")
        paths.append(item["path"])
    return _canonical_changed_paths(
        paths,
        "candidate changed paths",
    )


def _canonical_changed_paths(
    paths: Sequence[str],
    label: str,
) -> tuple[str, ...]:
    canonical: list[str] = []
    for raw in paths:
        path = PurePosixPath(raw)
        if (
            not raw
            or "\x00" in raw
            or "\\" in raw
            or path.is_absolute()
            or not path.parts
            or any(part in {"", ".", ".."} for part in path.parts)
            or path.as_posix() != raw
        ):
            raise OfflineEvaluationError(
                f"{label} contains a non-canonical path"
            )
        canonical.append(raw)
    if len(set(canonical)) != len(canonical):
        raise OfflineEvaluationError(f"{label} contains duplicate paths")
    return tuple(sorted(canonical))


def _apply_candidate_changes(workspace: Path, task_candidate: Path) -> None:
    record = _read_object(task_candidate / "changes.json")
    if record.get("schema_version") != 1:
        raise OfflineEvaluationError("candidate changes schema is invalid")
    raw_entries = record.get("entries")
    if not isinstance(raw_entries, list):
        raise OfflineEvaluationError("candidate changes are invalid")
    for raw in raw_entries:
        if not isinstance(raw, dict):
            raise OfflineEvaluationError("candidate change is invalid")
        relative = raw.get("path")
        if not isinstance(relative, str):
            raise OfflineEvaluationError("candidate change path is invalid")
        if PurePosixPath(relative).parts[:1] == (".git",):
            raise OfflineEvaluationError(
                "candidate change targets private Git metadata"
            )
        target = _change_target(workspace, relative)
        operation = raw.get("operation")
        if operation == "delete":
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink(missing_ok=True)
            continue
        if operation != "upsert":
            raise OfflineEvaluationError("candidate change operation is invalid")
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() or target.is_symlink():
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink()
        object_type = raw.get("object_type")
        if object_type == "regular":
            source = _change_target(task_candidate / "changed-files", relative)
            content = source.read_bytes()
            if (
                raw.get("byte_length") != len(content)
                or raw.get("content_sha256")
                != hashlib.sha256(content).hexdigest()
            ):
                raise OfflineEvaluationError("candidate changed file digest is invalid")
            target.write_bytes(content)
            mode = raw.get("mode")
            if not isinstance(mode, int) or isinstance(mode, bool):
                raise OfflineEvaluationError("candidate changed file mode is invalid")
            target.chmod(mode)
        elif object_type == "symlink":
            link_target = raw.get("target")
            if not isinstance(link_target, str):
                raise OfflineEvaluationError("candidate symlink target is invalid")
            encoded = os.fsencode(link_target)
            if (
                raw.get("byte_length") != len(encoded)
                or raw.get("content_sha256")
                != hashlib.sha256(encoded).hexdigest()
            ):
                raise OfflineEvaluationError("candidate symlink digest is invalid")
            if Path(link_target).is_absolute():
                raise OfflineEvaluationError("candidate symlink target escapes workspace")
            resolved_target = (
                target.parent.joinpath(link_target).resolve(strict=False)
            )
            try:
                resolved_relative = resolved_target.relative_to(workspace)
            except ValueError as exc:
                raise OfflineEvaluationError(
                    "candidate symlink target escapes workspace"
                ) from exc
            if resolved_relative.parts[:1] == (".git",):
                raise OfflineEvaluationError(
                    "candidate symlink targets private Git metadata"
                )
            target.symlink_to(link_target)
        else:
            raise OfflineEvaluationError(
                "candidate changed object type is invalid"
            )


def _change_target(root: Path, relative: str) -> Path:
    value = PurePosixPath(relative)
    if (
        value.is_absolute()
        or not value.parts
        or any(part in {"", ".", ".."} for part in value.parts)
    ):
        raise OfflineEvaluationError("candidate change path escapes workspace")
    canonical_root = root.resolve(strict=True)
    target = canonical_root.joinpath(*value.parts)
    current = canonical_root
    try:
        for part in value.parts[:-1]:
            current = current / part
            if current.exists() and current.is_symlink():
                raise OfflineEvaluationError(
                    "candidate change parent is a symlink"
                )
        Path(os.path.abspath(target)).relative_to(canonical_root)
    except (OSError, ValueError) as exc:
        raise OfflineEvaluationError(
            "candidate change parent escapes workspace"
        ) from exc
    return target


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
                    raise OfflineEvaluationError(
                        "evaluation archive contains an unsafe entry"
                    )
                target = destination.joinpath(*relative.parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    target.chmod(stat.S_IMODE(member.mode) & 0o755)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                source = opened.extractfile(member)
                if source is None:
                    raise OfflineEvaluationError(
                        "evaluation archive entry could not be read"
                    )
                with source, target.open("xb") as output:
                    shutil.copyfileobj(source, output)
                target.chmod(stat.S_IMODE(member.mode) & 0o755)
    except (OSError, tarfile.TarError) as exc:
        raise OfflineEvaluationError(
            "evaluation archive could not be materialized"
        ) from exc


def _package_file(
    root: Path,
    relative: object,
    expected_sha256: object,
    label: str,
) -> Path:
    if not isinstance(relative, str) or not isinstance(expected_sha256, str):
        raise OfflineEvaluationError(f"{label} identity is invalid")
    path = _beneath(root, relative, label)
    if not path.is_file() or path.is_symlink():
        raise OfflineEvaluationError(f"{label} is not a regular file")
    if _sha256_file(path) != expected_sha256:
        raise OfflineEvaluationError(f"{label} digest changed")
    return path


def _package_entries(root: Path) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    for path in sorted(root.rglob("*")):
        if path.parent == root and path.name == "candidate-manifest.json":
            continue
        relative = path.relative_to(root).as_posix()
        status = path.lstat()
        if stat.S_ISDIR(status.st_mode):
            kind = "directory"
            length = 0
            digest = hashlib.sha256(b"").hexdigest()
        elif stat.S_ISREG(status.st_mode):
            if status.st_nlink != 1:
                raise OfflineEvaluationError(
                    "candidate contains a hard-linked regular file"
                )
            content = path.read_bytes()
            kind = "regular"
            length = len(content)
            digest = hashlib.sha256(content).hexdigest()
        else:
            raise OfflineEvaluationError("candidate contains an unsupported entry")
        entries.append(
            {
                "path_length": len(os.fsencode(relative)),
                "path": relative,
                "object_type": kind,
                "mode": stat.S_IMODE(status.st_mode),
                "byte_length": length,
                "content_sha256": digest,
                "tree_role": relative.split("/", 1)[0],
            }
        )
    return entries


def _tree_manifest(root: Path) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if relative == ".git" or relative.startswith(".git/"):
            continue
        status = path.lstat()
        if stat.S_ISDIR(status.st_mode):
            kind = "directory"
            length = 0
            digest = hashlib.sha256(b"").hexdigest()
        elif stat.S_ISREG(status.st_mode):
            content = path.read_bytes()
            kind = "regular"
            length = len(content)
            digest = hashlib.sha256(content).hexdigest()
        else:
            raise OfflineEvaluationError(
                "evaluated tree contains an unsupported entry"
            )
        entries.append(
            {
                "path_length": len(os.fsencode(relative)),
                "path": relative,
                "object_type": kind,
                "mode": stat.S_IMODE(status.st_mode),
                "byte_length": length,
                "content_sha256": digest,
            }
        )
    return entries


def _git_tree_oid(root: Path) -> str:
    """Derive the Git tree identity of a regular-file archive without Git state."""

    def object_oid(kind: bytes, content: bytes) -> bytes:
        header = kind + b" " + str(len(content)).encode("ascii") + b"\0"
        return hashlib.sha1(header + content).digest()  # noqa: S324

    def directory_oid(directory: Path) -> tuple[bytes, bool]:
        entries: list[tuple[bytes, bytes]] = []
        try:
            children = tuple(directory.iterdir())
        except OSError as exc:
            raise OfflineEvaluationError(
                "evaluation baseline tree could not be read"
            ) from exc
        for child in children:
            name = os.fsencode(child.name)
            if b"\0" in name or b"/" in name:
                raise OfflineEvaluationError(
                    "evaluation baseline tree contains an invalid name"
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
                raise OfflineEvaluationError(
                    "evaluation baseline tree contains an unsupported object"
                )
            entry = mode + b" " + name + b"\0" + oid
            entries.append((sort_key, entry))
        payload = b"".join(entry for _key, entry in sorted(entries))
        return object_oid(b"tree", payload), bool(entries)

    oid, _has_entries = directory_oid(root)
    return oid.hex()


def _new_output_directory(
    output: Path,
    candidate: Path,
    package: Path,
) -> _OutputDirectory:
    absolute = Path(os.path.abspath(output))
    if output.exists() or output.is_symlink():
        raise OfflineEvaluationError("output directory already exists")
    try:
        lexical_parent = absolute.parent
        resolved_parent = lexical_parent.resolve(strict=True)
        if lexical_parent != resolved_parent or not resolved_parent.is_dir():
            raise OfflineEvaluationError(
                "output parent must not traverse a symlink or alternate path"
            )
        prospective = resolved_parent / absolute.name
    except OfflineEvaluationError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise OfflineEvaluationError(
            "output parent could not be resolved"
        ) from exc
    if (
        _overlaps(prospective, candidate.parent)
        or _overlaps(prospective, candidate)
        or _overlaps(prospective, package)
    ):
        raise OfflineEvaluationError(
            "output must be separate from campaign, candidate, and evaluation package"
        )
    parent_descriptor = _pin_directory(resolved_parent, "output parent")
    try:
        staging_path = Path(
            tempfile.mkdtemp(
                prefix=f".{absolute.name}.staging-",
                dir=resolved_parent,
            )
        )
        descriptor = _pin_directory(staging_path, "output staging directory")
        identity = _descriptor_identity(descriptor)
        target = _OutputDirectory(
            path=prospective,
            descriptor=descriptor,
            identity=identity,
            staging_path=staging_path,
            staging_name=staging_path.name,
            parent=resolved_parent,
            parent_descriptor=parent_descriptor,
            parent_identity=_descriptor_identity(parent_descriptor),
            candidate=candidate,
            package=package,
        )
        _verify_output_directory(target)
        return target
    except Exception:
        with contextlib.suppress(UnboundLocalError, OSError):
            os.close(descriptor)
        os.close(parent_descriptor)
        raise


def _pin_directory(path: Path, label: str) -> int:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise OfflineEvaluationError(f"{label} could not be pinned") from exc
    try:
        _require_pinned_path(path, descriptor, label)
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


def _descriptor_identity(descriptor: int) -> tuple[int, int, int]:
    status = os.fstat(descriptor)
    if not stat.S_ISDIR(status.st_mode):
        raise OfflineEvaluationError("pinned authority is not a directory")
    return status.st_dev, status.st_ino, stat.S_IMODE(status.st_mode)


def _require_pinned_path(path: Path, descriptor: int, label: str) -> None:
    try:
        status = path.lstat()
    except OSError as exc:
        raise OfflineEvaluationError(f"{label} pathname changed") from exc
    if (
        stat.S_ISLNK(status.st_mode)
        or not stat.S_ISDIR(status.st_mode)
        or (
            status.st_dev,
            status.st_ino,
            stat.S_IMODE(status.st_mode),
        )
        != _descriptor_identity(descriptor)
    ):
        raise OfflineEvaluationError(f"{label} pathname identity changed")


def _verify_output_directory(target: _OutputDirectory) -> None:
    _require_pinned_path(
        target.staging_path,
        target.descriptor,
        "output staging directory",
    )
    _require_pinned_path(
        target.parent,
        target.parent_descriptor,
        "output parent",
    )
    if (
        _descriptor_identity(target.descriptor) != target.identity
        or _descriptor_identity(target.parent_descriptor)
        != target.parent_identity
        or target.path.exists()
        or target.path.is_symlink()
        or _overlaps(target.path, target.candidate.parent)
        or _overlaps(target.path, target.candidate)
        or _overlaps(target.path, target.package)
    ):
        raise OfflineEvaluationError(
            "output directory identity or separation changed"
        )


def _cleanup_output_staging(target: _OutputDirectory) -> None:
    """Remove an unpublished, still-pinned report staging directory."""
    if target.published:
        return
    try:
        target.staging_path.lstat()
    except FileNotFoundError:
        return
    except OSError:
        return
    try:
        _require_pinned_path(
            target.parent,
            target.parent_descriptor,
            "output parent",
        )
        staging_status = os.stat(
            target.staging_name,
            dir_fd=target.parent_descriptor,
            follow_symlinks=False,
        )
        if (
            _descriptor_identity(target.parent_descriptor)
            != target.parent_identity
            or not stat.S_ISDIR(staging_status.st_mode)
            or (staging_status.st_dev, staging_status.st_ino)
            != target.identity[:2]
            or _descriptor_identity(target.descriptor)[:2]
            != target.identity[:2]
        ):
            return
        os.fchmod(target.descriptor, 0o700)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(REPORT_NAME, dir_fd=target.descriptor)
        with contextlib.suppress(FileNotFoundError):
            artifact_descriptor = os.open(
                "artifacts",
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=target.descriptor,
            )
            try:
                os.fchmod(artifact_descriptor, 0o700)
                for name in os.listdir(artifact_descriptor):
                    status = os.stat(
                        name,
                        dir_fd=artifact_descriptor,
                        follow_symlinks=False,
                    )
                    if not stat.S_ISREG(status.st_mode):
                        return
                    os.unlink(name, dir_fd=artifact_descriptor)
            finally:
                os.close(artifact_descriptor)
            os.rmdir("artifacts", dir_fd=target.descriptor)
        os.rmdir(
            target.staging_name,
            dir_fd=target.parent_descriptor,
        )
        os.fsync(target.parent_descriptor)
    except (OSError, OfflineEvaluationError):
        return


def _write_output_report(
    target: _OutputDirectory,
    report: Mapping[str, object],
    artifacts: Mapping[str, bytes],
) -> Path:
    _verify_output_directory(target)
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        _write_output_artifacts(target, artifacts)
        descriptor = os.open(
            REPORT_NAME,
            flags,
            0o600,
            dir_fd=target.descriptor,
        )
        try:
            encoded = _json_bytes(report)
            view = memoryview(encoded)
            while view:
                written = os.write(descriptor, view)
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.fsync(target.descriptor)
        _verify_output_directory(target)
        os.chmod(
            REPORT_NAME,
            0o400,
            dir_fd=target.descriptor,
            follow_symlinks=False,
        )
        os.fchmod(target.descriptor, 0o500)
        _rename_output_noreplace(target)
    except OfflineEvaluationError:
        raise
    except OSError as exc:
        raise OfflineEvaluationError(
            "offline evaluation report could not be published"
        ) from exc
    return target.path / REPORT_NAME


def _write_output_artifacts(
    target: _OutputDirectory,
    artifacts: Mapping[str, bytes],
) -> None:
    try:
        os.mkdir("artifacts", 0o700, dir_fd=target.descriptor)
        artifact_descriptor = os.open(
            "artifacts",
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=target.descriptor,
        )
        try:
            for relative, content in sorted(artifacts.items()):
                path = PurePosixPath(relative)
                if (
                    len(path.parts) != 2
                    or path.parts[0] != "artifacts"
                    or not path.parts[1]
                    or path.parts[1] in {".", ".."}
                ):
                    raise OfflineEvaluationError(
                        "offline evaluation artifact path is invalid"
                    )
                descriptor = os.open(
                    path.parts[1],
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                    dir_fd=artifact_descriptor,
                )
                try:
                    view = memoryview(content)
                    while view:
                        written = os.write(descriptor, view)
                        view = view[written:]
                    os.fsync(descriptor)
                    os.fchmod(descriptor, 0o400)
                finally:
                    os.close(descriptor)
            os.fsync(artifact_descriptor)
            os.fchmod(artifact_descriptor, 0o500)
        finally:
            os.close(artifact_descriptor)
    except OfflineEvaluationError:
        raise
    except OSError as exc:
        raise OfflineEvaluationError(
            "offline evaluation artifacts could not be published"
        ) from exc


def _rename_output_noreplace(target: _OutputDirectory) -> None:
    parent_real = Path(
        os.path.realpath(f"/proc/self/fd/{target.parent_descriptor}")
    )
    if (
        parent_real != target.parent
        or parent_real == target.candidate.parent
        or target.candidate.parent in parent_real.parents
        or parent_real == target.candidate
        or target.candidate in parent_real.parents
        or parent_real == target.package
        or target.package in parent_real.parents
    ):
        raise OfflineEvaluationError(
            "output parent entered campaign or evaluation authority"
        )
    library = ctypes.CDLL(None, use_errno=True)
    try:
        renameat2 = library.renameat2
    except AttributeError as exc:
        raise OfflineEvaluationError(
            "atomic output publication is unavailable"
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
        target.parent_descriptor,
        os.fsencode(target.staging_name),
        target.parent_descriptor,
        os.fsencode(target.path.name),
        1,
    )
    if result == 0:
        target.published = True
        with contextlib.suppress(OSError):
            os.fsync(target.parent_descriptor)
        return
    selected_errno = ctypes.get_errno()
    if selected_errno == errno.EEXIST:
        raise OfflineEvaluationError(
            "output directory appeared during publication"
        )
    raise OfflineEvaluationError(
        f"atomic output publication failed with errno {selected_errno}"
    )


def _exact_directory(path: Path, label: str) -> Path:
    try:
        absolute = Path(os.path.abspath(path))
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise OfflineEvaluationError(f"{label} could not be resolved") from exc
    if absolute != resolved or not resolved.is_dir():
        raise OfflineEvaluationError(f"{label} is not an exact directory")
    return resolved


def _validate_evaluation_package(root: Path) -> dict[str, object]:
    try:
        manifest = verify_evaluation_package(root)
        for path in root.rglob("*"):
            status = path.lstat()
            if stat.S_ISLNK(status.st_mode) or not (
                stat.S_ISDIR(status.st_mode) or stat.S_ISREG(status.st_mode)
            ):
                raise OfflineEvaluationError(
                    "evaluation package contains an unsupported object"
                )
            relative = path.relative_to(root)
            executable_dependency = (
                bool(relative.parts)
                and relative.parts[0] == "dependencies"
            )
            if (
                stat.S_ISREG(status.st_mode)
                and stat.S_IMODE(status.st_mode) & 0o111
                and not executable_dependency
            ):
                raise OfflineEvaluationError(
                    "only dependency snapshot files may be executable"
                )
            if stat.S_ISREG(status.st_mode) and status.st_nlink != 1:
                raise OfflineEvaluationError(
                    "evaluation package files must own independent inodes"
                )
    except OfflineEvaluationError:
        raise
    except EvaluationPackageError as exc:
        raise OfflineEvaluationError(str(exc)) from exc
    except OSError as exc:
        raise OfflineEvaluationError(
            "evaluation package could not be validated"
        ) from exc
    return manifest


def _runtime_dependency_mounts(
    value: object,
    package_root: Path,
) -> tuple[tuple[Path, Path], ...]:
    if value is None:
        return ()
    if not isinstance(value, dict) or set(value) != {
        "profile",
        "dependency_roots",
    }:
        raise OfflineEvaluationError("offline runtime profile is invalid")
    if value["profile"] != "gl_historical_replay_v1":
        raise OfflineEvaluationError("offline runtime profile is not registered")
    raw_dependencies = value["dependency_roots"]
    if not isinstance(raw_dependencies, list):
        raise OfflineEvaluationError("offline dependency roots are invalid")
    expected = {
        "chombo-dependency": "Chombo",
        "grchombo-dependency": "GRChombo",
    }
    mounts: dict[str, tuple[Path, Path]] = {}
    common_external_parent: Path | None = None
    for raw in raw_dependencies:
        if not isinstance(raw, dict) or set(raw) != {
            "role",
            "path",
            "source_commit",
            "source_tree",
            "namespace_path",
        }:
            raise OfflineEvaluationError("offline dependency record is invalid")
        role = raw["role"]
        if not isinstance(role, str) or role not in expected or role in mounts:
            raise OfflineEvaluationError("offline dependency role is invalid")
        source = _beneath(
            package_root,
            raw["path"] if isinstance(raw["path"], str) else "",
            "offline dependency",
        )
        if not source.is_dir() or source.is_symlink():
            raise OfflineEvaluationError("offline dependency is not a directory")
        commit = raw["source_commit"]
        tree = raw["source_tree"]
        namespace = raw["namespace_path"]
        if not (
            isinstance(commit, str)
            and len(commit) == 40
            and isinstance(tree, str)
            and len(tree) == 40
            and isinstance(namespace, str)
        ):
            raise OfflineEvaluationError("offline dependency identity is invalid")
        destination = Path(namespace)
        if (
            not destination.is_absolute()
            or ".." in destination.parts
            or destination.name != expected[role]
            or destination.parent.name != "external"
            or any(
                destination == reserved
                or destination.is_relative_to(reserved)
                for reserved in (
                    Path("/dev"),
                    Path("/evaluation"),
                    Path("/proc"),
                    Path("/sys"),
                    Path("/usr"),
                    Path("/workspace"),
                )
            )
        ):
            raise OfflineEvaluationError(
                "offline dependency namespace path is invalid"
            )
        if common_external_parent is None:
            common_external_parent = destination.parent
        elif destination.parent != common_external_parent:
            raise OfflineEvaluationError(
                "offline dependency namespace paths are inconsistent"
            )
        mounts[role] = (source, destination)
    if set(mounts) != set(expected):
        raise OfflineEvaluationError("offline dependency roots are incomplete")
    return tuple(mounts[role] for role in sorted(mounts))


def _production_profile_analysis(
    value: object,
    changed_paths: Sequence[str],
) -> dict[str, object]:
    if value is None:
        return {
            "passed": True,
            "classifications_exhaustive": False,
            "classified_paths": {},
            "unclassified_paths": [],
        }
    if not isinstance(value, dict) or set(value) != {
        "hot_path",
        "post_update",
        "validation_only",
    }:
        raise OfflineEvaluationError("production profile is invalid")
    patterns: dict[str, list[str]] = {}
    for role in ("hot_path", "post_update", "validation_only"):
        patterns[role] = _string_list(
            value[role],
            f"production profile {role}",
        )
    classified: dict[str, list[str]] = {}
    unclassified: list[str] = []
    for changed in changed_paths:
        path = PurePosixPath(changed)
        roles = [
            role
            for role, role_patterns in patterns.items()
            if any(path.match(pattern) for pattern in role_patterns)
        ]
        if roles:
            classified[changed] = roles
        else:
            unclassified.append(changed)
    return {
        "passed": True,
        "classifications_exhaustive": False,
        "classified_paths": classified,
        "unclassified_paths": unclassified,
    }


def _beneath(root: Path, relative: str, label: str) -> Path:
    candidate = PurePosixPath(relative)
    if relative == ".":
        return root
    if (
        candidate.is_absolute()
        or not candidate.parts
        or any(part in {"", ".", ".."} for part in candidate.parts)
    ):
        raise OfflineEvaluationError(f"{label} path is invalid")
    try:
        resolved = root.joinpath(*candidate.parts).resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise OfflineEvaluationError(f"{label} escapes its package") from exc
    return resolved


def _task_id(value: object) -> str:
    if not isinstance(value, Mapping):
        raise OfflineEvaluationError("evaluation task is invalid")
    task_id = value.get("task_id")
    if (
        not isinstance(task_id, str)
        or not task_id
        or "/" in task_id
        or task_id in {".", ".."}
    ):
        raise OfflineEvaluationError("evaluation task id is invalid")
    return task_id


def _bounded_int(value: object, minimum: int, maximum: int, label: str) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not minimum <= value <= maximum
    ):
        raise OfflineEvaluationError(f"evaluation test {label} is invalid")
    return value


def _string_list(value: object, label: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise OfflineEvaluationError(f"{label} is invalid")
    return value


def _require_exact_keys(
    value: Mapping[str, object],
    *,
    required: set[str],
    optional: set[str],
    label: str,
) -> None:
    keys = set(value)
    if not required <= keys or keys - required - optional:
        raise OfflineEvaluationError(f"{label} fields are invalid")


def _overlaps(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise OfflineEvaluationError("offline evaluation JSON is invalid") from exc
    if not isinstance(value, dict):
        raise OfflineEvaluationError("offline evaluation JSON must be an object")
    return value


def _write_json(path: Path, value: object) -> None:
    path.write_bytes(_json_bytes(value))


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
    return hashlib.sha256(path.read_bytes()).hexdigest()


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


def main(argv: Sequence[str] | None = None) -> int:
    """Standalone command entry point; intentionally has no campaign argument."""
    parser = argparse.ArgumentParser(
        prog="evaluate-historical-replay",
        description="Deterministically evaluate an exported candidate.",
    )
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--evaluation-package", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args(argv)
    try:
        report = evaluate_historical_replay(
            arguments.candidate,
            arguments.evaluation_package,
            arguments.output,
        )
    except OfflineEvaluationError as exc:
        print(f"offline evaluation failed: {exc}", file=sys.stderr)
        return 2
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
