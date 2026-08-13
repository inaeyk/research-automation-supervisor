#!/usr/bin/env python3
"""Independent PA-5C4 acceptance through Windows launcher and real Chrome only."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import platform
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.request import urlopen

from playwright.sync_api import Browser, Page, sync_playwright


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--evidence",
        type=Path,
        default=Path("docs/validation/pa5c4-real-browser-evidence.json"),
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    screenshot_root = args.evidence.parent / "pa5c4-real-browser"
    screenshot_root.mkdir(parents=True, exist_ok=True)
    screenshots: dict[str, str] = {}
    _require_real_windows_wsl(root)
    app_port = _available_port()
    debug_port = _available_port()
    relay_port = _available_port()
    windows_host = _windows_host_address()
    chrome = Path("/mnt/c/Program Files/Google/Chrome/Application/chrome.exe")
    powershell = Path("/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")
    wscript = Path("/mnt/c/Windows/System32/wscript.exe")
    run_root = Path(tempfile.mkdtemp(prefix="ras-pa5c4-real-browser-"))
    windows_profile = Path(tempfile.mkdtemp(prefix="ras-pa5c4-chrome-", dir="/mnt/c/Users/Public"))
    data_root = run_root / "application-data" / "research-automation-supervisor"
    scenario = run_root / "pa5c4-real-browser-scenario.json"
    scenario.write_text('{"schema_version":1}\n', encoding="ascii")
    repository = _repository(run_root)
    launcher = root / "Research Supervisor.vbs"
    launch_arguments = (
        "-DataRoot",
        _windows_path(data_root),
        "-Port",
        str(app_port),
        "-AcceptanceScenario",
        _windows_path(scenario),
        "-AcceptanceBrowserPath",
        str(chrome).replace("/mnt/c/", "C:\\").replace("/", "\\"),
        "-AcceptanceBrowserDebugPort",
        str(debug_port),
        "-AcceptanceBrowserDebugAddress",
        windows_host,
        "-AcceptanceBrowserRelayPort",
        str(relay_port),
        "-AcceptanceBrowserProfile",
        str(windows_profile).replace("/mnt/c/", "C:\\").replace("/", "\\"),
    )
    steps: list[str] = []
    browser: Browser | None = None
    backend_pid: int | None = None
    try:
        _launch(wscript, launcher, launch_arguments)
        debug_endpoint = f"http://{windows_host}:{relay_port}"
        initial_cdp = _wait_json(debug_endpoint + "/json/version", timeout=180)
        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(debug_endpoint)
            context = browser.contexts[0]
            page = _application_page(context.pages, app_port)
            page.wait_for_selector("text=Your research campaigns")
            steps.append("real_windows_launcher_opened_real_browser")
            page.get_by_role("button", name="New Campaign").first.click()
            page.locator("#repoLocator").fill(str(repository))
            page.get_by_role("button", name="Continue").last.click()
            page.locator("#contractText").fill("Frozen contract ORIGINAL-C4.\n")
            page.get_by_role("button", name="Continue").last.click()
            page.locator("#planText").fill("Frozen plan ORIGINAL-C4.\n")
            page.get_by_role("button", name="Continue").last.click()
            page.locator("#campaignName").fill("PA-5C4 real browser campaign")
            page.locator("#taskText").fill("Complete the deterministic acceptance task.\n")
            page.get_by_role("button", name="Continue").last.click()
            page.get_by_role("button", name="Preview").click()
            page.get_by_text("Ready to freeze").first.wait_for()
            page.get_by_role("button", name="Start Campaign").click()
            page.get_by_text("Local setup needs attention").wait_for()
            screenshots["environment_blocked"] = _screenshot(
                page, screenshot_root / "environment-blocked.png"
            )
            steps.append("environment_blocked_after_start")

            intent_before = _frozen_identity(data_root)
            readiness = _read_json(data_root / "custodian-state/backend-readiness.json")
            backend_pid = int(readiness["pid"])
            os.kill(backend_pid, signal.SIGTERM)
            _wait_process_exit(backend_pid)
            _launch(wscript, launcher, launch_arguments)
            _wait_health(app_port, timeout=180)
            steps.append("custodian_restarted")

            page.close()
            page = context.new_page()
            page.goto(f"http://127.0.0.1:{app_port}/")
            page.get_by_text("PA-5C4 real browser campaign").wait_for()
            page.get_by_text("PA-5C4 real browser campaign").click()
            page.get_by_text("Local setup needs attention").wait_for()
            steps.append("browser_restarted")
            if _frozen_identity(data_root) != intent_before:
                raise AssertionError("frozen launch intent changed across restart")
            steps.append("original_frozen_launch_intent_remained_authoritative")

            page.get_by_role("button", name="Continue").click()
            page.get_by_text("Campaign interrupted safely").wait_for(timeout=30_000)
            page.get_by_text("acceptance_recoverable_interruption").wait_for(state="attached")
            screenshots["recoverable_interruption"] = _screenshot(
                page, screenshot_root / "recoverable-interruption.png"
            )
            steps.append("recoverable_campaign_interruption_observed")
            page.get_by_role("button", name="Continue").click()
            page.get_by_text("Action needed").wait_for(timeout=30_000)
            screenshots["human_action"] = _screenshot(page, screenshot_root / "human-action.png")
            steps.append("human_action_observed")
            with page.expect_popup() as evidence_popup:
                page.get_by_role("link", name="Independent audit evidence").click()
            evidence_page = evidence_popup.value
            evidence_page.wait_for_load_state()
            if "frozen authority intact" not in evidence_page.locator("body").inner_text():
                raise AssertionError("human evidence did not load")
            evidence_page.close()
            steps.append("human_evidence_inspected")
            page.locator('input[value="continue_existing"]').click()
            page.locator("#responseNote").fill("Continue under the original frozen authority.")
            page.get_by_role("button", name="Submit response").click()
            page.get_by_text("Verified result", exact=True).wait_for(timeout=30_000)
            screenshots["completed"] = _screenshot(page, screenshot_root / "completed.png")
            steps.append("verified_durable_completion_observed")
            with page.expect_popup() as report_popup:
                page.get_by_role("link", name="Final report").click()
            report_page = report_popup.value
            report_page.wait_for_load_state()
            if (
                "Verified durable qualification completion"
                not in report_page.locator("body").inner_text()
            ):
                raise AssertionError("final report did not load")
            report_page.close()
            steps.append("final_report_opened")
            page.get_by_role("button", name="Export Campaign Bundle").click()
            exported = _wait_export(data_root, timeout=30)
            if exported.read_bytes()[:2] != b"PK":
                raise AssertionError("result export is not a ZIP bundle")
            steps.append("result_bundle_exported")

            _launch(wscript, launcher, launch_arguments)
            launcher_evidence = _launcher_evidence(data_root)
            if not any(bool(item["backend_reused"]) for item in launcher_evidence):
                raise AssertionError("already-running backend was not reused")
            steps.append("already_running_backend_reused")
            cdp_version = initial_cdp
            browser.close()
            browser = None

        failure_path = run_root / "wsl-failure.json"
        completed = subprocess.run(
            [
                str(powershell),
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                _windows_path(root / "launch-research-supervisor.ps1"),
                "-WslExecutable",
                "Z:\\missing\\wsl.exe",
                "-FailureEvidence",
                _windows_path(failure_path),
            ],
            check=False,
            timeout=60,
        )
        failure = _read_json(failure_path)
        if completed.returncode == 0 or "No scientific campaign state changed" not in str(
            failure["message"]
        ):
            raise AssertionError("WSL failure path was not intelligible and fail-closed")
        steps.append("wsl_unavailable_failure_reported")

        intent_after = _frozen_identity(data_root)
        evidence = {
            "schema_version": 1,
            "stage": "PA-5C4-T",
            "qualified": True,
            "windows_launcher": "Research Supervisor.vbs",
            "windows_execution_path": True,
            "wsl_kernel": platform.release(),
            "browser_engine": cdp_version.get("Browser"),
            "browser_user_agent": cdp_version.get("User-Agent"),
            "operator_actions_via_browser_ui_only": True,
            "backend_api_used_for_operator_actions": False,
            "launch_intent_sha256": intent_after["intent_sha256"],
            "launch_receipt_sha256": intent_after["receipt_sha256"],
            "launcher_evidence": launcher_evidence,
            "screenshots": screenshots,
            "steps": steps,
        }
        args.evidence.parent.mkdir(parents=True, exist_ok=True)
        args.evidence.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
        print(json.dumps(evidence, indent=2, sort_keys=True))
        return 0
    finally:
        if browser is not None:
            with contextlib.suppress(Exception):
                browser.close()
        readiness_path = data_root / "custodian-state/backend-readiness.json"
        if readiness_path.exists():
            try:
                current = int(_read_json(readiness_path)["pid"])
                os.kill(current, signal.SIGTERM)
                _wait_process_exit(current)
            except (KeyError, OSError, ValueError):
                pass
        _stop_windows_acceptance(windows_profile, relay_port)
        # The Windows profile contains only qualification browser state.
        shutil.rmtree(windows_profile, ignore_errors=True)


def _require_real_windows_wsl(root: Path) -> None:
    if "microsoft" not in platform.release().casefold():
        raise SystemExit("PA-5C4 does not qualify: this is not WSL")
    required = (
        root / "Research Supervisor.vbs",
        Path("/mnt/c/Windows/System32/wscript.exe"),
        Path("/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"),
        Path("/mnt/c/Program Files/Google/Chrome/Application/chrome.exe"),
    )
    if any(not path.is_file() for path in required):
        raise SystemExit("PA-5C4 does not qualify: Windows launcher/browser is unavailable")


def _repository(root: Path) -> Path:
    repository = root / "test-repository"
    repository.mkdir()
    (repository / "README.md").write_text("# PA-5C4 browser repository\n", encoding="utf-8")
    for arguments in (
        ("init", "-q"),
        ("config", "user.name", "PA-5C4 Acceptance"),
        ("config", "user.email", "pa5c4@localhost.invalid"),
        ("add", "."),
        ("commit", "-q", "-m", "baseline"),
    ):
        subprocess.run(["git", "-C", str(repository), *arguments], check=True)
    return repository


def _launch(wscript: Path, launcher: Path, arguments: tuple[str, ...]) -> None:
    completed = subprocess.run(
        [str(wscript), _windows_path(launcher), *arguments],
        check=False,
        timeout=240,
    )
    if completed.returncode != 0:
        raise AssertionError(f"Windows launcher failed with {completed.returncode}")


def _application_page(pages: list[Page], port: int) -> Page:
    expected = f"http://127.0.0.1:{port}/"
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        for page in pages:
            if page.url == expected:
                return page
        time.sleep(0.2)
    raise AssertionError("Windows launcher did not open the local UI")


def _wait_health(port: int, *, timeout: int) -> dict[str, object]:
    return _wait_json(f"http://127.0.0.1:{port}/api/health", timeout=timeout)


def _wait_json(url: str, *, timeout: int) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urlopen(url, timeout=1) as response:
                value = json.load(response)
            if isinstance(value, dict):
                return value
        except OSError:
            time.sleep(0.25)
    raise AssertionError(f"timed out waiting for {url}")


def _windows_host_address() -> str:
    output = subprocess.run(
        ["ip", "route", "show", "default"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    if len(output) < 3 or output[0:2] != ["default", "via"]:
        raise AssertionError("Windows host address could not be identified")
    return output[2]


def _frozen_identity(data_root: Path) -> dict[str, str]:
    authority = data_root.parent / "research-automation-supervisor-core/prelaunch-authority"
    receipts = sorted((authority / "receipts").glob("*.json"))
    if len(receipts) != 1:
        raise AssertionError("expected exactly one frozen launch receipt")
    receipt = _read_json(receipts[0])
    intent_sha = str(receipt["launch_intent_sha256"])
    intent = authority / "objects" / intent_sha[:2] / f"{intent_sha}.json"
    return {
        "intent_sha256": intent_sha,
        "intent_file_sha256": hashlib.sha256(intent.read_bytes()).hexdigest(),
        "receipt_sha256": str(receipt["receipt_sha256"]),
        "receipt_file_sha256": hashlib.sha256(receipts[0].read_bytes()).hexdigest(),
    }


def _launcher_evidence(data_root: Path) -> list[dict[str, object]]:
    values = [
        _read_json(path)
        for path in sorted((data_root / "custodian-state/launcher-evidence").glob("*.json"))
    ]
    if len(values) < 3 or not all(bool(item["windows_execution_path"]) for item in values):
        raise AssertionError("real Windows launcher evidence is incomplete")
    return values


def _wait_process_exit(pid: int) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.1)
    raise AssertionError("Custodian backend did not stop")


def _wait_export(data_root: Path, *, timeout: int) -> Path:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        exports = tuple((data_root / "exports").glob("campaign-*.zip"))
        if len(exports) == 1 and exports[0].is_file():
            return exports[0]
        time.sleep(0.1)
    raise AssertionError("UI export did not produce the result bundle")


def _available_port() -> int:
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        return int(server.getsockname()[1])


def _windows_path(path: Path) -> str:
    return subprocess.run(
        ["wslpath", "-w", str(path)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _stop_windows_acceptance(profile: Path, relay_port: int) -> None:
    profile_text = str(profile).replace("/mnt/c/", "C:\\").replace("/", "\\")
    command = (
        "$self=$PID; $profile=$env:RAS_ACCEPTANCE_PROFILE; "
        "$relay=$env:RAS_ACCEPTANCE_RELAY; "
        "Get-CimInstance Win32_Process | Where-Object { "
        "$_.ProcessId -ne $self -and ("
        "($_.Name -eq 'chrome.exe' -and $_.CommandLine -like ('*'+$profile+'*')) -or "
        "($_.Name -eq 'powershell.exe' -and "
        "$_.CommandLine -like ('*windows-cdp-relay.ps1*'+$relay+'*'))) "
        "} | ForEach-Object {Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue}"
    )
    subprocess.run(
        [
            "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe",
            "-NoProfile",
            "-Command",
            command,
        ],
        check=False,
        timeout=30,
        env={
            **os.environ,
            "RAS_ACCEPTANCE_PROFILE": profile_text,
            "RAS_ACCEPTANCE_RELAY": str(relay_port),
        },
    )


def _read_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise AssertionError(f"{path} is not a JSON object")
    return value


def _screenshot(page: Page, path: Path) -> str:
    page.screenshot(path=path, full_page=True)
    return hashlib.sha256(path.read_bytes()).hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
