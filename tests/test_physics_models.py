from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, cast

import pytest
import yaml  # type: ignore[import-untyped]
from pydantic import ValidationError

from research_automation_supervisor.errors import PhysicsAuditError, PhysicsContractError
from research_automation_supervisor.physics_models import (
    PHYSICS_AUDIT_REPORT_OUTPUT_SCHEMA,
    PhysicsAuditReportV1,
    PhysicsTaskContractV1,
    load_physics_audit_report,
    load_physics_task_contract,
    parse_physics_audit_report_json,
)
from research_automation_supervisor.physics_routing import (
    derive_physics_audit_decision,
)
from research_automation_supervisor.structured_outputs import validate_production_schema

FIXTURES = Path(__file__).parent / "fixtures/physics"
NEGATIVE_CASES = json.loads(
    (FIXTURES / "negative_cases.json").read_text(encoding="utf-8")
)


def _contract_data() -> dict[str, Any]:
    return cast(
        dict[str, Any],
        yaml.safe_load((FIXTURES / "full_contract.yaml").read_text(encoding="utf-8")),
    )


def _report_data(name: str = "passing_report.json") -> dict[str, Any]:
    return cast(
        dict[str, Any], json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    )


def _invalid_contract(case: str) -> dict[str, Any]:
    value = copy.deepcopy(_contract_data())
    if case == "unknown_field":
        value["unknown"] = True
    elif case == "duplicate_convention_ids":
        value["conventions"].append(copy.deepcopy(value["conventions"][0]))
    elif case == "whitespace_identifier":
        value["assumptions"][0]["id"] = "   "
    elif case == "invalid_convention_authority":
        value["conventions"][0]["authority"] = "model_selected"
    elif case == "absolute_evidence_path":
        value["evidence"][0]["path"] = "/private/derivation.md"
    elif case == "drive_relative_evidence_path":
        value["evidence"][0]["path"] = "C:private/derivation.md"
    elif case == "scheme_evidence_path":
        value["evidence"][0]["path"] = "file:/private/derivation.md"
    elif case == "traversal_evidence_path":
        value["evidence"][0]["path"] = "../private/derivation.md"
    elif case == "undeclared_oracle_reference":
        value["required_identities"][0]["oracle_ids"].append("missing_oracle")
    elif case == "undeclared_evidence_requirement":
        value["evidence"][0]["required_for"].append("missing_check")
    elif case == "contradictory_not_applicable_convention":
        value["conventions"][0]["authority"] = "not_applicable"
    elif case == "missing_profile_required_collection":
        value["limiting_cases"] = []
    elif case == "missing_mandatory_human_gate":
        value["human_gate"]["required_for"].remove("convention_change")
    elif case == "unsupported_profile":
        value["profile"] = "scientific_claim_review"
    elif case == "unsupported_schema_version":
        value["schema_version"] = 2
    elif case == "mapping_instead_of_ordered_list":
        value["conventions"] = {"metric_signature": value["conventions"][0]}
    else:  # pragma: no cover - fixture/test synchronization guard
        raise AssertionError(case)
    return value


def _invalid_report(case: str) -> dict[str, Any]:
    value = copy.deepcopy(_report_data())
    if case in {
        "duplicate_finding_ids",
        "finding_without_evidence",
        "pass_with_critical_finding",
        "convention_change_marked_repairable",
    }:
        source = (
            "convention_change_report.json"
            if case == "convention_change_marked_repairable"
            else "repairable_report.json"
        )
        value = copy.deepcopy(_report_data(source))
    elif case == "pass_with_insufficient_evidence":
        value = copy.deepcopy(_report_data("insufficient_evidence_report.json"))

    if case == "unknown_field":
        value["unknown"] = True
    elif case == "duplicate_finding_ids":
        value["findings"].append(copy.deepcopy(value["findings"][0]))
    elif case == "duplicate_check_ids":
        value["checks"].append(copy.deepcopy(value["checks"][0]))
    elif case == "absolute_source_path":
        reference = value["checks"][2]["evidence"][2]
        reference["path"] = "/private/source.py"
    elif case == "drive_relative_source_path":
        value["checks"][2]["evidence"][2]["path"] = "C:private/source.py"
    elif case == "scheme_source_path":
        value["checks"][2]["evidence"][2]["path"] = "file:/private/source.py"
    elif case == "traversal_document_path":
        reference = value["checks"][2]["evidence"][2]
        reference.update(
            {
                "kind": "document",
                "path": "../private/note.md",
                "line_start": None,
                "line_end": None,
            }
        )
    elif case == "negative_line_number":
        value["checks"][2]["evidence"][2]["line_start"] = -1
    elif case == "reversed_line_range":
        reference = value["checks"][2]["evidence"][2]
        reference["line_start"] = 20
        reference["line_end"] = 10
    elif case == "undeclared_oracle_reference":
        value["checks"][0]["evidence"][0]["reference"] = "missing_oracle"
    elif case == "undeclared_test_reference":
        value["checks"][2]["evidence"][1]["reference"] = "missing_test"
    elif case == "finding_without_evidence":
        value["findings"][0]["evidence"] = []
    elif case == "pass_with_critical_finding":
        value["verdict"] = "pass"
        value["findings"][0]["severity"] = "critical"
    elif case == "pass_with_insufficient_evidence":
        value["verdict"] = "pass"
    elif case == "contradictory_check_status_and_verdict":
        value["checks"][0]["status"] = "unresolved"
    elif case == "mapping_instead_of_ordered_list":
        value["checks"] = {"check": value["checks"][0]}
    elif case == "convention_change_marked_repairable":
        value["human_gate_triggers"] = []
        value["verdict"] = "fail_repairable"
        value["findings"][0]["disposition"] = "repairable"
    elif case in {"resolved_human_gate_bypass", "nonintegrity_infrastructure_bypass"}:
        finding = {
            "id": "bypass_finding",
            "severity": "low",
            "category": "convention_change_requested",
            "status": "resolved",
            "disposition": "human_review",
            "check_ids": ["check_trace_free"],
            "forbidden_claim_ids": [],
            "evidence": [
                {
                    "kind": "task_contract",
                    "reference": "conventions.fourier_convention",
                    "path": None,
                    "line_start": None,
                    "line_end": None,
                }
            ],
            "statement": "A report cannot resolve its own mandatory human gate.",
            "required_action": "Require deterministic fail-closed routing.",
        }
        if case == "nonintegrity_infrastructure_bypass":
            finding.update(
                {
                    "category": "oracle_failure",
                    "status": "open",
                    "disposition": "infrastructure_failure",
                }
            )
        value["findings"] = [finding]
    elif case == "gauge_ambiguity_marked_pass":
        value["human_gate_triggers"] = [
            "unresolved_gauge_constraint_ambiguity"
        ]
    elif case == "new_interpretation_marked_pass":
        value["human_gate_triggers"] = ["new_physical_interpretation"]
    elif case not in {
        "duplicate_finding_ids",
        "finding_without_evidence",
        "pass_with_critical_finding",
        "pass_with_insufficient_evidence",
    }:  # pragma: no cover - fixture/test synchronization guard
        raise AssertionError(case)
    return value


def test_positive_contract_fixtures_are_strict_and_canonical() -> None:
    minimal = load_physics_task_contract(FIXTURES / "minimal_contract.yaml")
    full = load_physics_task_contract(FIXTURES / "full_contract.yaml")

    assert minimal.schema_version == 1
    assert full.profile == "physics_implementation"
    assert tuple(item.id for item in full.conventions) == (
        "fourier_convention",
        "metric_signature",
    )
    assert full.canonical_sha256() == full.canonical_sha256()
    assert full.to_canonical_json().endswith(b"\n")


def test_report_schema_is_in_existing_production_subset() -> None:
    validate_production_schema(PHYSICS_AUDIT_REPORT_OUTPUT_SCHEMA)
    assert PHYSICS_AUDIT_REPORT_OUTPUT_SCHEMA["additionalProperties"] is False


@pytest.mark.parametrize("case", NEGATIVE_CASES["contract_cases"])
def test_negative_contract_fixtures_are_rejected(case: str) -> None:
    with pytest.raises(ValidationError):
        PhysicsTaskContractV1.model_validate(_invalid_contract(case))


@pytest.mark.parametrize("case", NEGATIVE_CASES["report_cases"])
def test_negative_report_fixtures_fail_closed_in_routing(case: str) -> None:
    contract = load_physics_task_contract(FIXTURES / "full_contract.yaml")
    assert contract.audit_policy is not None

    decision = derive_physics_audit_decision(
        contract,
        contract.audit_policy,
        _invalid_report(case),
    )

    assert decision.outcome == "infrastructure_failure"
    assert decision.rules[0].rule in {
        "report_schema_invalid",
        "report_reference_integrity_invalid",
    }


def test_malformed_yaml_and_json_fixtures_use_safe_domain_errors() -> None:
    contract = load_physics_task_contract(FIXTURES / "full_contract.yaml")

    with pytest.raises(PhysicsContractError, match="malformed physics contract"):
        load_physics_task_contract(FIXTURES / "malformed_contract.yaml")
    with pytest.raises(PhysicsAuditError, match="malformed physics audit report"):
        load_physics_audit_report(FIXTURES / "malformed_report.json", contract)
    with pytest.raises(PhysicsAuditError, match="missing or invalid"):
        parse_physics_audit_report_json(b'{"schema_version": NaN}', contract)


def test_unsorted_inputs_canonicalize_to_identical_bytes_and_hashes() -> None:
    contract_data = _contract_data()
    reversed_contract = copy.deepcopy(contract_data)
    for field in (
        "conventions",
        "assumptions",
        "required_identities",
        "limiting_cases",
        "evidence",
        "oracles",
        "forbidden_claims",
    ):
        reversed_contract[field].reverse()
    contract_a = PhysicsTaskContractV1.model_validate(contract_data)
    contract_b = PhysicsTaskContractV1.model_validate(reversed_contract)

    report_data = _report_data()
    reversed_report = copy.deepcopy(report_data)
    reversed_report["checks"].reverse()
    for check in reversed_report["checks"]:
        check["evidence"].reverse()
    report_a = PhysicsAuditReportV1.model_validate(report_data)
    report_b = PhysicsAuditReportV1.model_validate(reversed_report)

    assert contract_a.to_canonical_json() == contract_b.to_canonical_json()
    assert contract_a.canonical_sha256() == contract_b.canonical_sha256()
    assert report_a.to_canonical_json() == report_b.to_canonical_json()
    assert report_a.canonical_sha256() == report_b.canonical_sha256()


def test_contract_and_report_reference_closure_accepts_only_declared_forms() -> None:
    contract = load_physics_task_contract(FIXTURES / "full_contract.yaml")
    report = load_physics_audit_report(FIXTURES / "passing_report.json", contract)

    assert report.verdict == "pass"
    assert {item.target_id for item in report.checks} == {
        "analytic_identity",
        "background_limit_oracle",
        "trace_free_A",
        "trace_free_oracle",
        "uniform_background",
    }
