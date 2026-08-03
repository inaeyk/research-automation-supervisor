"""Deterministic human-written prompt assembly for the standalone Physics Auditor."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from research_automation_supervisor.codex_models import MAX_PROMPT_BYTES
from research_automation_supervisor.durable_state import canonical_json
from research_automation_supervisor.physics_auditor_models import (
    PHYSICS_AUDITOR_PROMPT_TEMPLATE_VERSION,
    PhysicsAuditorChangedPathManifestV1,
    PhysicsAuditorEvidenceIndexV1,
    PhysicsAuditorProjectionManifestV1,
)
from research_automation_supervisor.physics_models import (
    PHYSICS_AUDIT_REPORT_OUTPUT_SCHEMA,
    PhysicsTaskContractV1,
)

_ROLE = (
    "You are a fresh, independent Physics Auditor. You are not the Worker and not the Code "
    "Auditor. Work read-only. Do not repair or modify anything. Your report is untrusted input "
    "to deterministic validation and routing."
)
_SCOPE = (
    "Audit only physical consistency under the declared authority: conventions, assumptions, "
    "required identities, limiting cases, verified oracle evidence, gauge or constraint "
    "ambiguity, equation-to-implementation consistency, and unsupported physical claims."
)
_NON_GOALS = (
    "Do not perform general code-style review. Do not modify files. Do not run arbitrary "
    "commands. Do not execute, redefine, or replace an oracle. Do not invent missing "
    "conventions or evidence. Do not approve a new scientific interpretation or "
    "publication-level claim. Do not request or reveal hidden reasoning; provide only the "
    "bounded rationale fields required by the output schema."
)
_CITATIONS = (
    "Every check, finding, and unresolved question must cite only authority present in the task "
    "contract and safe evidence index. Use these exact PhysicsEvidenceReferenceV1 shapes: "
    "task_contract uses reference='<collection>.<id>' (for example "
    "'conventions.force_sign' or 'assumptions.unit_mass'), path=null, and null line fields; "
    "source, derivation, and document use reference=null plus a declared relative POSIX path, "
    "with both valid line fields required for source and either both or neither for derivation "
    "and document; test, artifact, numerical, and oracle use a declared ID in reference with "
    "path=null and null line fields. Do not put a declared evidence ID in reference for source, "
    "derivation, or document. A missing oracle entry may be cited only as evidence that required "
    "evidence is missing. Never invent a path, ID, command, oracle result, artifact, test, or "
    "contract field."
)
_REQUIRED_EVIDENCE = (
    "For every required-identity or limiting-case check whose status is passed or failed, cite "
    "evidence satisfying every required_evidence_kinds entry declared for that target. A verified "
    "oracle reference contributes both 'oracle' and that oracle's declared contract kind (for "
    "example, an analytic oracle satisfies an analytic requirement). An unresolved check may lack "
    "a required kind only when its evidence_sufficiency and evidence-blocking finding consistently "
    "state that the authority is missing. A sign defect does not by itself fail a zero-input "
    "limiting case: assess that case separately and cite its analytic authority."
)
_HUMAN_GATES = (
    "Report a human gate for any convention change, unresolved gauge or constraint ambiguity, "
    "or new physical interpretation. You may identify those conditions but cannot resolve or "
    "approve them."
)
_OUTPUT = (
    "Return exactly one JSON object satisfying PhysicsAuditReportV1 and the supplied strict "
    "output schema. Assess exactly every required identity, limiting case, and required oracle. "
    "The report verdict is advisory: deterministic PA-1 routing is authoritative. Do not include "
    "markdown fences, commentary, commands, environment data, credentials, or fields outside "
    "the schema."
)
_STOP = (
    "When required evidence is absent, return blocked_insufficient_evidence with unresolved "
    "checks and evidence-blocking findings consistent with the schema. Never guess missing "
    "physics authority."
)

PHYSICS_AUDITOR_PROMPT_TEMPLATE_TEXT = "\n".join(
    (
        f"PHYSICS AUDITOR PROMPT TEMPLATE {PHYSICS_AUDITOR_PROMPT_TEMPLATE_VERSION}",
        "",
        "1. ROLE AND INDEPENDENCE",
        _ROLE,
        "",
        "2. AUDIT SCOPE",
        _SCOPE,
        "",
        "3. EXPLICIT NON-GOALS",
        _NON_GOALS,
        "",
        "4. PHYSICS TASK CONTRACT",
        "<ENGINE_CANONICAL_CONTRACT_JSON>",
        "",
        "5. SAFE EVIDENCE INDEX",
        "<ENGINE_CANONICAL_EVIDENCE_INDEX_JSON>",
        "",
        "6. VERIFIED ORACLE RESULT SUMMARIES AND COMPLETION PROOFS",
        "<ENGINE_CANONICAL_ORACLE_SUMMARIES_JSON>",
        "",
        "7. DECLARED WORKSPACE PATHS AND CHANGED-PATH MANIFEST",
        "<ENGINE_CANONICAL_CHANGED_PATHS_JSON>",
        "<ENGINE_CANONICAL_WORKSPACE_FILES_JSON>",
        "The filesystem visible at /workspace is exactly this read-only projection manifest:",
        "<ENGINE_CANONICAL_PROJECTION_MANIFEST_JSON>",
        "",
        "8. EVIDENCE CITATION RULES",
        _CITATIONS,
        _REQUIRED_EVIDENCE,
        "",
        "9. HUMAN-GATE RULES",
        _HUMAN_GATES,
        "",
        "10. STRICT PHYSICSAUDITREPORTV1 OUTPUT REQUIREMENTS",
        _OUTPUT,
        "<ENGINE_CANONICAL_OUTPUT_SCHEMA_JSON>",
        "",
        "11. STOP CONDITION",
        _STOP,
        "",
    )
)
PHYSICS_AUDITOR_PROMPT_TEMPLATE_SHA256 = hashlib.sha256(
    PHYSICS_AUDITOR_PROMPT_TEMPLATE_TEXT.encode("ascii")
).hexdigest()


@dataclass(frozen=True)
class RenderedPhysicsAuditorPrompt:
    """Canonical prompt bytes and semantic component hashes."""

    content: bytes
    template_version: str
    template_sha256: str
    contract_sha256: str
    evidence_index_sha256: str
    changed_path_manifest_sha256: str
    projection_manifest_sha256: str
    output_schema_sha256: str
    rendered_sha256: str
    byte_count: int


def build_physics_auditor_prompt(
    contract: PhysicsTaskContractV1,
    evidence_index: PhysicsAuditorEvidenceIndexV1,
    changed_paths: PhysicsAuditorChangedPathManifestV1,
    projection_manifest: PhysicsAuditorProjectionManifestV1,
) -> RenderedPhysicsAuditorPrompt:
    """Render the exact PA-3 prompt in fixed section and collection order."""
    contract_bytes = contract.to_canonical_json()
    evidence_bytes = evidence_index.to_canonical_json()
    oracle_bytes = canonical_json(
        [item.model_dump(mode="json") for item in evidence_index.oracle_evidence]
    )
    changed_bytes = changed_paths.to_canonical_json()
    workspace_files_bytes = canonical_json(
        [item.model_dump(mode="json") for item in evidence_index.workspace_files]
    )
    schema_bytes = canonical_json(PHYSICS_AUDIT_REPORT_OUTPUT_SCHEMA)
    projection_bytes = projection_manifest.to_canonical_json()
    replacements = {
        "<ENGINE_CANONICAL_CONTRACT_JSON>": contract_bytes.decode("ascii").rstrip("\n"),
        "<ENGINE_CANONICAL_EVIDENCE_INDEX_JSON>": evidence_bytes.decode("ascii").rstrip("\n"),
        "<ENGINE_CANONICAL_ORACLE_SUMMARIES_JSON>": oracle_bytes.decode("ascii").rstrip("\n"),
        "<ENGINE_CANONICAL_CHANGED_PATHS_JSON>": changed_bytes.decode("ascii").rstrip("\n"),
        "<ENGINE_CANONICAL_WORKSPACE_FILES_JSON>": workspace_files_bytes.decode("ascii").rstrip(
            "\n"
        ),
        "<ENGINE_CANONICAL_PROJECTION_MANIFEST_JSON>": projection_bytes.decode("ascii").rstrip(
            "\n"
        ),
        "<ENGINE_CANONICAL_OUTPUT_SCHEMA_JSON>": schema_bytes.decode("ascii").rstrip("\n"),
    }
    rendered = PHYSICS_AUDITOR_PROMPT_TEMPLATE_TEXT
    for marker, value in replacements.items():
        if rendered.count(marker) != 1:
            raise ValueError("Physics Auditor prompt template marker is not unique")
        rendered = rendered.replace(marker, value)
    content = rendered.encode("ascii")
    if not content.endswith(b"\n"):
        raise ValueError("Physics Auditor prompt must end with one canonical newline")
    if len(content) > MAX_PROMPT_BYTES:
        raise ValueError("Physics Auditor prompt exceeds the qualified adapter limit")
    return RenderedPhysicsAuditorPrompt(
        content=content,
        template_version=PHYSICS_AUDITOR_PROMPT_TEMPLATE_VERSION,
        template_sha256=PHYSICS_AUDITOR_PROMPT_TEMPLATE_SHA256,
        contract_sha256=contract.canonical_sha256(),
        evidence_index_sha256=evidence_index.canonical_sha256(),
        changed_path_manifest_sha256=changed_paths.canonical_sha256(),
        projection_manifest_sha256=projection_manifest.canonical_sha256(),
        output_schema_sha256=hashlib.sha256(schema_bytes).hexdigest(),
        rendered_sha256=hashlib.sha256(content).hexdigest(),
        byte_count=len(content),
    )
