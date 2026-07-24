"""Deterministic in-memory assembly of quarantined Stage 4 supervisor inputs."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass

from research_automation_supervisor.errors import (
    LiveShadowInputError,
    ShadowInputError,
)
from research_automation_supervisor.live_shadow_models import LiveDecisionEnvelope
from research_automation_supervisor.live_shadow_sources import (
    PreparedLiveShadowSpecification,
)
from research_automation_supervisor.shadow_confidentiality import (
    preflight_shadow_confidentiality,
)
from research_automation_supervisor.shadow_models import BlindInputManifest
from research_automation_supervisor.shadow_prompts import (
    build_supervisor_output_schema,
)

POLICY_FOOTER = b"\n\n[END FROZEN HUMAN SUPERVISOR POLICY]\n"
CONTEXT_HEADER = b"\n[BEGIN FROZEN HUMAN PROJECT CONTEXT]\n"
CONTEXT_FOOTER = b"\n[END FROZEN HUMAN PROJECT CONTEXT]\n"
INSTRUCTION_HEADER = b"\n[BEGIN FIXED STAGE 4 LIVE-SHADOW INSTRUCTIONS]\n"
INSTRUCTION_FOOTER = b"[END FIXED STAGE 4 LIVE-SHADOW INSTRUCTIONS]\n"
CONTRACT_HEADER = b"\n[BEGIN FROZEN STAGE 2 CONTRACT]\n"
CONTRACT_FOOTER = b"\n[END FROZEN STAGE 2 CONTRACT]\n"
SUMMARY_HEADER = b"\n[BEGIN NORMALIZED AUTHORITATIVE SOURCE SUMMARY]\n"
SUMMARY_FOOTER = b"[END NORMALIZED AUTHORITATIVE SOURCE SUMMARY]\n"
ENVELOPE_HEADER = b"\n[BEGIN IMMUTABLE LIVE DECISION ENVELOPE]\n"
ENVELOPE_FOOTER = b"[END IMMUTABLE LIVE DECISION ENVELOPE]\n"
SCHEMA_HEADER = b"\n[BEGIN ENGINE-OWNED SUPERVISOR OUTPUT SCHEMA]\n"
SCHEMA_FOOTER = b"[END ENGINE-OWNED SUPERVISOR OUTPUT SCHEMA]\n"

LIVE_SHADOW_INSTRUCTION = (
    b"This is live shadow observation. The proposal is quarantined and will "
    b"not be sent automatically to any worker or auditor. Authoritative Stage 2 "
    b"execution proceeds independently and never waits for this result. The "
    b"contract, scope, permissions, acceptance tests, conventions, and "
    b"checkpoints are frozen. Use only the immutable envelope. Do not inspect "
    b"or attempt to access the live repository. referenced_paths lists only "
    b"authorized modification targets using normalized workspace-relative "
    b"POSIX paths; do not list read-only contracts, tests, protected paths, or "
    b"evidence. required_checks lists exact Stage 2 acceptance-test IDs, never "
    b"commands. Recommend a human pause when evidence is insufficient. Return "
    b"only the strict structured object required by the engine-owned schema.\n"
)


@dataclass(frozen=True)
class RenderedLiveBlindPrompt:
    """One non-persisted live blind input and its hash-only manifest."""

    content: bytes
    output_schema: dict[str, object]
    manifest: BlindInputManifest


def build_live_blind_supervisor_prompt(
    prepared: PreparedLiveShadowSpecification,
    envelope: LiveDecisionEnvelope,
    *,
    sensitive_values: Sequence[str] = (),
) -> RenderedLiveBlindPrompt:
    """Build a live input from frozen policy, context, contract, and envelope only."""
    envelope_value = envelope.model_dump(mode="json")
    hash_body = dict(envelope_value)
    expected_hash = hash_body.pop("envelope_sha256")
    if hashlib.sha256(_canonical_json(hash_body)).hexdigest() != expected_hash:
        raise LiveShadowInputError("live decision envelope hash is invalid")
    summary = prepared.blind_source_summary()
    summary_bytes = _canonical_json(summary)
    envelope_bytes = _canonical_json(envelope_value)
    schema = build_supervisor_output_schema(
        envelope.proposal_kind,
        prepared.specification.max_proposal_bytes,
    )
    schema_bytes = _canonical_json(schema)
    context_parts: list[bytes] = []
    for context in prepared.contexts:
        context_parts.extend((CONTEXT_HEADER, context.content, CONTEXT_FOOTER))
    content = b"".join(
        (
            prepared.policy.content,
            POLICY_FOOTER,
            *context_parts,
            INSTRUCTION_HEADER,
            LIVE_SHADOW_INSTRUCTION,
            INSTRUCTION_FOOTER,
            CONTRACT_HEADER,
            prepared.stage2.contract.content,
            CONTRACT_FOOTER,
            SUMMARY_HEADER,
            summary_bytes,
            SUMMARY_FOOTER,
            ENVELOPE_HEADER,
            envelope_bytes,
            ENVELOPE_FOOTER,
            SCHEMA_HEADER,
            schema_bytes,
            SCHEMA_FOOTER,
        )
    )
    try:
        preflight_shadow_confidentiality(
            (
                prepared.policy.content,
                tuple(context.content for context in prepared.contexts),
                LIVE_SHADOW_INSTRUCTION,
                prepared.stage2.contract.content,
                summary,
                envelope,
                schema,
                content,
            ),
            sensitive_values,
            label="live blind supervisor input",
        )
    except ShadowInputError as exc:
        raise LiveShadowInputError(str(exc)) from exc
    forbidden = {
        value
        for source in (
            prepared.stage2.worker_initial_prompt,
            prepared.stage2.worker_repair_prompt,
            prepared.stage2.auditor_prompt,
        )
        for value in (
            source.content,
            str(source.path).encode("utf-8"),
            source.sha256.encode("ascii"),
        )
        if value
    }
    if any(value in content for value in forbidden):
        raise LiveShadowInputError(
            "authoritative prompt material appears in live blind supervisor input"
        )
    manifest = BlindInputManifest(
        proposal_id=envelope.decision_id,
        proposal_kind=envelope.proposal_kind,
        policy_sha256=prepared.policy.sha256,
        context_files=prepared.context_manifests(),
        contract_sha256=prepared.stage2.contract.sha256,
        source_summary_sha256=hashlib.sha256(summary_bytes).hexdigest(),
        evidence_sha256=hashlib.sha256(envelope_bytes).hexdigest(),
        output_schema_sha256=hashlib.sha256(schema_bytes).hexdigest(),
        rendered_blind_input_sha256=hashlib.sha256(content).hexdigest(),
        rendered_blind_input_byte_count=len(content),
        authoritative_sentinel_absent=True,
        shadow_only=True,
        automatic_send_disabled=True,
    )
    return RenderedLiveBlindPrompt(
        content=content,
        output_schema=schema,
        manifest=manifest,
    )


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("ascii")
