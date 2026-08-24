from __future__ import annotations

import ast
import hashlib
import json
import os
import runpy
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from dulwich.repo import Repo

import research_automation_supervisor.custodian_bootstrap as custodian_bootstrap_module
import research_automation_supervisor.gitless_repository as gitless_repository_module
import research_automation_supervisor.qualified_campaign as qualified_campaign_module
import research_automation_supervisor.secure_cli as secure_cli_module
from research_automation_supervisor.core_authority_models import CampaignLaunchRequestV1
from research_automation_supervisor.custodian_bootstrap import inspect_environment
from research_automation_supervisor.custodian_errors import (
    QualifiedCampaignInputError,
    QualifiedCampaignStateError,
)
from research_automation_supervisor.custodian_models import (
    CampaignProfileSettingsV1,
    FrozenInputFileV1,
)
from research_automation_supervisor.custodian_server import _pick_repository_folder
from research_automation_supervisor.durable_state import canonical_json
from research_automation_supervisor.git_evidence import record_git_baseline
from research_automation_supervisor.gitless_repository import (
    _HttpsOnlyPoolManager,
    freeze_repository_import,
    inspect_requested_repository,
)
from research_automation_supervisor.managed_codex import (
    ManagedCodexIdentity,
    prepare_managed_codex_home,
)
from research_automation_supervisor.prelaunch_authority import (
    authoritative_start_count,
    consume_start_intent_for_qualified_launch,
    create_start_intent,
    initialize_authority_store,
    resume_start_snapshot,
)
from research_automation_supervisor.qualified_campaign import _materialize_visible_authority
from tests.custodian_helpers import create_repository, git


def _request(root: Path, repository: Path) -> CampaignLaunchRequestV1:
    requested = inspect_requested_repository(
        "existing_folder", str(repository), sterile_root=root / "preview"
    )
    return CampaignLaunchRequestV1(
        preview_id="preview-" + "d" * 24,
        client_start_key_sha256=hashlib.sha256(b"transactional authority").hexdigest(),
        human_name="Transactional authority",
        repository=requested,
        research_contract=FrozenInputFileV1.from_bytes("contract.md", b"contract\n"),
        research_plan=FrozenInputFileV1.from_bytes("plan.md", b"plan\n"),
        initial_task=FrozenInputFileV1.from_bytes("task.md", b"task\n"),
        requested_settings=CampaignProfileSettingsV1(),
    )


def test_sqlite_is_the_only_start_authority_and_uses_crash_durable_pragmas(
    tmp_path: Path,
) -> None:
    authority = tmp_path / "authority"
    snapshots = tmp_path / "snapshots"
    initialize_authority_store(authority, snapshots)
    (authority / "requests" / "orphan").mkdir()
    (authority / "requests" / "orphan" / "partial.json").write_text("{}")
    (snapshots / "staging" / "orphan-building").mkdir()
    (tmp_path / "custodian-card.json").write_text('{"campaign":"forged"}')
    assert authoritative_start_count(authority) == 0

    database = authority / "authority.sqlite3"
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(starts)").fetchall()
        }
        required = {
            "start_intent_id",
            "immutable_start_request_id",
            "canonical_request_sha256",
            "campaign_public_id",
            "operator_uid",
            "repository_input_sha256",
            "contract_sha256",
            "plan_sha256",
            "task_sha256",
            "supporting_manifest_sha256",
            "settings_sha256",
            "input_bundle_sha256",
            "creation_transaction_id",
            "current_snapshot_id",
        }
        assert required <= columns
    assert database.stat().st_mode & 0o077 == 0
    assert (authority / "store-key-v1").stat().st_mode & 0o077 == 0


def test_incomplete_snapshot_substitution_never_launches(tmp_path: Path) -> None:
    repository = create_repository(tmp_path)
    request = _request(tmp_path, repository)
    authority = tmp_path / "authority"
    snapshots = tmp_path / "snapshots"

    def crash(boundary: str) -> None:
        if boundary == "during_repository_snapshot_staging":
            raise RuntimeError("snapshot crash")

    with pytest.raises(RuntimeError, match="snapshot crash"):
        create_start_intent(
            request,
            authority,
            snapshots,
            crash_injector=crash,
        )
    assert authoritative_start_count(authority) == 1
    with sqlite3.connect(authority / "authority.sqlite3") as connection:
        intent_id, campaign_id, snapshot_id = connection.execute(
            "SELECT start_intent_id, campaign_public_id, expected_snapshot_id FROM starts"
        ).fetchone()
    fake = snapshots / "complete" / str(snapshot_id)
    fake.mkdir(parents=True)
    (fake / "snapshot-v1.json").write_text("{}")
    with pytest.raises(QualifiedCampaignStateError, match="incomplete"):
        consume_start_intent_for_qualified_launch(
            authority,
            str(intent_id),
            expected_campaign_public_id=str(campaign_id),
        )
    shutil.rmtree(fake)
    resumed = resume_start_snapshot(authority, snapshots, str(intent_id))
    assert resumed.snapshot_state == "complete"
    assert resumed.snapshot_identity == snapshot_id


def test_pre_snapshot_production_path_attempts_no_process_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "HOSTILE_EXECUTION"
    repository = create_repository(tmp_path)
    (repository / ".gitattributes").write_text("* filter=arbitrary diff=arbitrary\n")
    git(repository, "add", ".gitattributes")
    git(repository, "commit", "-q", "-m", "hostile attributes")
    git(repository, "config", "filter.arbitrary.clean", f"touch {marker}")
    git(repository, "config", "filter.arbitrary.smudge", f"touch {marker}")
    git(repository, "config", "filter.arbitrary.process", f"touch {marker}")
    git(repository, "config", "core.fsmonitor", f"touch {marker}")
    git(repository, "config", "credential.helper", f"!touch {marker}")
    git(repository, "config", "alias.hostile", f"!touch {marker}")

    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("pre-snapshot path attempted process execution")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    request = _request(tmp_path / "request", repository)
    reference = create_start_intent(
        request, tmp_path / "authority", tmp_path / "snapshots"
    )
    assert reference.snapshot_identity is not None
    assert not marker.exists()


def test_pre_snapshot_bootstrap_never_executes_ambient_path_programs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hostile = tmp_path / "hostile-path"
    hostile.mkdir()
    marker = tmp_path / "AMBIENT_PROGRAM_EXECUTED"
    for name in ("codex", "bwrap", "git"):
        program = hostile / name
        program.write_text(f"#!/bin/sh\ntouch '{marker}'\n", encoding="utf-8")
        program.chmod(0o755)

    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("pre-snapshot bootstrap attempted process execution")

    monkeypatch.setattr(
        custodian_bootstrap_module, "_trusted_system_executable", lambda _path: None
    )
    monkeypatch.setattr(
        custodian_bootstrap_module, "_verified_managed_codex_identity", lambda: None
    )
    report = inspect_environment(
        tmp_path / "data",
        runner=forbidden,  # type: ignore[arg-type]
        which=lambda name: str(hostile / name),
    )
    assert not report.ready
    assert not report.codex_ready
    assert not report.codex_authenticated
    assert report.issues[0].code == "codex_unavailable"
    assert not marker.exists()


def test_fake_login_runner_cannot_authenticate_without_managed_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    codex_home = prepare_managed_codex_home(tmp_path / "data")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setattr(
        custodian_bootstrap_module, "_verified_managed_codex_identity", lambda: None
    )

    def falsely_authenticated(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("injected login result must be unreachable")

    report = inspect_environment(
        tmp_path / "data",
        runner=falsely_authenticated,  # type: ignore[arg-type]
        which=lambda _name: "/hostile/path/codex",
        allow_program_execution=True,
    )
    assert not report.ready
    assert not report.codex_ready
    assert not report.codex_authenticated
    assert any(issue.code == "codex_unavailable" for issue in report.issues)


def test_pre_snapshot_folder_picker_never_resolves_ambient_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hostile = tmp_path / "hostile-path"
    hostile.mkdir()
    marker = tmp_path / "PICKER_PROGRAM_EXECUTED"
    for name in ("zenity", "powershell.exe", "wslpath"):
        program = hostile / name
        program.write_text(f"#!/bin/sh\ntouch '{marker}'\n", encoding="utf-8")
        program.chmod(0o755)
    monkeypatch.setenv("PATH", str(hostile))
    monkeypatch.setattr(
        "research_automation_supervisor.custodian_server._trusted_system_program",
        lambda _path: None,
    )
    with pytest.raises(ValueError, match="Folder picker is unavailable"):
        _pick_repository_folder()
    assert not marker.exists()


def test_qualified_authentication_uses_fixed_codex_and_managed_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    codex_home = prepare_managed_codex_home(tmp_path / "data")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    identity = ManagedCodexIdentity(
        executable=Path("/usr/bin/codex"),
        sha256="1" * 64,
        version="0.144.0",
        release_id="test-release",
        device=1,
        inode=1,
    )
    monkeypatch.setattr(
        qualified_campaign_module,
        "_verified_managed_codex_identity",
        lambda: identity,
    )
    monkeypatch.setattr(
        qualified_campaign_module, "_managed_codex_home", lambda: codex_home
    )
    observed: dict[str, object] = {}

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        observed["command"] = command
        observed.update(kwargs)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(qualified_campaign_module.subprocess, "run", run)
    qualified_campaign_module.run_qualified_authentication()
    assert observed["command"] == ["/usr/bin/codex", "login"]
    assert observed["cwd"] == "/"
    environment = observed["env"]
    assert isinstance(environment, dict)
    assert environment["CODEX_HOME"] == str(codex_home)
    assert environment["PATH"] == "/usr/bin:/bin"
    assert qualified_campaign_module._qualified_replay_services().codex_executable == (
        "/usr/bin/codex"
    )


@pytest.mark.parametrize(
    "arguments",
    (
        ("doctor",),
        ("--", "doctor"),
        ("--", "validate-codex-request", "request.yaml"),
        ("--", "run-codex", "request.yaml"),
    ),
)
def test_installed_legacy_cli_gate_never_reaches_git_in_untrusted_repository(
    tmp_path: Path, arguments: tuple[str, ...]
) -> None:
    repository = create_repository(tmp_path)
    marker = tmp_path / "LEGACY_CLI_GIT_EXECUTED"
    git(repository, "config", "core.fsmonitor", f"!touch '{marker}'")
    completed = subprocess.run(
        [sys.executable, "-m", "research_automation_supervisor.secure_cli", *arguments],
        cwd=repository,
        check=False,
        capture_output=True,
        env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
    )
    assert completed.returncode == 4
    assert not marker.exists()


@pytest.mark.parametrize(
    "arguments",
    (
        ("validate-codex-request", "--json", "{request}"),
        ("run-codex", "--runs-dir", "runs", "--json", "{request}"),
    ),
)
def test_installed_legacy_cli_accepts_only_verified_request_workspace_layouts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    arguments: tuple[str, ...],
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    request = tmp_path / "request.yaml"
    request.write_text(f"workspace: {workspace}\n", encoding="utf-8")
    verified: list[Path] = []
    monkeypatch.setattr(
        secure_cli_module,
        "verify_operator_campaign_workspace",
        lambda path: verified.append(Path(path)),
    )
    secure_cli_module._require_signed_workspace_for_legacy_git(
        [item.format(request=request) for item in arguments]
    )
    assert verified == [workspace]


def test_installed_legacy_cli_seals_path_and_cross_uid_git_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_environment = dict(os.environ)
    hostile = tmp_path / "hostile-bin"
    hostile.mkdir()
    codex_home = prepare_managed_codex_home(tmp_path / "data")
    try:
        monkeypatch.setenv("PATH", f"{hostile}:/usr/bin:/bin")
        monkeypatch.setenv("CODEX_HOME", str(codex_home))
        monkeypatch.setattr(
            secure_cli_module, "verified_managed_codex_home", lambda: codex_home
        )
        monkeypatch.setenv("OPENAI_API_KEY", "must-not-survive")
        secure_cli_module._seal_legacy_environment()
        assert os.environ == {
            "CODEX_HOME": str(codex_home),
            "PATH": "/usr/bin:/bin",
            "HOME": "/nonexistent",
            "XDG_CONFIG_HOME": "/nonexistent",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "safe.directory",
            "GIT_CONFIG_VALUE_0": "*",
            "LANG": "C.UTF-8",
        }
    finally:
        os.environ.clear()
        os.environ.update(original_environment)


@pytest.mark.parametrize(
    "arguments",
    (
        ("doctor",),
        ("validate-codex-request", "{request}"),
        ("run-codex", "{request}"),
    ),
)
def test_signed_legacy_cli_ignores_hostile_workspace_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    arguments: tuple[str, ...],
) -> None:
    repository = create_repository(tmp_path)
    request = _request(tmp_path / "start-request", repository)
    authority = tmp_path / "authority"
    snapshots = tmp_path / "snapshots"
    reference = create_start_intent(request, authority, snapshots)
    material = consume_start_intent_for_qualified_launch(
        authority,
        reference.launch_intent_id,
        expected_campaign_public_id=reference.campaign_public_id,
    )
    workspace = Path(material.input_bundle.repository.prepared_workspace)
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Exact prompt.\n", encoding="utf-8")
    codex_request = tmp_path / "codex-request.yaml"
    codex_request.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "run_id": "signed-workspace",
                "role": "worker",
                "workspace": str(workspace),
                "prompt_path": str(prompt),
                "model": "gpt-5.6-sol",
                "reasoning_effort": "xhigh",
                "timeout_seconds": 30,
            }
        ),
        encoding="utf-8",
    )
    hostile = workspace / "hostile-bin"
    hostile.mkdir()
    marker = tmp_path / "SIGNED_WORKSPACE_PATH_EXECUTED"
    for name in ("git", "codex"):
        program = hostile / name
        program.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 1\n", encoding="utf-8")
        program.chmod(0o755)
    monkeypatch.setenv("PATH", f"{hostile}:/usr/bin:/bin")
    monkeypatch.setattr(
        gitless_repository_module, "_required_snapshot_root", lambda: snapshots
    )
    monkeypatch.chdir(workspace)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "research-supervisor",
            *(item.format(request=codex_request) for item in arguments),
        ],
    )
    original_environment = dict(os.environ)
    try:
        with pytest.raises(SystemExit):
            secure_cli_module.main()
    finally:
        os.environ.clear()
        os.environ.update(original_environment)
    assert not marker.exists()


def test_https_transport_policy_rejects_non_https_or_changed_origin() -> None:
    pool = _HttpsOnlyPoolManager("https://example.invalid/repository.git")
    with pytest.raises(QualifiedCampaignInputError, match="transport changed"):
        pool.request("GET", "http://example.invalid/repository.git/info/refs")
    with pytest.raises(QualifiedCampaignInputError, match="transport changed"):
        pool.request("GET", "https://other.invalid/repository.git/info/refs")


def test_logical_import_reuses_authority_after_source_repack(tmp_path: Path) -> None:
    repository = create_repository(tmp_path)
    requested = inspect_requested_repository(
        "existing_folder", str(repository), sterile_root=tmp_path / "preview"
    )
    imports = tmp_path / "imports"
    first = freeze_repository_import(requested, import_root=imports)
    git(repository, "gc", "--aggressive", "--prune=now")
    second = freeze_repository_import(requested, import_root=imports)
    assert second == first


def test_original_repository_path_config_refs_hooks_and_worktree_are_dead_after_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = create_repository(tmp_path)
    request = _request(tmp_path / "request", repository)
    authority = tmp_path / "authority"
    snapshots = tmp_path / "snapshots"
    reference = create_start_intent(request, authority, snapshots)
    first = consume_start_intent_for_qualified_launch(
        authority,
        reference.launch_intent_id,
        expected_campaign_public_id=reference.campaign_public_id,
    )
    identity = first.input_bundle.bundle_sha256
    workspace = Path(first.input_bundle.repository.prepared_workspace)
    original = tmp_path / "original-replaced"
    repository.rename(original)
    replacement_root = tmp_path / "replacement-root"
    replacement_root.mkdir()
    replacement = create_repository(replacement_root)
    replacement.rename(repository)
    (original / ".git/config").write_text("[alias]\n hostile = !false\n")
    (original / ".git/HEAD").write_text("ref: refs/heads/hostile\n")
    hooks = original / ".git/hooks"
    hooks.mkdir(exist_ok=True)
    (hooks / "post-checkout").write_text("#!/bin/sh\nexit 99\n")
    (original / ".gitattributes").write_text("* filter=hostile\n")
    (original / "README.md").write_text("mutated original\n")

    second = consume_start_intent_for_qualified_launch(
        authority,
        reference.launch_intent_id,
        expected_campaign_public_id=reference.campaign_public_id,
    )
    assert second.input_bundle.bundle_sha256 == identity
    assert Path(second.input_bundle.repository.prepared_workspace) == workspace
    assert (workspace / "README.md").read_text() != "mutated original\n"
    campaign_authority = tmp_path / "qualified-campaign"
    campaign_authority.mkdir()
    monkeypatch.setattr(
        gitless_repository_module, "_required_snapshot_root", lambda: snapshots
    )
    manifest = _materialize_visible_authority(
        second.input_bundle, campaign_authority
    )
    assert manifest.is_file()


def test_runtime_git_interception_observes_only_the_core_derived_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = create_repository(tmp_path)
    request = _request(tmp_path / "request", repository)
    authority = tmp_path / "authority"
    snapshots = tmp_path / "snapshots"
    observed: list[Path] = []
    real_run = subprocess.run

    def intercept(*args: object, **kwargs: object) -> subprocess.CompletedProcess[object]:
        command = args[0] if args else kwargs.get("args")
        if isinstance(command, (list, tuple)) and command:
            executable = Path(str(command[0])).name.casefold()
            if executable == "git":
                values = [str(item) for item in command]
                if "-C" in values:
                    candidate = Path(values[values.index("-C") + 1])
                else:
                    candidate = Path(str(kwargs.get("cwd", ".")))
                observed.append(candidate.resolve(strict=True))
        return real_run(*args, **kwargs)  # type: ignore[arg-type,return-value]

    monkeypatch.setattr(subprocess, "run", intercept)
    reference = create_start_intent(request, authority, snapshots)
    assert not observed
    material = consume_start_intent_for_qualified_launch(
        authority,
        reference.launch_intent_id,
        expected_campaign_public_id=reference.campaign_public_id,
    )
    workspace = Path(material.input_bundle.repository.prepared_workspace).resolve()
    campaign_authority = tmp_path / "qualified-runtime"
    campaign_authority.mkdir()
    monkeypatch.setattr(
        gitless_repository_module, "_required_snapshot_root", lambda: snapshots
    )
    _materialize_visible_authority(material.input_bundle, campaign_authority)
    record_git_baseline(workspace)
    assert observed
    assert set(observed) == {workspace}

    (repository / ".git/config").write_text("[alias]\n hostile = !false\n")
    (repository / ".gitattributes").write_text("* filter=hostile\n")
    before = tuple(observed)
    record_git_baseline(workspace)
    assert tuple(observed[: len(before)]) == before
    assert set(observed) == {workspace}


def test_unsigned_workspace_binding_mutation_cannot_authorize_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = create_repository(tmp_path)
    request = _request(tmp_path / "request", repository)
    authority = tmp_path / "authority"
    snapshots = tmp_path / "snapshots"
    reference = create_start_intent(request, authority, snapshots)
    material = consume_start_intent_for_qualified_launch(
        authority,
        reference.launch_intent_id,
        expected_campaign_public_id=reference.campaign_public_id,
    )
    workspace = Path(material.input_bundle.repository.prepared_workspace)
    binding = workspace.parent / "snapshot-binding-v1.json"
    value = json.loads(binding.read_bytes())
    value["bundle_sha256"] = "0" * 64
    binding.chmod(0o600)
    binding.write_bytes(canonical_json(value))
    binding.chmod(0o440)
    monkeypatch.setattr(
        gitless_repository_module, "_required_snapshot_root", lambda: snapshots
    )
    campaign_authority = tmp_path / "qualified-tampered"
    campaign_authority.mkdir()
    with pytest.raises(QualifiedCampaignInputError, match="binding is invalid"):
        _materialize_visible_authority(material.input_bundle, campaign_authority)


def test_https_intake_uses_dulwich_without_source_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = create_repository(tmp_path)
    head = git(source, "rev-parse", "HEAD")

    def fake_ls_remote(*_args: object, **_kwargs: object) -> object:
        return {b"HEAD": head.encode("ascii")}

    def fake_clone(
        _locator: object, target: Path, **_kwargs: object
    ) -> Repo:
        shutil.copytree(source / ".git", target)
        return Repo(str(target))

    monkeypatch.setattr(
        "research_automation_supervisor.gitless_repository._https_remote_refs",
        fake_ls_remote,
    )
    monkeypatch.setattr(
        "research_automation_supervisor.gitless_repository.porcelain.clone", fake_clone
    )
    requested = inspect_requested_repository(
        "git_url",
        "https://example.invalid/research/repository.git",
        sterile_root=tmp_path / "sterile",
    )
    imported = freeze_repository_import(requested, import_root=tmp_path / "imports")
    assert imported.source_commit == head
    config = Path(imported.object_store_path) / "config"
    assert "remote" not in config.read_text()
    assert "include" not in config.read_text()


@pytest.mark.skipif(
    os.environ.get("RAS_RUN_HTTPS_INTAKE") != "1",
    reason="explicit network qualification only",
)
def test_actual_https_repository_intake_builds_sanitized_snapshot(tmp_path: Path) -> None:
    locator = "https://github.com/octocat/Hello-World.git"
    requested = inspect_requested_repository(
        "git_url", locator, sterile_root=tmp_path / "sterile"
    )
    request = CampaignLaunchRequestV1(
        preview_id="preview-" + "f" * 24,
        client_start_key_sha256=hashlib.sha256(b"actual HTTPS intake").hexdigest(),
        human_name="HTTPS intake",
        repository=requested,
        research_contract=FrozenInputFileV1.from_bytes("contract.md", b"contract\n"),
        research_plan=FrozenInputFileV1.from_bytes("plan.md", b"plan\n"),
        initial_task=FrozenInputFileV1.from_bytes("task.md", b"task\n"),
        requested_settings=CampaignProfileSettingsV1(),
    )
    reference = create_start_intent(
        request, tmp_path / "authority", tmp_path / "snapshots"
    )
    material = consume_start_intent_for_qualified_launch(
        tmp_path / "authority",
        reference.launch_intent_id,
        expected_campaign_public_id=reference.campaign_public_id,
    )
    workspace = Path(material.input_bundle.repository.prepared_workspace)
    assert (workspace / "README").read_text().strip() == "Hello World!"
    config = (workspace / ".git/config").read_text()
    assert "remote" not in config
    assert locator not in config


def test_complete_production_git_inventory_has_no_unclassified_callsite() -> None:
    completed = subprocess.run(
        [sys.executable, "scripts/verify_pa5c4_git_inventory.py"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    report = json.loads(completed.stdout)
    assert report["ok"] is True
    assert report["categories"]["UNCLASSIFIED"] == 0
    assert report["callsite_count"] >= len(report["git_likely_callsites"])


def test_git_inventory_detects_aliases_shell_helpers_and_split_git_literals() -> None:
    inventory = runpy.run_path("scripts/verify_pa5c4_git_inventory.py")
    tree = ast.parse(
        """
import os as operating_system
import subprocess as process
from subprocess import run as execute
alias = process.Popen
def probes():
    execute(["g" + "it", "status"])
    alias(["git", "status"])
    operating_system.popen("git status")
    process.getoutput("git status")
"""
    )
    calls = inventory["_process_calls"](tree)
    assert len(calls) == 4
    capable = inventory["_git_capable_scopes"](tree)
    assert all(
        inventory["_git_likely"](
            node,
            scope,
            "probe.py",
            git_capable_scopes=capable,
        )
        for node, scope in calls
    )
