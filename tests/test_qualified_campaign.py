from __future__ import annotations

import hashlib
import json
import runpy
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import research_automation_supervisor.gitless_repository as gitless_repository_module
import research_automation_supervisor.prelaunch_authority as prelaunch_authority_module
import research_automation_supervisor.qualified_campaign as qualified_campaign_module
from research_automation_supervisor.core_authority_models import CampaignLaunchRequestV1
from research_automation_supervisor.custodian_models import (
    CampaignInputBundleV1,
    CampaignProfileSettingsV1,
    FrozenInputFileV1,
    render_qualified_acceptance_runner,
)
from research_automation_supervisor.gitless_repository import inspect_requested_repository
from research_automation_supervisor.prelaunch_authority import (
    consume_start_intent_for_qualified_launch,
    create_start_intent,
)
from research_automation_supervisor.qualified_campaign import (
    BUNDLE_FILE,
    FAILURE_FILE,
    QualifiedCampaignLocatorV1,
    _materialize_visible_authority,
    _qualified_replay_services,
    qualified_campaign_status,
    resume_qualified_campaign,
)
from research_automation_supervisor.replay_campaign_sources import (
    load_replay_campaign_specification,
)
from tests.custodian_helpers import create_repository


def qualified_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    repository: Path | None = None,
) -> CampaignInputBundleV1:
    selected_repository = repository or create_repository(tmp_path)
    requested = inspect_requested_repository(
        "existing_folder", str(selected_repository), sterile_root=tmp_path / "preview"
    )
    request = CampaignLaunchRequestV1(
        preview_id="preview-" + "c" * 24,
        client_start_key_sha256=hashlib.sha256(b"qualified campaign test").hexdigest(),
        human_name="Qualified campaign",
        repository=requested,
        research_contract=FrozenInputFileV1.from_bytes(
            "contract.md", b"Do the bounded research task.\n"
        ),
        research_plan=FrozenInputFileV1.from_bytes(
            "plan.md", b"Implement and independently audit the task.\n"
        ),
        initial_task=FrozenInputFileV1.from_bytes("task.md", b"Implement the research task.\n"),
        requested_settings=CampaignProfileSettingsV1(),
    )
    reference = create_start_intent(
        request, tmp_path / "core-authority", tmp_path / "workspace"
    )
    bundle = consume_start_intent_for_qualified_launch(
        tmp_path / "core-authority",
        reference.launch_intent_id,
        expected_campaign_public_id=reference.campaign_public_id,
    ).input_bundle
    # Production pins /var/lib.  Same-process Core fixtures explicitly pin
    # their isolated snapshot root instead of weakening production discovery.
    snapshot_root = Path(bundle.repository.prepared_workspace).parents[2]
    monkeypatch.setattr(
        gitless_repository_module, "_required_snapshot_root", lambda: snapshot_root
    )
    return bundle


def test_qualified_replay_enables_process_enforcement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "codex"
    monkeypatch.setattr(
        qualified_campaign_module,
        "_verified_managed_codex_identity",
        lambda: SimpleNamespace(executable=executable),
    )
    monkeypatch.setattr(
        qualified_campaign_module,
        "_managed_codex_home",
        lambda: tmp_path / "codex-home",
    )

    services = _qualified_replay_services()

    assert services.codex_executable == str(executable)
    assert services.process_enforcement_policy is not None


def test_qualified_materialization_compiles_plain_inputs_into_existing_strict_core(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = qualified_bundle(tmp_path, monkeypatch)
    authority = tmp_path / "qualified"
    authority.mkdir()
    manifest = _materialize_visible_authority(bundle, authority)
    prepared = load_replay_campaign_specification(manifest)
    assert prepared.specification.campaign_id == bundle.campaign_public_id
    assert prepared.tasks[0].stage2.contract.content == bundle.research_contract.content_bytes()
    assert prepared.tasks[0].stage2.specification.allowed_paths == ("**",)
    assert prepared.tasks[0].stage2.acceptance_tests[0].specification.argv == (
        "/usr/bin/python3",
        ".research-supervisor/acceptance.py",
        "repository_integrity",
    )
    assert not prepared.tasks[0].stage2.specification.checkpoint_after
    control = manifest.parent / ".research-supervisor-control"
    assert manifest.stat().st_mode & 0o222 == 0
    assert all(path.stat().st_mode & 0o222 == 0 for path in control.rglob("*") if path.is_file())


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="Bubblewrap unavailable")
def test_standard_profile_runs_bare_python_tests_without_pytest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = create_repository(tmp_path)
    (repository / "src").mkdir()
    (repository / "src" / "message.py").write_text(
        'def message() -> str:\n    return "hello"\n', encoding="utf-8"
    )
    (repository / "tests").mkdir()
    (repository / "tests" / "test_message.py").write_text(
        "from src.message import message\n\n"
        "def test_message() -> None:\n"
        '    assert message() == "hello"\n\n'
        "def test_second() -> None:\n"
        "    assert 2 + 2 == 4\n",
        encoding="utf-8",
    )
    subprocess.run(
        ["git", "-C", str(repository), "add", "src/message.py", "tests/test_message.py"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repository), "commit", "-q", "-m", "add bare tests"],
        check=True,
    )
    runtime = tmp_path / "production-runtime"
    subprocess.run(
        ["/usr/bin/python3", "-m", "venv", "--without-pip", str(runtime)],
        check=True,
    )
    runtime_python = runtime / "bin" / "python"
    missing_pytest = subprocess.run(
        [str(runtime_python), "-m", "pytest", "--version"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert missing_pytest.returncode != 0
    monkeypatch.setattr(
        prelaunch_authority_module.sys, "executable", str(runtime_python)
    )

    bundle = qualified_bundle(tmp_path, monkeypatch, repository=repository)
    authority = tmp_path / "qualified"
    authority.mkdir()
    manifest = _materialize_visible_authority(bundle, authority)
    prepared = load_replay_campaign_specification(manifest)
    workspace = Path(bundle.repository.prepared_workspace)
    runner = workspace / ".research-supervisor" / "acceptance.py"

    assert prepared.tasks[0].stage2.acceptance_tests[0].specification.argv[-1] == (
        "python_bare"
    )
    assert str(runtime_python).encode("ascii") in runner.read_bytes()
    frozen_runner = subprocess.run(
        ["git", "-C", str(workspace), "show", "HEAD:.research-supervisor/acceptance.py"],
        check=True,
        capture_output=True,
    ).stdout
    assert frozen_runner == runner.read_bytes()
    completed = subprocess.run(
        ["/usr/bin/python3", str(runner), "python_bare"],
        cwd=workspace,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    assert "2 bare Python test(s) passed" in completed.stdout
    assert not tuple(workspace.rglob("__pycache__"))


def test_qualified_acceptance_runner_rejects_unsupported_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = tmp_path / "acceptance.py"
    runner.write_bytes(render_qualified_acceptance_runner(sys.executable))
    monkeypatch.setattr(sys, "argv", [str(runner), "unsupported"])

    with pytest.raises(SystemExit) as outcome:
        runpy.run_path(str(runner), run_name="__main__")

    assert outcome.value.code == 64


def test_qualified_acceptance_reuses_codex_network_namespace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot_root = tmp_path / "snapshots"
    repository = snapshot_root / "workspaces" / "campaign-nested-sandbox" / "repository"
    repository.mkdir(parents=True)
    (snapshot_root / "workspace-verification-key-v1").write_bytes(b"k" * 32)
    runner = repository / "acceptance.py"
    runner.write_bytes(render_qualified_acceptance_runner(sys.executable))
    captured: list[list[str]] = []

    def record(command: list[str], **_kwargs: object) -> SimpleNamespace:
        captured.append(command)
        return SimpleNamespace(returncode=0)

    monkeypatch.chdir(repository)
    monkeypatch.setenv("CODEX_SANDBOX_NETWORK_DISABLED", "1")
    monkeypatch.setattr(subprocess, "run", record)
    monkeypatch.setattr(sys, "argv", [str(runner), "repository_integrity"])

    with pytest.raises(SystemExit) as outcome:
        runpy.run_path(str(runner), run_name="__main__")

    assert outcome.value.code == 0
    assert captured
    assert captured[0][:6] == [
        "/usr/bin/bwrap",
        "--die-with-parent",
        "--new-session",
        "--unshare-all",
        "--share-net",
        "--proc",
    ]
    monkeypatch.delenv("CODEX_SANDBOX_NETWORK_DISABLED")

    with pytest.raises(SystemExit) as host_outcome:
        runpy.run_path(str(runner), run_name="__main__")

    assert host_outcome.value.code == 0
    assert captured[1][:5] == [
        "/usr/bin/bwrap",
        "--die-with-parent",
        "--new-session",
        "--unshare-all",
        "--proc",
    ]
    assert "--share-net" not in captured[1]


def test_permission_failure_before_visible_authority_recovers_without_duplicate_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = qualified_bundle(tmp_path, monkeypatch)
    authority = tmp_path / "qualified"
    authority.mkdir()
    bundle_path = authority / BUNDLE_FILE
    bundle_path.write_text(
        json.dumps(bundle.model_dump(mode="json"), sort_keys=True),
        encoding="utf-8",
    )
    bundle_path.chmod(0o400)
    failure_path = authority / FAILURE_FILE
    failure_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "reason_code": "PermissionError",
                "message": "Qualified campaign operation failed before verified completion.",
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    failure_path.chmod(0o600)
    launches: list[Path] = []
    monkeypatch.setattr(
        qualified_campaign_module,
        "_qualified_replay_services",
        lambda **_kwargs: object(),
    )

    def record_launch(manifest: Path, **_kwargs: object) -> None:
        launches.append(manifest)

    monkeypatch.setattr(qualified_campaign_module, "run_replay_campaign", record_launch)
    monkeypatch.setattr(
        qualified_campaign_module,
        "qualified_campaign_status",
        lambda **_kwargs: qualified_campaign_module._preparing_projection(bundle),
    )

    projection = resume_qualified_campaign(
        authority_directory=authority,
        exchange_root=tmp_path / "exchange",
    )

    visible = Path(bundle.repository.prepared_workspace).parent
    assert projection.status == "preparing"
    assert launches == [visible / "campaign.json"]
    assert (authority / "qualified-locator-v1.json").is_file()
    assert not failure_path.exists()


def test_operator_status_cannot_fabricate_completed_from_unverified_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = qualified_bundle(tmp_path, monkeypatch)
    authority = tmp_path / "qualified"
    authority.mkdir()
    (authority / BUNDLE_FILE).write_text(
        json.dumps(bundle.model_dump(mode="json"), sort_keys=True),
        encoding="utf-8",
    )
    run = authority / "runs" / f"{bundle.campaign_public_id}-{bundle.bundle_sha256[:32]}"
    run.mkdir(parents=True)
    locator = QualifiedCampaignLocatorV1(
        campaign_public_id=bundle.campaign_public_id,
        bundle_sha256=bundle.bundle_sha256,
        visible_manifest=str(tmp_path / "missing-manifest.json"),
        core_run_directory=str(run),
        prepared_workspace=bundle.repository.prepared_workspace,
    )
    (authority / "qualified-locator-v1.json").write_text(
        json.dumps(locator.model_dump(mode="json"), sort_keys=True),
        encoding="utf-8",
    )
    (run / "state.json").write_text(json.dumps({"status": "completed"}), encoding="utf-8")
    with pytest.raises(Exception, match="locator was substituted"):
        qualified_campaign_status(
            authority_directory=authority,
            exchange_root=tmp_path / "exchange",
        )


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="Bubblewrap unavailable")
def test_repository_acceptance_cannot_escape_workspace(tmp_path: Path) -> None:
    snapshot_root = tmp_path / "snapshots"
    campaign = snapshot_root / "workspaces" / "campaign-sandbox-test"
    repository = campaign / "repository"
    repository.mkdir(parents=True)
    (snapshot_root / "workspace-verification-key-v1").write_bytes(b"k" * 32)
    outside = campaign / "outside-campaign-authority.txt"
    (repository / "test_escape.py").write_text(
        "from pathlib import Path\n"
        "def test_escape():\n"
        "    try:\n"
        "        (Path.cwd().parent / 'outside-campaign-authority.txt').write_text('mutated')\n"
        "    except OSError:\n"
        "        pass\n",
        encoding="utf-8",
    )
    runner = repository / "acceptance.py"
    runner.write_bytes(render_qualified_acceptance_runner(sys.executable))
    completed = subprocess.run(
        [sys.executable, str(runner), "python_pytest"],
        cwd=repository,
        check=False,
        timeout=60,
    )
    assert completed.returncode == 0
    assert not outside.exists()
