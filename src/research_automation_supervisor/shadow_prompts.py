"""Deterministic in-memory assembly of blind Stage 3 supervisor inputs."""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass

from research_automation_supervisor.errors import ShadowInputError
from research_automation_supervisor.shadow_confidentiality import (
    preflight_shadow_confidentiality,
)
from research_automation_supervisor.shadow_models import (
    BlindInputManifest,
    ProposalKind,
)
from research_automation_supervisor.shadow_sources import (
    DecisionReconstruction,
    PreparedShadowSpecification,
)
from research_automation_supervisor.structured_outputs import (
    normalize_production_schema,
)

POLICY_LABEL = b"\n\n[END FROZEN HUMAN SUPERVISOR POLICY]\n"
CONTEXT_HEADER = b"\n[BEGIN FROZEN HUMAN PROJECT CONTEXT]\n"
CONTEXT_FOOTER = b"\n[END FROZEN HUMAN PROJECT CONTEXT]\n"
CONTRACT_HEADER = b"\n[BEGIN FROZEN STAGE 2 CONTRACT]\n"
CONTRACT_FOOTER = b"\n[END FROZEN STAGE 2 CONTRACT]\n"
SUMMARY_HEADER = b"\n[BEGIN NORMALIZED STAGE 2 SOURCE SUMMARY]\n"
SUMMARY_FOOTER = b"[END NORMALIZED STAGE 2 SOURCE SUMMARY]\n"
EVIDENCE_HEADER = b"\n[BEGIN DECISION-POINT EVIDENCE]\n"
EVIDENCE_FOOTER = b"[END DECISION-POINT EVIDENCE]\n"
SCHEMA_HEADER = b"\n[BEGIN ENGINE-OWNED SUPERVISOR OUTPUT SCHEMA]\n"
SCHEMA_FOOTER = b"[END ENGINE-OWNED SUPERVISOR OUTPUT SCHEMA]\n"

SHADOW_INSTRUCTION = (
    b"\nThis is retrospective shadow calibration only. Your proposal is advisory "
    b"and will never be sent automatically to a worker or auditor. The frozen "
    b"contract, tests, conventions, scope, and permissions cannot be changed. "
    b"Use only evidence available at this decision point. Recommend a human "
    b"pause when evidence is insufficient. Return only the strict JSON object "
    b"required by the engine-owned schema.\n"
)

SUPERVISOR_OUTPUT_SCHEMA: dict[str, object] = normalize_production_schema({
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "schema_version",
        "proposal_kind",
        "disposition",
        "prompt",
        "summary",
        "referenced_paths",
        "required_checks",
        "assumptions",
        "questions",
        "contract_change_requested",
        "scope_expansion_requested",
        "permission_change_requested",
        "acceptance_change_requested",
        "convention_change_requested",
    ],
    "properties": {
        "schema_version": {"const": 1},
        "proposal_kind": {
            "enum": [
                "worker_initial",
                "worker_scope_repair",
                "worker_test_repair",
                "worker_audit_repair",
                "worker_human_continuation",
                "auditor",
            ]
        },
        "disposition": {
            "enum": ["propose", "recommend_human_pause"]
        },
        "prompt": {
            "type": ["string", "null"],
            "maxLength": 2 * 1024 * 1024,
        },
        "summary": {"type": "string", "maxLength": 16384},
        "referenced_paths": {
            "type": "array",
            "maxItems": 200,
            "items": {"type": "string", "maxLength": 16384},
        },
        "required_checks": {
            "type": "array",
            "maxItems": 200,
            "items": {"type": "string", "maxLength": 16384},
        },
        "assumptions": {
            "type": "array",
            "maxItems": 200,
            "items": {"type": "string", "maxLength": 16384},
        },
        "questions": {
            "type": "array",
            "maxItems": 200,
            "items": {"type": "string", "maxLength": 16384},
        },
        "contract_change_requested": {"type": "boolean"},
        "scope_expansion_requested": {"type": "boolean"},
        "permission_change_requested": {"type": "boolean"},
        "acceptance_change_requested": {"type": "boolean"},
        "convention_change_requested": {"type": "boolean"},
    },
})


@dataclass(frozen=True)
class RenderedBlindPrompt:
    """One non-persisted blind input and its safe hash-only manifest."""

    content: bytes
    output_schema: dict[str, object]
    manifest: BlindInputManifest


def build_supervisor_output_schema(
    proposal_kind: ProposalKind,
    max_proposal_bytes: int,
) -> dict[str, object]:
    """Specialize the fixed schema to one exact decision kind and size bound."""
    schema = copy.deepcopy(SUPERVISOR_OUTPUT_SCHEMA)
    properties = schema["properties"]
    assert isinstance(properties, dict)
    properties["proposal_kind"] = {"const": proposal_kind}
    prompt = properties["prompt"]
    assert isinstance(prompt, dict)
    prompt["maxLength"] = max_proposal_bytes
    return normalize_production_schema(schema)


def build_blind_supervisor_prompt(
    prepared: PreparedShadowSpecification,
    decision: DecisionReconstruction,
    *,
    sensitive_values: Sequence[str] = (),
) -> RenderedBlindPrompt:
    """Concatenate only the contract-authorized blind-domain bytes."""
    source_summary_bytes = _canonical_json(
        prepared.source.blind_source_summary()
    )
    evidence_bytes = _canonical_json(decision.blind_evidence)
    if (
        hashlib.sha256(evidence_bytes).hexdigest()
        != decision.point.evidence_sha256
    ):
        raise ShadowInputError(
            "decision evidence no longer matches its reconstruction hash"
        )
    schema = build_supervisor_output_schema(
        decision.point.proposal_kind,
        prepared.specification.max_proposal_bytes,
    )
    schema_bytes = _canonical_json(schema)
    context_parts: list[bytes] = []
    for context in prepared.contexts:
        context_parts.extend(
            (CONTEXT_HEADER, context.content, CONTEXT_FOOTER)
        )
    content = b"".join(
        (
            prepared.policy.content,
            POLICY_LABEL,
            *context_parts,
            CONTRACT_HEADER,
            prepared.source.prepared.contract.content,
            CONTRACT_FOOTER,
            SUMMARY_HEADER,
            source_summary_bytes,
            SUMMARY_FOOTER,
            EVIDENCE_HEADER,
            evidence_bytes,
            EVIDENCE_FOOTER,
            SCHEMA_HEADER,
            schema_bytes,
            SCHEMA_FOOTER,
            SHADOW_INSTRUCTION,
        )
    )
    preflight_shadow_confidentiality(
        (
            prepared.policy.content,
            tuple(context.content for context in prepared.contexts),
            prepared.source.prepared.contract.content,
            prepared.source.blind_source_summary(),
            decision.point.model_dump(mode="json"),
            decision.blind_evidence,
            schema,
            (
                POLICY_LABEL,
                CONTEXT_HEADER,
                CONTEXT_FOOTER,
                CONTRACT_HEADER,
                CONTRACT_FOOTER,
                SUMMARY_HEADER,
                SUMMARY_FOOTER,
                EVIDENCE_HEADER,
                EVIDENCE_FOOTER,
                SCHEMA_HEADER,
                SCHEMA_FOOTER,
                SHADOW_INSTRUCTION,
            ),
            content,
        ),
        sensitive_values,
        label="blind supervisor input",
    )
    forbidden = {
        value
        for reconstructed in prepared.source.decisions
        for value in (
            (
                reconstructed.authoritative_source.content
                if reconstructed.authoritative_source is not None
                else None
            ),
            (
                reconstructed.authoritative_rendered.content
                if reconstructed.authoritative_rendered is not None
                else None
            ),
            (
                str(reconstructed.authoritative_source.path).encode("utf-8")
                if reconstructed.authoritative_source is not None
                else None
            ),
            (
                reconstructed.authoritative_source.sha256.encode("ascii")
                if reconstructed.authoritative_source is not None
                else None
            ),
            (
                reconstructed.authoritative_rendered.rendered_sha256.encode(
                    "ascii"
                )
                if reconstructed.authoritative_rendered is not None
                else None
            ),
        )
        if value
    }
    if any(value in content for value in forbidden):
        raise ShadowInputError(
            "authoritative prompt material appears in blind supervisor input"
        )
    manifest = BlindInputManifest(
        proposal_id=decision.point.decision_id,
        proposal_kind=decision.point.proposal_kind,
        policy_sha256=prepared.policy.sha256,
        context_files=tuple(
            context.manifest() for context in prepared.contexts
        ),
        contract_sha256=prepared.source.prepared.contract.sha256,
        source_summary_sha256=hashlib.sha256(
            source_summary_bytes
        ).hexdigest(),
        evidence_sha256=hashlib.sha256(evidence_bytes).hexdigest(),
        output_schema_sha256=hashlib.sha256(schema_bytes).hexdigest(),
        rendered_blind_input_sha256=hashlib.sha256(content).hexdigest(),
        rendered_blind_input_byte_count=len(content),
        authoritative_sentinel_absent=True,
        shadow_only=True,
        automatic_send_disabled=True,
    )
    return RenderedBlindPrompt(
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
