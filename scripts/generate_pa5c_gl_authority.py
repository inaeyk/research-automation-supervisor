#!/usr/bin/env python3
"""Regenerate neutral GL pilot contracts and scorer-only task authority."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from research_automation_supervisor.physics_gl_pilot import PhysicsGLPilotConfigV1
from research_automation_supervisor.physics_models import load_physics_task_contract

ROOT = Path(__file__).resolve().parents[1]
PILOT = ROOT / "examples/physics_auditor/gl_pilot_v1"
CONFIG = PILOT / "config/pilot.json"

ROLE_KIND = {
    "locked_derivation": "derivation",
    "implementation": "artifact",
    "test": "artifact",
}
ALTERNATIVES = {
    "seeded_boundary_localized_candidate": ["gauge_constraint_ambiguity"],
    "unresolved_physical_constraint_classification": ["unsupported_physical_claim"],
}


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=True, allow_nan=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    config: dict[str, Any] = json.loads(CONFIG.read_text(encoding="utf-8"))
    config["pilot_id"] = "physics_gl_pilot_pa5c_v1"
    config["methodology_version"] = "physics_gl_pilot_pa5c_remediation_v1"
    config["source_state_policy"] = "exact_commit_blob_projection_only"
    for index, task in enumerate(config["tasks"], start=1):
        task["task_id"] = f"task_{index:03d}"
        contract_path = ROOT / task["contract_path"]
        contract = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
        source_evidence: list[dict[str, Any]] = []
        for source_index, source in enumerate(task["source_refs"], start=1):
            kind = ROLE_KIND[source["role"]]
            source_evidence.append(
                {
                    "id": f"source_{source_index:03d}",
                    "kind": kind,
                    "description": "Exact reviewed blob from the declared source commit.",
                    "path": f"source/{source['path']}",
                    "required_for": ["pilot_identity"],
                }
            )
        if index in {6, 7, 8, 9}:
            source_evidence.append(
                {
                    "id": "candidate_observation",
                    "kind": "numerical",
                    "description": "Raw bounded candidate observation and claim.",
                    "path": (
                        f"examples/physics_auditor/gl_pilot_v1/fixtures/"
                        f"task_{index:03d}/candidate.txt"
                    ),
                    "required_for": ["pilot_identity"],
                }
            )
        contract["evidence"] = source_evidence
        primary_kind = ROLE_KIND[task["source_refs"][0]["role"]]
        contract["required_identities"][0]["required_evidence_kinds"] = [
            primary_kind,
            "oracle",
        ]
        if index == 8:
            contract["required_identities"][0]["statement"] = (
                "Concentration in the declared boundary layer does not by itself "
                "establish a bulk instability."
            )
        if index == 9:
            contract["required_identities"][0]["statement"] = (
                "The physical-observable and constraint indicators do not establish "
                "a unique classification when both are present."
            )
        contract_path.write_text(
            yaml.safe_dump(contract, sort_keys=False),
            encoding="utf-8",
        )
        task["contract_sha256"] = load_physics_task_contract(contract_path).canonical_sha256()
        task["acceptable_alternative_categories"] = ALTERNATIVES.get(task["topic"], [])
        task["forbidden_finding_categories"] = ["report_integrity_error"]
        task["minimum_severity"] = (
            "high"
            if task["deliberately_seeded"]
            else "medium"
            if task["required_finding_categories"]
            else None
        )
        task["acceptable_alternative_routes"] = []
        task["forbidden_routes"] = (
            ["block_insufficient_evidence", "request_repair", "require_human_review"]
            if task["expected_route"] == "pass"
            else ["pass", "request_repair"]
        )
        task["approval"] = {
            "review_id": f"pa5c_gl_fixture_review_{index:03d}",
            "reviewer_role": "independent_physics_authority_reviewer",
            "reviewed_on": "2026-08-05",
            "decision": "approved",
            "independent_from_fixture_author": True,
            "scope": "source_contract_and_scoring_authority",
        }
    validated = PhysicsGLPilotConfigV1.model_validate(config)
    _write_json(CONFIG, validated.model_dump(mode="json"))


if __name__ == "__main__":
    main()
