from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

import research_automation_supervisor.custodian_bootstrap as custodian_bootstrap_module
import research_automation_supervisor.managed_codex as managed_codex_module
import research_automation_supervisor.managed_codex_installer as managed_codex_installer_module
import research_automation_supervisor.physics_auditor_execution as physics_execution_module
import research_automation_supervisor.qualified_campaign as qualified_campaign_module
import research_automation_supervisor.qualified_runner as qualified_runner_module
from research_automation_supervisor.custodian_errors import (
    CustodianEnvironmentError,
    QualifiedCampaignInputError,
)
from research_automation_supervisor.errors import (
    PhysicsAuditorDependencyError,
    PhysicsAuditorInputError,
)
from research_automation_supervisor.managed_codex import (
    MANAGED_CODEX_HOME_BINDING,
    ManagedCodexContract,
    ManagedCodexHomeAuthority,
    ManagedCodexHomeAuthorityContract,
    ManagedCodexSecurityError,
    initialize_managed_codex_home,
    render_managed_codex_home_authority,
    verified_managed_codex_home,
    verify_managed_codex_installation,
)
from research_automation_supervisor.managed_codex_installer import (
    ARTIFACT_RELATIVE_PATH,
    ManagedCodexInstallerLayout,
    _qualification_installer_layout,
    install_managed_codex,
)
from research_automation_supervisor.physics_auditor_execution import (
    resolve_qualified_physics_auditor_codex,
)
from research_automation_supervisor.physics_auditor_models import (
    PhysicsAuditorExecutionConfigV1,
    load_physics_auditor_execution_config,
)
from research_automation_supervisor.protected_release import (
    PROTECTED_RELEASE_APPROVAL,
    PROTECTED_RELEASE_CANDIDATE,
    PROTECTED_RELEASE_INSTALLER,
    PROTECTED_RELEASE_RECEIPT,
    PROTECTED_RELEASE_ROOT,
    ProtectedReleaseLayout,
    ProtectedReleaseSecurityError,
    install_approved_release,
    verify_installed_release,
)


def _mkdir(path: Path, mode: int = 0o755) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(mode)
    return path


def _write(path: Path, content: bytes, mode: int) -> None:
    path.write_bytes(content)
    path.chmod(mode)


def _approval(
    artifact: bytes,
    *,
    release_id: str = "codex-test-v1",
    version: str = "0.144.0",
    update_from: str | None = None,
) -> bytes:
    return (
        json.dumps(
            {
                "schema_version": 1,
                "release_id": release_id,
                "artifact": str(ARTIFACT_RELATIVE_PATH),
                "sha256": hashlib.sha256(artifact).hexdigest(),
                "version": version,
                "update_from_sha256": update_from,
            },
            sort_keys=True,
        )
        + "\n"
    ).encode("ascii")


def _installer_layout(
    tmp_path: Path,
    artifact: bytes,
    *,
    release_id: str = "codex-test-v1",
    version: str = "0.144.0",
    update_from: str | None = None,
) -> ManagedCodexInstallerLayout:
    tmp_path.mkdir(parents=True, exist_ok=True)
    tmp_path.chmod(0o700)
    release = _mkdir(tmp_path / "release")
    artifact_path = _mkdir(release / "artifacts") / "codex"
    approval_path = release / "managed-codex-approval-v1.json"
    _write(artifact_path, artifact, 0o755)
    _write(
        approval_path,
        _approval(
            artifact,
            release_id=release_id,
            version=version,
            update_from=update_from,
        ),
        0o644,
    )

    system = _mkdir(tmp_path / "system", 0o700)
    executable = _mkdir(system / "usr/bin") / "codex"
    receipt_root = _mkdir(system / "etc/research-supervisor-core")
    pending = receipt_root / "managed-codex-install-pending-v1.json"
    contract = ManagedCodexContract(
        executable=executable,
        receipt=receipt_root / "managed-codex-install-v1.json",
        pending_receipt=pending,
        executable_trust_root=system,
        receipt_trust_root=system,
        authority_uid=os.getuid(),
        authority_gid=os.getgid(),
    )
    return ManagedCodexInstallerLayout(
        release_root=release,
        release_trust_root=tmp_path,
        approval=approval_path,
        artifact=artifact_path,
        installation=contract,
        pending_receipt=pending,
        authority_uid=os.getuid(),
        authority_gid=os.getgid(),
    )


def _replace_approval(
    layout: ManagedCodexInstallerLayout,
    artifact: bytes,
    *,
    release_id: str,
    version: str,
    update_from: str | None,
) -> None:
    _write(layout.artifact, artifact, 0o755)
    _write(
        layout.approval,
        _approval(
            artifact,
            release_id=release_id,
            version=version,
            update_from=update_from,
        ),
        0o644,
    )


def _home_contract(tmp_path: Path) -> ManagedCodexHomeAuthorityContract:
    tmp_path.mkdir(parents=True, exist_ok=True)
    tmp_path.chmod(0o700)
    system = _mkdir(tmp_path / "system", 0o700)
    receipt_root = _mkdir(system / "etc/research-supervisor-core")
    operator_home = _mkdir(tmp_path / "operator-home", 0o700)
    data_root = operator_home / ".local/share/research-automation-supervisor"
    authority = ManagedCodexHomeAuthority(
        operator_uid=os.getuid(),
        data_root=data_root,
        codex_home=data_root / "codex-home",
    )
    receipt = receipt_root / "managed-codex-home-v1.json"
    _write(receipt, render_managed_codex_home_authority(authority), 0o644)
    return ManagedCodexHomeAuthorityContract(
        receipt=receipt,
        receipt_trust_root=system,
        authority_uid=os.getuid(),
        authority_gid=os.getgid(),
        operator_uid=os.getuid(),
        expected_data_root=data_root,
        data_trust_root=operator_home,
    )


def _protected_release_layout(tmp_path: Path) -> ProtectedReleaseLayout:
    tmp_path.mkdir(parents=True, exist_ok=True)
    tmp_path.chmod(0o700)
    authority_root = _mkdir(tmp_path / "distribution-authority", 0o700)
    authority = _mkdir(authority_root / "bin") / "install-protected-release"
    _write(authority, b"distribution-provisioned-helper\n", 0o755)
    approval_root = _mkdir(tmp_path / "approval-authority", 0o700)
    candidate = _mkdir(tmp_path / "candidate", 0o700)
    release_parent = _mkdir(tmp_path / "protected-destination", 0o700)
    receipt_root = _mkdir(tmp_path / "protected-state", 0o700)
    codex_artifact = Path("/usr/bin/python3").resolve(strict=True).read_bytes()
    protected_python_version = (
        f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    )
    managed_approval = _approval(
        codex_artifact,
        release_id="codex-protected-v1",
        version=protected_python_version,
    )
    release_sources = (
        "scripts/install-research-supervisor.sh",
        "scripts/install-managed-codex.sh",
        "scripts/install-core-authority-service.sh",
        "scripts/run-protected-python.sh",
        "scripts/protected-managed-codex-entry.py",
        "src/research_automation_supervisor/__init__.py",
        "src/research_automation_supervisor/errors.py",
        "src/research_automation_supervisor/custodian_errors.py",
        "src/research_automation_supervisor/doctor.py",
        "src/research_automation_supervisor/managed_codex.py",
        "src/research_automation_supervisor/managed_codex_installer.py",
    )
    approved_files = {
        relative: (
            Path(relative).read_bytes(),
            0o755 if relative.startswith("scripts/") else 0o644,
        )
        for relative in release_sources
    }
    approved_files.update(
        {
            "artifacts/codex": (codex_artifact, 0o755),
            "managed-codex-approval-v1.json": (managed_approval, 0o644),
        }
    )
    manifest_files: list[dict[str, str]] = []
    for relative, (content, mode) in approved_files.items():
        target = candidate / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        _write(target, content, mode)
        manifest_files.append(
            {
                "path": relative,
                "sha256": hashlib.sha256(content).hexdigest(),
                "mode": f"{mode:04o}",
            }
        )
    approval = approval_root / "approved-release-v1.json"
    _write(
        approval,
        (
            json.dumps(
                {
                    "schema_version": 1,
                    "release_id": "ras-test-release-v1",
                    "files": manifest_files,
                },
                sort_keys=True,
            )
            + "\n"
        ).encode("ascii"),
        0o644,
    )
    return ProtectedReleaseLayout(
        authority_executable=authority,
        approval=approval,
        candidate_root=candidate,
        release_root=release_parent / "research-supervisor-release",
        receipt=receipt_root / "installed-release-v1.json",
        authority_trust_root=tmp_path,
        approval_trust_root=tmp_path,
        release_trust_root=tmp_path,
        receipt_trust_root=tmp_path,
        authority_uid=os.getuid(),
        authority_gid=os.getgid(),
    )


def _write_import_marker(path: Path, marker: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"with open({str(marker)!r}, 'w', encoding='utf-8') as handle:\n"
        f"    handle.write({text!r})\n",
        encoding="utf-8",
    )


def _invoke_protected_installer_main(
    layout: ProtectedReleaseLayout,
    *,
    payload: str,
    cwd: Path,
    environment: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "/bin/sh",
            str(layout.release_root / "scripts" / payload),
            "--qualification-import-probe",
        ],
        cwd=cwd,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _parse_protected_installer_main(
    completed: subprocess.CompletedProcess[str],
) -> tuple[dict[str, object], dict[str, object]]:
    assert completed.returncode == 0, completed.stderr
    lines = completed.stdout.splitlines()
    assert len(lines) == 2
    import_evidence = json.loads(lines[0])
    installer_result = json.loads(lines[1])
    assert isinstance(import_evidence, dict)
    assert isinstance(installer_result, dict)
    return import_evidence, installer_result


def _physics_config(
    *,
    executable: Path | None = None,
    digest: str | None = None,
) -> PhysicsAuditorExecutionConfigV1:
    base = load_physics_auditor_execution_config(
        Path("examples/physics_auditor/synthetic/execution-config.yaml")
    ).model_dump(mode="json")
    if executable is not None:
        base["trusted_executable"] = {
            "path": str(executable),
            "sha256": digest or ("0" * 64),
        }
    return PhysicsAuditorExecutionConfigV1.model_validate(base)


def _managed_layout_from_protected_release(
    release_layout: ProtectedReleaseLayout,
) -> ManagedCodexInstallerLayout:
    layout = _qualification_installer_layout(release_layout.release_root)
    state_root = _mkdir(layout.installation.executable_trust_root, 0o700)
    _mkdir(state_root / "system", 0o700)
    _mkdir(layout.installation.executable.parent)
    _mkdir(layout.installation.receipt.parent)
    return layout


def test_simulated_fresh_install_runtime_verification_and_identical_reinstall(
    tmp_path: Path,
) -> None:
    artifact = b"\x7fELF-qualified-codex-v1"
    layout = _installer_layout(tmp_path, artifact)

    first = install_managed_codex(layout, version_probe=lambda _path: "0.144.0")
    verified = verify_managed_codex_installation(layout.installation)
    second = install_managed_codex(layout, version_probe=lambda _path: "0.144.0")

    assert first.disposition == "installed"
    assert second.disposition == "unchanged"
    assert first.identity == verified == second.identity
    assert verified.sha256 == hashlib.sha256(artifact).hexdigest()
    assert stat.S_IMODE(layout.installation.executable.stat().st_mode) == 0o755
    assert stat.S_IMODE(layout.installation.receipt.stat().st_mode) == 0o644
    assert not layout.pending_receipt.exists()


def test_root_owned_looking_substitution_and_receipt_failures_are_rejected(
    tmp_path: Path,
) -> None:
    layout = _installer_layout(tmp_path, b"\x7fELF-qualified-codex-v1")
    install_managed_codex(layout, version_probe=lambda _path: "0.144.0")
    _write(layout.installation.executable, b"\x7fELF-substitute", 0o755)
    with pytest.raises(ManagedCodexSecurityError, match="does not match"):
        verify_managed_codex_installation(layout.installation)

    layout.installation.executable.unlink()
    _write(layout.installation.executable, b"\x7fELF-qualified-codex-v1", 0o755)
    layout.installation.receipt.write_text("not-json\n", encoding="ascii")
    layout.installation.receipt.chmod(0o644)
    with pytest.raises(ManagedCodexSecurityError, match="malformed"):
        verify_managed_codex_installation(layout.installation)

    layout.installation.receipt.unlink()
    with pytest.raises(ManagedCodexSecurityError, match="unavailable"):
        verify_managed_codex_installation(layout.installation)


def test_unsafe_or_multiply_linked_install_receipt_is_rejected(tmp_path: Path) -> None:
    layout = _installer_layout(tmp_path, b"\x7fELF-qualified-codex-v1")
    install_managed_codex(layout, version_probe=lambda _path: "0.144.0")
    receipt = layout.installation.receipt
    receipt.chmod(0o666)
    with pytest.raises(ManagedCodexSecurityError, match="metadata"):
        verify_managed_codex_installation(layout.installation)
    receipt.chmod(0o644)
    os.link(receipt, receipt.with_name("receipt-hardlink"))
    with pytest.raises(ManagedCodexSecurityError, match="metadata"):
        verify_managed_codex_installation(layout.installation)


def test_distinct_identity_requires_explicit_protected_update_authority(
    tmp_path: Path,
) -> None:
    first_bytes = b"\x7fELF-qualified-codex-v1"
    second_bytes = b"\x7fELF-qualified-codex-v2"
    layout = _installer_layout(tmp_path, first_bytes)
    first = install_managed_codex(layout, version_probe=lambda _path: "0.144.0")

    _replace_approval(
        layout,
        second_bytes,
        release_id="codex-test-v2",
        version="0.145.0",
        update_from=None,
    )
    with pytest.raises(ManagedCodexSecurityError, match="update authority"):
        install_managed_codex(layout, version_probe=lambda _path: "0.145.0")

    _replace_approval(
        layout,
        second_bytes,
        release_id="codex-test-v2",
        version="0.145.0",
        update_from=first.identity.sha256,
    )
    updated = install_managed_codex(layout, version_probe=lambda _path: "0.145.0")

    assert updated.disposition == "updated"
    assert updated.identity.sha256 == hashlib.sha256(second_bytes).hexdigest()
    assert updated.identity.version == "0.145.0"


def test_unsafe_release_destination_and_interrupted_install_fail_closed(
    tmp_path: Path,
) -> None:
    layout = _installer_layout(tmp_path, b"\x7fELF-qualified-codex-v1")
    target = layout.installation.executable
    target.symlink_to("/bin/false")
    with pytest.raises(ManagedCodexSecurityError, match="incomplete generation"):
        install_managed_codex(layout, version_probe=lambda _path: "0.144.0")
    target.unlink()

    layout.artifact.unlink()
    layout.artifact.symlink_to("/bin/false")
    with pytest.raises(ManagedCodexSecurityError, match="mutable or linked"):
        install_managed_codex(layout, version_probe=lambda _path: "0.144.0")
    layout.artifact.unlink()
    _write(layout.artifact, b"\x7fELF-qualified-codex-v1", 0o755)

    def interrupt(phase: str) -> None:
        if phase == "executable_replaced":
            raise RuntimeError("simulated interruption")

    with pytest.raises(RuntimeError, match="simulated interruption"):
        install_managed_codex(
            layout,
            version_probe=lambda _path: "0.144.0",
            fault_injector=interrupt,
        )
    assert layout.pending_receipt.exists()
    with pytest.raises(ManagedCodexSecurityError, match="incomplete"):
        verify_managed_codex_installation(layout.installation)
    with pytest.raises(ManagedCodexSecurityError, match="explicit administrator recovery"):
        install_managed_codex(layout, version_probe=lambda _path: "0.144.0")


def test_mutable_release_ancestor_is_not_approval_authority(tmp_path: Path) -> None:
    layout = _installer_layout(tmp_path, b"\x7fELF-qualified-codex-v1")
    layout.release_root.chmod(0o777)
    with pytest.raises(ManagedCodexSecurityError, match="metadata"):
        install_managed_codex(layout, version_probe=lambda _path: "0.144.0")


def test_protected_distribution_boundary_installs_only_exact_approved_data(
    tmp_path: Path,
) -> None:
    layout = _protected_release_layout(tmp_path)

    installed = install_approved_release(layout)
    verified = verify_installed_release(layout)
    repeated = install_approved_release(layout)

    assert installed.disposition == "installed"
    assert verified.release_id == repeated.release_id == "ras-test-release-v1"
    assert repeated.disposition == "unchanged"
    assert installed.manifest_sha256 == verified.manifest_sha256
    assert verified.release_root == layout.release_root
    assert stat.S_IMODE(layout.release_root.stat().st_mode) == 0o755
    assert stat.S_IMODE(layout.receipt.stat().st_mode) == 0o644


def test_actual_protected_shell_to_python_boundary_ignores_hostile_cwd_and_environment(
    tmp_path: Path,
) -> None:
    layout = _protected_release_layout(tmp_path / "release")
    install_approved_release(layout)
    verify_installed_release(layout)
    managed_layout = _managed_layout_from_protected_release(layout)

    markers = {
        name: tmp_path / f"{name}.marker"
        for name in (
            "cwd_package",
            "cwd_transitive",
            "path_package",
            "path_transitive",
            "user_site",
            "startup",
            "path_python",
        )
    }
    hostile_cwd = _mkdir(tmp_path / "hostile-cwd", 0o700)
    _write_import_marker(
        hostile_cwd / "research_automation_supervisor/__init__.py",
        markers["cwd_package"],
        "hostile cwd package imported",
    )
    _write_import_marker(
        hostile_cwd / "research_automation_supervisor/managed_codex_installer.py",
        markers["cwd_package"],
        "hostile cwd installer imported",
    )
    _write_import_marker(
        hostile_cwd / "hashlib.py",
        markers["cwd_transitive"],
        "hostile cwd transitive imported",
    )

    pythonpath_root = _mkdir(tmp_path / "pythonpath-shadow", 0o700)
    _write_import_marker(
        pythonpath_root / "research_automation_supervisor/__init__.py",
        markers["path_package"],
        "hostile PYTHONPATH package imported",
    )
    _write_import_marker(
        pythonpath_root / "hashlib.py",
        markers["path_transitive"],
        "hostile PYTHONPATH transitive imported",
    )
    user_site = (
        tmp_path
        / "userbase/lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    user_site.mkdir(parents=True)
    (user_site / "hostile-user-site.pth").write_text(
        "import hostile_user_site_marker\n", encoding="utf-8"
    )
    _write_import_marker(
        user_site / "hostile_user_site_marker.py",
        markers["user_site"],
        "hostile user site imported",
    )
    startup = tmp_path / "python-startup.py"
    _write_import_marker(startup, markers["startup"], "hostile startup imported")
    fake_bin = _mkdir(tmp_path / "operator-bin", 0o700)
    fake_python = fake_bin / "python3"
    fake_python.write_text(
        "#!/bin/sh\n"
        f"printf selected > {markers['path_python']}\n"
        "exit 91\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)

    hostile_environment = {
        "BASH_ENV": str(startup),
        "ENV": str(startup),
        "HOME": str(tmp_path / "hostile-home"),
        "LANG": "operator-controlled",
        "LC_ALL": "operator-controlled",
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "PYTHONBREAKPOINT": "hostile.breakpoint",
        "PYTHONHOME": str(tmp_path / "missing-python-home"),
        "PYTHONINSPECT": "1",
        "PYTHONPATH": str(pythonpath_root),
        "PYTHONPYCACHEPREFIX": str(tmp_path / "hostile-pycache"),
        "PYTHONSTARTUP": str(startup),
        "PYTHONUSERBASE": str(tmp_path / "userbase"),
        "PYTHONWARNINGS": "error",
        "XDG_CONFIG_HOME": str(tmp_path / "hostile-config"),
    }
    normal_cwd = _mkdir(tmp_path / "normal-cwd", 0o700)
    normal_environment = {"LANG": "C.UTF-8", "PATH": "/usr/bin:/bin"}
    expected_installer = str(
        layout.release_root
        / "src/research_automation_supervisor/managed_codex_installer.py"
    )
    expected_transitive = str(
        layout.release_root / "src/research_automation_supervisor/managed_codex.py"
    )

    installer_results: list[dict[str, object]] = []
    for payload in (
        "install-managed-codex.sh",
        "install-core-authority-service.sh",
    ):
        hostile, hostile_result = _parse_protected_installer_main(
            _invoke_protected_installer_main(
                layout,
                payload=payload,
                cwd=hostile_cwd,
                environment=hostile_environment,
            )
        )
        normal, normal_result = _parse_protected_installer_main(
            _invoke_protected_installer_main(
                layout,
                payload=payload,
                cwd=normal_cwd,
                environment=normal_environment,
            )
        )
        assert hostile["managed_codex_installer"] == expected_installer
        assert hostile["managed_codex"] == expected_transitive
        assert hostile["managed_codex_installer"] == normal["managed_codex_installer"]
        assert hostile["managed_codex"] == normal["managed_codex"]
        assert hostile["cwd"] == normal["cwd"] == str(layout.release_root)
        assert hostile["sys_path"] == normal["sys_path"]
        assert hostile["environment"] == normal["environment"] == [
            "LANG",
            "LC_ALL",
            "PATH",
            "RAS_PROTECTED_IMPORT_QUALIFICATION",
        ]
        assert hostile["isolated"] is True
        assert hostile["safe_path"] is True
        assert hostile["user_site_disabled"] is True
        assert hostile_result["operation"] == normal_result["operation"] == "install"
        installer_results.extend((hostile_result, normal_result))

    installed = verify_managed_codex_installation(managed_layout.installation)
    protected_receipt = json.loads(
        managed_layout.installation.receipt.read_text(encoding="ascii")
    )
    assert installer_results[0]["disposition"] == "installed"
    assert all(
        result["disposition"] == "unchanged" for result in installer_results[1:]
    )
    for result in installer_results:
        identity = result["identity"]
        assert isinstance(identity, dict)
        assert identity["executable"] == str(installed.executable)
        assert identity["sha256"] == installed.sha256
        assert identity["version"] == installed.version
        assert identity["release_id"] == installed.release_id
        assert result["protected_receipt"] == protected_receipt
    assert all(not marker.exists() for marker in markers.values())


def test_protected_shell_python_boundary_fails_closed_for_missing_or_unsafe_authority(
    tmp_path: Path,
) -> None:
    layout = _protected_release_layout(tmp_path / "release")
    install_approved_release(layout)
    normal_cwd = _mkdir(tmp_path / "normal-cwd", 0o700)
    base_environment = {"LANG": "C.UTF-8", "PATH": "/usr/bin:/bin"}

    missing_interpreter_environment = {
        **base_environment,
        "RAS_PROTECTED_PYTHON_TEST_ONLY_EXECUTABLE": str(
            tmp_path / "missing-python"
        ),
    }
    missing_interpreter = _invoke_protected_installer_main(
        layout,
        payload="install-managed-codex.sh",
        cwd=normal_cwd,
        environment=missing_interpreter_environment,
    )
    assert missing_interpreter.returncode == 2
    assert "interpreter is missing" in missing_interpreter.stderr

    fake_python = tmp_path / "operator-python3.14"
    fake_python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_python.chmod(0o755)
    unsafe_interpreter = _invoke_protected_installer_main(
        layout,
        payload="install-managed-codex.sh",
        cwd=normal_cwd,
        environment={
            **base_environment,
            "RAS_PROTECTED_PYTHON_TEST_ONLY_EXECUTABLE": str(fake_python),
        },
    )
    assert unsafe_interpreter.returncode == 2
    assert "outside the fixed system contract" in unsafe_interpreter.stderr

    source_root = layout.release_root / "src"
    source_root.chmod(0o777)
    unsafe_source = _invoke_protected_installer_main(
        layout,
        payload="install-core-authority-service.sh",
        cwd=normal_cwd,
        environment=base_environment,
    )
    assert unsafe_source.returncode == 2
    assert "release ancestry is missing or unsafe" in unsafe_source.stderr
    source_root.chmod(0o755)

    installer_module = (
        source_root / "research_automation_supervisor/managed_codex_installer.py"
    )
    installer_module.unlink()
    missing_module = _invoke_protected_installer_main(
        layout,
        payload="install-managed-codex.sh",
        cwd=normal_cwd,
        environment=base_environment,
    )
    assert missing_module.returncode != 0
    assert "managed_codex_installer" in missing_module.stderr


def test_protected_shell_python_boundary_fails_closed_for_missing_entrypoint(
    tmp_path: Path,
) -> None:
    layout = _protected_release_layout(tmp_path / "release")
    install_approved_release(layout)
    managed_layout = _managed_layout_from_protected_release(layout)
    entrypoint = layout.release_root / "scripts/protected-managed-codex-entry.py"
    entrypoint.unlink()

    marker = tmp_path / "fallback.marker"
    hostile_cwd = _mkdir(tmp_path / "hostile-cwd", 0o700)
    _write_import_marker(
        hostile_cwd / "protected-managed-codex-entry.py",
        marker,
        "caller CWD entrypoint selected",
    )
    pythonpath_root = _mkdir(tmp_path / "pythonpath-shadow", 0o700)
    _write_import_marker(
        pythonpath_root / "research_automation_supervisor/managed_codex_installer.py",
        marker,
        "caller PYTHONPATH installer selected",
    )
    fake_bin = _mkdir(tmp_path / "operator-bin", 0o700)
    fake_python = fake_bin / "python3"
    fake_python.write_text(
        "#!/bin/sh\n"
        f"printf selected > {marker}\n"
        "exit 91\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)

    completed = _invoke_protected_installer_main(
        layout,
        payload="install-managed-codex.sh",
        cwd=hostile_cwd,
        environment={
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "PYTHONHOME": str(tmp_path / "missing-python-home"),
            "PYTHONPATH": str(pythonpath_root),
        },
    )

    assert completed.returncode == 2
    assert "application entrypoint is missing or unsafe" in completed.stderr
    assert not completed.stdout
    assert not marker.exists()
    assert not managed_layout.installation.executable.exists()
    assert not managed_layout.installation.receipt.exists()
    assert not managed_layout.pending_receipt.exists()


def test_qualification_installer_backend_is_unavailable_under_privilege(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(managed_codex_installer_module.os, "geteuid", lambda: 0)

    returncode = managed_codex_installer_module.main(
        ["install"], _qualification_release_root=tmp_path
    )

    captured = capsys.readouterr()
    assert returncode == 2
    assert "qualification installer backend is unavailable under privilege" in (
        captured.err
    )
    assert not (tmp_path / ".managed-codex-main-qualification").exists()


def test_candidate_substitution_and_protected_receipt_substitution_fail_closed(
    tmp_path: Path,
) -> None:
    substituted = _protected_release_layout(tmp_path / "candidate-case")
    candidate_installer = (
        substituted.candidate_root / "scripts/install-research-supervisor.sh"
    )
    _write(candidate_installer, b"#!/bin/sh\noperator-substitute\n", 0o755)
    with pytest.raises(ProtectedReleaseSecurityError, match="not externally approved"):
        install_approved_release(substituted)
    assert not substituted.release_root.exists()
    assert not substituted.receipt.exists()

    receipt_case = _protected_release_layout(tmp_path / "receipt-case")
    install_approved_release(receipt_case)
    receipt_case.receipt.write_text("{}\n", encoding="ascii")
    receipt_case.receipt.chmod(0o644)
    with pytest.raises(ProtectedReleaseSecurityError, match="substituted"):
        verify_installed_release(receipt_case)


def test_installed_release_substitution_and_unsafe_authority_fail_closed(
    tmp_path: Path,
) -> None:
    layout = _protected_release_layout(tmp_path / "release-case")
    install_approved_release(layout)
    installed_entrypoint = layout.release_root / "scripts/install-research-supervisor.sh"
    _write(installed_entrypoint, b"#!/bin/sh\nsubstituted-after-install\n", 0o755)
    with pytest.raises(ProtectedReleaseSecurityError, match="bytes were substituted"):
        verify_installed_release(layout)

    unsafe = _protected_release_layout(tmp_path / "authority-case")
    unsafe.authority_executable.chmod(0o777)
    with pytest.raises(ProtectedReleaseSecurityError, match="metadata is unsafe"):
        install_approved_release(unsafe)


def test_production_privileged_entrypoint_and_authorities_are_fixed() -> None:
    assert Path(
        "/usr/libexec/research-supervisor/install-protected-release"
    ) == PROTECTED_RELEASE_INSTALLER
    assert Path(
        "/usr/share/research-supervisor-release-authority/approved-release-v1.json"
    ) == PROTECTED_RELEASE_APPROVAL
    assert Path(
        "/var/tmp/research-supervisor-release-candidate"
    ) == PROTECTED_RELEASE_CANDIDATE
    assert Path("/opt/research-supervisor-release") == PROTECTED_RELEASE_ROOT
    assert Path(
        "/var/lib/research-supervisor-release-authority/installed-release-v1.json"
    ) == PROTECTED_RELEASE_RECEIPT

    readme = Path("README.md").read_text(encoding="utf-8")
    custodian = Path("docs/campaign_custodian.md").read_text(encoding="utf-8")
    product_installer = Path("scripts/install-research-supervisor.sh").read_text(
        encoding="utf-8"
    )
    assert "sudo /usr/libexec/research-supervisor/install-protected-release" in readme
    assert "sudo /bin/sh" not in readme
    old_entrypoint = (
        "/opt/research-supervisor-release/scripts/install-research-supervisor.sh OPERATOR"
    )
    assert old_entrypoint not in readme + custodian
    assert "release_verifier=/usr/libexec/research-supervisor/verify-protected-release" in (
        product_installer
    )


def test_canonical_home_initialization_reuse_and_environment_override_irrelevance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = _home_contract(tmp_path)
    first = initialize_managed_codex_home(contract)
    authentication = first / "auth.json"
    authentication.write_bytes(b"credential-marker\n")
    inode = first.stat().st_ino

    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "redirected/codex-home"))
    second = verified_managed_codex_home(contract)
    monkeypatch.setattr(
        managed_codex_module,
        "production_managed_codex_home_contract",
        lambda: contract,
    )
    environment_selected = managed_codex_module.managed_codex_home_from_environment()
    third = initialize_managed_codex_home(contract)

    assert environment_selected == second == third == first
    assert third.stat().st_ino == inode
    assert authentication.read_bytes() == b"credential-marker\n"


@pytest.mark.parametrize("failure", ("missing", "mode", "hardlink", "content", "symlink"))
def test_missing_or_tampered_home_binding_is_not_repaired(
    tmp_path: Path,
    failure: str,
) -> None:
    contract = _home_contract(tmp_path)
    home = initialize_managed_codex_home(contract)
    binding = home.parent / MANAGED_CODEX_HOME_BINDING
    if failure == "missing":
        binding.unlink()
    elif failure == "mode":
        binding.chmod(0o666)
    elif failure == "hardlink":
        linked = binding.with_name("managed-codex-home-link")
        os.link(binding, linked)
    elif failure == "content":
        binding.write_text("/different/codex-home\n", encoding="utf-8")
    else:
        binding.unlink()
        binding.symlink_to(home)

    with pytest.raises(CustodianEnvironmentError, match="unavailable"):
        verified_managed_codex_home(contract)
    with pytest.raises(CustodianEnvironmentError, match="unavailable|failed closed"):
        initialize_managed_codex_home(contract)
    if failure == "missing":
        assert not binding.exists()
    if failure == "mode":
        assert stat.S_IMODE(binding.stat().st_mode) == 0o666


def test_unsafe_home_ancestor_is_rejected(tmp_path: Path) -> None:
    contract = _home_contract(tmp_path)
    home = initialize_managed_codex_home(contract)
    assert home.is_dir()
    assert contract.data_trust_root is not None
    contract.data_trust_root.chmod(0o777)
    with pytest.raises(CustodianEnvironmentError, match="unavailable"):
        verified_managed_codex_home(contract)


@pytest.mark.parametrize("failure", ("missing", "malformed", "mode", "symlink"))
def test_protected_home_authority_tampering_is_rejected(
    tmp_path: Path,
    failure: str,
) -> None:
    contract = _home_contract(tmp_path)
    initialize_managed_codex_home(contract)
    receipt = contract.receipt
    if failure == "missing":
        receipt.unlink()
    elif failure == "malformed":
        receipt.write_text("{}\n", encoding="ascii")
    elif failure == "mode":
        receipt.chmod(0o666)
    else:
        receipt.unlink()
        receipt.symlink_to("/dev/null")
    with pytest.raises(CustodianEnvironmentError, match="authority is unavailable"):
        verified_managed_codex_home(contract)


def test_sign_in_and_replay_services_receive_one_verified_pair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _installer_layout(tmp_path, b"\x7fELF-qualified-codex-v1")
    identity = install_managed_codex(
        layout, version_probe=lambda _path: "0.144.0"
    ).identity
    home_contract = _home_contract(tmp_path / "home-state")
    home = initialize_managed_codex_home(home_contract)
    observed: dict[str, object] = {}
    monkeypatch.setattr(
        qualified_campaign_module,
        "_verified_managed_codex_identity",
        lambda: identity,
    )
    monkeypatch.setattr(qualified_campaign_module, "_managed_codex_home", lambda: home)

    def run(command: list[str], **kwargs: object) -> object:
        observed["command"] = command
        observed["environment"] = kwargs["env"]
        return type("Completed", (), {"returncode": 0})()

    monkeypatch.setattr(qualified_campaign_module.subprocess, "run", run)
    qualified_campaign_module.run_qualified_authentication()
    services = qualified_campaign_module._qualified_replay_services()

    assert observed["command"] == [str(identity.executable), "login"]
    environment = observed["environment"]
    assert isinstance(environment, dict)
    assert environment["CODEX_HOME"] == str(home)
    assert services.codex_executable == str(identity.executable)
    assert services.codex_identity_verifier is not None
    services.codex_identity_verifier(str(identity.executable))
    with pytest.raises(QualifiedCampaignInputError):
        services.codex_identity_verifier(str(identity.executable.parent / "other"))


def test_physics_auditor_rejects_operator_and_path_executable_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _installer_layout(tmp_path / "managed", b"\x7fELF-qualified-codex-v1")
    identity = install_managed_codex(
        layout, version_probe=lambda _path: "0.144.0"
    ).identity
    home_contract = _home_contract(tmp_path / "home")
    home = initialize_managed_codex_home(home_contract)
    path_codex = tmp_path / "operator-bin/codex"
    path_codex.parent.mkdir()
    _write(path_codex, b"#!/bin/sh\noperator-codex\n", 0o755)
    monkeypatch.setenv("PATH", str(path_codex.parent))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "operator-home"))

    selected = resolve_qualified_physics_auditor_codex(
        _physics_config(),
        installation_contract=layout.installation,
        home_contract=home_contract,
    )
    assert selected.identity == identity
    assert selected.identity.executable != path_codex
    assert selected.codex_home == home
    assert selected.environment["PATH"] == "/usr/bin:/bin"
    assert selected.environment["CODEX_HOME"] == str(home)

    arbitrary_pin = _physics_config(
        executable=path_codex,
        digest=hashlib.sha256(path_codex.read_bytes()).hexdigest(),
    )
    with pytest.raises(PhysicsAuditorInputError, match="conflicts"):
        resolve_qualified_physics_auditor_codex(
            arbitrary_pin,
            installation_contract=layout.installation,
            home_contract=home_contract,
        )

    system_pin = _physics_config(executable=Path("/usr/bin/codex"), digest="f" * 64)
    with pytest.raises(PhysicsAuditorInputError, match="conflicts"):
        resolve_qualified_physics_auditor_codex(
            system_pin,
            installation_contract=layout.installation,
            home_contract=home_contract,
        )


@pytest.mark.parametrize("failure", ("missing", "malformed", "digest"))
def test_physics_auditor_fails_before_launch_for_receipt_or_digest_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    layout = _installer_layout(tmp_path / "managed", b"\x7fELF-qualified-codex-v1")
    install_managed_codex(layout, version_probe=lambda _path: "0.144.0")
    home_contract = _home_contract(tmp_path / "home")
    initialize_managed_codex_home(home_contract)
    path_codex = tmp_path / "operator-bin/codex"
    path_codex.parent.mkdir()
    _write(path_codex, b"#!/bin/sh\noperator-codex\n", 0o755)
    monkeypatch.setenv("PATH", str(path_codex.parent))
    if failure == "missing":
        layout.installation.receipt.unlink()
    elif failure == "malformed":
        layout.installation.receipt.write_text("{}\n", encoding="ascii")
        layout.installation.receipt.chmod(0o644)
    else:
        _write(layout.installation.executable, b"\x7fELF-substituted", 0o755)

    with pytest.raises(PhysicsAuditorDependencyError, match="identity is unavailable"):
        resolve_qualified_physics_auditor_codex(
            _physics_config(),
            installation_contract=layout.installation,
            home_contract=home_contract,
        )


def test_physics_auditor_retry_and_resume_prepare_the_same_managed_pair(
    tmp_path: Path,
) -> None:
    layout = _installer_layout(tmp_path / "managed", b"\x7fELF-qualified-codex-v1")
    identity = install_managed_codex(
        layout, version_probe=lambda _path: "0.144.0"
    ).identity
    home_contract = _home_contract(tmp_path / "home")
    home = initialize_managed_codex_home(home_contract)
    config = _physics_config(executable=identity.executable, digest=identity.sha256)

    first = resolve_qualified_physics_auditor_codex(
        config,
        environ={"PATH": "/operator/first", "CODEX_HOME": "/operator/first-home"},
        installation_contract=layout.installation,
        home_contract=home_contract,
    )
    resumed = resolve_qualified_physics_auditor_codex(
        config,
        environ={"PATH": "/operator/retry", "CODEX_HOME": "/operator/retry-home"},
        installation_contract=layout.installation,
        home_contract=home_contract,
    )

    assert first.identity == resumed.identity == identity
    assert first.codex_home == resumed.codex_home == home
    assert first.environment["CODEX_HOME"] == resumed.environment["CODEX_HOME"]
    assert first.environment["PATH"] == resumed.environment["PATH"] == "/usr/bin:/bin"


def test_physics_auditor_rejects_missing_or_unsafe_canonical_home(tmp_path: Path) -> None:
    layout = _installer_layout(tmp_path / "managed", b"\x7fELF-qualified-codex-v1")
    install_managed_codex(layout, version_probe=lambda _path: "0.144.0")
    home_contract = _home_contract(tmp_path / "home")
    home = initialize_managed_codex_home(home_contract)
    binding = home.parent / MANAGED_CODEX_HOME_BINDING
    binding.unlink()

    with pytest.raises(PhysicsAuditorDependencyError, match="identity is unavailable"):
        resolve_qualified_physics_auditor_codex(
            _physics_config(),
            installation_contract=layout.installation,
            home_contract=home_contract,
        )


def test_prestart_preparation_cannot_launch_without_managed_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    absent_layout = _installer_layout(
        tmp_path / "managed",
        b"\x7fELF-qualified-codex-v1",
    )
    home_contract = _home_contract(tmp_path / "home")
    home = initialize_managed_codex_home(home_contract)
    operator_codex = tmp_path / "operator-bin/codex"
    operator_codex.parent.mkdir()
    _write(operator_codex, b"#!/bin/sh\noperator-authenticated-codex\n", 0o755)
    monkeypatch.setenv("PATH", str(operator_codex.parent))
    monkeypatch.setattr(
        managed_codex_module,
        "PRODUCTION_MANAGED_CODEX_CONTRACT",
        absent_layout.installation,
    )
    monkeypatch.setattr(
        managed_codex_module,
        "production_managed_codex_home_contract",
        lambda: home_contract,
    )

    readiness = custodian_bootstrap_module.inspect_environment(
        home.parent,
        allow_program_execution=False,
    )
    assert not readiness.codex_ready
    assert any(issue.code == "codex_unavailable" for issue in readiness.issues)
    with pytest.raises(QualifiedCampaignInputError, match="identity is unavailable"):
        qualified_campaign_module._qualified_replay_services()
    with pytest.raises(PhysicsAuditorDependencyError, match="identity is unavailable"):
        resolve_qualified_physics_auditor_codex(_physics_config())


@pytest.mark.skipif(
    not Path("/usr/bin/bwrap").is_file(),
    reason="Bubblewrap Physics Auditor input preparation unavailable",
)
def test_schema_v2_physics_auditor_never_launches_path_codex_without_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_physics_auditor_execution import SYNTHETIC, _evidence, _workspace

    workspace = _workspace(tmp_path)
    evidence = _evidence(tmp_path, workspace)
    absent_layout = _installer_layout(
        tmp_path / "managed",
        b"\x7fELF-qualified-codex-v1",
    )
    home_contract = _home_contract(tmp_path / "home")
    initialize_managed_codex_home(home_contract)
    operator_codex = tmp_path / "operator-bin/codex"
    operator_codex.parent.mkdir()
    _write(operator_codex, b"#!/bin/sh\noperator-path-codex\n", 0o755)
    monkeypatch.setenv("PATH", str(operator_codex.parent))
    monkeypatch.setattr(
        managed_codex_module,
        "PRODUCTION_MANAGED_CODEX_CONTRACT",
        absent_layout.installation,
    )
    monkeypatch.setattr(
        managed_codex_module,
        "production_managed_codex_home_contract",
        lambda: home_contract,
    )
    launched = False

    def forbidden_launch(**_kwargs: object) -> object:
        nonlocal launched
        launched = True
        raise AssertionError("PATH Codex must not launch")

    monkeypatch.setattr(
        physics_execution_module,
        "_invoke_qualified_codex",
        forbidden_launch,
    )
    with pytest.raises(PhysicsAuditorDependencyError, match="identity is unavailable"):
        physics_execution_module.run_physics_auditor(
            contract_path=SYNTHETIC / "contract.yaml",
            execution_config_path=SYNTHETIC / "execution-config.yaml",
            task_id="synthetic-task",
            workspace=workspace,
            oracle_evidence_root=evidence,
            output_directory=tmp_path / "physics-output",
        )
    assert not launched


@pytest.mark.skipif(
    not Path("/usr/bin/bwrap").is_file(),
    reason="Bubblewrap Physics Auditor input preparation unavailable",
)
def test_schema_v2_resume_reverifies_and_uses_the_same_production_pair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_physics_auditor_execution import (
        SYNTHETIC,
        ScriptedCodex,
        _evidence,
        _workspace,
    )

    workspace = _workspace(tmp_path)
    evidence = _evidence(tmp_path, workspace)
    layout = _installer_layout(tmp_path / "managed", b"\x7fELF-qualified-codex-v1")
    identity = install_managed_codex(
        layout,
        version_probe=lambda _path: "0.144.0",
    ).identity
    home_contract = _home_contract(tmp_path / "home")
    home = initialize_managed_codex_home(home_contract)
    monkeypatch.setattr(
        managed_codex_module,
        "PRODUCTION_MANAGED_CODEX_CONTRACT",
        layout.installation,
    )
    monkeypatch.setattr(
        managed_codex_module,
        "production_managed_codex_home_contract",
        lambda: home_contract,
    )
    observed: list[tuple[Path, str]] = []
    scripted = ScriptedCodex(SYNTHETIC / "reports/clean.json")

    def capture_pair(**kwargs: object) -> object:
        environment = kwargs["environ"]
        assert isinstance(environment, dict)
        executable = kwargs["codex_executable"]
        assert isinstance(executable, Path)
        observed.append((executable, environment["CODEX_HOME"]))
        return scripted(**kwargs)

    monkeypatch.setattr(
        physics_execution_module,
        "_invoke_qualified_codex",
        capture_pair,
    )

    def interrupt_after_prompt(name: str) -> None:
        if name == "prompt_finalized":
            raise RuntimeError("simulated safe interruption")

    output = tmp_path / "physics-output"
    with pytest.raises(RuntimeError, match="safe interruption"):
        physics_execution_module.run_physics_auditor(
            contract_path=SYNTHETIC / "contract.yaml",
            execution_config_path=SYNTHETIC / "execution-config.yaml",
            task_id="synthetic-task",
            workspace=workspace,
            oracle_evidence_root=evidence,
            output_directory=output,
            checkpoint=interrupt_after_prompt,
        )
    assert not observed

    physics_execution_module.resume_physics_auditor(
        contract_path=SYNTHETIC / "contract.yaml",
        execution_config_path=SYNTHETIC / "execution-config.yaml",
        task_id="synthetic-task",
        workspace=workspace,
        oracle_evidence_root=evidence,
        output_directory=output,
    )
    assert observed == [(identity.executable, str(home))]


def test_production_wired_protected_release_to_every_launch_preparation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release_layout = _protected_release_layout(tmp_path / "release-boundary")
    protected_release = install_approved_release(release_layout)
    managed_layout = _managed_layout_from_protected_release(release_layout)
    shell_boundary, installer_result = _parse_protected_installer_main(
        _invoke_protected_installer_main(
            release_layout,
            payload="install-managed-codex.sh",
            cwd=_mkdir(tmp_path / "caller-cwd", 0o700),
            environment={
                "PATH": str(_mkdir(tmp_path / "caller-bin", 0o700)),
                "PYTHONHOME": str(tmp_path / "caller-python-home"),
                "PYTHONPATH": str(tmp_path / "caller-python-path"),
                "PYTHONUSERBASE": str(tmp_path / "caller-userbase"),
            },
        )
    )
    installed = verify_managed_codex_installation(managed_layout.installation)
    home_contract = _home_contract(tmp_path / "home-boundary")
    home = initialize_managed_codex_home(home_contract)
    credential = home / "auth.json"
    credential.write_bytes(b"credential-material-must-not-propagate\n")

    monkeypatch.setattr(
        managed_codex_module,
        "PRODUCTION_MANAGED_CODEX_CONTRACT",
        managed_layout.installation,
    )
    monkeypatch.setattr(
        managed_codex_module,
        "production_managed_codex_home_contract",
        lambda: home_contract,
    )
    operator_codex = tmp_path / "operator-bin/codex"
    operator_codex.parent.mkdir()
    _write(operator_codex, b"#!/bin/sh\noperator-path-codex\n", 0o755)
    monkeypatch.setenv("PATH", str(operator_codex.parent))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "redirected-home"))

    verified = verify_managed_codex_installation()
    readiness = custodian_bootstrap_module.inspect_environment(
        home.parent,
        allow_program_execution=False,
    )
    captured: dict[str, object] = {}

    def capture_authentication(command: list[str], **kwargs: object) -> object:
        captured["command"] = command
        captured["environment"] = kwargs["env"]
        return type("Completed", (), {"returncode": 0})()

    monkeypatch.setattr(qualified_campaign_module.subprocess, "run", capture_authentication)
    qualified_campaign_module.run_qualified_authentication()
    replay = qualified_campaign_module._qualified_replay_services()
    assert replay.codex_identity_verifier is not None
    worker_executable = replay.codex_executable
    ordinary_auditor_executable = replay.codex_executable
    replay.codex_identity_verifier(worker_executable)
    replay.codex_identity_verifier(ordinary_auditor_executable)
    physics_fresh = resolve_qualified_physics_auditor_codex(
        _physics_config(),
        environ={"PATH": "/operator/fresh", "CODEX_HOME": "/operator/fresh-home"},
    )
    physics_retry = resolve_qualified_physics_auditor_codex(
        _physics_config(),
        environ={"PATH": "/operator/retry", "CODEX_HOME": "/operator/retry-home"},
    )
    physics_resume = resolve_qualified_physics_auditor_codex(
        _physics_config(),
        environ={"PATH": "/operator/resume", "CODEX_HOME": "/operator/resume-home"},
    )

    original_environment = dict(os.environ)
    try:
        qualified_runner_module._seal_production_git_environment()
        sealed_environment = dict(os.environ)
    finally:
        os.environ.clear()
        os.environ.update(original_environment)

    assert protected_release.release_root == release_layout.release_root
    assert shell_boundary["managed_codex_installer"] == str(
        release_layout.release_root
        / "src/research_automation_supervisor/managed_codex_installer.py"
    )
    assert shell_boundary["cwd"] == str(release_layout.release_root)
    assert shell_boundary["isolated"] is True
    assert shell_boundary["safe_path"] is True
    assert shell_boundary["user_site_disabled"] is True
    assert installer_result["operation"] == "install"
    assert installer_result["disposition"] == "installed"
    assert installer_result["identity"] == {
        "executable": str(installed.executable),
        "release_id": installed.release_id,
        "sha256": installed.sha256,
        "version": installed.version,
    }
    assert installer_result["protected_receipt"] == json.loads(
        managed_layout.installation.receipt.read_text(encoding="ascii")
    )
    assert verified == installed
    assert readiness.codex_ready
    assert captured["command"] == [str(installed.executable), "login"]
    authentication_environment = captured["environment"]
    assert isinstance(authentication_environment, dict)
    assert authentication_environment["CODEX_HOME"] == str(home)
    assert worker_executable == ordinary_auditor_executable == str(installed.executable)
    assert (
        physics_fresh.identity
        == physics_retry.identity
        == physics_resume.identity
        == installed
    )
    assert (
        physics_fresh.codex_home
        == physics_retry.codex_home
        == physics_resume.codex_home
        == home
    )
    assert (
        physics_fresh.environment["CODEX_HOME"]
        == physics_retry.environment["CODEX_HOME"]
        == physics_resume.environment["CODEX_HOME"]
        == str(home)
    )
    assert sealed_environment["CODEX_HOME"] == str(home)
    assert operator_codex != installed.executable
    assert b"credential-material" not in json.dumps(
        {
            "readiness": readiness.model_dump(mode="json"),
            "auth_command": captured["command"],
            "auth_environment": authentication_environment,
            "physics_environments": [
                dict(physics_fresh.environment),
                dict(physics_retry.environment),
                dict(physics_resume.environment),
            ],
            "sealed_environment": sealed_environment,
        },
        sort_keys=True,
    ).encode("utf-8")


def test_qualified_start_verifies_runtime_pair_before_core_consumption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    consumed = False

    class Core:
        def consume_start_intent_for_qualified_launch(
            self, *_args: object, **_kwargs: object
        ) -> object:
            nonlocal consumed
            consumed = True
            raise AssertionError("Core Start must not be consumed")

    def reject(_executable: str) -> None:
        raise QualifiedCampaignInputError("managed identity changed")

    monkeypatch.setattr(
        qualified_campaign_module, "_verify_qualified_runtime_pair", reject
    )
    with pytest.raises(QualifiedCampaignInputError, match="managed identity changed"):
        qualified_campaign_module.start_qualified_launch(
            "intent-test",
            Core(),  # type: ignore[arg-type]
            expected_campaign_public_id="campaign-test",
            authority_directory=tmp_path / "authority",
            exchange_root=tmp_path / "exchange",
        )
    assert not consumed


def test_credentials_are_outside_campaign_and_export_source_allowlists(
    tmp_path: Path,
) -> None:
    contract = _home_contract(tmp_path)
    home = initialize_managed_codex_home(contract)
    credential = home / "auth.json"
    credential.write_bytes(b"never-export-this\n")
    campaign_roots = (
        home.parent / "qualified-campaigns",
        home.parent / "exports",
        home.parent / "custodian-state",
    )

    assert all(
        credential.parent != root and root not in credential.parents
        for root in campaign_roots
    )
    export_source = Path(
        "src/research_automation_supervisor/qualified_campaign.py"
    ).read_text(encoding="utf-8")
    assert "auth.json" not in export_source


@pytest.mark.skip(reason="qualification-only: requires real root-owned /usr/bin and /etc state")
def test_real_host_managed_codex_identity_qualification_pending() -> None:
    verify_managed_codex_installation()
