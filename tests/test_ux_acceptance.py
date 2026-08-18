from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from pydantic import ValidationError

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

SHA = "a" * 64


def _evidence() -> UXAcceptanceEvidenceV1:
    file = UXFileIdentityV1(path="candidate.py", sha256=SHA, size=12, git_state="modified")
    manifest = [{"path": file.path, "sha256": file.sha256, "size": file.size}]
    tree = UXCandidateTreeFingerprintV1(
        sha256=hashlib.sha256(canonical_json(manifest)).hexdigest(),
        file_count=1,
        files=(file,),
    )
    identities = (
        UXIdentityV1(role="launcher", path="0.txt", sha256=SHA),
        UXIdentityV1(role="launcher_script", path="1.txt", sha256=SHA),
        UXIdentityV1(role="bootstrap", path="2.txt", sha256=SHA),
        UXIdentityV1(role="ui_backend", path="3.txt", sha256=SHA),
        UXIdentityV1(role="core_seam", path="4.txt", sha256=SHA),
    )
    transcript = (
        UXInteractionV1(
            sequence=1,
            observed_at="2026-08-18T00:00:00+00:00",
            action="Double-click launcher",
            visible_result="The browser opened.",
        ),
    )
    launchers = tuple(
        UXLauncherInvocationV1(
            sequence=index,
            observed_at=f"2026-08-18T00:00:0{index}+00:00",
            launcher="Research Supervisor.vbs",
            exit_code=0,
            backend_reused=index > 1,
            readiness_instance=str(index) * 64,
        )
        for index in range(1, 4)
    )
    return UXAcceptanceEvidenceV1.bind(
        qualified=True,
        branch="feature/campaign-custodian-real-operator-ux",
        head="b" * 40,
        candidate_tree=tree.model_dump(mode="python"),
        executable_identities=tuple(item.model_dump(mode="python") for item in identities),
        windows_version="Windows 10.0.26200",
        wsl_version="WSL 2.7.10",
        browser_name_version="Chrome/151.0.7922.138",
        transcript=tuple(item.model_dump(mode="python") for item in transcript),
        launcher_invocations=tuple(item.model_dump(mode="python") for item in launchers),
        browser_process=UXBrowserProcessEvidenceV1(
            executable="C:\\Chrome\\chrome.exe",
            initial_pids=(101,),
            terminated_pids=(101,),
            terminated_processes_absent=True,
            restarted_pids=(202,),
            default_browser_path_exercised=True,
        ).model_dump(mode="python"),
        backend_restart=UXBackendRestartEvidenceV1(
            initial_pid=303,
            terminated_pid=303,
            restarted_pid=404,
            old_process_absent=True,
            frozen_identity_before_sha256=SHA,
            frozen_identity_after_sha256=SHA,
        ).model_dump(mode="python"),
        failure_dialog_title="Research Supervisor needs attention",
        failure_dialog_screenshot_sha256=SHA,
        human_action=UXHumanActionEvidenceV1(
            request_sha256=SHA,
            response_sha256="b" * 64,
            response_type="choice",
            safe_evidence_opened=True,
            submitted_through_ui=True,
        ).model_dump(mode="python"),
        completion=UXCompletionEvidenceV1(
            campaign_public_id="campaign-acceptance",
            completion_state_sha256=SHA,
            completion_verified=True,
            final_report_opened=True,
        ).model_dump(mode="python"),
        notification=UXNotificationEvidenceV1(
            mechanism="browser-notification-and-visible-status",
            permission="granted",
            title="Campaign completed",
            visible_text="Campaign completed visibly.",
            screenshot_sha256=SHA,
            durable_notification_sha256=SHA,
        ).model_dump(mode="python"),
        screenshot_hashes={"completed": SHA},
        exported_bundle_sha256=SHA,
    )


def test_ux_acceptance_evidence_round_trips_and_self_verifies(tmp_path: Path) -> None:
    evidence = _evidence()
    destination = tmp_path / "evidence.json"
    destination.write_text(evidence.model_dump_json(), encoding="utf-8")
    loaded = UXAcceptanceEvidenceV1.model_validate_json(destination.read_text(encoding="utf-8"))
    assert loaded == evidence


def test_ux_acceptance_evidence_rejects_nonchronological_launcher_records() -> None:
    value = _evidence().model_dump(mode="json")
    value["launcher_invocations"][0]["observed_at"] = "2026-08-18T00:00:09+00:00"
    with pytest.raises(ValidationError, match="not chronological"):
        UXAcceptanceEvidenceV1.model_validate(value, strict=False)
