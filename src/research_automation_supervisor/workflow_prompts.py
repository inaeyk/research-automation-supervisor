"""Deterministic append-only assembly of exact human-written Stage 2 prompts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from research_automation_supervisor.git_evidence import GitBaseline, GitEvidence
from research_automation_supervisor.structured_outputs import (
    normalize_production_schema,
)
from research_automation_supervisor.test_runner import TestAttemptResult
from research_automation_supervisor.workflow_models import (
    AuditorModelResult,
    HumanFile,
    PreparedSubstage,
    WorkerModelResult,
)

APPENDIX_HEADER = b"\n\n--- RESEARCH AUTOMATION SUPERVISOR APPENDIX (FIXED) ---\n"
CONTRACT_HEADER = b"\n[BEGIN FROZEN HUMAN CONTRACT]\n"
CONTRACT_FOOTER = b"\n[END FROZEN HUMAN CONTRACT]\n"
EVIDENCE_HEADER = b"\n[BEGIN DETERMINISTIC EVIDENCE]\n"
EVIDENCE_FOOTER = b"[END DETERMINISTIC EVIDENCE]\n"
SCHEMA_HEADER = b"\n[BEGIN ENGINE-OWNED OUTPUT SCHEMA]\n"
SCHEMA_FOOTER = b"[END ENGINE-OWNED OUTPUT SCHEMA]\n"

WORKER_OUTPUT_SCHEMA: dict[str, object] = normalize_production_schema({
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "schema_version",
        "status",
        "summary",
        "changed_files",
        "assumptions",
        "questions",
    ],
    "properties": {
        "schema_version": {"const": 1},
        "status": {"enum": ["completed", "blocked", "needs_human"]},
        "summary": {"type": "string", "maxLength": 16384},
        "changed_files": {
            "type": "array",
            "maxItems": 200,
            "items": {"type": "string"},
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
    },
})

AUDITOR_OUTPUT_SCHEMA: dict[str, object] = normalize_production_schema({
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "schema_version",
        "verdict",
        "summary",
        "scope_compliant",
        "contract_satisfied",
        "findings",
        "human_questions",
    ],
    "properties": {
        "schema_version": {"const": 1},
        "verdict": {"enum": ["pass", "fail_repairable", "escalate"]},
        "summary": {"type": "string", "maxLength": 16384},
        "scope_compliant": {"type": "boolean"},
        "contract_satisfied": {"type": "boolean"},
        "findings": {
            "type": "array",
            "maxItems": 200,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "id",
                    "severity",
                    "category",
                    "file",
                    "line",
                    "evidence",
                    "required_fix",
                ],
                "properties": {
                    "id": {"type": "string"},
                    "severity": {"enum": ["critical", "high", "medium", "low"]},
                    "category": {"type": "string", "maxLength": 256},
                    "file": {"type": ["string", "null"]},
                    "line": {"type": ["integer", "null"], "minimum": 1},
                    "evidence": {"type": "string", "maxLength": 16384},
                    "required_fix": {"type": "string", "maxLength": 16384},
                },
            },
        },
        "human_questions": {
            "type": "array",
            "maxItems": 200,
            "items": {"type": "string", "maxLength": 16384},
        },
    },
})


@dataclass(frozen=True)
class RenderedWorkflowPrompt:
    """In-memory prompt plus only the metadata that may be persisted."""

    content: bytes
    source_path: Path
    source_sha256: str
    contract_sha256: str
    evidence_sha256: dict[str, str]
    rendered_sha256: str
    byte_count: int
    kind: str

    def manifest(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "kind": self.kind,
            "source_path": str(self.source_path),
            "source_sha256": self.source_sha256,
            "contract_sha256": self.contract_sha256,
            "evidence_sha256": dict(sorted(self.evidence_sha256.items())),
            "rendered_prompt_sha256": self.rendered_sha256,
            "rendered_prompt_byte_count": self.byte_count,
        }


def build_initial_worker_prompt(
    prepared: PreparedSubstage,
    baseline: GitBaseline,
) -> RenderedWorkflowPrompt:
    """Append the frozen contract, normalized substage, tests, paths, and baseline."""
    specification = prepared.specification
    evidence: dict[str, object] = {
        "substage": {
            "schema_version": specification.schema_version,
            "substage_id": specification.substage_id,
            "title": specification.title,
            "workspace": str(prepared.workspace),
            "worker_model": specification.worker_model,
            "worker_reasoning_effort": specification.worker_reasoning_effort,
            "max_repair_rounds": specification.max_repair_rounds,
            "checkpoint_after": specification.checkpoint_after,
        },
        "scope": {
            "allowed_paths": list(specification.allowed_paths),
            "protected_paths": list(specification.protected_paths),
        },
        "acceptance_tests": [
            {"id": test.specification.id, "argv": list(test.specification.argv)}
            for test in prepared.acceptance_tests
        ],
        "baseline": baseline.to_dict(),
    }
    return _assemble(
        prepared.worker_initial_prompt,
        prepared.contract,
        evidence,
        WORKER_OUTPUT_SCHEMA,
        "initial_worker",
        "Return only one JSON object satisfying the worker schema. Model prose never controls "
        "workflow state.",
    )


def build_fixed_test_repair_prompt(
    prepared: PreparedSubstage,
    repair_round: int,
    test_results: tuple[TestAttemptResult, ...],
    git_evidence: GitEvidence,
) -> RenderedWorkflowPrompt:
    """Build a deterministic repair turn for a fixed-test or scope failure."""
    evidence: dict[str, object] = {
        "repair_round": repair_round,
        "test_results": [result.to_dict() for result in test_results],
        "scope": {
            "scope_compliant": git_evidence.scope_compliant,
            "findings": [item.model_dump(mode="json") for item in git_evidence.scope_findings],
            "changed_paths": [item.model_dump(mode="json") for item in git_evidence.changed_paths],
        },
        "bounded_logs": [
            {
                "test_id": result.test_id,
                "stdout_artifact": result.stdout_artifact,
                "stdout_sha256": result.stdout_sha256,
                "stderr_artifact": result.stderr_artifact,
                "stderr_sha256": result.stderr_sha256,
            }
            for result in test_results
            if result.status != "skipped"
        ],
    }
    return _assemble(
        prepared.worker_repair_prompt,
        prepared.contract,
        evidence,
        WORKER_OUTPUT_SCHEMA,
        "fixed_test_or_scope_repair",
        "Repair only under the frozen contract, then return only one JSON object satisfying the "
        "worker schema.",
    )


def build_audit_repair_prompt(
    prepared: PreparedSubstage,
    repair_round: int,
    audit: AuditorModelResult,
    test_results: tuple[TestAttemptResult, ...],
    git_evidence: GitEvidence,
) -> RenderedWorkflowPrompt:
    """Build a deterministic repair turn from validated auditor findings."""
    evidence: dict[str, object] = {
        "repair_round": repair_round,
        "auditor_result": audit.model_dump(mode="json"),
        "tests": [result.to_dict() for result in test_results],
        "scope": {
            "scope_compliant": git_evidence.scope_compliant,
            "findings": [item.model_dump(mode="json") for item in git_evidence.scope_findings],
        },
    }
    return _assemble(
        prepared.worker_repair_prompt,
        prepared.contract,
        evidence,
        WORKER_OUTPUT_SCHEMA,
        "audit_repair",
        "Address the validated findings under the frozen contract, then return only one JSON "
        "object satisfying the worker schema.",
    )


def build_human_continuation_prompt(
    prepared: PreparedSubstage,
    instruction: HumanFile,
    current_state: str,
    repair_round: int,
    test_results: tuple[TestAttemptResult, ...],
    git_evidence: GitEvidence | None,
    audit: AuditorModelResult | None,
) -> RenderedWorkflowPrompt:
    """Append unresolved evidence to exact human continuation bytes."""
    evidence: dict[str, object] = {
        "current_state": current_state,
        "repair_round": repair_round,
        "tests": [result.to_dict() for result in test_results],
        "scope": None
        if git_evidence is None
        else {
            "scope_compliant": git_evidence.scope_compliant,
            "findings": [item.model_dump(mode="json") for item in git_evidence.scope_findings],
        },
        "audit": None if audit is None else audit.model_dump(mode="json"),
    }
    return _assemble(
        instruction,
        prepared.contract,
        evidence,
        WORKER_OUTPUT_SCHEMA,
        "human_continuation",
        "Follow the exact human instruction without changing the frozen contract, then return "
        "only one JSON object satisfying the worker schema.",
    )


def build_auditor_prompt(
    prepared: PreparedSubstage,
    baseline: GitBaseline,
    git_evidence: GitEvidence,
    patch_bytes: bytes,
    worker_result: WorkerModelResult,
    test_results: tuple[TestAttemptResult, ...],
    prior_audits: tuple[AuditorModelResult, ...],
) -> RenderedWorkflowPrompt:
    """Build one fresh auditor prompt with complete bounded local evidence."""
    patch_text = patch_bytes.decode("utf-8", errors="replace")
    evidence: dict[str, object] = {
        "instruction": "Inspect the current workspace directly; this evidence is not a "
        "substitute for direct inspection.",
        "substage": {
            "substage_id": prepared.specification.substage_id,
            "title": prepared.specification.title,
            "workspace": str(prepared.workspace),
            "allowed_paths": list(prepared.specification.allowed_paths),
            "protected_paths": list(prepared.specification.protected_paths),
        },
        "baseline": baseline.to_dict(),
        "current_git": git_evidence.to_dict(),
        "patch_evidence": {
            "complete": git_evidence.patch_complete,
            "sha256": git_evidence.patch_sha256,
            "byte_count": git_evidence.patch_byte_count,
            "content": patch_text,
        },
        "worker_result": worker_result.model_dump(mode="json"),
        "tests": [result.to_dict() for result in test_results],
        "prior_audits": [audit.model_dump(mode="json") for audit in prior_audits],
    }
    return _assemble(
        prepared.auditor_prompt,
        prepared.contract,
        evidence,
        AUDITOR_OUTPUT_SCHEMA,
        "auditor",
        "Inspect the workspace directly and return only one JSON object satisfying the auditor "
        "schema.",
    )


def write_output_schemas(directory: Path) -> tuple[Path, Path]:
    """Write the two fixed engine-owned schemas used by the Codex adapter."""
    directory.mkdir(parents=True, exist_ok=True)
    worker = directory / "worker-output-schema.json"
    auditor = directory / "auditor-output-schema.json"
    worker.write_bytes(_canonical_json(WORKER_OUTPUT_SCHEMA))
    auditor.write_bytes(_canonical_json(AUDITOR_OUTPUT_SCHEMA))
    return worker, auditor


def _assemble(
    source: HumanFile,
    contract: HumanFile,
    evidence: dict[str, object],
    schema: dict[str, object],
    kind: Literal[
        "initial_worker",
        "fixed_test_or_scope_repair",
        "audit_repair",
        "human_continuation",
        "auditor",
    ],
    reporting_instruction: str,
) -> RenderedWorkflowPrompt:
    evidence_bytes = _canonical_json(evidence)
    schema_bytes = _canonical_json(schema)
    instruction_bytes = ("Reporting instruction: " + reporting_instruction + "\n").encode("utf-8")
    content = b"".join(
        (
            source.content,
            APPENDIX_HEADER,
            CONTRACT_HEADER,
            contract.content,
            CONTRACT_FOOTER,
            EVIDENCE_HEADER,
            evidence_bytes,
            EVIDENCE_FOOTER,
            SCHEMA_HEADER,
            schema_bytes,
            SCHEMA_FOOTER,
            instruction_bytes,
        )
    )
    hashes = {
        "evidence": hashlib.sha256(evidence_bytes).hexdigest(),
        "output_schema": hashlib.sha256(schema_bytes).hexdigest(),
        "reporting_instruction": hashlib.sha256(instruction_bytes).hexdigest(),
    }
    return RenderedWorkflowPrompt(
        content=content,
        source_path=source.path,
        source_sha256=source.sha256,
        contract_sha256=contract.sha256,
        evidence_sha256=hashes,
        rendered_sha256=hashlib.sha256(content).hexdigest(),
        byte_count=len(content),
        kind=kind,
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
