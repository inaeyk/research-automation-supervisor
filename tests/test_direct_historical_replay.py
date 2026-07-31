from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

import research_automation_supervisor.direct_historical_replay as direct_replay


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ("git", "-C", str(repository), *arguments),
        env={
            **os.environ,
            "GIT_AUTHOR_EMAIL": "synthetic@example.invalid",
            "GIT_AUTHOR_NAME": "Synthetic Replay",
            "GIT_COMMITTER_EMAIL": "synthetic@example.invalid",
            "GIT_COMMITTER_NAME": "Synthetic Replay",
            "LC_ALL": "C",
        },
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _synthetic_authority(
    tmp_path: Path,
    *,
    outcome: str = "passed",
) -> tuple[Path, Path]:
    task_id = "synthetic-task"
    prepared = tmp_path / "prepared"
    workspace = prepared / f"visible/tasks/{task_id}/workspace"
    workspace.mkdir(parents=True)
    (workspace / "src").mkdir()
    (workspace / "src/result.txt").write_text("baseline\n", encoding="utf-8")
    _git(workspace, "init", "-q")
    _git(workspace, "add", ".")
    _git(workspace, "commit", "-q", "-m", "synthetic baseline")
    commit = _git(workspace, "rev-parse", "HEAD")
    tree = _git(workspace, "rev-parse", "HEAD^{tree}")

    control = prepared / f"visible/tasks/{task_id}/control"
    control.mkdir(parents=True)
    (control / "stage2.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "substage_id": task_id,
                "allowed_paths": ["src/result.txt"],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    fixture = prepared / f"engine-only/gold/{task_id}"
    (fixture / "src").mkdir(parents=True)
    (fixture / "src/result.txt").write_text("accepted\n", encoding="utf-8")
    evaluator = prepared / "engine-only/evaluators/evaluate.py"
    evaluator.parent.mkdir(parents=True)
    evaluator.write_text(_evaluator_source(outcome), encoding="utf-8")
    evaluator.chmod(0o555)
    evaluations = [
        {
            "id": f"{task_id}-{mode}",
            "cwd": f"engine-only/gold/{task_id}",
            "argv": [
                sys.executable,
                str(evaluator),
                mode,
                task_id,
                str(workspace),
                str(fixture),
            ],
        }
        for mode in ("functional", "exact")
    ]
    (prepared / "campaign.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "campaign_id": "synthetic-prepared-campaign",
                "tasks": [
                    {
                        "task_id": task_id,
                        "stage2_specification_path": (
                            f"visible/tasks/{task_id}/control/stage2.yaml"
                        ),
                        "gold_artifact_roots": [f"engine-only/gold/{task_id}"],
                        "gold_evaluations": evaluations,
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (prepared / "preparation-report.json").write_bytes(
        direct_replay._json_bytes(
            {
                "schema_version": 1,
                "campaign_id": "synthetic-prepared-campaign",
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
                "tasks": [
                    {
                        "task_id": task_id,
                        "local_baseline_commit": commit,
                        "source_workspace_head": commit,
                        "source_tree": tree,
                        "target_tree": tree,
                        "functional_evaluator_proof": True,
                    }
                ],
            }
        )
    )
    source_state = prepared / "engine-only/historical-audits/source-state.json"
    source_state.parent.mkdir(parents=True)
    source_state.write_bytes(
        direct_replay._json_bytes(
            {
                "schema_version": 1,
                "repositories": [
                    {
                        "name": "synthetic-dependency",
                        "path": str(workspace),
                        "head": commit,
                        "status": [],
                    }
                ],
            }
        )
    )
    candidate = _synthetic_candidate(tmp_path / "candidate", task_id, commit, tree)
    return prepared, candidate


def _evaluator_source(outcome: str) -> str:
    if outcome == "no_structured_result":
        terminal = "print('synthetic evaluator produced no result')\nraise SystemExit(1)\n"
    elif outcome == "malformed":
        terminal = "print('{')\nraise SystemExit(1)\n"
    else:
        passed = outcome == "passed"
        terminal = (
            "passed = "
            f"{passed!r}"
            " and mode == 'functional' and path_match and content_match\n"
            "print(json.dumps({\n"
            "    'schema_version': 1,\n"
            "    'evaluation': 'functional',\n"
            "    'task_id': task_id,\n"
            "    'passed': passed,\n"
            "    'hidden_tests_passed': passed,\n"
            "    'visible_tests_passed': passed,\n"
            "    'changed_path_match': passed,\n"
            "    'hidden_runner': {'passed': passed},\n"
            "    'visible_runner': {'passed': passed},\n"
            "}, sort_keys=True))\n"
            "raise SystemExit(0 if passed else 1)\n"
        )
    return (
        "import json\n"
        "from pathlib import Path\n"
        "import shutil\n"
        "import subprocess\n"
        "import sys\n"
        "mode, task_id, workspace, fixture = sys.argv[1:]\n"
        "relative = Path('src/result.txt')\n"
        "# This overlay requires owner-write permission on a candidate file exported 0400.\n"
        "shutil.copyfile(Path(fixture, relative), Path(workspace, relative))\n"
        "status = subprocess.run(\n"
        "    ['git', '-C', workspace, 'status', '--porcelain=v1', '-z',\n"
        "     '--untracked-files=all'], capture_output=True, check=False\n"
        ")\n"
        "path_match = b'src/result.txt' in status.stdout\n"
        "content_match = (\n"
        "    Path(workspace, relative).read_bytes()\n"
        "    == Path(fixture, relative).read_bytes()\n"
        ")\n"
        + terminal
    )


def _synthetic_candidate(
    root: Path,
    task_id: str,
    commit: str,
    tree: str,
) -> Path:
    task_root = root / "tasks" / task_id
    changed = task_root / "changed-files/src/result.txt"
    changed.parent.mkdir(parents=True)
    changed.write_text("accepted\n", encoding="utf-8")
    content = changed.read_bytes()
    changes = {
        "schema_version": 1,
        "entries": [
            {
                "path": "src/result.txt",
                "operation": "upsert",
                "object_type": "regular",
                "mode": 0o400,
                "byte_length": len(content),
                "content_sha256": hashlib.sha256(content).hexdigest(),
            }
        ],
    }
    (task_root / "changes.json").write_bytes(direct_replay._json_bytes(changes))
    candidate_record = {
        "schema_version": 1,
        "campaign_id": "synthetic-visible-campaign",
        "task_order": [task_id],
        "tasks": [
            {
                "task_id": task_id,
                "source_provenance": {
                    "source_commit": commit,
                    "source_tree": tree,
                },
                "execution_baseline_tree": tree,
                "changes_sha256": direct_replay._sha256_json(changes),
            }
        ],
    }
    (root / "candidate.json").write_bytes(
        direct_replay._json_bytes(candidate_record)
    )
    for path in reversed(direct_replay._walk_objects(root)):
        if path.is_dir():
            path.chmod(0o500)
        else:
            path.chmod(0o400)
    payload = {
        "schema_version": 1,
        "snapshot_id": "synthetic-visible-campaign:candidate-v1",
        "campaign_id": "synthetic-visible-campaign",
        "entries": direct_replay._package_entries(
            root,
            excluded_manifest="candidate-manifest.json",
        ),
    }
    manifest = {
        **payload,
        "candidate_manifest_sha256": direct_replay._sha256_json(payload),
    }
    (root / "candidate-manifest.json").write_bytes(
        direct_replay._json_bytes(manifest)
    )
    (root / "candidate-manifest.json").chmod(0o400)
    root.chmod(0o500)
    return root


def _modes(root: Path) -> dict[str, int]:
    return {
        path.relative_to(root).as_posix(): stat.S_IMODE(path.lstat().st_mode)
        for path in [root, *direct_replay._walk_objects(root)]
    }


def test_direct_replay_passes_without_mutating_read_only_inputs(tmp_path: Path) -> None:
    prepared, candidate = _synthetic_authority(tmp_path)
    candidate_before = direct_replay._tree_fingerprint(candidate)
    prepared_before = direct_replay._tree_fingerprint(prepared)
    candidate_modes = _modes(candidate)
    output = tmp_path / "report"

    destination, report = direct_replay.run_direct_historical_replay(
        candidate,
        prepared,
        output,
    )

    assert destination == output
    assert report["evaluation_status"] == "passed"
    assert report["input_immutability_verified"] is True
    assert report["experimental_packaged_evaluator_used"] is False
    assert report["totals"] == {
        "tasks": 1,
        "functional_passed": 1,
        "functional_failed": 0,
        "evaluator_infrastructure_failed": 0,
        "no_structured_result": 0,
        "exact_match": "not_evaluated_by_this_command",
    }
    task = report["tasks"][0]
    assert task["hidden_acceptance_passed"] is True
    assert task["visible_acceptance_passed"] is True
    assert task["changed_path_match"] is True
    assert not (output / ".workspaces").exists()
    assert direct_replay._tree_fingerprint(candidate) == candidate_before
    assert direct_replay._tree_fingerprint(prepared) == prepared_before
    assert _modes(candidate) == candidate_modes
    assert stat.S_IMODE(
        (candidate / "tasks/synthetic-task/changed-files/src/result.txt").stat().st_mode
    ) == 0o400


def test_direct_replay_retains_only_disposable_workspace_when_requested(
    tmp_path: Path,
) -> None:
    prepared, candidate = _synthetic_authority(tmp_path)

    _destination, report = direct_replay.run_direct_historical_replay(
        candidate,
        prepared,
        tmp_path / "report",
        keep_workspaces=True,
    )

    retained = tmp_path / "report/workspaces/synthetic-task/workspace"
    assert retained.is_dir()
    assert os.access(retained / "src/result.txt", os.W_OK)
    assert report["tasks"][0]["workspace_artifact"] == (
        "workspaces/synthetic-task/workspace"
    )


def test_direct_replay_rejects_output_inside_qualified_source_repository(
    tmp_path: Path,
) -> None:
    prepared, candidate = _synthetic_authority(tmp_path)
    source_state_path = prepared / "engine-only/historical-audits/source-state.json"
    source_state = direct_replay._read_json(source_state_path, "synthetic source state")
    repository_record = source_state["repositories"][0]
    assert isinstance(repository_record, dict)
    source_repository = tmp_path / "qualified-source"
    source_repository.mkdir()
    (source_repository / "source.txt").write_text("qualified\n", encoding="utf-8")
    _git(source_repository, "init", "-q")
    _git(source_repository, "add", ".")
    _git(source_repository, "commit", "-q", "-m", "qualified source")
    repository_record["path"] = str(source_repository)
    repository_record["head"] = _git(source_repository, "rev-parse", "HEAD")
    source_state_path.write_bytes(direct_replay._json_bytes(source_state))
    output = source_repository / "replay-report"

    with pytest.raises(
        direct_replay.DirectReplayInputError,
        match="qualified source repositories",
    ):
        direct_replay.run_direct_historical_replay(
            candidate,
            prepared,
            output,
        )

    assert not output.exists()
    assert not tuple(source_repository.glob(".replay-report.staging-*"))
    assert _git(source_repository, "status", "--porcelain=v1") == ""


def test_direct_replay_distinguishes_functional_failure(tmp_path: Path) -> None:
    prepared, candidate = _synthetic_authority(tmp_path, outcome="failed")

    _destination, report = direct_replay.run_direct_historical_replay(
        candidate,
        prepared,
        tmp_path / "report",
    )

    assert report["evaluation_status"] == "functional_failure"
    assert report["tasks"][0]["status"] == "functional_failure"
    assert direct_replay.report_exit_code(report) == 1


def test_direct_replay_distinguishes_missing_structured_result(tmp_path: Path) -> None:
    prepared, candidate = _synthetic_authority(
        tmp_path,
        outcome="no_structured_result",
    )

    _destination, report = direct_replay.run_direct_historical_replay(
        candidate,
        prepared,
        tmp_path / "report",
    )

    assert report["evaluation_status"] == "no_structured_result"
    assert report["tasks"][0]["status"] == "no_structured_result"
    assert direct_replay.report_exit_code(report) == 4


def test_direct_replay_distinguishes_evaluator_infrastructure_failure(
    tmp_path: Path,
) -> None:
    prepared, candidate = _synthetic_authority(tmp_path, outcome="malformed")

    _destination, report = direct_replay.run_direct_historical_replay(
        candidate,
        prepared,
        tmp_path / "report",
    )

    assert report["evaluation_status"] == "evaluator_infrastructure_failure"
    assert report["tasks"][0]["status"] == "evaluator_infrastructure_failure"
    assert report["tasks"][0]["reason_code"] == "malformed_structured_result"
    assert direct_replay.report_exit_code(report) == 3


def test_direct_replay_cli_warns_that_models_must_be_stopped(
    tmp_path: Path,
    capsys: object,
) -> None:
    prepared, candidate = _synthetic_authority(tmp_path)

    exit_code = direct_replay.main(
        [
            "--candidate",
            str(candidate),
            "--prepared-campaign",
            str(prepared),
            "--output",
            str(tmp_path / "report"),
        ]
    )

    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert exit_code == 0
    assert "every Supervisor, Worker, Auditor, Codex" in captured.err
    assert "Status: passed" in captured.out
