from __future__ import annotations

import ast
import hashlib
import io
import json
import os
import posixpath
import shutil
import stat
import subprocess
import tarfile
from collections.abc import Sequence
from pathlib import Path

import pytest
import yaml

import research_automation_supervisor.offline_evaluation_package as package_builder
import research_automation_supervisor.offline_replay_evaluator as offline_evaluator
from research_automation_supervisor.offline_evaluation_package import (
    EvaluationPackageError,
    historical_replay_command_report,
    prepare_historical_replay_evaluation_package,
    verify_evaluation_package,
)
from research_automation_supervisor.offline_replay_evaluator import (
    evaluate_historical_replay,
)

_GRCHOMBO_LINKS = {
    "Source/CCZ4/CCZ4.hpp": "CCZ4RHS.hpp",
    "Source/Matter/MatterCCZ4.hpp": "MatterCCZ4RHS.hpp",
    "Tests/SphericalExtractionUniformTest/SetHarmonic.hpp": (
        "../SphericalExtractionTest/SetHarmonic.hpp"
    ),
    "Tests/SphericalExtractionUniformTest/SetHarmonic.impl.hpp": (
        "../SphericalExtractionTest/SetHarmonic.impl.hpp"
    ),
    "Tests/SphericalExtractionUniformTest/SimulationParameters.hpp": (
        "../SphericalExtractionTest/SimulationParameters.hpp"
    ),
    (
        "Tests/SphericalExtractionUniformTest/"
        "SphericalExtractionUniformTestLevel.hpp"
    ): "../SphericalExtractionTest/SphericalExtractionTestLevel.hpp",
    "Tests/SphericalExtractionUniformTest/UserVariables.hpp": (
        "../SphericalExtractionTest/UserVariables.hpp"
    ),
}


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ("git", "-C", str(repository), *arguments),
        env={
            "GIT_AUTHOR_EMAIL": "synthetic@example.invalid",
            "GIT_AUTHOR_NAME": "Synthetic Authority",
            "GIT_COMMITTER_EMAIL": "synthetic@example.invalid",
            "GIT_COMMITTER_NAME": "Synthetic Authority",
            "LC_ALL": "C",
            "PATH": os.environ["PATH"],
        },
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _repository(path: Path, files: dict[str, str]) -> tuple[str, str]:
    path.mkdir(parents=True)
    _git(path, "init", "-q")
    for relative, content in files.items():
        target = path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    _git(path, "add", ".")
    _git(path, "commit", "-q", "-m", "synthetic baseline")
    return _git(path, "rev-parse", "HEAD"), _git(
        path,
        "rev-parse",
        "HEAD^{tree}",
    )


def _repository_with_links(
    path: Path,
    *,
    files: dict[str, bytes],
    links: dict[str, str],
    executable: set[str] | None = None,
) -> tuple[str, str]:
    path.mkdir(parents=True)
    _git(path, "init", "-q")
    for relative, content in files.items():
        target = path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        target.chmod(0o755 if relative in (executable or set()) else 0o644)
    for relative, link_target in links.items():
        target = path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.symlink_to(link_target)
    _git(path, "add", ".")
    _git(path, "commit", "-q", "-m", "synthetic linked dependency")
    return _git(path, "rev-parse", "HEAD"), _git(
        path,
        "rev-parse",
        "HEAD^{tree}",
    )


def _replace_dependency_repository(
    source: Path,
    *,
    files: dict[str, bytes],
    links: dict[str, str],
    executable: set[str] | None = None,
) -> tuple[Path, str, str]:
    repository = source / "external/GRChombo"
    shutil.rmtree(repository)
    commit, tree = _repository_with_links(
        repository,
        files=files,
        links=links,
        executable=executable,
    )
    source_state_path = (
        source / "engine-only/historical-audits/source-state.json"
    )
    source_state = json.loads(source_state_path.read_text(encoding="utf-8"))
    for record in source_state["repositories"]:
        if record["name"] == "grchombo-dependency":
            record["path"] = str(repository)
            record["head"] = commit
            record["status"] = []
            break
    source_state_path.write_text(
        json.dumps(source_state, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return repository, commit, tree


def _synthetic_source(tmp_path: Path) -> Path:
    root = tmp_path / "preserved-prepared-campaign"
    evaluator = root / "engine-only/evaluators/evaluate.py"
    evaluator.parent.mkdir(parents=True)
    evaluator.write_text(
        "from pathlib import Path\n"
        "import subprocess\n"
        "import sys\n"
        "mode, task, workspace, fixture = sys.argv[1:]\n"
        "relative = Path('src') / f'{task}.txt'\n"
        "status = subprocess.run(\n"
        "    ['/usr/bin/git', '-C', workspace, 'status',\n"
        "     '--porcelain=v1', '-z', '--untracked-files=all'],\n"
        "    stdin=subprocess.DEVNULL, capture_output=True, check=False,\n"
        ")\n"
        "passed = (\n"
        "    mode == 'functional'\n"
        "    and status.returncode == 0\n"
        "    and str(relative).encode() in status.stdout\n"
        "    and (\n"
        "    Path(workspace, relative).read_bytes()\n"
        "    == Path(fixture, relative).read_bytes()\n"
        "    )\n"
        ")\n"
        "print(f'synthetic functional stdout: {task}')\n"
        "print(f'synthetic functional stderr: {task}', file=sys.stderr)\n"
        "raise SystemExit(not passed)\n",
        encoding="utf-8",
    )
    evaluator.chmod(0o555)
    tasks: list[dict[str, object]] = []
    preparation_tasks: list[dict[str, object]] = []
    for task_id in package_builder.TASK_IDS:
        workspace = root / f"visible/tasks/{task_id}/workspace"
        commit, tree = _repository(
            workspace,
            {f"src/{task_id}.txt": "baseline\n"},
        )
        control = root / f"visible/tasks/{task_id}/control"
        control.mkdir(parents=True)
        allowed = f"src/{task_id}.txt"
        (control / "stage2.yaml").write_text(
            yaml.safe_dump(
                {
                    "schema_version": 1,
                    "substage_id": task_id,
                    "allowed_paths": [allowed],
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        gold = root / f"engine-only/gold/{task_id}/{allowed}"
        gold.parent.mkdir(parents=True)
        gold.write_text(f"expected {task_id}\n", encoding="utf-8")
        tasks.append(
            {
                "task_id": task_id,
                "stage2_specification_path": (
                    f"visible/tasks/{task_id}/control/stage2.yaml"
                ),
                "gold_artifact_roots": [f"engine-only/gold/{task_id}"],
                "gold_evaluations": [
                    {
                        "id": f"{task_id}-{mode}",
                        "cwd": f"engine-only/gold/{task_id}",
                        "argv": [
                            "/usr/bin/python3",
                            str(evaluator),
                            mode,
                            task_id,
                            str(workspace),
                            str(root / f"engine-only/gold/{task_id}"),
                        ],
                    }
                    for mode in ("functional", "exact")
                ],
                "production_profile": {
                    "hot_path": [f"{task_id}.txt"],
                    "post_update": [],
                    "validation_only": [],
                },
            }
        )
        preparation_tasks.append(
            {
                "task_id": task_id,
                "local_baseline_commit": commit,
                "source_workspace_head": commit,
                "source_tree": tree,
                "target_tree": tree,
                "functional_evaluator_proof": True,
                "exact_evaluator_proof": True,
            }
        )
    (root / "campaign.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "campaign_id": "synthetic-five-replay",
                "tasks": tasks,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (root / "preparation-report.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "campaign_id": "synthetic-five-replay",
                "campaign_started": False,
                "real_model_invoked": False,
                "status": "prepared_and_preflight_passed",
                "preflight": {
                    "passed": True,
                    "blocked_checks": [],
                    "passed_checks": [
                        "source_matching_policies_before_and_after",
                        "implemented_loader",
                        "five_clean_isolated_workspaces",
                        "bounded_visible_gold_and_evaluator_leak_scan",
                        "all_five_functional_evaluator_proofs",
                        "all_five_exact_evaluator_proofs",
                    ],
                },
                "tasks": preparation_tasks,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    dependency_records: list[dict[str, object]] = []
    for name, directory in (
        ("chombo-dependency", "Chombo"),
        ("grchombo-dependency", "GRChombo"),
    ):
        repository = root / "external" / directory
        commit, _tree = _repository(
            repository,
            {f"include/{directory}.hpp": f"// synthetic {directory}\n"},
        )
        dependency_records.append(
            {
                "name": name,
                "path": str(repository),
                "head": commit,
                "match_policy": "exact_snapshot",
                "status": [],
            }
        )
    source_state = root / "engine-only/historical-audits/source-state.json"
    source_state.parent.mkdir(parents=True)
    source_state.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "repositories": dependency_records,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    runtime_state = root / "launch/runtime/completed-run/state.json"
    runtime_state.parent.mkdir(parents=True)
    runtime_state.write_text('{"status":"preserved"}\n', encoding="utf-8")
    return root


def _build_package(tmp_path: Path) -> tuple[Path, Path]:
    source = _synthetic_source(tmp_path)
    output = tmp_path / "private-offline-package"
    package, _digest = prepare_historical_replay_evaluation_package(
        source,
        output,
    )
    return source, package


def _files_for_links(links: dict[str, str]) -> dict[str, bytes]:
    resolved = {
        posixpath.normpath(
            posixpath.join(posixpath.dirname(link_path), link_target)
        )
        for link_path, link_target in links.items()
    }
    return {
        path: f"synthetic target {path}\n".encode()
        for path in sorted(resolved)
    }


def _materialize_linked_dependency(
    tmp_path: Path,
    *,
    files: dict[str, bytes],
    links: dict[str, str],
    executable: set[str] | None = None,
) -> tuple[Path, Path, dict[str, object]]:
    repository = tmp_path / "repository"
    commit, tree = _repository_with_links(
        repository,
        files=files,
        links=links,
        executable=executable,
    )
    staging = tmp_path / "staging"
    _runtime, materialization = package_builder._materialize_dependencies(
        (
            package_builder._DependencySource(
                name="grchombo-dependency",
                repository=repository,
                commit=commit,
                tree=tree,
                namespace_path="/synthetic/external/GRChombo",
            ),
        ),
        staging,
    )
    return (
        repository,
        staging / "dependencies/grchombo-dependency",
        materialization["grchombo-dependency"],
    )


def _reseal_package(package: Path) -> None:
    manifest_path = package / package_builder.PACKAGE_MANIFEST_NAME
    package.chmod(0o700)
    for path in package.rglob("*"):
        if path.is_dir():
            path.chmod(0o700)
        else:
            path.chmod(0o600)
    manifest = json.loads(manifest_path.read_text(encoding="ascii"))
    manifest_path.unlink()
    package_builder._make_payload_read_only(package)
    package.chmod(0o700)
    package_builder.write_evaluation_package_manifest(
        package,
        package_id=str(manifest["package_id"]),
        snapshot_id=str(manifest["snapshot_id"]),
    )
    manifest_path.chmod(0o400)
    package.chmod(0o500)


def _synthetic_candidate(package: Path, destination: Path) -> Path:
    config = json.loads(
        (package / package_builder.PACKAGE_CONFIG_PATH).read_text(
            encoding="ascii"
        )
    )
    records: list[dict[str, object]] = []
    for task in config["tasks"]:
        task_id = task["task_id"]
        relative = task["expected_changed_paths"][0]
        source = package / f"protected-fixtures/{task_id}/{relative}"
        task_root = destination / "tasks" / task_id
        changed = task_root / "changed-files" / relative
        changed.parent.mkdir(parents=True)
        shutil.copyfile(source, changed)
        content = changed.read_bytes()
        (task_root / "changes.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "entries": [
                        {
                            "path": relative,
                            "operation": "upsert",
                            "object_type": "regular",
                            "mode": 0o644,
                            "byte_length": len(content),
                            "content_sha256": hashlib.sha256(content).hexdigest(),
                        }
                    ],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="ascii",
        )
        (task_root / "git-evidence.json").write_text(
            json.dumps(
                {"changed_paths": [{"path": relative}]},
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="ascii",
        )
        records.append(
            {
                "task_id": task_id,
                "source_provenance": {
                    "source_commit": task["source_commit"],
                    "source_tree": task["source_tree"],
                    "baseline_archive_sha256": task[
                        "baseline_archive_sha256"
                    ],
                },
                "execution_baseline_tree": task["source_tree"],
            }
        )
    (destination / "candidate.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "campaign_id": "synthetic-visible-campaign",
                "tasks": records,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="ascii",
    )
    for path in sorted(destination.rglob("*"), reverse=True):
        if path.is_dir():
            path.chmod(0o500)
        else:
            path.chmod(0o400)
    destination.chmod(0o700)
    payload = {
        "schema_version": 1,
        "snapshot_id": "synthetic-visible-campaign:candidate-v1",
        "campaign_id": "synthetic-visible-campaign",
        "entries": offline_evaluator._package_entries(destination),
    }
    digest = offline_evaluator._sha256_json(payload)
    (destination / "candidate-manifest.json").write_text(
        json.dumps(
            {**payload, "candidate_manifest_sha256": digest},
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="ascii",
    )
    (destination / "candidate-manifest.json").chmod(0o400)
    destination.chmod(0o500)
    return destination


def _reseal_candidate(candidate: Path) -> None:
    manifest_path = candidate / "candidate-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="ascii"))
    candidate.chmod(0o700)
    for path in candidate.rglob("*"):
        path.chmod(0o700 if path.is_dir() else 0o600)
    manifest_path.unlink()
    for path in sorted(candidate.rglob("*"), reverse=True):
        path.chmod(0o500 if path.is_dir() else 0o400)
    candidate.chmod(0o700)
    payload = {
        "schema_version": 1,
        "snapshot_id": manifest["snapshot_id"],
        "campaign_id": manifest["campaign_id"],
        "entries": offline_evaluator._package_entries(candidate),
    }
    digest = offline_evaluator._sha256_json(payload)
    manifest_path.write_text(
        json.dumps(
            {**payload, "candidate_manifest_sha256": digest},
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="ascii",
    )
    manifest_path.chmod(0o400)
    candidate.chmod(0o500)


def test_package_creation_is_deterministic_and_maps_all_five_tasks(
    tmp_path: Path,
) -> None:
    source = _synthetic_source(tmp_path)
    first, first_digest = prepare_historical_replay_evaluation_package(
        source,
        tmp_path / "package-one",
    )
    second, second_digest = prepare_historical_replay_evaluation_package(
        source,
        tmp_path / "package-two",
    )

    assert first_digest == second_digest
    assert (first / package_builder.PACKAGE_MANIFEST_NAME).read_bytes() == (
        second / package_builder.PACKAGE_MANIFEST_NAME
    ).read_bytes()
    config = json.loads(
        (first / package_builder.PACKAGE_CONFIG_PATH).read_text(encoding="ascii")
    )
    assert tuple(task["task_id"] for task in config["tasks"]) == (
        package_builder.TASK_IDS
    )
    assert all(task["tests"] for task in config["tasks"])
    assert all(task["exact_reference_archive"] for task in config["tasks"])
    assert all(task["production_profile"] for task in config["tasks"])


def test_dependency_links_are_canonicalized_from_committed_blobs(
    tmp_path: Path,
) -> None:
    links = {
        "same/link.hpp": "target.hpp",
        "uniform/sibling.hpp": "../base/sibling.hpp",
        "chain/first": "second",
        "chain/second": "final.sh",
    }
    files = {
        "same/target.hpp": b"same-directory\n",
        "base/sibling.hpp": b"safe-sibling\n",
        "chain/final.sh": b"#!/bin/sh\nexit 0\n",
    }
    repository, dependency, metadata = _materialize_linked_dependency(
        tmp_path,
        files=files,
        links=links,
        executable={"chain/final.sh"},
    )

    expected = {
        "same/link.hpp": ("same/target.hpp", 0o400),
        "uniform/sibling.hpp": ("base/sibling.hpp", 0o400),
        "chain/first": ("chain/final.sh", 0o500),
        "chain/second": ("chain/final.sh", 0o500),
    }
    provenance = {
        record["original_git_path"]: record
        for record in metadata["canonicalized_symlinks"]
    }
    assert set(provenance) == set(expected)
    for original, (resolved, mode) in expected.items():
        output = dependency / original
        target = repository / resolved
        assert output.is_file()
        assert not output.is_symlink()
        assert output.read_bytes() == target.read_bytes()
        assert stat.S_IMODE(output.stat().st_mode) == mode
        assert output.stat().st_nlink == 1
        assert (output.stat().st_dev, output.stat().st_ino) != (
            target.stat().st_dev,
            target.stat().st_ino,
        )
        assert provenance[original]["resolved_git_path"] == resolved
        assert provenance[original]["result_mode"] == mode
        assert provenance[original]["link_target"] == links[original]
        assert provenance[original]["link_target_sha256"] == hashlib.sha256(
            links[original].encode()
        ).hexdigest()
        assert provenance[original]["link_blob_oid"] == _git(
            repository,
            "rev-parse",
            f"HEAD:{original}",
        )
        assert provenance[original]["resolved_content_sha256"] == (
            hashlib.sha256(target.read_bytes()).hexdigest()
        )


def test_dependency_link_resolution_ignores_live_checkout_changes(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    commit, tree = _repository_with_links(
        repository,
        files={"target": b"committed target\n"},
        links={"link": "target"},
    )
    (repository / "target").write_bytes(b"uncommitted checkout content\n")
    (repository / "link").unlink()
    (repository / "other").write_bytes(b"uncommitted link destination\n")
    (repository / "link").symlink_to("other")

    staging = tmp_path / "staging"
    package_builder._materialize_dependencies(
        (
            package_builder._DependencySource(
                name="grchombo-dependency",
                repository=repository,
                commit=commit,
                tree=tree,
                namespace_path="/synthetic/external/GRChombo",
            ),
        ),
        staging,
    )

    dependency = staging / "dependencies/grchombo-dependency"
    assert (dependency / "target").read_bytes() == b"committed target\n"
    assert (dependency / "link").read_bytes() == b"committed target\n"
    assert not (dependency / "other").exists()


def test_dependency_repository_path_replacement_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    commit, tree = _repository_with_links(
        repository,
        files={"target": b"committed target\n"},
        links={"link": "target"},
    )
    parked = tmp_path / "parked"
    original = package_builder._git_archive

    def replace_then_archive(
        pinned_repository: Path,
        selected_commit: str,
        archive: Path,
    ) -> None:
        repository.rename(parked)
        shutil.copytree(parked, repository, copy_function=shutil.copy2)
        original(pinned_repository, selected_commit, archive)

    monkeypatch.setattr(
        package_builder,
        "_git_archive",
        replace_then_archive,
    )

    with pytest.raises(EvaluationPackageError, match="repository changed"):
        package_builder._materialize_dependencies(
            (
                package_builder._DependencySource(
                    name="grchombo-dependency",
                    repository=repository,
                    commit=commit,
                    tree=tree,
                    namespace_path="/synthetic/external/GRChombo",
                ),
            ),
            tmp_path / "staging",
        )


def test_dependency_replacement_refs_cannot_change_pinned_commit(
    tmp_path: Path,
) -> None:
    source = _synthetic_source(tmp_path / "source")
    repository, original_commit, _tree = _replace_dependency_repository(
        source,
        files={"target": b"ORIGINAL\n"},
        links={"link": "target"},
    )
    (repository / "target").write_bytes(b"REPLACEMENT\n")
    _git(repository, "add", "target")
    _git(repository, "commit", "-q", "-m", "replacement content")
    replacement_commit = _git(repository, "rev-parse", "HEAD")
    _git(repository, "reset", "--hard", "-q", original_commit)
    _git(repository, "replace", original_commit, replacement_commit)

    package, _digest = prepare_historical_replay_evaluation_package(
        source,
        tmp_path / "package",
    )

    dependency = package / "dependencies/grchombo-dependency"
    assert (dependency / "target").read_bytes() == b"ORIGINAL\n"
    assert (dependency / "link").read_bytes() == b"ORIGINAL\n"


@pytest.mark.parametrize(
    ("links", "files", "message"),
    (
        (
            {"link": "/absolute/target"},
            {"target": b"target\n"},
            "relative POSIX path",
        ),
        (
            {"nested/link": "../../outside"},
            {"outside": b"target\n"},
            "escapes the repository",
        ),
        (
            {"link": "missing"},
            {"target": b"target\n"},
            "dangling",
        ),
        (
            {"first": "second", "second": "first"},
            {"target": b"target\n"},
            "cycle",
        ),
        (
            {"link": "directory"},
            {"directory/file": b"target\n"},
            "directory",
        ),
    ),
)
def test_unsafe_committed_dependency_links_are_rejected(
    tmp_path: Path,
    links: dict[str, str],
    files: dict[str, bytes],
    message: str,
) -> None:
    with pytest.raises(EvaluationPackageError, match=message):
        _materialize_linked_dependency(
            tmp_path,
            files=files,
            links=links,
        )


def test_committed_dependency_symlink_depth_limit_is_enforced(
    tmp_path: Path,
) -> None:
    links = {
        f"chain/link-{index}": (
            f"link-{index + 1}"
            if index < package_builder._MAX_GIT_SYMLINK_DEPTH
            else "target"
        )
        for index in range(package_builder._MAX_GIT_SYMLINK_DEPTH + 1)
    }
    with pytest.raises(EvaluationPackageError, match="depth limit"):
        _materialize_linked_dependency(
            tmp_path,
            files={"chain/target": b"target\n"},
            links=links,
        )


def test_committed_dependency_submodule_is_rejected(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    commit, _tree = _repository(repository, {"regular.txt": "regular\n"})
    _git(
        repository,
        "update-index",
        "--add",
        "--cacheinfo",
        "160000",
        commit,
        "vendor/submodule",
    )
    _git(repository, "commit", "-q", "-m", "add synthetic gitlink")
    dependency = package_builder._DependencySource(
        name="grchombo-dependency",
        repository=repository,
        commit=_git(repository, "rev-parse", "HEAD"),
        tree=_git(repository, "rev-parse", "HEAD^{tree}"),
        namespace_path="/synthetic/external/GRChombo",
    )

    with pytest.raises(EvaluationPackageError, match="submodule"):
        package_builder._materialize_dependencies(
            (dependency,),
            tmp_path / "staging",
        )


def test_exact_grchombo_link_shapes_are_deterministic_and_evaluable(
    tmp_path: Path,
) -> None:
    source = _synthetic_source(tmp_path / "source")
    files = _files_for_links(_GRCHOMBO_LINKS)
    repository, _commit, _tree = _replace_dependency_repository(
        source,
        files=files,
        links=_GRCHOMBO_LINKS,
        executable={"Source/CCZ4/CCZ4RHS.hpp"},
    )
    first, first_digest = prepare_historical_replay_evaluation_package(
        source,
        tmp_path / "package-one",
    )
    second, second_digest = prepare_historical_replay_evaluation_package(
        source,
        tmp_path / "package-two",
    )

    assert first_digest == second_digest
    assert (first / package_builder.PACKAGE_MANIFEST_NAME).read_bytes() == (
        second / package_builder.PACKAGE_MANIFEST_NAME
    ).read_bytes()
    provenance = json.loads(
        (first / "provenance/source-authority.json").read_text(
            encoding="ascii"
        )
    )
    grchombo = next(
        dependency
        for dependency in provenance["dependencies"]
        if dependency["name"] == "grchombo-dependency"
    )
    records = grchombo["canonicalized_symlinks"]
    assert [record["original_git_path"] for record in records] == sorted(
        _GRCHOMBO_LINKS
    )
    dependency = first / "dependencies/grchombo-dependency"
    for original, link_target in _GRCHOMBO_LINKS.items():
        resolved = posixpath.normpath(
            posixpath.join(posixpath.dirname(original), link_target)
        )
        materialized = dependency / original
        live_target = repository / resolved
        assert materialized.is_file() and not materialized.is_symlink()
        assert materialized.read_bytes() == live_target.read_bytes()
        assert (materialized.stat().st_dev, materialized.stat().st_ino) != (
            live_target.stat().st_dev,
            live_target.stat().st_ino,
        )
    assert all(
        path.is_dir() or (path.is_file() and not path.is_symlink())
        for path in first.rglob("*")
    )
    verify_evaluation_package(first)
    candidate = _synthetic_candidate(
        first,
        tmp_path / "campaign/final-candidate",
    )
    report = evaluate_historical_replay(
        candidate,
        first,
        tmp_path / "evaluation-report",
    )
    assert report.is_file()


@pytest.mark.parametrize(
    "mutation",
    ("forged_commit_and_tree", "removed_link_records"),
)
def test_dependency_provenance_cannot_be_resealed_inconsistently(
    tmp_path: Path,
    mutation: str,
) -> None:
    source = _synthetic_source(tmp_path / "source")
    _replace_dependency_repository(
        source,
        files={"target": b"committed target\n"},
        links={"link": "target"},
    )
    package, _digest = prepare_historical_replay_evaluation_package(
        source,
        tmp_path / "package",
    )
    package.chmod(0o700)
    provenance_path = package / "provenance/source-authority.json"
    provenance_path.chmod(0o600)
    provenance = json.loads(provenance_path.read_text(encoding="ascii"))
    dependency = next(
        record
        for record in provenance["dependencies"]
        if record["name"] == "grchombo-dependency"
    )
    if mutation == "forged_commit_and_tree":
        dependency["source_commit"] = "0" * 40
        dependency["source_tree"] = dependency["materialized_tree"]
    else:
        dependency["canonicalized_symlinks"] = []
    provenance_path.write_text(
        json.dumps(provenance, indent=2, sort_keys=True) + "\n",
        encoding="ascii",
    )
    _reseal_package(package)

    with pytest.raises(
        EvaluationPackageError,
        match="(dependency provenance|commit provenance|original tree)",
    ):
        verify_evaluation_package(package)


def test_package_uses_independent_inodes_and_does_not_modify_campaign(
    tmp_path: Path,
) -> None:
    source = _synthetic_source(tmp_path)
    state = source / "launch/runtime/completed-run/state.json"
    state_before = hashlib.sha256(state.read_bytes()).hexdigest()
    package, _digest = prepare_historical_replay_evaluation_package(
        source,
        tmp_path / "package",
    )

    for task_id in package_builder.TASK_IDS:
        relative = f"src/{task_id}.txt"
        original = source / f"engine-only/gold/{task_id}/{relative}"
        copied = package / f"protected-fixtures/{task_id}/{relative}"
        assert (original.stat().st_dev, original.stat().st_ino) != (
            copied.stat().st_dev,
            copied.stat().st_ino,
        )
        assert copied.stat().st_nlink == 1
    assert hashlib.sha256(state.read_bytes()).hexdigest() == state_before


def test_package_preparation_does_not_refresh_source_git_indexes(
    tmp_path: Path,
) -> None:
    source = _synthetic_source(tmp_path)
    workspace = source / "visible/tasks/reduced-vars-gp/workspace"
    tracked = workspace / "src/reduced-vars-gp.txt"
    tracked_status = tracked.stat()
    os.utime(
        tracked,
        ns=(
            tracked_status.st_atime_ns,
            tracked_status.st_mtime_ns + 1_000_000_000,
        ),
    )
    index = workspace / ".git/index"
    index_before = index.read_bytes()
    index_status_before = index.stat()

    prepare_historical_replay_evaluation_package(
        source,
        tmp_path / "package",
    )

    index_status_after = index.stat()
    assert index.read_bytes() == index_before
    assert (
        index_status_after.st_mtime_ns,
        index_status_after.st_ctime_ns,
    ) == (
        index_status_before.st_mtime_ns,
        index_status_before.st_ctime_ns,
    )


def test_metadata_preserving_preserved_campaign_copy_is_accepted(
    tmp_path: Path,
) -> None:
    source = _synthetic_source(tmp_path / "original")
    restored = tmp_path / "restored/prepared-campaign"
    shutil.copytree(source, restored, copy_function=shutil.copy2)

    campaign_path = restored / "campaign.yaml"
    campaign = yaml.safe_load(campaign_path.read_text(encoding="utf-8"))
    for task in campaign["tasks"]:
        for evaluation in task["gold_evaluations"]:
            evaluation["cwd"] = evaluation["cwd"].replace(
                str(source),
                str(restored),
            )
            evaluation["argv"] = [
                value.replace(str(source), str(restored))
                for value in evaluation["argv"]
            ]
    campaign_path.write_text(
        yaml.safe_dump(campaign, sort_keys=False),
        encoding="utf-8",
    )
    source_state_path = (
        restored / "engine-only/historical-audits/source-state.json"
    )
    source_state = json.loads(source_state_path.read_text(encoding="utf-8"))
    for repository in source_state["repositories"]:
        repository["path"] = repository["path"].replace(
            str(source),
            str(restored),
        )
    source_state_path.write_text(
        json.dumps(source_state, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    package, _digest = prepare_historical_replay_evaluation_package(
        restored,
        tmp_path / "package",
    )

    assert package.is_dir()


def test_package_preparation_invokes_only_git_subprocesses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _synthetic_source(tmp_path)
    original = package_builder.subprocess.run
    commands: list[tuple[str, ...]] = []

    def guarded(
        command: Sequence[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        selected = tuple(command)
        commands.append(selected)
        assert selected[0] == "/usr/bin/git"
        return original(selected, **kwargs)

    monkeypatch.setattr(package_builder.subprocess, "run", guarded)
    prepare_historical_replay_evaluation_package(
        source,
        tmp_path / "package",
    )

    assert commands
    assert all("codex" not in " ".join(command).lower() for command in commands)


def test_incomplete_source_is_rejected(tmp_path: Path) -> None:
    source = _synthetic_source(tmp_path)
    missing = (
        source
        / "engine-only/gold/reduced-vars-gp/src/reduced-vars-gp.txt"
    )
    missing.unlink()

    with pytest.raises(
        EvaluationPackageError,
        match="incomplete|escapes source authority",
    ):
        prepare_historical_replay_evaluation_package(
            source,
            tmp_path / "package",
        )


def test_contaminated_workspace_is_rejected(tmp_path: Path) -> None:
    source = _synthetic_source(tmp_path)
    workspace = source / "visible/tasks/reduced-vars-gp/workspace"
    (workspace / "untracked-contamination.txt").write_text(
        "contaminated\n",
        encoding="utf-8",
    )

    with pytest.raises(EvaluationPackageError, match="contaminated"):
        prepare_historical_replay_evaluation_package(
            source,
            tmp_path / "package",
        )


def test_source_change_during_copy_rejects_atomic_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _synthetic_source(tmp_path)
    output = tmp_path / "package"
    campaign_manifest = source / "campaign.yaml"
    original = package_builder._copy_regular_file
    changed = False

    def copy_then_change(source_file: Path, destination: Path) -> None:
        nonlocal changed
        original(source_file, destination)
        if not changed:
            changed = True
            campaign_manifest.write_text(
                campaign_manifest.read_text(encoding="utf-8") + "\n",
                encoding="utf-8",
            )

    monkeypatch.setattr(
        package_builder,
        "_copy_regular_file",
        copy_then_change,
    )

    with pytest.raises(
        EvaluationPackageError,
        match="changed (during|after metadata loading)",
    ):
        prepare_historical_replay_evaluation_package(source, output)
    assert not output.exists()


def test_stage2_change_during_copy_rejects_atomic_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _synthetic_source(tmp_path)
    output = tmp_path / "package"
    stage2 = (
        source
        / "visible/tasks/reduced-vars-gp/control/stage2.yaml"
    )
    original = package_builder._copy_regular_file
    changed = False

    def copy_then_change(source_file: Path, destination: Path) -> None:
        nonlocal changed
        original(source_file, destination)
        if not changed:
            changed = True
            stage2.write_text(
                stage2.read_text(encoding="utf-8") + "\n",
                encoding="utf-8",
            )

    monkeypatch.setattr(
        package_builder,
        "_copy_regular_file",
        copy_then_change,
    )

    with pytest.raises(
        EvaluationPackageError,
        match="changed (during|after metadata loading)",
    ):
        prepare_historical_replay_evaluation_package(source, output)
    assert not output.exists()


def test_stage2_change_before_initial_fingerprint_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _synthetic_source(tmp_path)
    output = tmp_path / "package"
    stage2 = (
        source
        / "visible/tasks/reduced-vars-gp/control/stage2.yaml"
    )
    original = package_builder._source_fingerprint
    changed = False

    def change_then_fingerprint(
        authority: package_builder._SourceAuthority,
    ) -> str:
        nonlocal changed
        if not changed:
            changed = True
            stage2.write_text(
                stage2.read_text(encoding="utf-8") + "\n",
                encoding="utf-8",
            )
        return original(authority)

    monkeypatch.setattr(
        package_builder,
        "_source_fingerprint",
        change_then_fingerprint,
    )

    with pytest.raises(
        EvaluationPackageError,
        match="changed after metadata loading",
    ):
        prepare_historical_replay_evaluation_package(source, output)
    assert not output.exists()


def test_source_root_replacement_after_load_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _synthetic_source(tmp_path)
    parked = tmp_path / "parked-source"
    replacement = tmp_path / "replacement-source"
    shutil.copytree(source, replacement, copy_function=shutil.copy2)
    replacement_fixture = (
        replacement
        / "engine-only/gold/reduced-vars-gp/src/reduced-vars-gp.txt"
    )
    replacement_fixture.write_text(
        "replacement authority\n",
        encoding="utf-8",
    )
    original = package_builder._source_fingerprint
    replaced = False

    def replace_then_fingerprint(
        authority: package_builder._SourceAuthority,
    ) -> str:
        nonlocal replaced
        if not replaced:
            replaced = True
            source.rename(parked)
            replacement.rename(source)
        return original(authority)

    monkeypatch.setattr(
        package_builder,
        "_source_fingerprint",
        replace_then_fingerprint,
    )

    with pytest.raises(
        EvaluationPackageError,
        match="pathname identity",
    ):
        prepare_historical_replay_evaluation_package(
            source,
            tmp_path / "package",
        )
    assert replaced
    assert not (tmp_path / "package").exists()


def test_preparation_report_campaign_identity_must_match(
    tmp_path: Path,
) -> None:
    source = _synthetic_source(tmp_path)
    report_path = source / "preparation-report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["campaign_id"] = "different-campaign"
    report_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(EvaluationPackageError, match="incomplete"):
        prepare_historical_replay_evaluation_package(
            source,
            tmp_path / "package",
        )


def test_duplicate_dependency_authority_is_rejected(
    tmp_path: Path,
) -> None:
    source = _synthetic_source(tmp_path)
    source_state_path = (
        source / "engine-only/historical-audits/source-state.json"
    )
    source_state = json.loads(source_state_path.read_text(encoding="utf-8"))
    source_state["repositories"].append(
        dict(source_state["repositories"][0])
    )
    source_state_path.write_text(
        json.dumps(source_state, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(EvaluationPackageError, match="duplicated"):
        prepare_historical_replay_evaluation_package(
            source,
            tmp_path / "package",
        )


def test_copy_rejects_restored_source_after_torn_stream(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.bin"
    destination = tmp_path / "destination.bin"
    original_content = b"A" * (2 * 1024 * 1024)
    source.write_bytes(original_content)
    original_status = source.stat()
    original_read = package_builder.os.read
    source_reads = 0

    def adversarial_read(descriptor: int, count: int) -> bytes:
        nonlocal source_reads
        chunk = original_read(descriptor, count)
        status = os.fstat(descriptor)
        if status.st_ino != source.stat().st_ino or not chunk:
            return chunk
        source_reads += 1
        if source_reads == 3:
            mutation = os.open(source, os.O_WRONLY)
            try:
                os.pwrite(mutation, b"B" * (1024 * 1024), 1024 * 1024)
                os.fsync(mutation)
            finally:
                os.close(mutation)
        elif source_reads == 4:
            mutation = os.open(source, os.O_WRONLY)
            try:
                os.pwrite(mutation, b"A" * (1024 * 1024), 1024 * 1024)
                os.fsync(mutation)
            finally:
                os.close(mutation)
            os.utime(
                source,
                ns=(
                    original_status.st_atime_ns,
                    original_status.st_mtime_ns,
                ),
            )
        return chunk

    monkeypatch.setattr(package_builder.os, "read", adversarial_read)

    with pytest.raises(EvaluationPackageError, match="changed while"):
        package_builder._copy_regular_file(source, destination)
    assert source.read_bytes() == original_content
    assert source.stat().st_mtime_ns == original_status.st_mtime_ns
    assert source_reads >= 4


def test_copy_tree_rejects_restored_multifile_torn_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _synthetic_source(tmp_path)
    fixture_root = source / "engine-only/gold/reduced-vars-gp"
    allowed = fixture_root / "src/reduced-vars-gp.txt"
    functional_only = fixture_root / "src/zz-functional-only.txt"
    functional_only.write_text("stable\n", encoding="utf-8")
    original = package_builder._copy_regular_file
    mutated = False
    restored = False

    def adversarial_copy(source_file: Path, destination: Path) -> None:
        nonlocal mutated, restored
        original(source_file, destination)
        if source_file.name == allowed.name:
            functional_only.write_text("transient\n", encoding="utf-8")
            mutated = True
        elif source_file.name == functional_only.name:
            functional_only.write_text("stable\n", encoding="utf-8")
            restored = True

    monkeypatch.setattr(
        package_builder,
        "_copy_regular_file",
        adversarial_copy,
    )

    with pytest.raises(EvaluationPackageError, match="tree changed"):
        prepare_historical_replay_evaluation_package(
            source,
            tmp_path / "package",
        )
    assert mutated and restored
    assert functional_only.read_text(encoding="utf-8") == "stable\n"


def test_output_overlap_is_rejected_before_parent_creation(
    tmp_path: Path,
) -> None:
    source = _synthetic_source(tmp_path)
    missing_parent = source / "private-output"

    with pytest.raises(EvaluationPackageError, match="outside its source"):
        prepare_historical_replay_evaluation_package(
            source,
            missing_parent / "package",
        )
    assert not missing_parent.exists()


def test_atomic_publication_never_replaces_raced_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _synthetic_source(tmp_path)
    destination = tmp_path / "package"
    original = package_builder._rename_noreplace

    def create_destination_then_publish(
        staging: Path | str,
        selected_destination: Path | str,
        *,
        source_dir_fd: int = -100,
        destination_dir_fd: int = -100,
        forbidden_roots: tuple[Path, ...] = (),
    ) -> None:
        destination_path = destination.parent / selected_destination
        destination_path.mkdir()
        (destination_path / "owner-marker").write_text(
            "pre-existing\n",
            encoding="utf-8",
        )
        original(
            staging,
            selected_destination,
            source_dir_fd=source_dir_fd,
            destination_dir_fd=destination_dir_fd,
            forbidden_roots=forbidden_roots,
        )

    monkeypatch.setattr(
        package_builder,
        "_rename_noreplace",
        create_destination_then_publish,
    )

    with pytest.raises(EvaluationPackageError, match="appeared during"):
        prepare_historical_replay_evaluation_package(source, destination)
    assert (destination / "owner-marker").read_text(encoding="utf-8") == (
        "pre-existing\n"
    )


def test_output_parent_substitution_cannot_publish_inside_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _synthetic_source(tmp_path)
    parent = tmp_path / "offline-parent"
    parent.mkdir()
    injected = source / "injected-output-parent"
    destination = parent / "package"
    original = package_builder._rename_noreplace

    def substitute_parent_then_publish(
        staging: Path | str,
        selected_destination: Path | str,
        *,
        source_dir_fd: int = -100,
        destination_dir_fd: int = -100,
        forbidden_roots: tuple[Path, ...] = (),
    ) -> None:
        parent.rename(injected)
        original(
            staging,
            selected_destination,
            source_dir_fd=source_dir_fd,
            destination_dir_fd=destination_dir_fd,
            forbidden_roots=forbidden_roots,
        )

    monkeypatch.setattr(
        package_builder,
        "_rename_noreplace",
        substitute_parent_then_publish,
    )

    with pytest.raises(
        EvaluationPackageError,
        match="forbidden authority|output parent",
    ):
        prepare_historical_replay_evaluation_package(source, destination)
    assert not (injected / "package").exists()


def test_production_verifier_binds_archives_to_claimed_trees(
    tmp_path: Path,
) -> None:
    _source, package = _build_package(tmp_path)
    package.chmod(0o700)
    config_path = package / package_builder.PACKAGE_CONFIG_PATH
    config_path.chmod(0o600)
    config = json.loads(config_path.read_text(encoding="ascii"))
    baseline = package / config["tasks"][0]["baseline_archive"]
    baseline.chmod(0o600)
    replacement = tmp_path / "replacement.txt"
    replacement.write_text("contradictory baseline\n", encoding="utf-8")
    with tarfile.open(baseline, mode="w") as archive:
        archive.add(replacement, arcname="replacement.txt")
    config["tasks"][0]["baseline_archive_sha256"] = hashlib.sha256(
        baseline.read_bytes()
    ).hexdigest()
    config_path.write_text(
        json.dumps(config, indent=2, sort_keys=True) + "\n",
        encoding="ascii",
    )
    _reseal_package(package)

    with pytest.raises(EvaluationPackageError, match="contradicts"):
        verify_evaluation_package(package)
    with pytest.raises(EvaluationPackageError, match="contradicts"):
        historical_replay_command_report(
            candidate=tmp_path / "candidate",
            source_prepared_campaign=tmp_path / "source",
            evaluation_package=package,
            output=tmp_path / "report",
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ("failed_status", "incomplete"),
        ("missing_source_tree", "source tree"),
        ("stale_stage2", "newer than preparation"),
    ),
)
def test_failed_or_stale_preparation_authority_is_rejected(
    tmp_path: Path,
    mutation: str,
    message: str,
) -> None:
    source = _synthetic_source(tmp_path)
    report_path = source / "preparation-report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if mutation == "failed_status":
        report["status"] = "prepared_but_preflight_failed"
    elif mutation == "missing_source_tree":
        del report["tasks"][0]["source_tree"]
    else:
        stage2 = (
            source
            / "visible/tasks/reduced-vars-gp/control/stage2.yaml"
        )
        stage2.write_text(
            stage2.read_text(encoding="utf-8") + "\n",
            encoding="utf-8",
        )
    if mutation != "stale_stage2":
        report_path.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    with pytest.raises(EvaluationPackageError, match=message):
        prepare_historical_replay_evaluation_package(
            source,
            tmp_path / "package",
        )


def test_production_verifier_rejects_incomplete_sealed_package(
    tmp_path: Path,
) -> None:
    package = tmp_path / "incomplete-package"
    config = package / package_builder.PACKAGE_CONFIG_PATH
    config.parent.mkdir(parents=True)
    config.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "package_id": "incomplete",
                "tasks": [],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="ascii",
    )
    config.chmod(0o400)
    config.parent.chmod(0o500)
    package_builder.write_evaluation_package_manifest(
        package,
        package_id="incomplete",
    )
    manifest = package / package_builder.PACKAGE_MANIFEST_NAME
    manifest.chmod(0o400)
    package.chmod(0o500)

    with pytest.raises(
        EvaluationPackageError,
        match="provenance|five tasks",
    ):
        verify_evaluation_package(package)
    with pytest.raises(EvaluationPackageError):
        historical_replay_command_report(
            candidate=tmp_path / "candidate",
            source_prepared_campaign=tmp_path / "source",
            evaluation_package=package,
            output=tmp_path / "report",
        )


@pytest.mark.parametrize("mutation", ("root_mode", "manifest_mode", "encoding"))
def test_production_verifier_enforces_canonical_seal(
    tmp_path: Path,
    mutation: str,
) -> None:
    _source, package = _build_package(tmp_path)
    manifest_path = package / package_builder.PACKAGE_MANIFEST_NAME
    if mutation == "root_mode":
        package.chmod(0o700)
    elif mutation == "manifest_mode":
        package.chmod(0o700)
        manifest_path.chmod(0o600)
        package.chmod(0o500)
    else:
        package.chmod(0o700)
        manifest_path.chmod(0o600)
        manifest = json.loads(manifest_path.read_text(encoding="ascii"))
        manifest_path.write_text(
            json.dumps(manifest, separators=(",", ":"), sort_keys=True),
            encoding="ascii",
        )
        manifest_path.chmod(0o400)
        package.chmod(0o500)

    with pytest.raises(EvaluationPackageError, match="seal"):
        verify_evaluation_package(package)


def test_package_manifest_detects_protected_content_mutation(
    tmp_path: Path,
) -> None:
    _source, package = _build_package(tmp_path)
    fixture = (
        package
        / "protected-fixtures/reduced-vars-gp/src/reduced-vars-gp.txt"
    )
    for parent in (package, *fixture.parents):
        if parent == package.parent:
            break
        parent.chmod(0o700)
    fixture.chmod(0o600)
    fixture.write_text("mutated\n", encoding="utf-8")
    fixture.chmod(0o400)
    for parent in (package, *fixture.parents):
        if parent == package.parent:
            break
        parent.chmod(0o500)

    with pytest.raises(EvaluationPackageError, match="manifest"):
        verify_evaluation_package(package)


def test_evaluator_accepts_valid_synthetic_five_task_package(
    tmp_path: Path,
) -> None:
    _source, package = _build_package(tmp_path)
    candidate = _synthetic_candidate(
        package,
        tmp_path / "campaign-run/final-candidate",
    )
    candidate_manifest = (candidate / "candidate-manifest.json").read_bytes()
    package_manifest = (
        package / package_builder.PACKAGE_MANIFEST_NAME
    ).read_bytes()

    report_path = evaluate_historical_replay(
        candidate,
        package,
        tmp_path / "report",
    )

    report = json.loads(report_path.read_text(encoding="ascii"))
    assert report["passed"] is True
    assert report["score"] == {
        "passed_tasks": 5,
        "functional_passed_tasks": 5,
        "exact_match_tasks": 5,
        "strict_combined_passed_tasks": 5,
        "total_tasks": 5,
    }
    assert report["schema_version"] == 2
    assert report["all_functional_passed"] is True
    assert report["all_exact_matched"] is True
    assert all(task["changed_paths_passed"] for task in report["tasks"])
    assert all(task["functional_tests_passed"] for task in report["tasks"])
    assert all(task["production_profile"]["passed"] for task in report["tasks"])
    for task in report["tasks"]:
        test = task["tests"][0]
        stdout = report_path.parent / test["stdout_artifact"]
        stderr = report_path.parent / test["stderr_artifact"]
        stdout_record = json.loads(stdout.read_text(encoding="ascii"))
        stderr_record = json.loads(stderr.read_text(encoding="ascii"))
        assert stdout_record["content_policy"] == (
            "untrusted_output_digest_only"
        )
        assert stderr_record["content_policy"] == (
            "untrusted_output_digest_only"
        )
        assert stdout_record["stream"] == "stdout"
        assert stderr_record["stream"] == "stderr"
        assert stdout_record["python_exception"] is None
        assert stderr_record["python_exception"] is None
        assert hashlib.sha256(stdout.read_bytes()).hexdigest() == (
            test["stdout_artifact_sha256"]
        )
        assert hashlib.sha256(stderr.read_bytes()).hexdigest() == (
            test["stderr_artifact_sha256"]
        )
        assert stat.S_IMODE(stdout.stat().st_mode) == 0o400
        assert stat.S_IMODE(stderr.stat().st_mode) == 0o400
    second_report = evaluate_historical_replay(
        candidate,
        package,
        tmp_path / "report-second",
    )
    first_artifacts = {
        path.name: path.read_bytes()
        for path in (report_path.parent / "artifacts").iterdir()
    }
    second_artifacts = {
        path.name: path.read_bytes()
        for path in (second_report.parent / "artifacts").iterdir()
    }
    assert first_artifacts == second_artifacts
    assert (candidate / "candidate-manifest.json").read_bytes() == (
        candidate_manifest
    )
    assert (package / package_builder.PACKAGE_MANIFEST_NAME).read_bytes() == (
        package_manifest
    )


def test_functional_test_side_effects_do_not_change_exact_identity(
    tmp_path: Path,
) -> None:
    source = _synthetic_source(tmp_path)
    evaluator = source / "engine-only/evaluators/evaluate.py"
    content = evaluator.read_text(encoding="utf-8")
    evaluator.chmod(0o755)
    evaluator.write_text(
        content.replace(
            "raise SystemExit(not passed)\n",
            "Path(workspace, 'functional-test-created.tmp').write_text("
            "'test side effect')\n"
            "raise SystemExit(not passed)\n",
        ),
        encoding="utf-8",
    )
    evaluator.chmod(0o555)
    package, _digest = prepare_historical_replay_evaluation_package(
        source,
        tmp_path / "private-offline-package",
    )
    candidate = _synthetic_candidate(
        package,
        tmp_path / "campaign-run/final-candidate",
    )

    report_path = evaluate_historical_replay(
        candidate,
        package,
        tmp_path / "report",
    )
    report = json.loads(report_path.read_text(encoding="ascii"))

    assert report["all_functional_passed"] is True
    assert report["all_exact_matched"] is True
    assert report["exact_match_tasks"] == 5


def test_diagnostic_artifacts_do_not_store_protected_output(
    tmp_path: Path,
) -> None:
    source = _synthetic_source(tmp_path)
    evaluator = source / "engine-only/evaluators/evaluate.py"
    sentinel = "SYNTHETIC-PROTECTED-SOURCE-CONTENT"
    content = evaluator.read_text(encoding="utf-8")
    evaluator.chmod(0o755)
    evaluator.write_text(
        content.replace(
            "raise SystemExit(not passed)\n",
            f"print({sentinel!r})\nraise SystemExit(not passed)\n",
        ),
        encoding="utf-8",
    )
    evaluator.chmod(0o555)
    package, _digest = prepare_historical_replay_evaluation_package(
        source,
        tmp_path / "private-offline-package",
    )
    candidate = _synthetic_candidate(
        package,
        tmp_path / "campaign-run/final-candidate",
    )

    report_path = evaluate_historical_replay(
        candidate,
        package,
        tmp_path / "report",
    )

    for artifact in (report_path.parent / "artifacts").iterdir():
        assert sentinel.encode() not in artifact.read_bytes()
    assert sentinel.encode() not in report_path.read_bytes()


def test_output_capture_is_memory_bounded_and_artifact_is_diagnostic_only(
    tmp_path: Path,
) -> None:
    source = _synthetic_source(tmp_path)
    evaluator = source / "engine-only/evaluators/evaluate.py"
    content = evaluator.read_text(encoding="utf-8")
    evaluator.chmod(0o755)
    evaluator.write_text(
        content.replace(
            "raise SystemExit(not passed)\n",
            "sys.stdout.write('X' * 1048576)\n"
            "raise SystemExit(not passed)\n",
        ),
        encoding="utf-8",
    )
    evaluator.chmod(0o555)
    package, _digest = prepare_historical_replay_evaluation_package(
        source,
        tmp_path / "private-offline-package",
    )
    package.chmod(0o700)
    config_path = package / package_builder.PACKAGE_CONFIG_PATH
    config_path.parent.chmod(0o700)
    config_path.chmod(0o600)
    config = json.loads(config_path.read_text(encoding="ascii"))
    for task in config["tasks"]:
        task["tests"][0]["max_stdout_bytes"] = 64
    config_path.write_text(
        json.dumps(config, indent=2, sort_keys=True) + "\n",
        encoding="ascii",
    )
    _reseal_package(package)
    candidate = _synthetic_candidate(
        package,
        tmp_path / "campaign-run/final-candidate",
    )

    report_path = evaluate_historical_replay(
        candidate,
        package,
        tmp_path / "report",
    )
    report = json.loads(report_path.read_text(encoding="ascii"))
    first = report["tasks"][0]["tests"][0]
    artifact = report_path.parent / first["stdout_artifact"]

    assert first["stdout_byte_length"] == 64
    assert first["stdout_observed_byte_length"] > 1_000_000
    assert first["stdout_truncated"] is True
    assert artifact.stat().st_size < 1_024
    assert b"X" * 32 not in artifact.read_bytes()


def test_python_exception_diagnostic_retains_safe_git_failure_fields() -> None:
    raw = (
        b"Traceback (most recent call last):\n"
        b"FileNotFoundError: [Errno 2] No such file or directory: "
        b"'/usr/bin/git'\n"
        b"SyntheticProtectedError: '/usr/bin/protected-secret'\n"
        b"SYNTHETIC-PROTECTED-SOURCE-CONTENT\n"
    )
    record = offline_evaluator._bounded_stream_record(
        io.BytesIO(raw),
        len(raw),
    )
    artifact = offline_evaluator._diagnostic_artifact_bytes(
        "stderr",
        record,
    )
    parsed = json.loads(artifact)

    assert parsed["python_exception"] == {
        "type": "FileNotFoundError",
        "errno": 2,
        "qualified_executable": "/usr/bin/git",
    }
    assert b"SyntheticProtectedError" not in artifact
    assert b"/usr/bin/protected-secret" not in artifact
    assert b"SYNTHETIC-PROTECTED-SOURCE-CONTENT" not in artifact

    mixed_raw = (
        b"FileNotFoundError:\n"
        b"SYNTHETIC SECRET [Errno 8675309] '/usr/bin/git'\n"
    )
    mixed_record = offline_evaluator._bounded_stream_record(
        io.BytesIO(mixed_raw),
        len(mixed_raw),
    )
    mixed_artifact = json.loads(
        offline_evaluator._diagnostic_artifact_bytes(
            "stderr",
            mixed_record,
        )
    )
    assert mixed_artifact["python_exception"] == {
        "type": "FileNotFoundError",
        "errno": None,
        "qualified_executable": None,
    }
    assert b"8675309" not in json.dumps(mixed_artifact).encode()

    oversized_errno = (
        b"FileNotFoundError: [Errno " + (b"9" * 5_000) + b"]\n"
    )
    oversized_record = offline_evaluator._bounded_stream_record(
        io.BytesIO(oversized_errno),
        len(oversized_errno),
    )
    oversized_artifact = json.loads(
        offline_evaluator._diagnostic_artifact_bytes(
            "stderr",
            oversized_record,
        )
    )
    assert oversized_artifact["python_exception"] == {
        "type": "FileNotFoundError",
        "errno": None,
        "qualified_executable": None,
    }


def test_sealed_git_runtime_hides_host_repositories_and_configuration(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tracked = workspace / "tracked.txt"
    tracked.write_text("baseline\n", encoding="utf-8")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    offline_evaluator._initialize_ephemeral_git_baseline(
        workspace,
        scratch,
        offline_evaluator._git_tree_oid(workspace),
    )
    private_git_config = workspace / ".git/config"
    private_git_config_bytes = private_git_config.read_bytes()
    tracked.write_text("changed\n", encoding="utf-8")

    host_repository = tmp_path / "unrelated-host-repository"
    _repository(host_repository, {"secret.txt": "host-only\n"})
    host_config = tmp_path / "host.gitconfig"
    host_config.write_text(
        "[credential]\n\thelper = malicious-helper\n",
        encoding="utf-8",
    )
    package = tmp_path / "probe-package"
    package.mkdir()
    probe = package / "probe.py"
    probe.write_text(
        "import os\n"
        "from pathlib import Path\n"
        "import subprocess\n"
        "import sys\n"
        f"host_repo = Path({str(host_repository)!r})\n"
        f"host_config = Path({str(host_config)!r})\n"
        "required = {\n"
        "  'GIT_CONFIG_NOSYSTEM': '1',\n"
        "  'GIT_CONFIG_GLOBAL': '/dev/null',\n"
        "  'GIT_TERMINAL_PROMPT': '0',\n"
        "  'GIT_ASKPASS': '/nonexistent',\n"
        "}\n"
        "status = subprocess.run(\n"
        "  ['/usr/bin/git', 'status', '--porcelain=v1', '-z',\n"
        "   '--untracked-files=all'], capture_output=True, check=False,\n"
        ")\n"
        "config = subprocess.run(\n"
        "  ['/usr/bin/git', 'config', '--global', '--list'],\n"
        "  capture_output=True, check=False,\n"
        ")\n"
        "try:\n"
        "  Path('.git/config').write_text('[credential]\\nhelper=hostile\\n')\n"
        "except OSError:\n"
        "  git_metadata_read_only = True\n"
        "else:\n"
        "  git_metadata_read_only = False\n"
        "passed = (\n"
        "  all(os.environ.get(k) == v for k, v in required.items())\n"
        "  and 'GIT_DIR' not in os.environ\n"
        "  and 'GIT_WORK_TREE' not in os.environ\n"
        "  and not host_repo.exists()\n"
        "  and not host_config.exists()\n"
        "  and status.returncode == 0\n"
        "  and b'tracked.txt' in status.stdout\n"
        "  and config.returncode == 0\n"
        "  and config.stdout == b''\n"
        "  and git_metadata_read_only\n"
        ")\n"
        "sys.exit(not passed)\n",
        encoding="utf-8",
    )
    command = offline_evaluator._offline_bubblewrap_command(
        workspace,
        package,
        (
            "/usr/bin/python3",
            "-I",
            "-S",
            "-B",
            "/evaluation/probe.py",
        ),
    )
    completed = subprocess.run(
        command,
        cwd=None,
        env={
            "GIT_CONFIG_GLOBAL": str(host_config),
            "GIT_DIR": str(host_repository / ".git"),
            "GIT_WORK_TREE": str(host_repository),
            "HOME": str(tmp_path),
            "LC_ALL": "C",
            "PATH": "/usr/bin:/bin",
        },
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr.decode(
        "utf-8",
        errors="replace",
    )
    assert private_git_config.read_bytes() == private_git_config_bytes


def test_ephemeral_git_baseline_uses_private_config_and_tracks_ignored_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".gitignore").write_text("tracked.ignored\n", encoding="utf-8")
    (workspace / "tracked.ignored").write_text("committed\n", encoding="utf-8")
    expected_tree = offline_evaluator._git_tree_oid(workspace)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    hostile_xdg = tmp_path / "host-xdg"
    (hostile_xdg / "git").mkdir(parents=True)
    (hostile_xdg / "git/ignore").write_text("*\n", encoding="utf-8")
    monkeypatch.setattr(
        offline_evaluator,
        "_GIT_ENVIRONMENT",
        {
            **offline_evaluator._GIT_ENVIRONMENT,
            "XDG_CONFIG_HOME": str(hostile_xdg),
        },
    )
    original_run = offline_evaluator.subprocess.run
    initializer_environments: list[dict[str, str]] = []

    def record_run(
        command: Sequence[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        environment = kwargs.get("env")
        if (
            command
            and command[0] == str(offline_evaluator._GIT)
            and isinstance(environment, dict)
            and "GIT_AUTHOR_DATE" in environment
        ):
            initializer_environments.append(environment.copy())
        return original_run(command, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(offline_evaluator.subprocess, "run", record_run)
    offline_evaluator._initialize_ephemeral_git_baseline(
        workspace,
        scratch,
        expected_tree,
    )

    assert initializer_environments
    assert {
        environment["XDG_CONFIG_HOME"]
        for environment in initializer_environments
    } == {str(scratch / "git-xdg")}
    assert all(
        environment["GIT_ATTR_NOSYSTEM"] == "1"
        for environment in initializer_environments
    )
    assert _git(workspace, "ls-files").splitlines() == [
        ".gitignore",
        "tracked.ignored",
    ]


def test_ephemeral_git_baseline_bypasses_committed_attribute_filters(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "tracked.txt").write_bytes(b"committed\r\nbytes\r\n")
    (workspace / ".gitattributes").write_text(
        "*.txt text eol=lf\n",
        encoding="ascii",
    )
    (workspace / ".gitignore").write_text(
        "tracked.txt\n",
        encoding="ascii",
    )
    expected_tree = offline_evaluator._git_tree_oid(workspace)
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    offline_evaluator._initialize_ephemeral_git_baseline(
        workspace,
        scratch,
        expected_tree,
    )

    assert _git(workspace, "rev-parse", "HEAD^{tree}") == expected_tree
    assert _git(
        workspace,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
    ) == ""
    assert (workspace / "tracked.txt").read_bytes() == (
        b"committed\r\nbytes\r\n"
    )


@pytest.mark.parametrize("invalid_git", ("symlink", "non_executable"))
def test_sealed_git_runtime_requires_qualified_regular_executable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid_git: str,
) -> None:
    selected = tmp_path / "git"
    if invalid_git == "symlink":
        selected.symlink_to("/usr/bin/git")
    else:
        selected.write_bytes(Path("/usr/bin/git").read_bytes())
        selected.chmod(0o600)
    monkeypatch.setattr(offline_evaluator, "_GIT", selected)
    workspace = tmp_path / "workspace"
    package = tmp_path / "package"
    workspace.mkdir()
    package.mkdir()

    with pytest.raises(
        offline_evaluator.OfflineEvaluationError,
        match="Git is required",
    ):
        offline_evaluator._offline_bubblewrap_command(
            workspace,
            package,
            ("/usr/bin/python3", "-I", "-S", "-B", "-c", "pass"),
        )


def test_reduced_vars_profile_keeps_unclassified_expected_header_compatible() -> None:
    result = offline_evaluator._production_profile_analysis(
        {
            "hot_path": ["BlackStringReducedVars.hpp"],
            "post_update": [],
            "validation_only": [
                "BlackStringReducedVarsTest.cpp",
                "BlackStringGPPointwiseInitialDataTest.cpp",
            ],
        },
        [
            "code/BlackStringToy/BlackStringReducedVars.hpp",
            "code/BlackStringToy/BlackStringGPPointwiseInitialData.hpp",
        ],
    )

    assert result == {
        "passed": True,
        "classifications_exhaustive": False,
        "classified_paths": {
            "code/BlackStringToy/BlackStringReducedVars.hpp": ["hot_path"],
        },
        "unclassified_paths": [
            "code/BlackStringToy/BlackStringGPPointwiseInitialData.hpp"
        ],
    }


def test_functionally_passing_non_exact_candidate_scores_separately(
    tmp_path: Path,
) -> None:
    _source, package = _build_package(tmp_path)
    package.chmod(0o700)
    evaluator = package / "evaluators/historical-functional.py"
    evaluator.parent.chmod(0o700)
    evaluator.chmod(0o600)
    evaluator.write_text(
        "from pathlib import Path\n"
        "import subprocess\n"
        "import sys\n"
        "mode, task, workspace, fixture = sys.argv[1:]\n"
        "relative = Path('src') / f'{task}.txt'\n"
        "status = subprocess.run(\n"
        "  ['/usr/bin/git', '-C', workspace, 'status',\n"
        "   '--porcelain=v1', '-z', '--untracked-files=all'],\n"
        "  capture_output=True, check=False,\n"
        ")\n"
        "content = Path(workspace, relative).read_bytes()\n"
        "reference = Path(fixture, relative).read_bytes()\n"
        "passed = (\n"
        "  mode == 'functional' and status.returncode == 0\n"
        "  and str(relative).encode() in status.stdout\n"
        "  and (content == reference or content.startswith(b'alternative '))\n"
        ")\n"
        "raise SystemExit(not passed)\n",
        encoding="utf-8",
    )
    config_path = package / package_builder.PACKAGE_CONFIG_PATH
    config_path.parent.chmod(0o700)
    config_path.chmod(0o600)
    config = json.loads(config_path.read_text(encoding="ascii"))
    evaluator_sha256 = hashlib.sha256(evaluator.read_bytes()).hexdigest()
    for task in config["tasks"]:
        task["tests"][0]["script_sha256"] = evaluator_sha256
    config_path.write_text(
        json.dumps(config, indent=2, sort_keys=True) + "\n",
        encoding="ascii",
    )
    _reseal_package(package)
    candidate = _synthetic_candidate(
        package,
        tmp_path / "campaign-run/final-candidate",
    )
    task_id = package_builder.TASK_IDS[0]
    changed = (
        candidate
        / f"tasks/{task_id}/changed-files/src/{task_id}.txt"
    )
    changes_path = candidate / f"tasks/{task_id}/changes.json"
    candidate.chmod(0o700)
    for selected in (changed.parent, changes_path.parent):
        selected.chmod(0o700)
    changed.chmod(0o600)
    changes_path.chmod(0o600)
    content = f"alternative {task_id}\n".encode()
    changed.write_bytes(content)
    changes = json.loads(changes_path.read_text(encoding="ascii"))
    changes["entries"][0]["byte_length"] = len(content)
    changes["entries"][0]["content_sha256"] = hashlib.sha256(content).hexdigest()
    changes_path.write_text(
        json.dumps(changes, indent=2, sort_keys=True) + "\n",
        encoding="ascii",
    )
    _reseal_candidate(candidate)

    report_path = evaluate_historical_replay(
        candidate,
        package,
        tmp_path / "report",
    )
    report = json.loads(report_path.read_text(encoding="ascii"))

    assert report["passed"] is True
    assert report["all_functional_passed"] is True
    assert report["functional_passed_tasks"] == 5
    assert report["all_exact_matched"] is False
    assert report["exact_match_tasks"] == 4
    assert report["strict_combined_passed"] is False
    first = report["tasks"][0]
    assert first["changed_paths_passed"] is True
    assert first["functional_tests_passed"] is True
    assert first["production_profile_passed"] is True
    assert first["functional_passed"] is True
    assert first["exact_match"] is False
    assert first["passed"] is True


@pytest.mark.parametrize(
    "private_path",
    (".git/config", ".git/objects/info/alternates"),
)
def test_candidate_cannot_overlay_private_git_metadata(
    tmp_path: Path,
    private_path: str,
) -> None:
    _source, package = _build_package(tmp_path)
    candidate = _synthetic_candidate(
        package,
        tmp_path / "campaign-run/final-candidate",
    )
    task_id = package_builder.TASK_IDS[0]
    task_root = candidate / "tasks" / task_id
    changes_path = task_root / "changes.json"
    injected = task_root / "changed-files" / private_path
    candidate.chmod(0o700)
    for parent in reversed(injected.parents):
        if parent == candidate.parent:
            continue
        if (
            (parent == candidate or candidate in parent.parents)
            and parent.exists()
        ):
            parent.chmod(0o700)
    injected.parent.mkdir(parents=True, exist_ok=True)
    injected.write_text(
        "[credential]\n\thelper = hostile\n",
        encoding="utf-8",
    )
    changes_path.chmod(0o600)
    changes = json.loads(changes_path.read_text(encoding="ascii"))
    content = injected.read_bytes()
    changes["entries"].append(
        {
            "path": private_path,
            "operation": "upsert",
            "object_type": "regular",
            "mode": 0o644,
            "byte_length": len(content),
            "content_sha256": hashlib.sha256(content).hexdigest(),
        }
    )
    changes_path.write_text(
        json.dumps(changes, indent=2, sort_keys=True) + "\n",
        encoding="ascii",
    )
    _reseal_candidate(candidate)

    with pytest.raises(
        offline_evaluator.OfflineEvaluationError,
        match="private Git metadata",
    ):
        evaluate_historical_replay(
            candidate,
            package,
            tmp_path / "report",
        )
    assert not (tmp_path / "report").exists()


def test_candidate_symlink_cannot_target_private_git_metadata(
    tmp_path: Path,
) -> None:
    _source, package = _build_package(tmp_path)
    candidate = _synthetic_candidate(
        package,
        tmp_path / "campaign-run/final-candidate",
    )
    task_id = package_builder.TASK_IDS[0]
    changes_path = candidate / f"tasks/{task_id}/changes.json"
    candidate.chmod(0o700)
    changes_path.parent.chmod(0o700)
    changes_path.chmod(0o600)
    changes = json.loads(changes_path.read_text(encoding="ascii"))
    link_target = ".git/config"
    encoded = os.fsencode(link_target)
    changes["entries"].append(
        {
            "path": "git-config-link",
            "operation": "upsert",
            "object_type": "symlink",
            "target": link_target,
            "byte_length": len(encoded),
            "content_sha256": hashlib.sha256(encoded).hexdigest(),
        }
    )
    changes_path.write_text(
        json.dumps(changes, indent=2, sort_keys=True) + "\n",
        encoding="ascii",
    )
    _reseal_candidate(candidate)

    with pytest.raises(
        offline_evaluator.OfflineEvaluationError,
        match="symlink targets private Git metadata",
    ):
        evaluate_historical_replay(
            candidate,
            package,
            tmp_path / "report",
        )


def test_evaluator_snapshots_candidate_before_using_changed_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _source, package = _build_package(tmp_path)
    candidate = _synthetic_candidate(
        package,
        tmp_path / "campaign-run/final-candidate",
    )
    changed = (
        candidate
        / "tasks/reduced-vars-gp/changed-files/src/reduced-vars-gp.txt"
    )
    original = offline_evaluator._copy_authority_tree
    mutated = False

    def mutate_then_copy(source: Path, destination: Path) -> None:
        nonlocal mutated
        if not mutated:
            mutated = True
            candidate.chmod(0o700)
            for parent in changed.parents:
                if parent == candidate.parent:
                    break
                parent.chmod(0o700)
            changed.chmod(0o600)
            changed.write_text("post-verification mutation\n", encoding="utf-8")
            changed.chmod(0o400)
            for parent in changed.parents:
                if parent == candidate.parent:
                    break
                parent.chmod(0o500)
        original(source, destination)

    monkeypatch.setattr(
        offline_evaluator,
        "_copy_authority_tree",
        mutate_then_copy,
    )

    with pytest.raises(
        offline_evaluator.OfflineEvaluationError,
        match="changed (while it was snapshotted|after finalization)",
    ):
        evaluate_historical_replay(
            candidate,
            package,
            tmp_path / "report",
        )
    assert mutated


@pytest.mark.parametrize("replaced_authority", ("candidate", "package"))
def test_evaluator_rejects_root_replacement_after_initial_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replaced_authority: str,
) -> None:
    _source, package = _build_package(tmp_path)
    candidate = _synthetic_candidate(
        package,
        tmp_path / "campaign-run/final-candidate",
    )
    selected = candidate if replaced_authority == "candidate" else package
    replacement = selected.parent / f".replacement-{replaced_authority}"
    parked = selected.parent / f".parked-{replaced_authority}"
    shutil.copytree(selected, replacement, copy_function=shutil.copy2)
    original = offline_evaluator._copy_authority_tree
    call_count = 0

    def replace_then_copy(source: Path, destination: Path) -> None:
        nonlocal call_count
        call_count += 1
        selected_call = 1 if replaced_authority == "candidate" else 2
        if call_count == selected_call:
            if replaced_authority == "candidate":
                selected.parent.chmod(0o700)
            selected.rename(parked)
            replacement.rename(selected)
        original(source, destination)

    monkeypatch.setattr(
        offline_evaluator,
        "_copy_authority_tree",
        replace_then_copy,
    )

    with pytest.raises(
        offline_evaluator.OfflineEvaluationError,
        match="pathname identity",
    ):
        evaluate_historical_replay(
            candidate,
            package,
            tmp_path / "report",
        )


def test_evaluator_output_substitution_cannot_modify_campaign(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _source, package = _build_package(tmp_path)
    candidate = _synthetic_candidate(
        package,
        tmp_path / "campaign-run/final-candidate",
    )
    output = tmp_path / "report"
    original = offline_evaluator._write_output_report

    def substitute_then_write(
        target: offline_evaluator._OutputDirectory,
        report: dict[str, object],
        artifacts: dict[str, bytes],
    ) -> Path:
        candidate.parent.chmod(0o700)
        target.staging_path.rename(candidate.parent / "injected-report")
        return original(target, report, artifacts)

    monkeypatch.setattr(
        offline_evaluator,
        "_write_output_report",
        substitute_then_write,
    )

    with pytest.raises(
        offline_evaluator.OfflineEvaluationError,
        match="output (staging )?directory",
    ):
        evaluate_historical_replay(candidate, package, output)
    assert not (
        candidate.parent / "injected-report" / offline_evaluator.REPORT_NAME
    ).exists()


def test_failed_evaluation_removes_unpublished_output_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _source, package = _build_package(tmp_path)
    candidate = _synthetic_candidate(
        package,
        tmp_path / "campaign-run/final-candidate",
    )
    output = tmp_path / "report"

    def reject_publication(
        _target: offline_evaluator._OutputDirectory,
    ) -> None:
        raise offline_evaluator.OfflineEvaluationError(
            "synthetic publication failure"
        )

    monkeypatch.setattr(
        offline_evaluator,
        "_rename_output_noreplace",
        reject_publication,
    )

    with pytest.raises(
        offline_evaluator.OfflineEvaluationError,
        match="synthetic publication failure",
    ):
        evaluate_historical_replay(candidate, package, output)

    assert not output.exists()
    assert not tuple(tmp_path.glob(".report.staging-*"))


def test_raced_output_preserves_existing_output_and_removes_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _source, package = _build_package(tmp_path)
    candidate = _synthetic_candidate(
        package,
        tmp_path / "campaign-run/final-candidate",
    )
    output = tmp_path / "report"
    original = offline_evaluator._rename_output_noreplace

    def race_publication(
        target: offline_evaluator._OutputDirectory,
    ) -> None:
        target.path.mkdir()
        (target.path / "owner.txt").write_text(
            "unrelated output\n",
            encoding="ascii",
        )
        original(target)

    monkeypatch.setattr(
        offline_evaluator,
        "_rename_output_noreplace",
        race_publication,
    )

    with pytest.raises(
        offline_evaluator.OfflineEvaluationError,
        match="appeared during publication",
    ):
        evaluate_historical_replay(candidate, package, output)

    assert (output / "owner.txt").read_text(encoding="ascii") == (
        "unrelated output\n"
    )
    assert not tuple(tmp_path.glob(".report.staging-*"))


def test_post_rename_fsync_failure_has_successful_published_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _source, package = _build_package(tmp_path)
    candidate = _synthetic_candidate(
        package,
        tmp_path / "campaign-run/final-candidate",
    )
    output = tmp_path / "report"
    original = offline_evaluator.os.fsync

    def fail_after_publication(descriptor: int) -> None:
        if output.exists():
            raise OSError("synthetic post-rename fsync failure")
        original(descriptor)

    monkeypatch.setattr(offline_evaluator.os, "fsync", fail_after_publication)

    report = evaluate_historical_replay(candidate, package, output)

    assert report == output / offline_evaluator.REPORT_NAME
    assert report.is_file()
    assert not tuple(tmp_path.glob(".report.staging-*"))


def test_candidate_manifest_requires_canonical_identity_fields(
    tmp_path: Path,
) -> None:
    _source, package = _build_package(tmp_path)
    candidate = _synthetic_candidate(
        package,
        tmp_path / "campaign-run/final-candidate",
    )
    manifest_path = candidate / "candidate-manifest.json"
    candidate.chmod(0o700)
    manifest_path.chmod(0o600)
    manifest = json.loads(manifest_path.read_text(encoding="ascii"))
    incomplete = {"entries": manifest["entries"]}
    incomplete["candidate_manifest_sha256"] = (
        offline_evaluator._sha256_json(incomplete)
    )
    manifest_path.write_bytes(offline_evaluator._json_bytes(incomplete))
    manifest_path.chmod(0o400)
    candidate.chmod(0o500)

    with pytest.raises(
        offline_evaluator.OfflineEvaluationError,
        match="schema or identity",
    ):
        offline_evaluator._verify_candidate(candidate)


def test_reporting_requires_two_commands_when_package_is_missing(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing-private-package"
    report = historical_replay_command_report(
        candidate=tmp_path / "candidate",
        source_prepared_campaign=tmp_path / "preserved-source",
        evaluation_package=missing,
        output=tmp_path / "report",
    )

    assert report["evaluation_package_status"] == "missing"
    assert "prepare-historical-replay-evaluation-package" in str(
        report["package_preparation_command"]
    )
    assert str(missing) in str(report["package_preparation_command"])
    assert str(missing) in str(report["evaluation_command"])


def test_missing_package_cli_prints_preparation_before_evaluation(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = package_builder.command_report_main(
        (
            "--candidate",
            str(tmp_path / "candidate"),
            "--source-prepared-campaign",
            str(tmp_path / "preserved-source"),
            "--evaluation-package",
            str(tmp_path / "missing-package"),
            "--output",
            str(tmp_path / "report"),
        )
    )

    captured = capsys.readouterr()
    assert result == 0
    assert captured.err == ""
    assert captured.out.index("package_preparation_command") < (
        captured.out.index("evaluation_command")
    )


def test_reporting_allows_direct_evaluation_only_for_valid_package(
    tmp_path: Path,
) -> None:
    _source, package = _build_package(tmp_path)
    report = historical_replay_command_report(
        candidate=tmp_path / "candidate",
        source_prepared_campaign=tmp_path / "preserved-source",
        evaluation_package=package,
        output=tmp_path / "report",
    )

    assert report["evaluation_package_status"] == "validated"
    assert report["package_preparation_command"] is None
    assert report["evaluation_package_manifest_sha256"]


def test_cli_never_prints_protected_content(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = _synthetic_source(tmp_path)
    sentinel = "SYNTHETIC-PROTECTED-CONTENT-MUST-NOT-BE-LOGGED"
    fixture = source / "engine-only/gold/reduced-vars-gp/src/reduced-vars-gp.txt"
    fixture.write_text(sentinel, encoding="utf-8")

    result = package_builder.main(
        (
            "--source-prepared-campaign",
            str(source),
            "--output",
            str(tmp_path / "package"),
        )
    )

    captured = capsys.readouterr()
    assert result == 0
    assert sentinel not in captured.out
    assert sentinel not in captured.err


def test_package_builder_imports_no_campaign_or_model_runtime() -> None:
    source = Path(package_builder.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }

    assert not any(
        "campaign" in name
        or "supervisor" in name
        or "codex" in name
        or "model" in name
        for name in imported
        if name.startswith("research_automation_supervisor")
    )
