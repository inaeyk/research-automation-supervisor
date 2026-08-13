"""Strict messages crossing the Core Authority Service IPC boundary."""

from __future__ import annotations

import hashlib
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

from research_automation_supervisor.custodian_models import (
    MAX_PRIMARY_INPUT_BYTES,
    MAX_SUPPORTING_FILES,
    CampaignInputBundleV1,
    CampaignProfileSettingsV1,
    FrozenInputFileV1,
)
from research_automation_supervisor.durable_state import canonical_json

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
CommitId = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
StartIntentId = Annotated[
    str,
    Field(pattern=r"^intent_[0-9a-f]{64}_[0-9a-f]{64}$", min_length=136, max_length=136),
]


class RequestedRepositoryAuthorityV1(BaseModel):
    """Repository object observed by core during the non-authoritative preview."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    source_kind: Literal["existing_folder", "git_url"]
    source_display: Annotated[str, Field(min_length=1, max_length=1024)]
    source_locator: Annotated[str, Field(min_length=1, max_length=4096)]
    source_locator_sha256: Sha256
    requested_commit: CommitId
    requested_tree: CommitId | None = None
    source_device: Annotated[int, Field(ge=0)] | None = None
    source_inode: Annotated[int, Field(gt=0)] | None = None
    repository_id: Annotated[
        str, Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
    ]

    @model_validator(mode="after")
    def validate_locator(self) -> RequestedRepositoryAuthorityV1:
        digest = hashlib.sha256(self.source_locator.encode("utf-8")).hexdigest()
        if digest != self.source_locator_sha256:
            raise ValueError("requested repository locator hash is invalid")
        has_object_identity = self.source_device is not None and self.source_inode is not None
        if self.source_kind == "existing_folder":
            if self.requested_tree is None or not has_object_identity:
                raise ValueError("existing repository object identity is incomplete")
        elif self.requested_tree is not None or has_object_identity:
            raise ValueError("remote repository must not claim a local object identity")
        return self


class CampaignLaunchRequestV1(BaseModel):
    """Complete, strict Start request crossing from Custodian to trusted core."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    preview_id: Annotated[str, Field(pattern=r"^preview-[a-f0-9]{12,24}$")]
    client_start_key_sha256: Sha256
    human_name: Annotated[str, Field(min_length=1, max_length=160)]
    repository: RequestedRepositoryAuthorityV1
    research_contract: FrozenInputFileV1
    research_plan: FrozenInputFileV1
    initial_task: FrozenInputFileV1
    supporting_files: Annotated[
        tuple[FrozenInputFileV1, ...],
        BeforeValidator(tuple),
        Field(max_length=MAX_SUPPORTING_FILES),
    ] = ()
    requested_settings: CampaignProfileSettingsV1

    @model_validator(mode="after")
    def validate_primary_inputs(self) -> CampaignLaunchRequestV1:
        for item, label in (
            (self.research_contract, "research contract"),
            (self.research_plan, "research plan"),
            (self.initial_task, "initial task"),
        ):
            if item.byte_count > MAX_PRIMARY_INPUT_BYTES:
                raise ValueError(f"{label} exceeds the input size limit")
            try:
                text = item.content_bytes().decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError(f"{label} must be UTF-8 text") from exc
            if not text.strip():
                raise ValueError(f"{label} must not be blank")
        names = [item.display_name.casefold() for item in self.supporting_files]
        if len(names) != len(set(names)):
            raise ValueError("supporting file names must be unique")
        return self

    def canonical_sha256(self) -> str:
        return hashlib.sha256(canonical_json(self.model_dump(mode="json"))).hexdigest()


class CampaignLaunchReferenceV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    campaign_public_id: str
    launch_intent_id: StartIntentId
    launch_intent_sha256: Sha256
    input_bundle_sha256: Sha256


class CampaignLaunchSummaryV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    campaign_public_id: str
    preview_id: str
    launch_intent_id: StartIntentId
    launch_intent_sha256: Sha256
    input_bundle_sha256: Sha256
    human_name: Annotated[str, Field(min_length=1, max_length=160)]
    repository_display: Annotated[str, Field(min_length=1, max_length=1024)]
    created_at: Annotated[str, Field(min_length=20, max_length=40)]


class QualifiedLaunchMaterialV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    campaign_public_id: str
    launch_intent_id: StartIntentId
    launch_intent_sha256: Sha256
    frozen_input_sha256: Sha256
    input_bundle: CampaignInputBundleV1
