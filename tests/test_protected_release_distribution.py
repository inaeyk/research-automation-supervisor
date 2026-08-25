from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

from research_automation_supervisor import release_preparation
from research_automation_supervisor.managed_codex import (
    MANAGED_CODEX_CODE_MODE_HOST,
    verify_managed_codex_installation,
)
from research_automation_supervisor.managed_codex_installer import (
    _qualification_installer_layout,
)
from research_automation_supervisor.protected_release import (
    PROTECTED_RELEASE_APPROVAL,
    PROTECTED_RELEASE_CANDIDATE,
    PROTECTED_RELEASE_INSTALLER,
    PROTECTED_RELEASE_ROOT,
    PROTECTED_RELEASE_UPDATE_APPROVAL,
    PROTECTED_RELEASE_VERIFIER,
    ProtectedReleaseLayout,
    ProtectedReleaseSecurityError,
    install_approved_release,
    update_approved_release,
    verify_installed_release,
)
from research_automation_supervisor.release_preparation import (
    BOOTSTRAP_INVENTORY_NAME,
    INSTALLER_NAME,
    PREPARATION_VENV_NAME,
    VERIFIER_NAME,
    PreparedRelease,
    ReleasePreparationConfig,
    ReleasePreparationError,
    prepare_protected_release,
    verify_prepared_release,
)

REPOSITORY = Path(__file__).resolve().parents[1]
DIRECT_DEPENDENCIES = {
    "cryptography": "46.0.7",
    "dulwich": "1.2.12",
    "packaging": "25.0",
    "pydantic": "2.13.4",
    "PyYAML": "6.0.3",
    "urllib3": "2.7.0",
    "typer": "0.27.1",
}


def _write_fake_wheel(
    root: Path,
    distribution: str,
    version: str,
    *,
    requires_dist: str | None = None,
) -> None:
    normalized = distribution.replace("-", "_")
    path = root / f"{normalized}-{version}-py3-none-any.whl"
    dist_info = f"{normalized}-{version}.dist-info"
    with zipfile.ZipFile(path, "w") as archive:
        metadata = f"Metadata-Version: 2.3\nName: {distribution}\nVersion: {version}\n"
        if requires_dist is not None:
            metadata += f"Requires-Dist: {requires_dist}\n"
        archive.writestr(f"{dist_info}/METADATA", metadata + "\n")
        archive.writestr(
            f"{dist_info}/WHEEL",
            "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        archive.writestr(f"{dist_info}/RECORD", "")
    path.chmod(0o644)


def _write_wheelhouse(tmp_path: Path) -> Path:
    wheelhouse = tmp_path / "input-wheelhouse"
    wheelhouse.mkdir(mode=0o755)
    for distribution, dependency_version in DIRECT_DEPENDENCIES.items():
        _write_fake_wheel(wheelhouse, distribution, dependency_version)
    return wheelhouse


def _system_python_input() -> tuple[Path, str]:
    system_python = Path("/usr/bin/python3").resolve(strict=True)
    completed = subprocess.run(
        [str(system_python), "--version"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    version = completed.stdout.strip().removeprefix("Python ")
    return system_python, version


def _system_code_mode_host_input() -> Path:
    return Path("/usr/bin/true").resolve(strict=True)


def _prepare(
    tmp_path: Path,
    *,
    update_from_manifest_sha256: str | None = None,
) -> PreparedRelease:
    tmp_path.mkdir(parents=True, exist_ok=True)
    wheelhouse = _write_wheelhouse(tmp_path)
    system_python, version = _system_python_input()
    return prepare_protected_release(
        ReleasePreparationConfig(
            repository=REPOSITORY,
            candidate_root=tmp_path / "research-supervisor-release-candidate",
            authority_staging_root=tmp_path / "release-authority-candidate",
            release_id="ras-r0-dist-test-v1",
            codex_artifact=system_python,
            code_mode_host_artifact=_system_code_mode_host_input(),
            codex_version=version,
            wheelhouse_source=wheelhouse,
            update_from_manifest_sha256=update_from_manifest_sha256,
        )
    )


def _prepare_via_documented_cli(
    tmp_path: Path,
    label: str,
    *,
    wheelhouse: Path,
) -> tuple[PreparedRelease, subprocess.CompletedProcess[str]]:
    candidate = tmp_path / f"research-supervisor-release-candidate-{label}"
    authority = tmp_path / f"release-authority-candidate-{label}"
    system_python, version = _system_python_input()
    completed = subprocess.run(
        [
            str(REPOSITORY / "scripts/prepare-protected-release.py"),
            "prepare",
            "--repository",
            str(REPOSITORY),
            "--candidate-root",
            str(candidate),
            "--authority-staging-root",
            str(authority),
            "--release-id",
            f"ras-r0-dist-pep668-{label}",
            "--codex-artifact",
            str(system_python),
            "--code-mode-host-artifact",
            str(_system_code_mode_host_input()),
            "--codex-version",
            version,
            "--wheelhouse-source",
            str(wheelhouse),
        ],
        cwd=tmp_path,
        env={
            "PATH": str(tmp_path / "hostile-path"),
            "PIP_BREAK_SYSTEM_PACKAGES": "1",
            "PIP_INDEX_URL": "https://invalid.example.test/simple",
            "PYTHONHOME": str(tmp_path / "hostile-python-home"),
            "PYTHONPATH": str(tmp_path / "hostile-python-path"),
        },
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
    )
    if completed.returncode != 0:
        return (
            PreparedRelease(
                candidate_root=candidate,
                authority_staging_root=authority,
                approval=authority / "approved-release-v1.json",
                approval_sha256="",
                bootstrap_inventory=authority / BOOTSTRAP_INVENTORY_NAME,
                resolution_python=tmp_path / PREPARATION_VENV_NAME / "bin/python",
                release_id=f"ras-r0-dist-pep668-{label}",
            ),
            completed,
        )
    result = json.loads(completed.stdout)
    return (
        PreparedRelease(
            candidate_root=Path(result["candidate_root"]),
            authority_staging_root=Path(result["authority_staging_root"]),
            approval=Path(result["approval"]),
            approval_sha256=result["approval_sha256"],
            bootstrap_inventory=Path(result["bootstrap_inventory"]),
            resolution_python=Path(result["resolution_python"]),
            release_id=result["release_id"],
        ),
        completed,
    )


def _simulated_layout(tmp_path: Path, prepared: PreparedRelease) -> ProtectedReleaseLayout:
    tmp_path.mkdir(parents=True, exist_ok=True)
    tmp_path.chmod(0o700)
    release_parent = tmp_path / "protected-destination"
    release_parent.mkdir(mode=0o755)
    receipt_parent = tmp_path / "protected-state"
    receipt_parent.mkdir(mode=0o755)
    return ProtectedReleaseLayout(
        authority_executable=prepared.authority_staging_root / INSTALLER_NAME,
        verifier_executable=prepared.authority_staging_root / VERIFIER_NAME,
        approval=prepared.approval,
        candidate_root=prepared.candidate_root,
        release_root=release_parent / "research-supervisor-release",
        receipt=receipt_parent / "installed-release-v1.json",
        authority_trust_root=tmp_path,
        approval_trust_root=tmp_path,
        release_trust_root=tmp_path,
        receipt_trust_root=tmp_path,
        authority_uid=os.getuid(),
        authority_gid=os.getgid(),
    )


def test_documented_system_python_prepare_uses_and_reuses_private_venv(
    tmp_path: Path,
) -> None:
    wheelhouse = _write_wheelhouse(tmp_path)
    first, first_completed = _prepare_via_documented_cli(
        tmp_path,
        "first",
        wheelhouse=wheelhouse,
    )
    assert first_completed.returncode == 0, first_completed.stderr
    private_venv = tmp_path / PREPARATION_VENV_NAME
    private_python = private_venv / "bin/python"
    assert first.resolution_python == private_python
    assert private_python.is_file()
    assert not private_python.is_symlink()
    assert stat.S_IMODE(private_venv.stat().st_mode) == 0o700
    assert verify_prepared_release(first.candidate_root, first.approval) == (
        first.approval_sha256
    )
    private_python_identity = (private_python.stat().st_dev, private_python.stat().st_ino)

    second, second_completed = _prepare_via_documented_cli(
        tmp_path,
        "repeat",
        wheelhouse=wheelhouse,
    )
    assert second_completed.returncode == 0, second_completed.stderr
    assert second.resolution_python == private_python
    assert (private_python.stat().st_dev, private_python.stat().st_ino) == (
        private_python_identity
    )
    assert verify_prepared_release(second.candidate_root, second.approval) == (
        second.approval_sha256
    )
    assert (REPOSITORY / "scripts/prepare-protected-release.py").read_bytes().startswith(
        b"#!/usr/bin/python3 -I\n"
    )


def test_private_venv_resolution_command_is_strictly_offline() -> None:
    private_python = Path("/var/tmp/private-preparation-venv/bin/python")
    command = release_preparation._offline_resolution_command(
        Path("/tmp/product.whl"),
        Path("/tmp/wheelhouse"),
        private_python,
    )

    assert command[:4] == [str(private_python), "-I", "-m", "pip"]
    for required in (
        "--isolated",
        "--dry-run",
        "--ignore-installed",
        "--disable-pip-version-check",
        "--no-cache-dir",
        "--no-index",
        "--only-binary=:all:",
    ):
        assert required in command
    assert "--break-system-packages" not in command
    assert not any("index-url" in argument for argument in command)


def test_documented_prepare_requires_explicit_code_mode_host_artifact(
    tmp_path: Path,
) -> None:
    system_python, version = _system_python_input()
    completed = subprocess.run(
        [
            str(REPOSITORY / "scripts/prepare-protected-release.py"),
            "prepare",
            "--repository",
            str(REPOSITORY),
            "--candidate-root",
            str(tmp_path / "candidate"),
            "--authority-staging-root",
            str(tmp_path / "authority"),
            "--release-id",
            "ras-code-mode-host-required",
            "--codex-artifact",
            str(system_python),
            "--codex-version",
            version,
            "--wheelhouse-source",
            str(tmp_path / "wheelhouse"),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 2
    assert "--code-mode-host-artifact" in completed.stderr
    assert not (tmp_path / "candidate").exists()


def test_documented_prepare_rejects_missing_offline_transitive_closure(
    tmp_path: Path,
) -> None:
    wheelhouse = _write_wheelhouse(tmp_path)
    _write_fake_wheel(
        wheelhouse,
        "typer",
        DIRECT_DEPENDENCIES["typer"],
        requires_dist="r0-dist-deliberately-unavailable==1.0",
    )
    prepared, completed = _prepare_via_documented_cli(
        tmp_path,
        "offline-failure",
        wheelhouse=wheelhouse,
    )

    assert completed.returncode == 2
    assert "complete compatible dependency closure" in completed.stderr
    assert not prepared.candidate_root.exists()
    assert not prepared.authority_staging_root.exists()


def test_documented_prepare_fails_clearly_when_private_venv_pip_is_missing(
    tmp_path: Path,
) -> None:
    private_venv = tmp_path / PREPARATION_VENV_NAME
    subprocess.run(
        [
            "/usr/bin/python3",
            "-I",
            "-m",
            "venv",
            "--copies",
            "--without-pip",
            str(private_venv),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    private_venv.chmod(0o700)
    wheelhouse = _write_wheelhouse(tmp_path)
    prepared, completed = _prepare_via_documented_cli(
        tmp_path,
        "missing-pip",
        wheelhouse=wheelhouse,
    )

    assert completed.returncode == 2
    assert "private preparation virtual environment pip is unavailable" in completed.stderr
    assert not prepared.candidate_root.exists()
    assert not prepared.authority_staging_root.exists()


def test_complete_candidate_manifest_and_bootstrap_authority_are_exact(
    tmp_path: Path,
) -> None:
    prepared = _prepare(tmp_path)

    assert verify_prepared_release(prepared.candidate_root, prepared.approval) == (
        prepared.approval_sha256
    )
    approval = json.loads(prepared.approval.read_text(encoding="ascii"))
    approved = {item["path"]: item for item in approval["files"]}
    observed = {
        path.relative_to(prepared.candidate_root).as_posix()
        for path in prepared.candidate_root.rglob("*")
        if path.is_file()
    }
    assert set(approved) == observed
    for relative, item in approved.items():
        path = prepared.candidate_root / relative
        assert item["mode"] == f"{stat.S_IMODE(path.stat().st_mode):04o}"
        assert item["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()

    expected_candidate_files = {
        "artifacts/codex",
        "artifacts/codex-code-mode-host",
        "artifacts/research_automation_supervisor-0.3.0-py3-none-any.whl",
        "managed-codex-approval-v1.json",
        "scripts/install-research-supervisor.sh",
        "scripts/install-managed-codex.sh",
        "scripts/install-core-authority-service.sh",
        "scripts/run-protected-python.sh",
        "scripts/protected-managed-codex-entry.py",
        "scripts/research-supervisor-core-authority.service",
        "src/research_automation_supervisor/protected_release.py",
        "src/research_automation_supervisor/release_preparation.py",
    }
    assert expected_candidate_files.issubset(observed)
    managed_approval = json.loads(
        (prepared.candidate_root / "managed-codex-approval-v1.json").read_text(
            encoding="ascii"
        )
    )
    companion = prepared.candidate_root / "artifacts/codex-code-mode-host"
    assert managed_approval["schema_version"] == 2
    assert managed_approval["code_mode_host_artifact"] == (
        "artifacts/codex-code-mode-host"
    )
    assert managed_approval["code_mode_host_sha256"] == hashlib.sha256(
        companion.read_bytes()
    ).hexdigest()
    assert managed_approval["sha256"] != managed_approval["code_mode_host_sha256"]
    assert len(list((prepared.candidate_root / "wheelhouse").glob("*.whl"))) == len(
        DIRECT_DEPENDENCIES
    )

    product_wheel = (
        prepared.candidate_root
        / "artifacts/research_automation_supervisor-0.3.0-py3-none-any.whl"
    )
    with zipfile.ZipFile(product_wheel) as archive:
        names = set(archive.namelist())
        assert "research_automation_supervisor/secure_cli.py" in names
        assert any(name.endswith(".dist-info/METADATA") for name in names)
        assert any(name.endswith(".dist-info/RECORD") for name in names)
        assert any("physics_benchmark_blind_v1" in name for name in names)

    helper = prepared.authority_staging_root / INSTALLER_NAME
    verifier = prepared.authority_staging_root / VERIFIER_NAME
    assert helper.read_bytes() == verifier.read_bytes()
    assert helper.read_bytes().startswith(b"#!/usr/bin/python3 -I\n")
    assert stat.S_IMODE(helper.stat().st_mode) == 0o755
    assert stat.S_IMODE(verifier.stat().st_mode) == 0o755
    inventory = json.loads(
        (prepared.authority_staging_root / BOOTSTRAP_INVENTORY_NAME).read_text(
            encoding="ascii"
        )
    )
    assert inventory["authority_is_trusted"] is False
    assert inventory["production_candidate_destination"] == str(
        PROTECTED_RELEASE_CANDIDATE
    )
    assert inventory["production_release_destination"] == str(PROTECTED_RELEASE_ROOT)
    assert {item["destination"] for item in inventory["files"]} == {
        str(PROTECTED_RELEASE_INSTALLER),
        str(PROTECTED_RELEASE_VERIFIER),
        str(PROTECTED_RELEASE_APPROVAL),
    }
    completed = subprocess.run(
        [
            str(REPOSITORY / "scripts/prepare-protected-release.py"),
            "verify",
            "--candidate-root",
            str(prepared.candidate_root),
            "--approval",
            str(prepared.approval),
        ],
        cwd=tmp_path,
        env={"PATH": str(tmp_path / "hostile-path"), "PYTHONPATH": str(tmp_path)},
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["verified"] is True


def test_tampered_missing_extra_and_mode_changed_candidate_files_fail(
    tmp_path: Path,
) -> None:
    prepared = _prepare(tmp_path)
    cases = tmp_path / "failure-cases"
    cases.mkdir()

    tampered = shutil.copytree(prepared.candidate_root, cases / "tampered")
    target = tampered / "src/research_automation_supervisor/__init__.py"
    target.write_bytes(target.read_bytes() + b"\n# substituted\n")
    target.chmod(0o644)
    with pytest.raises(ReleasePreparationError, match="bytes"):
        verify_prepared_release(tampered, prepared.approval)

    missing = shutil.copytree(prepared.candidate_root, cases / "missing")
    (missing / "src/research_automation_supervisor/__init__.py").unlink()
    with pytest.raises(ReleasePreparationError, match="missing"):
        verify_prepared_release(missing, prepared.approval)

    extra = shutil.copytree(prepared.candidate_root, cases / "extra")
    extra_file = extra / "unapproved.txt"
    extra_file.write_bytes(b"not approved\n")
    extra_file.chmod(0o644)
    with pytest.raises(ReleasePreparationError, match="extra"):
        verify_prepared_release(extra, prepared.approval)

    wrong_mode = shutil.copytree(prepared.candidate_root, cases / "wrong-mode")
    (wrong_mode / "src/research_automation_supervisor/__init__.py").chmod(0o755)
    with pytest.raises(ReleasePreparationError, match="mode"):
        verify_prepared_release(wrong_mode, prepared.approval)

    install_case = replace(
        _simulated_layout(tmp_path, prepared),
        candidate_root=extra,
    )
    with pytest.raises(ProtectedReleaseSecurityError, match="unapproved"):
        install_approved_release(install_case)


def test_prepared_release_enters_existing_protected_installer_and_receipt_chain(
    tmp_path: Path,
) -> None:
    wheelhouse = _write_wheelhouse(tmp_path)
    prepared, completed = _prepare_via_documented_cli(
        tmp_path,
        "protected-contract",
        wheelhouse=wheelhouse,
    )
    assert completed.returncode == 0, completed.stderr
    layout = _simulated_layout(tmp_path, prepared)

    installed = install_approved_release(layout)
    verified = verify_installed_release(layout)
    assert installed.disposition == "installed"
    assert verified.release_id == prepared.release_id
    assert verified.manifest_sha256 == prepared.approval_sha256

    managed = _qualification_installer_layout(layout.release_root)
    managed.installation.executable_trust_root.mkdir(mode=0o755)
    (managed.installation.executable_trust_root / "system").mkdir(mode=0o755)
    managed.installation.executable.parent.mkdir(parents=True, mode=0o755)
    managed.installation.receipt.parent.mkdir(parents=True, mode=0o755)
    completed = subprocess.run(
        [
            "/bin/sh",
            str(layout.release_root / "scripts/install-managed-codex.sh"),
            "--qualification-import-probe",
        ],
        cwd=tmp_path,
        env={"PATH": str(tmp_path / "operator-path"), "PYTHONPATH": str(tmp_path)},
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout.splitlines()[-1])
    identity = verify_managed_codex_installation(managed.installation)
    assert result["disposition"] == "installed"
    assert result["identity"]["sha256"] == identity.sha256
    assert result["protected_receipt"] == json.loads(
        managed.installation.receipt.read_text(encoding="ascii")
    )


def test_protected_update_is_bound_to_exact_installed_manifest(
    tmp_path: Path,
) -> None:
    initial = _prepare(tmp_path / "initial")
    initial_layout = _simulated_layout(tmp_path, initial)
    installed = install_approved_release(initial_layout)
    updated = _prepare(
        tmp_path / "updated",
        update_from_manifest_sha256=installed.manifest_sha256,
    )
    update_layout = replace(
        initial_layout,
        candidate_root=updated.candidate_root,
        update_approval=updated.approval,
    )
    updated_approval_bytes = updated.approval.read_bytes()
    update_inventory = json.loads(
        updated.bootstrap_inventory.read_text(encoding="ascii")
    )
    approval_entry = next(
        item
        for item in update_inventory["files"]
        if item["staged_name"] == "approved-release-v1.json"
    )

    wrong_binding = json.loads(updated_approval_bytes)
    wrong_binding["update_from_manifest_sha256"] = "f" * 64
    updated.approval.write_text(
        json.dumps(wrong_binding, sort_keys=True) + "\n",
        encoding="ascii",
    )
    updated.approval.chmod(0o644)
    with pytest.raises(ProtectedReleaseSecurityError, match="update authority"):
        update_approved_release(update_layout)
    assert verify_installed_release(initial_layout).manifest_sha256 == (
        installed.manifest_sha256
    )
    updated.approval.write_bytes(updated_approval_bytes)
    updated.approval.chmod(0o644)

    result = update_approved_release(update_layout)
    verified = verify_installed_release(update_layout)

    assert result.disposition == "updated"
    assert result.release_id == installed.release_id == verified.release_id
    assert result.manifest_sha256 == updated.approval_sha256
    assert verified.manifest_sha256 == updated.approval_sha256
    assert approval_entry["destination"] == str(PROTECTED_RELEASE_UPDATE_APPROVAL)
    assert initial_layout.approval.read_bytes() == updated_approval_bytes
    assert not updated.approval.exists()


def test_supported_bootstrap_contract_uses_only_fixed_production_destinations() -> None:
    documentation = Path("docs/protected_release_bootstrap.md").read_text(encoding="utf-8")
    readme = Path("README.md").read_text(encoding="utf-8")
    custodian = Path("docs/campaign_custodian.md").read_text(encoding="utf-8")
    supported = documentation + readme + custodian

    for fixed in (
        PROTECTED_RELEASE_INSTALLER,
        PROTECTED_RELEASE_VERIFIER,
        PROTECTED_RELEASE_APPROVAL,
        PROTECTED_RELEASE_CANDIDATE,
        PROTECTED_RELEASE_ROOT,
        Path("/usr/bin/codex"),
        MANAGED_CODEX_CODE_MODE_HOST,
        PROTECTED_RELEASE_UPDATE_APPROVAL,
    ):
        assert str(fixed) in supported
    assert "sudo /usr/bin/install" in documentation
    assert "sudo /usr/bin/sha256sum" in documentation
    assert "sudo /usr/libexec/research-supervisor/install-protected-release" in supported
    for forbidden in (
        "sudo ./",
        "sudo /bin/sh scripts/",
        "sudo python",
        "sudo python3",
        "sudo pip",
        "sudo npm",
        "sudo curl",
    ):
        assert forbidden not in supported
