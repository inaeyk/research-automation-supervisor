from __future__ import annotations

import ast
import hashlib
import json
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest
import yaml

import research_automation_supervisor.candidate_export as candidate_export
import research_automation_supervisor.offline_evaluation_package as package_builder
import research_automation_supervisor.offline_replay_evaluator as offline_evaluator
import research_automation_supervisor.replay_campaign_sources as campaign_sources
import research_automation_supervisor.workflow_models as workflow_models
from research_automation_supervisor.errors import ReplayCampaignInputError
from research_automation_supervisor.offline_replay_evaluator import (
    OfflineEvaluationError,
    evaluate_historical_replay,
)
from research_automation_supervisor.replay_campaign_engine import (
    replay_campaign_status,
    resume_replay_campaign,
    run_replay_campaign,
)
from research_automation_supervisor.replay_campaign_sources import (
    load_replay_campaign_specification,
)
from tests.test_replay_campaign import (
    AUDITOR_ONE_UUID,
    WORKER_ONE_UUID,
    FakeSupervisor,
    campaign_services,
    create_campaign,
    supervisor_action,
)
from tests.workflow_helpers import auditor_result, codex_response, worker_result


@pytest.fixture(autouse=True)
def _allow_legacy_one_task_synthetic_packages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep this module's legacy evaluator fixtures intentionally non-production."""

    def verify_synthetic(root: Path) -> dict[str, object]:
        return package_builder.verify_evaluation_package(
            root,
            require_production=False,
        )

    monkeypatch.setattr(
        offline_evaluator,
        "verify_evaluation_package",
        verify_synthetic,
    )


def _completed_candidate(tmp_path: Path) -> tuple[Path, Path, Path]:
    manifest, fake = create_campaign(
        tmp_path,
        [
            [
                codex_response(
                    "worker",
                    WORKER_ONE_UUID,
                    worker_result(),
                    write_files={"src/ready.txt": "ready\n"},
                ),
                codex_response("auditor", AUDITOR_ONE_UUID, auditor_result()),
            ]
        ],
        test_requires_marker=True,
    )
    raw = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    stage2 = Path(raw["tasks"][0]["stage2_specification_path"])
    stage2_raw = yaml.safe_load(stage2.read_text(encoding="utf-8"))
    workspace = (stage2.parent / stage2_raw["workspace"]).resolve()
    baseline_archive = tmp_path / "baseline.tar"
    subprocess.run(
        (
            "git",
            "-C",
            str(workspace),
            "archive",
            "--format=tar",
            "-o",
            str(baseline_archive),
            "HEAD",
        ),
        check=True,
    )
    supervisor = FakeSupervisor(
        [
            supervisor_action("worker_prompt"),
            supervisor_action("auditor_prompt"),
            supervisor_action("finish"),
        ]
    )
    state = run_replay_campaign(
        manifest,
        runs_dir=tmp_path / "runs",
        services=campaign_services(fake, supervisor, []),
    )
    assert state.status == "completed"
    assert state.candidate_path is not None
    return Path(state.candidate_path), baseline_archive, Path(state.specification_path)


def _evaluation_package(
    tmp_path: Path,
    candidate: Path,
    baseline_archive: Path,
    *,
    script_content: str | None = None,
) -> Path:
    package = tmp_path / "private-evaluation"
    config_root = package / "evaluation-config"
    archives = package / "baseline-archives"
    config_root.mkdir(parents=True)
    archives.mkdir()
    evaluators = package / "evaluators"
    evaluators.mkdir()
    copied = archives / "task-1.tar"
    shutil.copyfile(baseline_archive, copied)
    script = evaluators / "functional.py"
    selected_script = script_content or (
        "from pathlib import Path\n"
        "raise SystemExit("
        "Path('src/ready.txt').read_text() != 'ready\\n')\n"
    )
    if not selected_script.endswith("\n"):
        selected_script += "\n"
    indented_script = "".join(
        f"    {line}" for line in selected_script.splitlines(keepends=True)
    )
    script.write_text(
        "import json\n"
        "try:\n"
        f"{indented_script}"
        "except SystemExit as error:\n"
        "    code = error.code if isinstance(error.code, int) else 1\n"
        "else:\n"
        "    code = 0\n"
        "passed = code == 0\n"
        "print(json.dumps({\n"
        "    'schema_version': 1,\n"
        "    'evaluation': 'functional',\n"
        "    'task_id': 'replay-task-1',\n"
        "    'passed': passed,\n"
        "    'hidden_tests_passed': passed,\n"
        "    'visible_tests_passed': passed,\n"
        "    'changed_path_match': True,\n"
        "    'hidden_runner': {'passed': passed},\n"
        "    'visible_runner': {'passed': passed},\n"
        "}, sort_keys=True))\n"
        "raise SystemExit(code)\n",
        encoding="utf-8",
    )
    candidate_record = json.loads(
        (candidate / "candidate.json").read_text(encoding="ascii")
    )
    provenance = candidate_record["tasks"][0]["source_provenance"]
    config = {
        "schema_version": 1,
        "package_id": "synthetic-private-evaluation-v1",
        "tasks": [
            {
                "task_id": "replay-task-1",
                "baseline_archive": "baseline-archives/task-1.tar",
                "baseline_archive_sha256": hashlib.sha256(
                    copied.read_bytes()
                ).hexdigest(),
                "source_commit": provenance["source_commit"],
                "source_tree": provenance["source_tree"],
                "expected_changed_paths": ["src/ready.txt"],
                "tests": [
                    {
                        "id": "functional",
                        "runner": "python_script_v1",
                        "script": "evaluators/functional.py",
                        "script_sha256": hashlib.sha256(
                            script.read_bytes()
                        ).hexdigest(),
                    }
                ],
            }
        ],
    }
    (config_root / "offline-evaluation.json").write_text(
        json.dumps(config, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    package_builder.write_evaluation_package_manifest(
        package,
        package_id="synthetic-private-evaluation-v1",
    )
    return package


def test_campaign_loads_with_only_visible_authority(tmp_path: Path) -> None:
    manifest, _fake = create_campaign(tmp_path, [[]])
    prepared = load_replay_campaign_specification(manifest)

    assert len(prepared.tasks) == 1
    assert prepared.tasks[0].specification.gold_evaluations == ()
    assert prepared.tasks[0].specification.gold_artifact_roots == ()


def test_visible_package_rejects_declared_offline_material(
    tmp_path: Path,
) -> None:
    manifest, _fake = create_campaign(tmp_path, [[]])
    (tmp_path / "hidden-tests").mkdir()

    with pytest.raises(ReplayCampaignInputError, match="offline-evaluation"):
        load_replay_campaign_specification(manifest)


def test_external_context_is_rejected_before_its_content_is_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, fake = create_campaign(tmp_path, [[]])
    external = tmp_path.parent / f"{tmp_path.name}-innocuous-context.txt"
    external.write_text("EXTERNAL-OFFLINE-SENTINEL\n", encoding="utf-8")
    raw = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    raw["tasks"][0]["project_context_paths"] = [str(external)]
    manifest.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    original = campaign_sources._read_utf8

    def guarded_read(path: Path, label: str, *, limit: int | None) -> bytes:
        if path == external:
            raise AssertionError("external context was read before rejection")
        return original(path, label, limit=limit)

    monkeypatch.setattr(campaign_sources, "_read_utf8", guarded_read)
    supervisor = FakeSupervisor([])
    with pytest.raises(ReplayCampaignInputError, match="visible campaign authority"):
        run_replay_campaign(
            manifest,
            runs_dir=tmp_path / "runs",
            services=campaign_services(fake, supervisor, []),
        )
    assert supervisor.resume_ids == []


def test_external_supervisor_policy_is_rejected_before_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, _fake = create_campaign(tmp_path, [[]])
    external = tmp_path.parent / f"{tmp_path.name}-policy.txt"
    external.write_text("EXTERNAL-SUPERVISOR-SENTINEL\n", encoding="utf-8")
    raw = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    raw["supervisor_policy_path"] = str(external)
    manifest.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    original = campaign_sources._read_utf8

    def guarded_read(path: Path, label: str, *, limit: int | None) -> bytes:
        if path == external:
            raise AssertionError("external supervisor policy was read")
        return original(path, label, limit=limit)

    monkeypatch.setattr(campaign_sources, "_read_utf8", guarded_read)
    with pytest.raises(ReplayCampaignInputError, match="visible campaign authority"):
        load_replay_campaign_specification(manifest)


@pytest.mark.parametrize(
    "locator_key",
    (
        "contract_path",
        "worker_initial_prompt_path",
        "worker_repair_prompt_path",
        "auditor_prompt_path",
    ),
)
def test_external_stage2_model_input_is_rejected_before_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    locator_key: str,
) -> None:
    manifest, _fake = create_campaign(tmp_path, [[]])
    campaign = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    stage2 = Path(campaign["tasks"][0]["stage2_specification_path"])
    external = tmp_path.parent / f"{tmp_path.name}-{locator_key}.txt"
    external.write_text("EXTERNAL-MODEL-SENTINEL\n", encoding="utf-8")
    raw = yaml.safe_load(stage2.read_text(encoding="utf-8"))
    raw[locator_key] = str(external)
    stage2.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    original = workflow_models._read_utf8_file

    def guarded_read(path: Path, label: str, *, limit: int | None) -> bytes:
        if path == external:
            raise AssertionError("external Stage 2 input was read")
        return original(path, label, limit=limit)

    monkeypatch.setattr(workflow_models, "_read_utf8_file", guarded_read)
    with pytest.raises(ReplayCampaignInputError, match="visible campaign authority"):
        load_replay_campaign_specification(manifest)


@pytest.mark.parametrize(
    "argv",
    (
        ["/bin/sh", "-c", "exit 0"],
        ["/usr/bin/python3", "-c", "raise SystemExit(0)"],
        [
            "/usr/bin/python3",
            "tools/acceptance.py",
            "--input=/private/offline-evaluation/input",
        ],
        ["/usr/bin/python3", "tools/acceptance.py", "@/tmp/private-reference"],
        ["/usr/bin/python3", "tools/acceptance.py", "@//tmp/private-reference"],
        ["/usr/bin/python3", "tools/acceptance.py", "@../private-reference"],
        ["/usr/bin/python3", "tools/acceptance.py", "--input:../private-reference"],
    ),
)
def test_visible_campaign_rejects_unregistered_or_external_acceptance_commands(
    tmp_path: Path,
    argv: list[str],
) -> None:
    manifest, fake = create_campaign(tmp_path, [[]])
    raw = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    stage2 = Path(raw["tasks"][0]["stage2_specification_path"])
    stage2_raw = yaml.safe_load(stage2.read_text(encoding="utf-8"))
    stage2_raw["acceptance_tests"][0]["argv"] = argv
    stage2.write_text(
        yaml.safe_dump(stage2_raw, sort_keys=False),
        encoding="utf-8",
    )
    supervisor = FakeSupervisor([])

    with pytest.raises(
        ReplayCampaignInputError,
        match="registered|relative visible|external authority|visible campaign authority",
    ):
        run_replay_campaign(
            manifest,
            runs_dir=tmp_path / "runs",
            services=campaign_services(fake, supervisor, []),
        )
    assert supervisor.resume_ids == []


@pytest.mark.parametrize(
    "prompt",
    (
        "Read /private/offline-evaluation/reference before editing.",
        "Read the external input at read:/tmp/private-reference.",
        "Run the check with --input=/tmp/private-reference.",
        "Read the response file @/tmp/private-reference.",
    ),
)
def test_supervisor_prompt_cannot_declare_external_or_offline_authority(
    tmp_path: Path,
    prompt: str,
) -> None:
    manifest, fake = create_campaign(
        tmp_path,
        [
            [
                codex_response("worker", WORKER_ONE_UUID, worker_result()),
            ]
        ],
    )
    supervisor = FakeSupervisor(
        [
            supervisor_action(
                "worker_prompt",
                prompt=prompt,
            ),
        ]
    )

    result = run_replay_campaign(
        manifest,
        runs_dir=tmp_path / "runs",
        services=campaign_services(fake, supervisor, []),
    )

    assert result.status == "human_paused"
    assert not (tmp_path / "task-1/fake-counter").exists()


def test_model_prompts_contain_no_offline_evaluation_locator(
    tmp_path: Path,
) -> None:
    manifest, fake = create_campaign(
        tmp_path,
        [
            [
                codex_response("worker", WORKER_ONE_UUID, worker_result()),
                codex_response("auditor", AUDITOR_ONE_UUID, auditor_result()),
            ]
        ],
    )
    supervisor = FakeSupervisor(
        [
            supervisor_action("worker_prompt"),
            supervisor_action("auditor_prompt"),
            supervisor_action("finish"),
        ]
    )
    result = run_replay_campaign(
        manifest,
        runs_dir=tmp_path / "runs",
        services=campaign_services(fake, supervisor, []),
    )

    assert result.status == "completed"
    prompts = b"\n".join(supervisor.prompts).lower()
    assert b"offline-evaluation" not in prompts
    assert b"hidden evaluator" not in prompts
    assert b"gold" not in prompts


def test_candidate_manifest_is_stable_and_candidate_is_immutable(
    tmp_path: Path,
) -> None:
    candidate, _baseline, _manifest = _completed_candidate(tmp_path)
    manifest = json.loads(
        (candidate / "candidate-manifest.json").read_text(encoding="ascii")
    )
    copied = tmp_path / "candidate-copy"
    shutil.copytree(candidate, copied, copy_function=shutil.copy2)
    copied_manifest = json.loads(
        (copied / "candidate-manifest.json").read_text(encoding="ascii")
    )

    assert copied_manifest == manifest
    assert stat_mode(candidate) == 0o500
    assert stat_mode(candidate / "candidate.json") == 0o400
    with pytest.raises(OSError):
        (candidate / "candidate.json").write_text("changed", encoding="utf-8")


def test_offline_evaluator_rejects_symlinked_candidate_manifest(
    tmp_path: Path,
) -> None:
    candidate, baseline, _manifest = _completed_candidate(tmp_path)
    package = _evaluation_package(tmp_path, candidate, baseline)
    candidate.chmod(0o700)
    manifest = candidate / "candidate-manifest.json"
    external_manifest = tmp_path / "external-candidate-manifest.json"
    manifest.rename(external_manifest)
    manifest.symlink_to(external_manifest)

    with pytest.raises(OfflineEvaluationError, match="non-symlink"):
        evaluate_historical_replay(
            candidate,
            package,
            tmp_path / "evaluation-report",
        )


def test_offline_evaluator_checks_nested_candidate_manifest_name_type(
    tmp_path: Path,
) -> None:
    candidate, _baseline, _manifest = _completed_candidate(tmp_path)
    task_root = candidate / "tasks/replay-task-1"
    candidate.chmod(0o700)
    (candidate / "tasks").chmod(0o700)
    task_root.chmod(0o700)
    nested = task_root / "candidate-manifest.json"
    nested.symlink_to("/tmp/external-manifest")
    task_root.chmod(0o500)
    (candidate / "tasks").chmod(0o500)
    candidate.chmod(0o500)

    with pytest.raises(OfflineEvaluationError, match="unsupported entry"):
        offline_evaluator._verify_candidate(candidate)


def test_candidate_rebuild_is_byte_deterministic(tmp_path: Path) -> None:
    candidate, _baseline, manifest_path = _completed_candidate(tmp_path)
    run = candidate.parent
    state = replay_campaign_status(run)
    original = (candidate / "candidate-manifest.json").read_bytes()
    saved = run / "saved-candidate"
    candidate.rename(saved)
    prepared = load_replay_campaign_specification(
        manifest_path,
        require_clean=False,
    )

    rebuilt, rebuilt_sha = candidate_export.export_visible_candidate(
        prepared,
        run,
        run_token=state.run_token,
        completed_task_ids=state.completed_task_ids,
        human_decision_count=state.human_decision_count,
        model_turn_count=state.candidate_finalized_model_turn_count or 0,
    )

    assert (rebuilt / "candidate-manifest.json").read_bytes() == original
    assert rebuilt_sha == state.candidate_manifest_sha256


def test_candidate_rebuild_uses_terminal_snapshot_not_live_workspace(
    tmp_path: Path,
) -> None:
    candidate, _baseline, manifest_path = _completed_candidate(tmp_path)
    run = candidate.parent
    state = replay_campaign_status(run)
    original = (candidate / "candidate-manifest.json").read_bytes()
    saved = run / "saved-candidate"
    candidate.rename(saved)
    prepared = load_replay_campaign_specification(
        manifest_path,
        require_clean=False,
    )
    live_changed_file = prepared.tasks[0].stage2.workspace / "src/ready.txt"
    live_changed_file.write_text("post-terminal mutation\n", encoding="utf-8")

    rebuilt, rebuilt_sha = candidate_export.export_visible_candidate(
        prepared,
        run,
        run_token=state.run_token,
        completed_task_ids=state.completed_task_ids,
        human_decision_count=state.human_decision_count,
        model_turn_count=state.candidate_finalized_model_turn_count or 0,
    )

    assert (rebuilt / "candidate-manifest.json").read_bytes() == original
    assert rebuilt_sha == state.candidate_manifest_sha256
    assert (
        rebuilt / "tasks/replay-task-1/changed-files/src/ready.txt"
    ).read_text(encoding="utf-8") == "ready\n"


@pytest.mark.parametrize(
    "checkpoint",
    (
        "after_candidate_staging_creation",
        "after_candidate_payload_write",
        "before_candidate_atomic_publish",
        "after_candidate_atomic_publish",
    ),
)
def test_candidate_publication_crash_recovers_without_another_model_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    checkpoint: str,
) -> None:
    manifest, fake = create_campaign(
        tmp_path,
        [
            [
                codex_response("worker", WORKER_ONE_UUID, worker_result()),
                codex_response("auditor", AUDITOR_ONE_UUID, auditor_result()),
            ]
        ],
    )
    supervisor = FakeSupervisor(
        [
            supervisor_action("worker_prompt"),
            supervisor_action("auditor_prompt"),
            supervisor_action("finish"),
        ]
    )
    services = campaign_services(fake, supervisor, [])
    crashed = False

    def crash(name: str) -> None:
        nonlocal crashed
        if not crashed and name == checkpoint:
            crashed = True
            raise RuntimeError(f"crash at {checkpoint}")

    monkeypatch.setattr(candidate_export, "_candidate_checkpoint", crash)
    with pytest.raises(RuntimeError, match=checkpoint):
        run_replay_campaign(
            manifest,
            runs_dir=tmp_path / "runs",
            services=services,
        )
    run = next((tmp_path / "runs").iterdir())
    interrupted = replay_campaign_status(run)
    assert interrupted.status == "running"
    assert interrupted.completed_task_ids == ("replay-task-1",)
    model_calls = len(supervisor.resume_ids)

    monkeypatch.setattr(
        candidate_export,
        "_candidate_checkpoint",
        lambda _name: None,
    )
    completed = resume_replay_campaign(run, services=services)

    assert completed.status == "completed"
    assert len(supervisor.resume_ids) == model_calls
    assert (run / "final-candidate/candidate-manifest.json").is_file()
    assert not (run / candidate_export.CANDIDATE_STAGING_NAME).exists()


@pytest.mark.parametrize(
    "checkpoint",
    (
        "after_task_input_staging_creation",
        "after_task_input_payload_write",
        "before_task_input_atomic_publish",
        "after_task_input_atomic_publish",
    ),
)
def test_terminal_candidate_input_crash_recovers_without_another_model_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    checkpoint: str,
) -> None:
    manifest, fake = create_campaign(
        tmp_path,
        [
            [
                codex_response("worker", WORKER_ONE_UUID, worker_result()),
                codex_response("auditor", AUDITOR_ONE_UUID, auditor_result()),
            ]
        ],
    )
    supervisor = FakeSupervisor(
        [
            supervisor_action("worker_prompt"),
            supervisor_action("auditor_prompt"),
            supervisor_action("finish"),
        ]
    )
    services = campaign_services(fake, supervisor, [])
    crashed = False

    def crash(name: str) -> None:
        nonlocal crashed
        if not crashed and name == checkpoint:
            crashed = True
            raise RuntimeError(f"crash at {checkpoint}")

    monkeypatch.setattr(candidate_export, "_candidate_checkpoint", crash)
    with pytest.raises(RuntimeError, match=checkpoint):
        run_replay_campaign(
            manifest,
            runs_dir=tmp_path / "runs",
            services=services,
        )
    run = next((tmp_path / "runs").iterdir())
    interrupted = replay_campaign_status(run)
    assert interrupted.status == "running"
    assert interrupted.completed_task_ids == ()
    model_calls = len(supervisor.resume_ids)

    monkeypatch.setattr(
        candidate_export,
        "_candidate_checkpoint",
        lambda _name: None,
    )
    completed = resume_replay_campaign(run, services=services)

    assert completed.status == "completed"
    assert len(supervisor.resume_ids) == model_calls
    assert (run / "final-candidate/candidate-manifest.json").is_file()
    assert not (
        run
        / "tasks/replay-task-1"
        / candidate_export.TASK_INPUT_STAGING_NAME
    ).exists()


def test_offline_evaluation_is_standalone_and_does_not_modify_campaign(
    tmp_path: Path,
) -> None:
    candidate, baseline, manifest = _completed_candidate(tmp_path)
    package = _evaluation_package(tmp_path, candidate, baseline)
    run = candidate.parent
    state = run / "state.json"
    before = hashlib.sha256(state.read_bytes()).hexdigest()

    report_path = evaluate_historical_replay(
        candidate,
        package,
        tmp_path / "evaluation-report",
    )
    second_report = evaluate_historical_replay(
        candidate,
        package,
        tmp_path / "evaluation-report-second",
    )

    report = json.loads(report_path.read_text(encoding="ascii"))
    assert report["passed"] is True
    assert report["tasks"][0]["changed_paths"] == ["src/ready.txt"]
    assert hashlib.sha256(state.read_bytes()).hexdigest() == before
    assert manifest.is_file()
    assert not (run / "offline-evaluation").exists()
    assert second_report.read_bytes() == report_path.read_bytes()
    campaign_report = json.loads(
        (run / "campaign-report.json").read_text(encoding="ascii")
    )
    assert campaign_report["offline_evaluation"] == {
        "status": "not_performed",
        "evaluation_package_status": "not_supplied",
        "commands": [],
    }


def test_offline_evaluator_import_graph_has_no_campaign_or_model_adapter() -> None:
    source = Path(offline_evaluator.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert not any(
        name.startswith("research_automation_supervisor")
        and name
        != "research_automation_supervisor.offline_evaluation_package"
        for name in imported
    )


def test_candidate_overlay_handles_upsert_delete_symlink_and_mode(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "delete.txt").write_text("remove\n", encoding="utf-8")
    task_candidate = tmp_path / "task-candidate"
    changed = task_candidate / "changed-files/bin"
    changed.mkdir(parents=True)
    payload = b"#!/bin/sh\nexit 0\n"
    (changed / "tool").write_bytes(payload)
    (task_candidate / "changes.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "entries": [
                    {
                        "path": "bin/tool",
                        "operation": "upsert",
                        "object_type": "regular",
                        "mode": 0o755,
                        "byte_length": len(payload),
                        "content_sha256": hashlib.sha256(payload).hexdigest(),
                    },
                    {
                        "path": "delete.txt",
                        "operation": "delete",
                        "object_type": "absent",
                        "mode": 0,
                        "byte_length": 0,
                        "content_sha256": hashlib.sha256(b"").hexdigest(),
                    },
                    {
                        "path": "tool-link",
                        "operation": "upsert",
                        "object_type": "symlink",
                        "mode": 0o777,
                        "byte_length": len(b"bin/tool"),
                        "content_sha256": hashlib.sha256(b"bin/tool").hexdigest(),
                        "target": "bin/tool",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    offline_evaluator._apply_candidate_changes(workspace, task_candidate)

    assert not (workspace / "delete.txt").exists()
    assert (workspace / "bin/tool").read_bytes() == payload
    assert stat_mode(workspace / "bin/tool") == 0o755
    assert (workspace / "tool-link").is_symlink()
    assert (workspace / "tool-link").readlink().as_posix() == "bin/tool"


def test_offline_evaluation_namespace_blocks_model_and_campaign_adapters(
    tmp_path: Path,
) -> None:
    candidate, baseline, _manifest = _completed_candidate(tmp_path)
    state = candidate.parent / "state.json"
    sentinel = tmp_path / "model-launched"
    fake_model = tmp_path / "codex"
    fake_model.write_text(
        f"#!/bin/sh\nprintf launched > {sentinel}\n",
        encoding="utf-8",
    )
    fake_model.chmod(0o755)
    package = _evaluation_package(
        tmp_path,
        candidate,
        baseline,
        script_content=(
            "from pathlib import Path\n"
            "import subprocess\n"
            f"state = Path({str(state)!r})\n"
            f"sentinel = Path({str(sentinel)!r})\n"
            f"fake_model = {str(fake_model)!r}\n"
            "python_resume = subprocess.run([\n"
            "    '/usr/bin/python3', '-m',\n"
            "    'research_automation_supervisor.cli',\n"
            "    'resume-visible-campaign', str(state.parent),\n"
            "], check=False).returncode\n"
            "try:\n"
            "    shell_resume = subprocess.run([\n"
            "        '/usr/bin/sh', '-c',\n"
            "        'research-supervisor resume-visible-campaign ' + str(state.parent),\n"
            "    ], check=False).returncode\n"
            "except OSError:\n"
            "    shell_resume = 127\n"
            "try:\n"
            "    import research_automation_supervisor\n"
            "    campaign_import_blocked = False\n"
            "except ModuleNotFoundError:\n"
            "    campaign_import_blocked = True\n"
            "runtime_allowlist_closed = all(not Path(path).exists() for path in (\n"
            "    '/usr/bin/sh', '/usr/bin/node', '/usr/local/bin/codex',\n"
            "    '/usr/lib/python3/dist-packages',\n"
            "))\n"
            "try:\n"
            "    subprocess.run([fake_model, 'exec'], check=False)\n"
            "    model_blocked = False\n"
            "except OSError:\n"
            "    model_blocked = True\n"
            "try:\n"
            "    state.write_text('modified')\n"
            "    state_write_blocked = False\n"
            "except OSError:\n"
            "    state_write_blocked = True\n"
            "try:\n"
            "    sentinel.write_text('launched')\n"
            "    external_write_blocked = False\n"
            "except OSError:\n"
            "    external_write_blocked = True\n"
            "raise SystemExit(not (\n"
            "    python_resume != 0 and shell_resume != 0\n"
            "    and campaign_import_blocked and runtime_allowlist_closed\n"
            "    and model_blocked\n"
            "    and state_write_blocked and external_write_blocked\n"
            "))\n"
        ),
    )

    report_path = evaluate_historical_replay(
        candidate,
        package,
        tmp_path / "evaluation-report",
    )
    report = json.loads(report_path.read_text(encoding="ascii"))
    assert report["passed"] is True
    assert not sentinel.exists()


def test_offline_evaluation_sealed_runner_allows_compiler_execution(
    tmp_path: Path,
) -> None:
    candidate, baseline, _manifest = _completed_candidate(tmp_path)
    package = _evaluation_package(
        tmp_path,
        candidate,
        baseline,
        script_content=(
            "from pathlib import Path\n"
            "import subprocess\n"
            "source = Path('/workspace/compiler-probe.cpp')\n"
            "binary = Path('/workspace/compiler-probe')\n"
            "source.write_text('int main() { return 0; }\\n')\n"
            "compiled = subprocess.run([\n"
            "    '/usr/bin/g++', '-std=c++17', str(source), '-o', str(binary),\n"
            "], check=False).returncode\n"
            "executed = subprocess.run([str(binary)], check=False).returncode\n"
            "raise SystemExit(compiled != 0 or executed != 0)\n"
        ),
    )

    report_path = evaluate_historical_replay(
        candidate,
        package,
        tmp_path / "evaluation-report",
    )

    report = json.loads(report_path.read_text(encoding="ascii"))
    assert report["passed"] is True


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("runner", "shell"),
        ("argv", ["/usr/bin/python3", "-c", "raise SystemExit(0)"]),
    ),
)
def test_offline_evaluation_rejects_unregistered_command_specification_before_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
) -> None:
    candidate, baseline, _manifest = _completed_candidate(tmp_path)
    package = _evaluation_package(tmp_path, candidate, baseline)
    config_path = package / "evaluation-config/offline-evaluation.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["tasks"][0]["tests"][0][field] = value
    config_path.write_text(
        json.dumps(config, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    package_builder.write_evaluation_package_manifest(
        package,
        package_id="synthetic-private-evaluation-v1",
    )

    def unexpected_launch(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("unregistered evaluator command was launched")

    monkeypatch.setattr(offline_evaluator.subprocess, "run", unexpected_launch)
    with pytest.raises(
        OfflineEvaluationError,
        match="fields are invalid|runner is not registered|unregistered evaluator",
    ):
        evaluate_historical_replay(
            candidate,
            package,
            tmp_path / "evaluation-report",
        )


def test_offline_evaluation_rejects_baseline_not_bound_to_candidate(
    tmp_path: Path,
) -> None:
    candidate, baseline, _manifest = _completed_candidate(tmp_path)
    package = _evaluation_package(tmp_path, candidate, baseline)
    replacement_root = tmp_path / "different-baseline"
    replacement_root.mkdir()
    (replacement_root / "different.txt").write_text("different\n", encoding="utf-8")
    replacement = package / "baseline-archives/task-1.tar"
    with tarfile.open(replacement, "w") as archive:
        archive.add(replacement_root / "different.txt", arcname="different.txt")
    config_path = package / "evaluation-config/offline-evaluation.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["tasks"][0]["baseline_archive_sha256"] = hashlib.sha256(
        replacement.read_bytes()
    ).hexdigest()
    config_path.write_text(
        json.dumps(config, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    package_builder.write_evaluation_package_manifest(
        package,
        package_id="synthetic-private-evaluation-v1",
    )

    with pytest.raises(OfflineEvaluationError, match="baseline tree"):
        evaluate_historical_replay(
            candidate,
            package,
            tmp_path / "evaluation-report",
        )


def test_offline_evaluation_rejects_source_identity_not_bound_to_candidate(
    tmp_path: Path,
) -> None:
    candidate, baseline, _manifest = _completed_candidate(tmp_path)
    package = _evaluation_package(tmp_path, candidate, baseline)
    config_path = package / "evaluation-config/offline-evaluation.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["tasks"][0]["source_commit"] = "f" * 40
    config_path.write_text(
        json.dumps(config, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    package_builder.write_evaluation_package_manifest(
        package,
        package_id="synthetic-private-evaluation-v1",
    )

    with pytest.raises(OfflineEvaluationError, match="source identity"):
        evaluate_historical_replay(
            candidate,
            package,
            tmp_path / "evaluation-report",
        )


def test_offline_baseline_archive_tree_matches_candidate_source_tree(
    tmp_path: Path,
) -> None:
    candidate, baseline, _manifest = _completed_candidate(tmp_path)
    extracted = tmp_path / "extracted-baseline"
    extracted.mkdir()
    offline_evaluator._extract_regular_archive(baseline, extracted)
    record = json.loads(
        (candidate / "candidate.json").read_text(encoding="ascii")
    )

    assert offline_evaluator._git_tree_oid(extracted) == (
        record["tasks"][0]["source_provenance"]["source_tree"]
    )
    assert record["tasks"][0]["execution_baseline_tree"] == (
        record["tasks"][0]["source_provenance"]["source_tree"]
    )


def test_offline_evaluation_output_cannot_be_inside_campaign_run(
    tmp_path: Path,
) -> None:
    candidate, baseline, _manifest = _completed_candidate(tmp_path)
    package = _evaluation_package(tmp_path, candidate, baseline)

    with pytest.raises(OfflineEvaluationError, match="separate from campaign"):
        evaluate_historical_replay(
            candidate,
            package,
            candidate.parent / "standalone-report",
        )


def test_offline_evaluation_output_rejects_symlinked_campaign_parent(
    tmp_path: Path,
) -> None:
    candidate, baseline, _manifest = _completed_candidate(tmp_path)
    package = _evaluation_package(tmp_path, candidate, baseline)
    alias = tmp_path / "campaign-alias"
    alias.symlink_to(candidate.parent, target_is_directory=True)

    with pytest.raises(OfflineEvaluationError, match="symlink|alternate path"):
        evaluate_historical_replay(
            candidate,
            package,
            alias / "standalone-report",
        )
    assert not (candidate.parent / "standalone-report").exists()


def test_cli_exposes_only_visible_campaign_command_names() -> None:
    source = (
        Path(__file__).parents[1]
        / "src/research_automation_supervisor/cli.py"
    ).read_text(encoding="utf-8")

    assert '@app.command("run-visible-campaign")' in source
    assert '@app.command("resume-visible-campaign")' in source
    assert '@app.command("visible-campaign-status")' in source
    assert '@app.command("run-replay-campaign")' not in source
    assert '@app.command("resume-replay-campaign")' not in source
    assert '@app.command("replay-campaign-status")' not in source


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777
