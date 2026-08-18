#!/usr/bin/env python3
"""PA-5C4-U acceptance through Explorer launcher semantics and browser UI only."""

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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.request import urlopen

from playwright.sync_api import Browser, Page, sync_playwright

from research_automation_supervisor.durable_state import canonical_json
from research_automation_supervisor.ux_acceptance import (
    UXAcceptanceEvidenceV1,
    UXBackendRestartEvidenceV1,
    UXBrowserProcessEvidenceV1,
    UXCandidateTreeFingerprintV1,
    UXCompletionEvidenceV1,
    UXFileIdentityV1,
    UXHumanActionEvidenceV1,
    UXIdentityV1,
    UXInteractionV1,
    UXLauncherInvocationV1,
    UXNotificationEvidenceV1,
)


class Transcript:
    def __init__(self) -> None:
        self.items: list[UXInteractionV1] = []

    def add(
        self,
        action: str,
        visible_result: str,
        *,
        screenshot_sha256: str | None = None,
    ) -> None:
        self.items.append(
            UXInteractionV1(
                sequence=len(self.items) + 1,
                observed_at=_now(),
                action=action,
                visible_result=visible_result,
                screenshot_sha256=screenshot_sha256,
            )
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--evidence",
        type=Path,
        default=Path("docs/validation/pa5c4u-real-operator-evidence.json"),
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    _require_real_windows_wsl(root)
    candidate = _candidate_tree(root)
    identities = _executable_identities(root)
    evidence_path = args.evidence.resolve()
    screenshot_root = evidence_path.parent / "pa5c4u-real-operator"
    screenshot_root.mkdir(parents=True, exist_ok=True)
    screenshots: dict[str, str] = {}
    transcript = Transcript()
    app_port = _available_port()
    debug_port = _windows_available_port()
    relay_port = _windows_available_port()
    while relay_port == debug_port:
        relay_port = _windows_available_port()
    windows_host = _windows_host_address()
    chrome = Path("/mnt/c/Program Files/Google/Chrome/Application/chrome.exe")
    wscript = Path("/mnt/c/Windows/System32/wscript.exe")
    run_root = Path(tempfile.mkdtemp(prefix="ras-pa5c4u-real-operator-"))
    profile = Path(tempfile.mkdtemp(prefix="ras-pa5c4u-chrome-", dir="/mnt/c/Users/Public"))
    data_root = run_root / "application-data/research-automation-supervisor"
    scenario = run_root / "pa5c4-real-browser-scenario.json"
    scenario.write_text('{"schema_version":1}\n', encoding="ascii")
    repository = _repository(run_root)
    response_file = run_root / "operator-evidence.txt"
    response_file.write_text("Operator reviewed the frozen evidence.\n", encoding="utf-8")
    launcher = root / "Research Supervisor.vbs"
    common_arguments = (
        "-DataRoot",
        _windows_path(data_root),
        "-Port",
        str(app_port),
        "-AcceptanceScenario",
        _windows_path(scenario),
    )
    controlled_arguments = (
        *common_arguments,
        "-AcceptanceBrowserPath",
        _windows_path(chrome),
        "-AcceptanceBrowserDebugPort",
        str(debug_port),
        "-AcceptanceBrowserDebugAddress",
        windows_host,
        "-AcceptanceBrowserRelayPort",
        str(relay_port),
        "-AcceptanceBrowserProfile",
        _windows_path(profile),
    )
    browser: Browser | None = None
    backend_initial_pid = 0
    backend_restarted_pid = 0
    browser_initial_pids: tuple[int, ...] = ()
    browser_restarted_pids: tuple[int, ...] = ()
    exported: Path | None = None
    default_window: dict[str, Any] = {}
    failure_title = "Research Supervisor needs attention"
    failure_dialog_hash = ""
    try:
        # This invocation deliberately supplies no browser override.  It traverses the
        # exact Explorer/VBS -> PowerShell -> Start-Process $Url production path.
        _launch(wscript, launcher, common_arguments)
        default_window = _wait_window_title("Research Supervisor", timeout=60)
        default_capture = screenshot_root / "default-browser-opened.png"
        _windows_screenshot(default_capture, pid=int(default_window["pid"]))
        screenshots["default_browser_opened"] = _sha256(default_capture)
        transcript.add(
            "Double-click Research Supervisor.vbs using Windows Explorer semantics",
            (
                "The Windows default browser displayed Research Supervisor "
                f"({default_window['title']})."
            ),
            screenshot_sha256=screenshots["default_browser_opened"],
        )

        # A second launcher invocation attaches a qualification-only CDP seam to a
        # real Windows Chrome process; operator actions remain ordinary UI actions.
        _launch(wscript, launcher, controlled_arguments)
        endpoint = f"http://{windows_host}:{relay_port}"
        cdp_version = _wait_json(endpoint + "/json/version", timeout=180)
        browser_initial_pids = _profile_browser_pids(profile)
        if not browser_initial_pids:
            raise AssertionError("controlled real browser process was not observed")
        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(endpoint)
            context = browser.contexts[0]
            context.grant_permissions(
                ["notifications"], origin=f"http://127.0.0.1:{app_port}"
            )
            page = _application_page(context.pages, app_port)
            page.get_by_text("Your research campaigns").wait_for()
            page.get_by_role("button", name="Enable notifications").click()
            page.get_by_text("Notifications enabled.").wait_for()
            transcript.add(
                "Enable browser notifications",
                "The visible browser status confirmed that notifications are enabled.",
            )

            page.get_by_role("button", name="New Campaign").first.click()
            page.locator("#repoLocator").fill(str(repository))
            page.get_by_role("button", name="Continue").last.click()
            page.locator("#contractText").fill("Frozen contract ORIGINAL-C4-U.\n")
            page.get_by_role("button", name="Continue").last.click()
            page.locator("#planText").fill("Frozen plan ORIGINAL-C4-U.\n")
            page.get_by_role("button", name="Continue").last.click()
            page.locator("#campaignName").fill("PA-5C4-U real operator campaign")
            page.locator("#taskText").fill("Complete the deterministic acceptance task.\n")
            page.get_by_role("button", name="Continue").last.click()
            page.get_by_role("button", name="Preview").click()
            page.get_by_text("Ready to freeze").first.wait_for()
            preview_capture = screenshot_root / "human-readable-preview.png"
            screenshots["human_readable_preview"] = _screenshot(page, preview_capture)
            transcript.add(
                "Complete New Campaign and choose Preview",
                (
                    "The browser showed a plain-language repository, contract, plan, "
                    "task, and environment preview."
                ),
                screenshot_sha256=screenshots["human_readable_preview"],
            )

            page.get_by_role("button", name="Start Campaign").click()
            page.get_by_text("Local setup needs attention").wait_for()
            page.get_by_text("What happened?").wait_for()
            blocked_capture = screenshot_root / "environment-blocked.png"
            screenshots["environment_blocked"] = _screenshot(page, blocked_capture)
            transcript.add(
                "Choose Start",
                (
                    "Start froze the inputs and displayed a plain-language environment "
                    "action card before execution."
                ),
                screenshot_sha256=screenshots["environment_blocked"],
            )
            frozen_before = _frozen_identity(data_root)

            readiness = _read_json(data_root / "custodian-state/backend-readiness.json")
            backend_initial_pid = _required_int(readiness, "pid")
            _terminate_backend(backend_initial_pid)
            _launch(wscript, launcher, controlled_arguments)
            _wait_health(app_port, timeout=180)
            backend_restarted_pid = _required_int(
                _read_json(data_root / "custodian-state/backend-readiness.json"), "pid"
            )
            if backend_restarted_pid == backend_initial_pid:
                raise AssertionError("Custodian backend PID did not change")
            transcript.add(
                "Terminate and relaunch the Custodian backend",
                "The launcher started a new Custodian process and preserved the frozen campaign.",
            )

            _terminate_profile_browser(profile)
            _wait_endpoint_absent(endpoint + "/json/version", timeout=30)
            transcript.add(
                "Terminate the Windows browser process",
                (
                    "Every acceptance-profile browser process exited; no page or tab "
                    "substitution was used."
                ),
            )
            browser = None
            _launch(wscript, launcher, controlled_arguments)
            _wait_json(endpoint + "/json/version", timeout=180)
            browser_restarted_pids = _profile_browser_pids(profile)
            if not browser_restarted_pids or set(browser_initial_pids) & set(
                browser_restarted_pids
            ):
                raise AssertionError("real browser process did not restart with new identities")
            browser = playwright.chromium.connect_over_cdp(endpoint)
            context = browser.contexts[0]
            context.grant_permissions(
                ["notifications"], origin=f"http://127.0.0.1:{app_port}"
            )
            page = _application_page(context.pages, app_port)
            page.get_by_text("PA-5C4-U real operator campaign").wait_for()
            page.get_by_text("PA-5C4-U real operator campaign").click()
            page.get_by_text("Local setup needs attention").wait_for()
            frozen_after = _frozen_identity(data_root)
            if frozen_after != frozen_before:
                raise AssertionError("frozen authority changed across process restarts")
            restarted_capture = screenshot_root / "browser-and-backend-restarted.png"
            screenshots["browser_and_backend_restarted"] = _screenshot(
                page, restarted_capture
            )
            transcript.add(
                "Relaunch Research Supervisor after browser termination",
                (
                    "A new real browser process reopened the durable blocked campaign "
                    "without a run ID or shell command."
                ),
                screenshot_sha256=screenshots["browser_and_backend_restarted"],
            )

            page.get_by_role("button", name="Continue").click()
            page.get_by_text("Campaign interrupted safely").wait_for(timeout=30_000)
            interruption_capture = screenshot_root / "recoverable-interruption.png"
            screenshots["recoverable_interruption"] = _screenshot(
                page, interruption_capture
            )
            transcript.add(
                "Choose Continue after environment recovery",
                (
                    "The browser explained the recoverable interruption and offered "
                    "safe Resume through Continue."
                ),
                screenshot_sha256=screenshots["recoverable_interruption"],
            )

            page.get_by_role("button", name="Continue").click()
            page.get_by_text("Action needed").wait_for(timeout=30_000)
            page.get_by_text("What happens after I answer?").wait_for()
            action_capture = screenshot_root / "human-action-inbox.png"
            screenshots["human_action_inbox"] = _screenshot(page, action_capture)
            transcript.add(
                "Resume through the browser UI",
                (
                    "The Human Action Inbox explained what happened, what was needed, "
                    "and what would happen next."
                ),
                screenshot_sha256=screenshots["human_action_inbox"],
            )

            with page.expect_popup() as popup_info:
                page.get_by_role("link", name="Independent audit evidence").click()
            evidence_page = popup_info.value
            evidence_page.wait_for_load_state()
            evidence_text = evidence_page.locator("body").inner_text()
            if "frozen authority intact" not in evidence_text:
                raise AssertionError("safe Human Action evidence was not visible")
            safe_evidence_capture = screenshot_root / "safe-human-action-evidence.png"
            screenshots["safe_human_action_evidence"] = _screenshot(
                evidence_page, safe_evidence_capture
            )
            evidence_page.close()
            transcript.add(
                "Open relevant safe evidence",
                (
                    "The browser displayed independent evidence that the frozen "
                    "authority remained intact."
                ),
                screenshot_sha256=screenshots["safe_human_action_evidence"],
            )
            page.locator('input[value="continue_existing"]').click()
            page.locator("#responseNote").fill(
                "Approve and continue under the original frozen authority."
            )
            page.locator("#responseFiles").set_input_files(response_file)
            page.get_by_role("button", name="Submit response").click()
            page.get_by_text("Verified result", exact=True).wait_for(timeout=30_000)
            page.get_by_text("Scientific Report", exact=True).wait_for()
            page.get_by_text("Worker Reports", exact=True).wait_for()
            page.get_by_text("Auditor Reports", exact=True).wait_for()
            page.get_by_text("Changed Files / Diff", exact=True).wait_for()
            page.get_by_text("Provenance", exact=True).wait_for()
            page.locator("#browserNotice").get_by_text("Campaign completed").wait_for(
                timeout=15_000
            )
            completed_capture = screenshot_root / "completion-notification-and-results.png"
            screenshots["completion_notification_and_results"] = _screenshot(
                page, completed_capture
            )
            transcript.add(
                "Submit approval, choice, free-text note, and file through the UI",
                (
                    "The browser displayed a Campaign completed notification and "
                    "verified result counts, stage, commit, reports, and summary."
                ),
                screenshot_sha256=screenshots["completion_notification_and_results"],
            )

            with page.expect_popup() as report_popup:
                page.get_by_role("link", name="Scientific Report").click()
            report_page = report_popup.value
            report_page.wait_for_load_state()
            if "Verified durable qualification completion" not in report_page.locator(
                "body"
            ).inner_text():
                raise AssertionError("final scientific report did not load")
            report_capture = screenshot_root / "scientific-report-opened.png"
            screenshots["scientific_report_opened"] = _screenshot(
                report_page, report_capture
            )
            report_page.close()
            transcript.add(
                "Open Scientific Report",
                (
                    "The final report opened in the real browser and showed verified "
                    "durable completion."
                ),
                screenshot_sha256=screenshots["scientific_report_opened"],
            )

            page.get_by_role("button", name="Export Campaign Bundle").click()
            page.locator("#browserNotice").get_by_text(
                "Campaign bundle ready", exact=True
            ).wait_for(timeout=15_000)
            exported = _wait_export(data_root, timeout=30)
            if exported.read_bytes()[:2] != b"PK":
                raise AssertionError("export is not a ZIP campaign bundle")
            export_capture = screenshot_root / "campaign-bundle-exported.png"
            screenshots["campaign_bundle_exported"] = _screenshot(page, export_capture)
            transcript.add(
                "Choose Export Campaign Bundle",
                "The UI visibly confirmed the downloadable ZIP result bundle.",
                screenshot_sha256=screenshots["campaign_bundle_exported"],
            )

            # Final invocation proves safe reuse after durable completion.
            _launch(wscript, launcher, controlled_arguments)
            browser.close()
            browser = None

        failure_process = subprocess.Popen(
            [
                str(wscript),
                _windows_path(launcher),
                "-WslExecutable",
                r"Z:\missing\wsl.exe",
            ]
        )
        failure_window = _wait_window_title(failure_title, timeout=30)
        failure_capture = screenshot_root / "wsl-backend-failure-dialog.png"
        _windows_screenshot(failure_capture, pid=int(failure_window["pid"]))
        failure_dialog_hash = _sha256(failure_capture)
        screenshots["wsl_backend_failure_dialog"] = failure_dialog_hash
        _close_window_process(int(failure_window["pid"]))
        failure_process.wait(timeout=30)
        if failure_process.returncode == 0:
            raise AssertionError("WSL failure dialog invocation unexpectedly succeeded")
        transcript.add(
            "Double-click launcher with WSL unavailable",
            (
                f"Windows displayed the real '{failure_title}' dialog and kept campaign "
                "state unchanged."
            ),
            screenshot_sha256=failure_dialog_hash,
        )

        if exported is None:
            raise AssertionError("campaign bundle was not exported")
        launcher_records = _launcher_evidence(data_root)
        if not any(bool(item[1]["backend_reused"]) for item in launcher_records):
            raise AssertionError("launcher never reused the already-running backend")
        request, response = _human_action_identities(data_root)
        completion_file = _single_file(
            data_root / "qualified-campaigns", "*/acceptance-state.json"
        )
        completion_value = _read_json(completion_file)
        if completion_value.get("phase") != "completed":
            raise AssertionError("durable completion state is absent")
        completion_notification = _single_file(
            data_root / "operator-exchange", "*/notifications/campaign_completed.json"
        )
        notification_value = _read_json(completion_notification)
        if notification_value.get("completion_verified") is not True:
            raise AssertionError("durable completion notification is not verified")
        campaign_public_id = str(notification_value["campaign_public_id"])
        frozen_digest = _identity_digest(frozen_before)
        evidence = UXAcceptanceEvidenceV1.bind(
            qualified=True,
            branch=_git(root, "branch", "--show-current"),
            head=_git(root, "rev-parse", "HEAD"),
            candidate_tree=candidate.model_dump(mode="python"),
            executable_identities=tuple(item.model_dump(mode="python") for item in identities),
            windows_version=_windows_version(),
            wsl_version=_wsl_version(),
            browser_name_version=str(cdp_version["Browser"]),
            transcript=tuple(item.model_dump(mode="python") for item in transcript.items),
            launcher_invocations=tuple(
                UXLauncherInvocationV1(
                    sequence=index,
                    observed_at=observed_at,
                    launcher=str(value["launcher"]),
                    exit_code=0,
                    backend_reused=bool(value["backend_reused"]),
                    readiness_instance=str(value["requested_readiness_instance"]),
                ).model_dump(mode="python")
                for index, (observed_at, value) in enumerate(launcher_records, 1)
            ),
            browser_process=UXBrowserProcessEvidenceV1(
                executable=_windows_path(chrome),
                initial_pids=browser_initial_pids,
                terminated_pids=browser_initial_pids,
                terminated_processes_absent=True,
                restarted_pids=browser_restarted_pids,
                default_browser_path_exercised=True,
            ).model_dump(mode="python"),
            backend_restart=UXBackendRestartEvidenceV1(
                initial_pid=backend_initial_pid,
                terminated_pid=backend_initial_pid,
                restarted_pid=backend_restarted_pid,
                old_process_absent=True,
                frozen_identity_before_sha256=frozen_digest,
                frozen_identity_after_sha256=_identity_digest(frozen_after),
            ).model_dump(mode="python"),
            failure_dialog_title=failure_title,
            failure_dialog_screenshot_sha256=failure_dialog_hash,
            human_action=UXHumanActionEvidenceV1(
                request_sha256=str(request["request_sha256"]),
                response_sha256=str(response["response_sha256"]),
                response_type=str(request["response_type"]),  # type: ignore[arg-type]
                safe_evidence_opened=True,
                submitted_through_ui=True,
            ).model_dump(mode="python"),
            completion=UXCompletionEvidenceV1(
                campaign_public_id=campaign_public_id,
                completion_state_sha256=_sha256(completion_file),
                completion_verified=True,
                final_report_opened=True,
            ).model_dump(mode="python"),
            notification=UXNotificationEvidenceV1(
                mechanism="browser-notification-and-visible-status",
                permission="granted",
                title="Campaign completed",
                visible_text=(
                    "Campaign completed — PA-5C4-U real operator campaign — "
                    "Verified durable completion and result evidence are available"
                ),
                screenshot_sha256=screenshots["completion_notification_and_results"],
                durable_notification_sha256=_sha256(completion_notification),
            ).model_dump(mode="python"),
            screenshot_hashes=screenshots,
            exported_bundle_sha256=_sha256(exported),
        )
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        evidence_path.write_text(
            json.dumps(evidence.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(evidence.model_dump(mode="json"), indent=2, sort_keys=True))
        return 0
    finally:
        if browser is not None:
            with contextlib.suppress(Exception):
                browser.close()
        readiness_path = data_root / "custodian-state/backend-readiness.json"
        if readiness_path.exists():
            with contextlib.suppress(KeyError, OSError, ValueError):
                _terminate_backend(_required_int(_read_json(readiness_path), "pid"))
        with contextlib.suppress(Exception):
            _terminate_profile_browser(profile)
        _stop_relay(relay_port)
        profile_root = Path("/mnt/c/Users/Public").resolve()
        resolved_profile = profile.resolve()
        if resolved_profile.parent == profile_root and resolved_profile.name.startswith(
            "ras-pa5c4u-chrome-"
        ):
            shutil.rmtree(resolved_profile, ignore_errors=True)


def _candidate_tree(root: Path) -> UXCandidateTreeFingerprintV1:
    tracked = set(_git_z(root, "ls-files", "-z"))
    untracked = set(_git_z(root, "ls-files", "--others", "--exclude-standard", "-z"))
    modified = set(_git_z(root, "diff", "--name-only", "-z")) | set(
        _git_z(root, "diff", "--cached", "--name-only", "-z")
    )
    files: list[UXFileIdentityV1] = []
    for relative in sorted(tracked | untracked):
        path = root / relative
        if not path.is_file() or path.is_symlink():
            continue
        if relative in untracked:
            state = "untracked"
        elif relative in modified:
            state = "modified"
        else:
            state = "tracked"
        files.append(
            UXFileIdentityV1(
                path=relative,
                sha256=_sha256(path),
                size=path.stat().st_size,
                git_state=state,  # type: ignore[arg-type]
            )
        )
    manifest = [
        {"path": item.path, "sha256": item.sha256, "size": item.size} for item in files
    ]
    return UXCandidateTreeFingerprintV1(
        sha256=hashlib.sha256(canonical_json(manifest)).hexdigest(),
        file_count=len(files),
        files=tuple(files),
    )


def _executable_identities(root: Path) -> tuple[UXIdentityV1, ...]:
    roles = (
        ("launcher", "Research Supervisor.vbs"),
        ("launcher_script", "launch-research-supervisor.ps1"),
        ("bootstrap", "scripts/custodian-bootstrap.sh"),
        ("ui_backend", "src/research_automation_supervisor/custodian_server.py"),
        ("core_seam", "src/research_automation_supervisor/core_authority_service.py"),
        ("ui_backend", "tests/pa5c4_acceptance_backend.py"),
    )
    return tuple(
        UXIdentityV1(role=role, path=relative, sha256=_sha256(root / relative))  # type: ignore[arg-type]
        for role, relative in roles
    )


def _require_real_windows_wsl(root: Path) -> None:
    required = (
        root / "Research Supervisor.vbs",
        Path("/mnt/c/Windows/System32/wscript.exe"),
        Path("/mnt/c/Program Files/Google/Chrome/Application/chrome.exe"),
    )
    if "microsoft" not in platform.release().casefold() or any(
        not path.is_file() for path in required
    ):
        raise SystemExit("PA-5C4-U requires real Windows, WSL, launcher, and Chrome")


def _repository(root: Path) -> Path:
    repository = root / "operator-repository"
    repository.mkdir()
    (repository / "README.md").write_text("# PA-5C4-U repository\n", encoding="utf-8")
    for arguments in (
        ("init", "-q"),
        ("config", "user.name", "PA-5C4-U Acceptance"),
        ("config", "user.email", "pa5c4u@localhost.invalid"),
        ("add", "."),
        ("commit", "-q", "-m", "baseline"),
    ):
        subprocess.run(["git", "-C", str(repository), *arguments], check=True)
    return repository


def _launch(wscript: Path, launcher: Path, arguments: tuple[str, ...]) -> None:
    completed = subprocess.run(
        [str(wscript), _windows_path(launcher), *arguments], check=False, timeout=240
    )
    if completed.returncode != 0:
        raise AssertionError(f"Windows VBS launcher failed with {completed.returncode}")


def _application_page(pages: list[Page], port: int) -> Page:
    expected = f"http://127.0.0.1:{port}/"
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        for page in pages:
            if page.url == expected:
                return page
        time.sleep(0.2)
    raise AssertionError("launcher did not open the application in the real browser")


def _terminate_backend(pid: int) -> None:
    command = Path(f"/proc/{pid}/cmdline")
    if not command.is_file() or b"pa5c4_acceptance_backend.py" not in command.read_bytes():
        raise AssertionError("refusing to terminate an unverified backend process")
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if not Path(f"/proc/{pid}").exists():
            return
        time.sleep(0.1)
    raise AssertionError("Custodian backend did not terminate")


def _profile_browser_pids(profile: Path) -> tuple[int, ...]:
    profile_windows = _windows_path(profile)
    script = (
        "$OutputEncoding=[Console]::OutputEncoding=[Text.UTF8Encoding]::new();"
        "$profile=$env:RAS_UX_PROFILE;"
        "$items=@(Get-CimInstance Win32_Process | Where-Object {"
        "$_.Name -eq 'chrome.exe' -and "
        "$_.CommandLine -like ('*--user-data-dir='+$profile+'*') -and "
        "$_.CommandLine -notlike '*--type=*'});"
        "@($items | ForEach-Object {[int]$_.ProcessId}) | ConvertTo-Json -Compress"
    )
    value = _powershell_json(script, env={"RAS_UX_PROFILE": profile_windows})
    if isinstance(value, int):
        return (value,)
    if not isinstance(value, list):
        return ()
    return tuple(sorted(int(item) for item in value))


def _terminate_profile_browser(profile: Path) -> None:
    pids = _profile_browser_pids(profile)
    if not pids:
        return
    pid_list = ",".join(str(pid) for pid in pids)
    script = (
        "$ErrorActionPreference='Stop';"
        "$profile=$env:RAS_UX_PROFILE;"
        f"$expected=@({pid_list});"
        "$items=@(Get-CimInstance Win32_Process | Where-Object {"
        "$_.Name -eq 'chrome.exe' -and "
        "$_.CommandLine -like ('*--user-data-dir='+$profile+'*') -and "
        "$_.CommandLine -notlike '*--type=*'});"
        "$actual=@($items | ForEach-Object {[int]$_.ProcessId});"
        "if(Compare-Object $expected $actual){throw 'browser process identity changed'};"
        "$items | ForEach-Object {Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop}"
    )
    _powershell(script, env={"RAS_UX_PROFILE": _windows_path(profile)})
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if not _profile_browser_pids(profile):
            return
        time.sleep(0.2)
    raise AssertionError("acceptance browser process did not terminate")


def _stop_relay(port: int) -> None:
    script = (
        "$ErrorActionPreference='Stop';$port=$env:RAS_UX_RELAY;"
        "$items=@(Get-CimInstance Win32_Process | Where-Object {"
        "$_.Name -eq 'powershell.exe' -and "
        "$_.CommandLine -like ('*windows-cdp-relay.ps1*'+$port+'*')});"
        "$items | ForEach-Object {Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop}"
    )
    with contextlib.suppress(subprocess.SubprocessError):
        _powershell(script, env={"RAS_UX_RELAY": str(port)})


def _wait_window_title(fragment: str, *, timeout: int) -> dict[str, Any]:
    script = (
        "$OutputEncoding=[Console]::OutputEncoding=[Text.UTF8Encoding]::new();"
        "$fragment=$env:RAS_UX_TITLE;"
        "$item=Get-Process | Where-Object {$_.MainWindowTitle -like ('*'+$fragment+'*')} | "
        "Select-Object -First 1;"
        "if($null -eq $item){'null'}else{@{pid=[int]$item.Id;title=$item.MainWindowTitle;"
        "process=$item.ProcessName}|ConvertTo-Json -Compress}"
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = _powershell_json(script, env={"RAS_UX_TITLE": fragment})
        if isinstance(value, dict):
            return value
        time.sleep(0.25)
    raise AssertionError(f"Windows window containing {fragment!r} was not visible")


def _close_window_process(pid: int) -> None:
    script = (
        "$ErrorActionPreference='Stop';$pidValue=[int]$env:RAS_UX_PID;"
        "$process=Get-Process -Id $pidValue -ErrorAction Stop;"
        "if(-not $process.MainWindowHandle){throw 'process has no visible window'};"
        "if(-not $process.CloseMainWindow()){throw 'visible dialog did not accept close'}"
    )
    _powershell(script, env={"RAS_UX_PID": str(pid)})


def _windows_screenshot(destination: Path, *, pid: int) -> None:
    script = (
        "$ErrorActionPreference='Stop';Add-Type -AssemblyName System.Drawing;"
        "Add-Type -TypeDefinition 'using System; using System.Runtime.InteropServices; "
        "public static class RASWindowCapture { "
        "[StructLayout(LayoutKind.Sequential)] public struct RECT { "
        "public int Left; public int Top; public int Right; public int Bottom; } "
        "[DllImport(\"user32.dll\")] public static extern bool SetProcessDPIAware(); "
        "[DllImport(\"user32.dll\")] public static extern bool GetWindowRect("
        "IntPtr hWnd, out RECT rect); "
        "[DllImport(\"user32.dll\")] public static extern bool PrintWindow("
        "IntPtr hWnd, IntPtr hdc, uint flags); }';"
        "[void][RASWindowCapture]::SetProcessDPIAware();"
        "$process=Get-Process -Id ([int]$env:RAS_UX_PID) -ErrorAction Stop;"
        "$handle=$process.MainWindowHandle;"
        "if($handle -eq [IntPtr]::Zero){throw 'target process has no visible window'};"
        "$rect=New-Object RASWindowCapture+RECT;"
        "if(-not [RASWindowCapture]::GetWindowRect($handle,[ref]$rect)){"
        "throw 'could not read target window bounds'};"
        "$width=$rect.Right-$rect.Left;$height=$rect.Bottom-$rect.Top;"
        "if($width -lt 200 -or $height -lt 100){throw 'target window is too small'};"
        "$bitmap=New-Object Drawing.Bitmap $width,$height;"
        "$graphics=[Drawing.Graphics]::FromImage($bitmap);$hdc=$graphics.GetHdc();"
        "try{$captured=[RASWindowCapture]::PrintWindow($handle,$hdc,2)}"
        "finally{$graphics.ReleaseHdc($hdc)};"
        "if(-not $captured){throw 'PrintWindow failed'};"
        "$colors=New-Object 'System.Collections.Generic.HashSet[int]';"
        "for($x=0;$x -lt $width;$x+=[Math]::Max(1,[int]($width/24))){"
        "for($y=0;$y -lt $height;$y+=[Math]::Max(1,[int]($height/24))){"
        "[void]$colors.Add($bitmap.GetPixel($x,$y).ToArgb())}};"
        "if($colors.Count -lt 8){throw 'target window capture has no visible detail'};"
        "$bitmap.Save($env:RAS_UX_CAPTURE,[Drawing.Imaging.ImageFormat]::Png);"
        "$graphics.Dispose();$bitmap.Dispose()"
    )
    _powershell(
        script,
        env={"RAS_UX_CAPTURE": _windows_path(destination), "RAS_UX_PID": str(pid)},
    )
    if not destination.is_file() or destination.stat().st_size < 1000:
        raise AssertionError("Windows screenshot was not captured")


def _powershell(script: str, *, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    combined = dict(os.environ)
    combined.update(env)
    forwarded = ":".join(env)
    inherited_forwarding = combined.get("WSLENV", "")
    combined["WSLENV"] = ":".join(
        item for item in (inherited_forwarding, forwarded) if item
    )
    return subprocess.run(
        [
            "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            script,
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        env=combined,
    )


def _powershell_json(script: str, *, env: dict[str, str]) -> object:
    output = _powershell(script, env=env).stdout.strip()
    return json.loads(output or "null")


def _windows_version() -> str:
    script = (
        "$OutputEncoding=[Console]::OutputEncoding=[Text.UTF8Encoding]::new();"
        "[Environment]::OSVersion.VersionString"
    )
    return _powershell(script, env={}).stdout.strip()


def _wsl_version() -> str:
    raw = subprocess.run(
        ["/mnt/c/Windows/System32/wsl.exe", "--version"],
        check=True,
        capture_output=True,
    ).stdout
    decoded = raw.decode("utf-16", errors="replace").replace("\r", "").strip()
    return decoded or platform.release()


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


def _wait_endpoint_absent(url: str, *, timeout: int) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urlopen(url, timeout=0.5):
                pass
        except OSError:
            return
        time.sleep(0.2)
    raise AssertionError("browser debugging endpoint remained active after termination")


def _frozen_identity(data_root: Path) -> dict[str, str]:
    record_path = _single_file(data_root / "custodian-state/campaigns", "*.json")
    record = _read_json(record_path)
    intent_sha = str(record["launch_intent_sha256"])
    bundle_sha = str(record["input_bundle_sha256"])
    authority = data_root.parent / ".test-core-authority/authority"
    intent_path = _single_file(authority, "intents/*/*.json")
    bundle_path = _single_file(authority, "frozen-inputs/*/*.json")
    intent = _read_json(intent_path)
    frozen = _read_json(bundle_path)
    frozen_bundle = frozen.get("input_bundle")
    if (
        intent.get("intent_sha256") != intent_sha
        or not isinstance(frozen_bundle, dict)
        or frozen_bundle.get("bundle_sha256") != bundle_sha
        or frozen.get("launch_intent_sha256") != intent_sha
    ):
        raise AssertionError("frozen Core authority object bindings are invalid")
    return {
        "intent_sha256": intent_sha,
        "intent_file_sha256": _sha256(intent_path),
        "input_bundle_sha256": bundle_sha,
        "input_bundle_file_sha256": _sha256(bundle_path),
        "custodian_binding_sha256": _sha256(record_path),
    }


def _identity_digest(value: dict[str, str]) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _human_action_identities(data_root: Path) -> tuple[dict[str, object], dict[str, object]]:
    request = _read_json(_single_file(data_root / "operator-exchange", "*/requests/*.json"))
    response = _read_json(_single_file(data_root / "operator-exchange", "*/responses/*.json"))
    return request, response


def _launcher_evidence(data_root: Path) -> list[tuple[str, dict[str, object]]]:
    paths = sorted(
        (data_root / "custodian-state/launcher-evidence").glob("*.json"),
        key=lambda path: (path.stat().st_mtime_ns, path.name),
    )
    if len(paths) < 4:
        raise AssertionError("launcher invocation evidence is incomplete")
    return [
        (
            datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat(),
            _read_json(path),
        )
        for path in paths
    ]


def _single_file(root: Path, pattern: str) -> Path:
    values = tuple(root.glob(pattern))
    if len(values) != 1 or not values[0].is_file():
        raise AssertionError(f"expected one evidence file for {pattern}, found {len(values)}")
    return values[0]


def _wait_export(data_root: Path, *, timeout: int) -> Path:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        values = tuple((data_root / "exports").glob("campaign-*.zip"))
        if len(values) == 1 and values[0].is_file():
            return values[0]
        time.sleep(0.1)
    raise AssertionError("UI export did not produce one campaign bundle")


def _screenshot(page: Page, destination: Path) -> str:
    page.screenshot(path=destination, full_page=True)
    return _sha256(destination)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise AssertionError(f"{path} is not a JSON object")
    return value


def _required_int(value: dict[str, object], key: str) -> int:
    item = value.get(key)
    if not isinstance(item, int) or isinstance(item, bool):
        raise AssertionError(f"{key} is not an integer")
    return item


def _available_port() -> int:
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        return int(server.getsockname()[1])


def _windows_available_port() -> int:
    script = (
        "$listener=[Net.Sockets.TcpListener]::new([Net.IPAddress]::Any,0);"
        "$listener.Start();$port=([Net.IPEndPoint]$listener.LocalEndpoint).Port;"
        "$listener.Stop();$port"
    )
    output = _powershell(script, env={}).stdout.strip()
    try:
        port = int(output)
    except ValueError as exc:
        raise AssertionError("Windows did not allocate an acceptance port") from exc
    if port < 1024 or port > 65535:
        raise AssertionError("Windows returned an invalid acceptance port")
    return port


def _windows_host_address() -> str:
    output = subprocess.run(
        ["ip", "route", "show", "default"], check=True, capture_output=True, text=True
    ).stdout.split()
    if len(output) < 3 or output[:2] != ["default", "via"]:
        raise AssertionError("Windows host address is unavailable")
    return output[2]


def _windows_path(path: Path) -> str:
    return subprocess.run(
        ["wslpath", "-w", str(path)], check=True, capture_output=True, text=True
    ).stdout.strip()


def _git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


def _git_z(root: Path, *arguments: str) -> tuple[str, ...]:
    output = subprocess.run(
        ["git", *arguments], cwd=root, check=True, capture_output=True
    ).stdout
    return tuple(item.decode("utf-8") for item in output.split(b"\0") if item)


def _now() -> str:
    return datetime.now(UTC).isoformat()


if __name__ == "__main__":
    raise SystemExit(main())
