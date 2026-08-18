"""Strict evidence model for real zero-shell operator acceptance.

The model is intentionally observational.  It binds independently observable
launcher, process, browser, durable-state, notification, screenshot, and export
evidence without becoming campaign or scientific authority.
"""

from __future__ import annotations

import hashlib
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from research_automation_supervisor.durable_state import canonical_json

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class UXFileIdentityV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    path: Annotated[str, Field(min_length=1, max_length=4096)]
    sha256: Sha256
    size: Annotated[int, Field(ge=0)]
    git_state: Literal["tracked", "modified", "untracked"]


class UXCandidateTreeFingerprintV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    algorithm: Literal["sha256-path-content-manifest-v1"] = "sha256-path-content-manifest-v1"
    sha256: Sha256
    file_count: Annotated[int, Field(gt=0)]
    files: Annotated[tuple[UXFileIdentityV1, ...], Field(min_length=1)]
    generated_evidence_excluded: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_fingerprint(self) -> UXCandidateTreeFingerprintV1:
        if self.file_count != len(self.files):
            raise ValueError("candidate tree file count is invalid")
        ordered = tuple(sorted(self.files, key=lambda item: item.path))
        if self.files != ordered or len({item.path for item in self.files}) != len(self.files):
            raise ValueError("candidate tree files must be unique and sorted")
        payload = [
            {"path": item.path, "sha256": item.sha256, "size": item.size}
            for item in self.files
        ]
        if hashlib.sha256(canonical_json(payload)).hexdigest() != self.sha256:
            raise ValueError("candidate tree fingerprint is invalid")
        return self


class UXIdentityV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    role: Literal["launcher", "launcher_script", "bootstrap", "ui_backend", "core_seam"]
    path: Annotated[str, Field(min_length=1, max_length=4096)]
    sha256: Sha256


class UXInteractionV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    sequence: Annotated[int, Field(gt=0)]
    observed_at: Annotated[str, Field(min_length=20, max_length=80)]
    action: Annotated[str, Field(min_length=1, max_length=512)]
    visible_result: Annotated[str, Field(min_length=1, max_length=2048)]
    screenshot_sha256: Sha256 | None = None


class UXLauncherInvocationV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    sequence: Annotated[int, Field(gt=0)]
    observed_at: Annotated[str, Field(min_length=20, max_length=80)]
    launcher: Annotated[str, Field(min_length=1, max_length=4096)]
    exit_code: int
    backend_reused: bool
    windows_execution_path: Literal[True] = True
    readiness_instance: Annotated[str, Field(min_length=32, max_length=128)] | None = None


class UXBrowserProcessEvidenceV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    executable: Annotated[str, Field(min_length=1, max_length=4096)]
    initial_pids: Annotated[tuple[int, ...], Field(min_length=1)]
    terminated_pids: Annotated[tuple[int, ...], Field(min_length=1)]
    terminated_processes_absent: Literal[True]
    restarted_pids: Annotated[tuple[int, ...], Field(min_length=1)]
    default_browser_path_exercised: Literal[True]


class UXBackendRestartEvidenceV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    initial_pid: Annotated[int, Field(gt=0)]
    terminated_pid: Annotated[int, Field(gt=0)]
    restarted_pid: Annotated[int, Field(gt=0)]
    old_process_absent: Literal[True]
    frozen_identity_before_sha256: Sha256
    frozen_identity_after_sha256: Sha256

    @model_validator(mode="after")
    def validate_restart(self) -> UXBackendRestartEvidenceV1:
        if self.initial_pid != self.terminated_pid or self.initial_pid == self.restarted_pid:
            raise ValueError("backend restart process identities are invalid")
        if self.frozen_identity_before_sha256 != self.frozen_identity_after_sha256:
            raise ValueError("frozen authority changed across backend restart")
        return self


class UXHumanActionEvidenceV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    request_sha256: Sha256
    response_sha256: Sha256
    response_type: Literal["approval", "choice", "free_text", "file_upload", "contract_decision"]
    safe_evidence_opened: Literal[True]
    submitted_through_ui: Literal[True]


class UXCompletionEvidenceV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    campaign_public_id: Annotated[str, Field(pattern=r"^campaign-[a-z0-9-]+$")]
    completion_state_sha256: Sha256
    completion_verified: Literal[True]
    final_report_opened: Literal[True]


class UXNotificationEvidenceV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    mechanism: Literal["browser-notification-and-visible-status"]
    permission: Literal["granted"]
    title: Literal["Campaign completed"]
    visible_text: Annotated[str, Field(min_length=1, max_length=1024)]
    screenshot_sha256: Sha256
    durable_notification_sha256: Sha256


class UXAcceptanceEvidenceV1(BaseModel):
    """Evidence binding for one real PA-5C4-U candidate execution."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    evidence_type: Literal["UXAcceptanceEvidenceV1"] = "UXAcceptanceEvidenceV1"
    stage: Literal["PA-5C4-U"] = "PA-5C4-U"
    qualified: bool
    branch: Annotated[str, Field(min_length=1, max_length=512)]
    head: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    candidate_tree: UXCandidateTreeFingerprintV1
    executable_identities: Annotated[tuple[UXIdentityV1, ...], Field(min_length=5)]
    windows_version: Annotated[str, Field(min_length=1, max_length=1024)]
    wsl_version: Annotated[str, Field(min_length=1, max_length=2048)]
    browser_name_version: Annotated[str, Field(min_length=1, max_length=1024)]
    transcript: Annotated[tuple[UXInteractionV1, ...], Field(min_length=1)]
    launcher_invocations: Annotated[tuple[UXLauncherInvocationV1, ...], Field(min_length=3)]
    browser_process: UXBrowserProcessEvidenceV1
    backend_restart: UXBackendRestartEvidenceV1
    failure_dialog_title: Annotated[str, Field(min_length=1, max_length=240)]
    failure_dialog_screenshot_sha256: Sha256
    human_action: UXHumanActionEvidenceV1
    completion: UXCompletionEvidenceV1
    notification: UXNotificationEvidenceV1
    screenshot_hashes: dict[str, Sha256]
    exported_bundle_sha256: Sha256
    evidence_sha256: Sha256

    @model_validator(mode="after")
    def validate_evidence(self) -> UXAcceptanceEvidenceV1:
        sequences = tuple(item.sequence for item in self.transcript)
        if sequences != tuple(range(1, len(sequences) + 1)):
            raise ValueError("interaction transcript sequence is invalid")
        transcript_times = tuple(item.observed_at for item in self.transcript)
        if transcript_times != tuple(sorted(transcript_times)):
            raise ValueError("interaction transcript is not chronological")
        launcher_sequences = tuple(item.sequence for item in self.launcher_invocations)
        if launcher_sequences != tuple(range(1, len(launcher_sequences) + 1)):
            raise ValueError("launcher invocation sequence is invalid")
        launcher_times = tuple(item.observed_at for item in self.launcher_invocations)
        if launcher_times != tuple(sorted(launcher_times)):
            raise ValueError("launcher invocations are not chronological")
        payload = self.model_dump(mode="json", exclude={"evidence_sha256"})
        if hashlib.sha256(canonical_json(payload)).hexdigest() != self.evidence_sha256:
            raise ValueError("UX acceptance evidence self-hash is invalid")
        return self

    @classmethod
    def bind(cls, **values: object) -> UXAcceptanceEvidenceV1:
        payload = {
            "schema_version": 1,
            "evidence_type": "UXAcceptanceEvidenceV1",
            "stage": "PA-5C4-U",
            **values,
        }
        digest = hashlib.sha256(canonical_json(payload)).hexdigest()
        return cls.model_validate({**payload, "evidence_sha256": digest})
