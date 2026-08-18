from __future__ import annotations

import json
import os
import platform
import stat
import subprocess
from pathlib import Path

import pytest

from research_automation_supervisor.custodian import CampaignCustodian
from research_automation_supervisor.custodian_server import CustodianHTTPServer
from tests.custodian_helpers import FakeQualifiedRunner, ready_environment


def _windows_path(path: Path) -> str:
    return subprocess.run(
        ["wslpath", "-w", str(path)], check=True, capture_output=True, text=True
    ).stdout.strip()


def test_double_click_launcher_is_hidden_and_uses_supported_default_wsl() -> None:
    vbs = Path("Research Supervisor.vbs").read_text(encoding="utf-8")
    powershell = Path("launch-research-supervisor.ps1").read_text(encoding="utf-8")
    bootstrap = Path("scripts/custodian-bootstrap.sh").read_text(encoding="utf-8")
    assert "shell.Run(command, 0, True)" in vbs
    assert "launch-research-supervisor.ps1" in vbs
    assert "--list --quiet" in powershell
    assert "-d " not in powershell
    assert "ReadinessInstance" in powershell
    assert "Start-Process $Url" in powershell
    assert "Your campaign remains safe" in powershell
    assert "What happened?" in powershell
    assert "What do you need from me?" in powershell
    assert "What happens next?" in powershell
    assert "Technical detail:" not in powershell
    assert "$($_.Exception.Message)" not in powershell
    assert "health_matches_instance" in bootstrap
    assert "launcher-evidence" in bootstrap
    assert "/run/research-supervisor-core/authority.sock" in bootstrap
    assert "git -C" not in bootstrap


def test_core_service_installer_declares_real_os_identity_and_store_permissions() -> None:
    installer = Path("scripts/install-core-authority-service.sh").read_text(encoding="utf-8")
    unit = Path("scripts/research-supervisor-core-authority.service").read_text(encoding="utf-8")
    assert "useradd --system" in installer
    assert "research-supervisor-core" in installer
    assert "/var/lib/research-supervisor-core/authority" in installer
    assert "-m 0700" in installer
    assert installer.index("umask 022") < installer.index('if [ "$(id -u)" -ne 0 ]')
    assert (
        "install -d -o research-supervisor-core "
        "-g research-supervisor-custodian -m 0711" in installer
    )
    assert (
        "install -d -o research-supervisor-core "
        "-g research-supervisor-custodian -m 0700" in installer
    )
    assert "-g research-supervisor-core" not in installer
    assert "User=research-supervisor-core" in unit
    assert "Group=research-supervisor-custodian" in unit
    assert "RuntimeDirectoryMode=0750" in unit
    assert "CapabilityBoundingSet=" not in unit
    assert "AmbientCapabilities=" not in unit
    assert "NoNewPrivileges=true" in unit
    assert "RestrictSUIDSGID=true" in unit
    assert "ProtectSystem=strict" in unit
    assert "UMask=0077" in unit
    assert (
        "-g research-supervisor-custodian -m 2710" in installer
        and "/var/lib/research-supervisor-core/snapshots/workspaces" in installer
    )
    assert "systemctl enable research-supervisor-core-authority.service" in installer
    assert "systemctl restart research-supervisor-core-authority.service" in installer
    assert "systemctl enable --now" not in installer


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _isolated_installer(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    application_root = tmp_path / "opt/research-supervisor-core"
    state_root = tmp_path / "var/lib/research-supervisor-core"
    configuration_root = tmp_path / "etc/research-supervisor-core"
    unit_path = tmp_path / "etc/systemd/system/research-supervisor-core-authority.service"
    unit_path.parent.mkdir(parents=True)

    source = Path("scripts/install-core-authority-service.sh").read_text(encoding="utf-8")
    source = source.replace("/opt/research-supervisor-core", str(application_root))
    source = source.replace("/var/lib/research-supervisor-core", str(state_root))
    source = source.replace("/etc/research-supervisor-core", str(configuration_root))
    source = source.replace(
        "/etc/systemd/system/research-supervisor-core-authority.service",
        str(unit_path),
    )
    installer = tmp_path / "install-core-authority-service.sh"
    _write_executable(installer, source)

    shims = tmp_path / "shims"
    shims.mkdir()
    _write_executable(
        shims / "id",
        "#!/bin/sh\n"
        "if [ \"${1:-}\" = -u ]; then\n"
        "  if [ \"$#\" -eq 1 ]; then printf '0\\n'; else printf '1000\\n'; fi\n"
        "else\n"
        "  exec /usr/bin/id \"$@\"\n"
        "fi\n",
    )
    for command in ("getent", "usermod", "chown", "systemctl"):
        _write_executable(shims / command, "#!/bin/sh\nexit 0\n")
    _write_executable(
        shims / "install",
        "#!/usr/bin/python3\n"
        "import os\n"
        "import sys\n"
        "arguments = []\n"
        "index = 1\n"
        "while index < len(sys.argv):\n"
        "    if sys.argv[index] in {'-o', '-g'}:\n"
        "        index += 2\n"
        "    else:\n"
        "        arguments.append(sys.argv[index])\n"
        "        index += 1\n"
        "os.execv('/usr/bin/install', ['install', *arguments])\n",
    )
    _write_executable(
        shims / "python3",
        "#!/bin/sh\n"
        "test \"$1\" = -m && test \"$2\" = venv\n"
        "mkdir -p \"$3/bin\"\n"
        "cp \"$PA5C4_FAKE_VENV_PYTHON\" \"$3/bin/python\"\n"
        "chmod 0755 \"$3/bin/python\"\n",
    )
    fake_venv_python = tmp_path / "fake-venv-python"
    _write_executable(
        fake_venv_python,
        "#!/bin/sh\n"
        "venv=$(CDPATH= cd -- \"$(dirname -- \"$0\")/..\" && pwd)\n"
        "site=$venv/lib/python-test/site-packages\n"
        "package=$site/research_automation_supervisor\n"
        "metadata=$site/research_automation_supervisor-0.2.0.dist-info\n"
        "mkdir -p \"$package/nested\" \"$metadata\"\n"
        "printf '%s\\n' '__version__ = \"0.2.0\"' >\"$package/__init__.py\"\n"
        "printf '%s\\n' 'QUALIFIED = True' >\"$package/gitless_repository.py\"\n"
        "printf '%s\\n' 'VALUE = 1' >\"$package/nested/module.py\"\n"
        "printf '%s\\n' 'Metadata-Version: 2.4' "
        "'Name: research-automation-supervisor' 'Version: 0.2.0' >\"$metadata/METADATA\"\n"
        "printf '%s\\n' '[console_scripts]' "
        "'research-supervisor-core-authority = qualified:main' "
        ">\"$metadata/entry_points.txt\"\n"
        "printf '%s\\n' '#!/bin/sh' 'exit 0' >\"$venv/bin/research-supervisor-core-authority\"\n"
        "chmod 0755 \"$venv/bin/research-supervisor-core-authority\"\n",
    )
    environment = {
        "PATH": f"{shims}:/usr/bin:/bin",
        "PA5C4_FAKE_VENV_PYTHON": str(fake_venv_python),
    }
    return installer, application_root, environment


@pytest.mark.parametrize("caller_umask", ("077", "027", "022"))
def test_core_service_installer_modes_are_independent_of_caller_umask(
    tmp_path: Path, caller_umask: str
) -> None:
    installer, application_root, environment = _isolated_installer(tmp_path)
    completed = subprocess.run(
        [
            "/bin/sh",
            "-c",
            f"umask {caller_umask}; exec /bin/sh \"$1\" \"$2\" qualified-operator",
            "installer-test",
            str(installer),
            str(Path.cwd()),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert completed.returncode == 0, completed.stderr

    venv = application_root / "venv"
    site = venv / "lib/python-test/site-packages"
    package = site / "research_automation_supervisor"
    metadata = site / "research_automation_supervisor-0.2.0.dist-info"
    for directory in (
        application_root,
        venv,
        venv / "bin",
        site,
        package,
        package / "nested",
        metadata,
    ):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o755
    for file in (
        package / "__init__.py",
        package / "gitless_repository.py",
        package / "nested/module.py",
        metadata / "METADATA",
        metadata / "entry_points.txt",
    ):
        assert stat.S_IMODE(file.stat().st_mode) == 0o644
        assert os.access(file, os.R_OK)
    for executable in (
        venv / "bin/python",
        venv / "bin/research-supervisor-core-authority",
    ):
        assert stat.S_IMODE(executable.stat().st_mode) == 0o755
        assert os.access(executable, os.X_OK)
    assert stat.S_IMODE((tmp_path / "var/lib/research-supervisor-core").stat().st_mode) == 0o711
    assert (
        stat.S_IMODE(
            (tmp_path / "var/lib/research-supervisor-core/authority").stat().st_mode
        )
        == 0o700
    )
    assert (
        stat.S_IMODE(
            (tmp_path / "var/lib/research-supervisor-core/snapshots").stat().st_mode
        )
        == 0o710
    )
    assert (
        stat.S_IMODE(
            (
                tmp_path
                / "var/lib/research-supervisor-core/snapshots/workspaces"
            ).stat().st_mode
        )
        == 0o2710
    )
    metadata_text = (metadata / "METADATA").read_text(encoding="utf-8")
    assert "Name: research-automation-supervisor" in metadata_text
    assert "Version: 0.2.0" in metadata_text


def test_core_service_installer_repairs_preexisting_restrictive_venv(
    tmp_path: Path,
) -> None:
    installer, application_root, environment = _isolated_installer(tmp_path)
    venv = application_root / "venv"
    python = venv / "bin/python"
    python.parent.mkdir(parents=True)
    python.write_bytes(Path(environment["PA5C4_FAKE_VENV_PYTHON"]).read_bytes())
    python.chmod(0o700)
    stale = venv / "lib/python-test/site-packages/stale_package"
    stale.mkdir(parents=True)
    stale_file = stale / "stale.py"
    stale_file.write_text("STALE = True\n", encoding="utf-8")
    stale_file.chmod(0o600)
    stale_executable = venv / "bin/stale-tool"
    stale_executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    stale_executable.chmod(0o700)
    (venv / "lib64").symlink_to("lib")
    for directory in (venv, venv / "bin", venv / "lib", stale.parent, stale):
        directory.chmod(0o700)

    completed = subprocess.run(
        [
            "/bin/sh",
            "-c",
            'umask 077; exec /bin/sh "$1" "$2" qualified-operator',
            "installer-test",
            str(installer),
            str(Path.cwd()),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert completed.returncode == 0, completed.stderr

    for directory in (venv, *[path for path in venv.rglob("*") if path.is_dir()]):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o755, str(directory)
    for path in (stale_file,):
        assert stat.S_IMODE(path.stat().st_mode) == 0o644
    for path in (python, stale_executable, venv / "bin/research-supervisor-core-authority"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o755
    assert (venv / "lib64").is_symlink()
    assert os.readlink(venv / "lib64") == "lib"
    assert (venv / "lib64").resolve(strict=True) == (venv / "lib").resolve(strict=True)


@pytest.mark.skipif(
    "microsoft" not in platform.release().casefold(), reason="real Windows launcher requires WSL"
)
def test_real_windows_vbs_launcher_reports_unavailable_wsl_in_plain_language(
    tmp_path: Path,
) -> None:
    launcher = Path("Research Supervisor.vbs").resolve(strict=True)
    wscript = Path("/mnt/c/Windows/System32/cscript.exe")
    if not wscript.is_file():
        pytest.fail("actual Windows Script Host is unavailable")
    evidence = tmp_path / "failure.json"
    completed = subprocess.run(
        [
            str(wscript),
            "//nologo",
            _windows_path(launcher),
            "-WslExecutable",
            r"Z:\missing\wsl.exe",
            "-FailureEvidence",
            _windows_path(evidence),
        ],
        check=False,
        timeout=60,
    )
    assert completed.returncode != 0
    value = json.loads(evidence.read_text(encoding="utf-8-sig"))
    assert value["title"] == "Research Supervisor needs attention"
    assert "could not start WSL" in value["message"]
    assert "No scientific campaign state changed" in value["message"]


def test_health_is_bound_to_one_random_readiness_instance(tmp_path: Path) -> None:
    custodian = CampaignCustodian(
        tmp_path / "data",
        runner=FakeQualifiedRunner(),
        environment_inspector=lambda _root: ready_environment(),
    )
    server = CustodianHTTPServer(
        ("127.0.0.1", 0),
        custodian,
        session_secret="session",
        readiness_instance="a" * 64,
    )
    try:
        assert server.readiness_instance == "a" * 64
        assert server.server_address[0] == "127.0.0.1"
    finally:
        server.server_close()
