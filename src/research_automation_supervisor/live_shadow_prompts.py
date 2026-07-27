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
    b"This is live shadow observation. You are the shadow supervisor. Your own "
    b"input and actions are isolated: use only the immutable frozen decision "
    b"envelope, and do not inspect, execute against, or attempt to access the "
    b"live repository. These are supervisor-only isolation restrictions. The "
    b"candidate prompt is for the authoritative downstream Stage 2 role, which "
    b"acts in the real authoritative workspace. Do not transfer the shadow "
    b"supervisor's evidence-only, repository-blind, or execution-blind "
    b"restrictions into that candidate. Infer the downstream capabilities from "
    b"proposal_kind and the frozen Stage 2 specification and evidence, never "
    b"from hidden authoritative prompt bytes.\n\n"
    b"For every worker proposal kind, the candidate may and should instruct the "
    b"worker to inspect the authoritative workspace; read the relevant source, "
    b"tests, and contracts; modify only allowed_paths; run every exact "
    b"acceptance-test command given by acceptance_tests[*].argv; and report the "
    b"changes and command results. For an auditor proposal, the candidate may "
    b"and should instruct the auditor to inspect the authoritative workspace "
    b"and complete diff; read the relevant source, tests, and contracts; run "
    b"every exact acceptance-test command independently; perform additional "
    b"read-only checks within the frozen scope; and report concrete findings or "
    b"PASS. The auditor must never edit the workspace, so an auditor proposal "
    b"has no authorized modification targets.\n\n"
    b"Unless the frozen typed downstream action is explicitly evidence-only, "
    b"the candidate must not tell that role to use only supplied or frozen "
    b"evidence, avoid inspecting the live repository, avoid requesting or "
    b"performing execution, or rely on a recorded passing test instead of "
    b"rerunning its exact command. A recorded result is context, not a "
    b"replacement for downstream verification.\n\n"
    b"The proposal is quarantined and will not be sent automatically to any "
    b"worker or auditor. Authoritative Stage 2 execution proceeds independently "
    b"and never waits for this result. The contract, scope, permissions, "
    b"acceptance tests, conventions, and checkpoints are frozen. "
    b"referenced_paths lists only authorized modification targets using "
    b"normalized workspace-relative POSIX paths; do not list read-only "
    b"contracts, tests, protected paths, or evidence. required_checks lists "
    b"exact Stage 2 acceptance-test IDs, never commands. Recommend a human "
    b"pause when evidence is insufficient. Return only the strict structured "
    b"object required by the engine-owned schema.\n"
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
