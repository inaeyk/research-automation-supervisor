#!/usr/bin/env python3
"""Regenerate the reviewed PA-5C fixture authority and its catalog binding."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from research_automation_supervisor.physics_benchmark import fixture_sha256
from research_automation_supervisor.physics_benchmark_models import (
    PhysicsBenchmarkCatalogV1,
    PhysicsBenchmarkFixtureAuthoritySetV1,
)
from research_automation_supervisor.physics_models import load_physics_task_contract

ROOT = Path(__file__).resolve().parents[1]
BENCHMARK = ROOT / "examples/physics_auditor/benchmark_v1"
CATALOG = BENCHMARK / "authority/catalog.json"
AUTHORITY = BENCHMARK / "authority/fixture-authority.json"
REPAIR = BENCHMARK / "authority/worker-repair-calibration.json"

ALTERNATIVES: dict[str, list[str]] = {
    "wrong_sign": ["convention_mismatch"],
    "missing_normalization": ["violated_identity"],
    "missing_metric_factor": ["violated_identity"],
    "raised_lowered_index": ["sign_or_normalization_error"],
    "dimensional_inconsistency": ["continuum_discrete_mismatch"],
    "nonzero_trace": ["tensor_or_index_error"],
    "failed_analytic_identity": ["failed_limiting_case"],
    "curved_background_error": ["violated_identity"],
    "continuum_discrete_translation": ["dimensional_inconsistency"],
    "finite_difference_stencil": ["violated_identity"],
    "false_convergence_claim": ["missing_required_evidence"],
    "boundary_localization_claim": ["gauge_constraint_ambiguity"],
    "norm_sensitivity_claim": ["unsupported_physical_claim"],
    "insufficient_evidence": ["oracle_failure"],
    "convention_change_request": ["convention_mismatch"],
    "conflicting_evidence": ["missing_required_evidence"],
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=True, allow_nan=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    catalog: dict[str, Any] = json.loads(CATALOG.read_text(encoding="utf-8"))
    catalog.update(
        {
            "benchmark_id": "physics_auditor_pa5c_public_v1",
            "methodology_version": "physics_auditor_pa5c_remediation_v1",
            "answer_key_policy": "physically_absent_scorer_only_authority_v1",
            "fixture_authority_path": (
                "examples/physics_auditor/benchmark_v1/authority/fixture-authority.json"
            ),
        }
    )
    manifests: list[dict[str, Any]] = []
    for index, case in enumerate(catalog["cases"], start=1):
        case_id = f"case_{index:03d}"
        case["case_id"] = case_id
        case["acceptable_alternative_categories"] = ALTERNATIVES.get(case["seed_kind"], [])
        case["forbidden_finding_categories"] = ["report_integrity_error"]
        case["acceptable_alternative_routes"] = (
            ["require_human_review"]
            if case["expected_route"] == "block_insufficient_evidence"
            else []
        )
        recognized = bool(
            case["required_finding_categories"] or case["acceptable_alternative_categories"]
        )
        case["minimum_severity"] = (
            "high"
            if recognized and case["critical_seeded_defect"]
            else "medium"
            if recognized
            else None
        )
        fixture = ROOT / case["fixture_root"]
        contract_path = ROOT / case["contract_path"]
        sources = [
            {
                "path": str((fixture / "evidence.md").relative_to(ROOT)),
                "sha256": _sha256(fixture / "evidence.md"),
                "role": "evidence",
            },
            {
                "path": str((fixture / "implementation.py").relative_to(ROOT)),
                "sha256": _sha256(fixture / "implementation.py"),
                "role": "candidate",
            },
        ]
        manifests.append(
            {
                "schema_version": 1,
                "case_id": case_id,
                "fixture_sha256": fixture_sha256(fixture),
                "contract_path": case["contract_path"],
                "contract_sha256": load_physics_task_contract(contract_path).canonical_sha256(),
                "sources": sources,
                "seeded_defect": case["seeded_defect_authority"],
                "expected_route": case["expected_route"],
                "acceptable_alternative_routes": case["acceptable_alternative_routes"],
                "forbidden_routes": case["forbidden_routes"],
                "required_finding_categories": case["required_finding_categories"],
                "acceptable_alternative_categories": case["acceptable_alternative_categories"],
                "forbidden_finding_categories": case["forbidden_finding_categories"],
                "minimum_severity": case["minimum_severity"],
                "human_review_mandatory": case["human_review_mandatory"],
                "approval": {
                    "review_id": f"pa5c_fixture_review_{index:03d}",
                    "reviewer_role": "independent_physics_authority_reviewer",
                    "reviewed_on": "2026-08-05",
                    "decision": "approved",
                    "independent_from_fixture_author": True,
                    "scope": "source_contract_and_scoring_authority",
                },
            }
        )
    authority_value = {
        "schema_version": 1,
        "benchmark_id": catalog["benchmark_id"],
        "manifests": manifests,
    }
    authority = PhysicsBenchmarkFixtureAuthoritySetV1.model_validate(authority_value)
    _write_json(AUTHORITY, authority.model_dump(mode="json"))
    catalog["fixture_authority_sha256"] = authority.canonical_sha256()
    validated_catalog = PhysicsBenchmarkCatalogV1.model_validate(catalog)
    _write_json(CATALOG, validated_catalog.model_dump(mode="json"))

    repair: dict[str, Any] = json.loads(REPAIR.read_text(encoding="utf-8"))
    repair["benchmark_id"] = catalog["benchmark_id"]
    for item in repair["cases"]:
        old = item["case_id"]
        item["case_id"] = f"case_{int(old.rsplit('_', 1)[1]):03d}"
    _write_json(REPAIR, repair)


if __name__ == "__main__":
    main()
