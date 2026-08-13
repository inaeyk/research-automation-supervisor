from __future__ import annotations

import json
import platform
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
    assert "No scientific campaign state changed" in powershell
    assert "health_matches_instance" in bootstrap
    assert "launcher-evidence" in bootstrap


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
    assert "could not be launched through WSL" in value["message"]
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
