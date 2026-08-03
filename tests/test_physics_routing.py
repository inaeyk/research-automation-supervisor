from __future__ import annotations

import copy
import socket
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest

from research_automation_supervisor.physics_models import (
    PhysicsAuditPolicyV1,
    PhysicsAuditReportV1,
    PhysicsFindingV1,
    PhysicsTaskContractV1,
    load_physics_audit_report,
    load_physics_task_contract,
)
from research_automation_supervisor.physics_routing import (
    derive_physics_audit_decision,
)

FIXTURES = Path(__file__).parent / "fixtures/physics"


def _loaded(name: str) -> tuple[PhysicsTaskContractV1, PhysicsAuditReportV1]:
    contract = load_physics_task_contract(FIXTURES / "full_contract.yaml")
    return contract, load_physics_audit_report(FIXTURES / name, contract)


@pytest.mark.parametrize(
    ("report_name", "outcome", "required_rule"),
    [
        ("passing_report.json", "pass", "all_required_checks_passed"),
        ("repairable_report.json", "request_repair", "high_repairable_finding"),
        (
            "convention_change_report.json",
            "require_human_review",
            "convention_change_requires_human",
        ),
        (
            "insufficient_evidence_report.json",
            "block_insufficient_evidence",
            "insufficient_required_evidence",
        ),
    ],
)
def test_positive_routing_fixtures(
    report_name: str,
    outcome: str,
    required_rule: str,
) -> None:
    contract, report = _loaded(report_name)
    assert contract.audit_policy is not None

    decision = derive_physics_audit_decision(contract, contract.audit_policy, report)

    assert decision.outcome == outcome
    assert required_rule in {item.rule for item in decision.rules}
    assert decision.authoritative is True


@pytest.mark.parametrize(
    ("trigger", "category", "rule"),
    [
        (
            "unresolved_gauge_constraint_ambiguity",
            "gauge_constraint_ambiguity",
            "gauge_constraint_ambiguity_requires_human",
        ),
        (
            "new_physical_interpretation",
            "new_physical_interpretation",
            "new_physical_interpretation_requires_human",
        ),
    ],
)
def test_gauge_and_interpretation_human_gates_are_unconditional(
    trigger: str,
    category: str,
    rule: str,
) -> None:
    contract, source = _loaded("convention_change_report.json")
    value = source.model_dump(mode="json")
    value["human_gate_triggers"] = [trigger]
    value["findings"][0]["category"] = category
    report = PhysicsAuditReportV1.model_validate(value)
    assert contract.audit_policy is not None

    decision = derive_physics_audit_decision(contract, contract.audit_policy, report)

    assert decision.outcome == "require_human_review"
    assert rule in {item.rule for item in decision.rules}


def test_report_self_verdict_cannot_override_human_gate() -> None:
    contract, repairable = _loaded("repairable_report.json")
    value = repairable.model_dump(mode="json")
    value["human_gate_triggers"] = ["convention_change"]
    report = PhysicsAuditReportV1.model_validate(value)
    assert report.verdict == "fail_repairable"
    assert contract.audit_policy is not None

    decision = derive_physics_audit_decision(contract, contract.audit_policy, report)

    assert decision.outcome == "require_human_review"
    assert decision.rules[-1].rule == "report_verdict_overridden"


def test_report_integrity_error_routes_to_infrastructure_failure() -> None:
    contract, repairable = _loaded("repairable_report.json")
    value = repairable.model_dump(mode="json")
    value["verdict"] = "infrastructure_failure"
    finding = value["findings"][0]
    finding["category"] = "report_integrity_error"
    finding["disposition"] = "infrastructure_failure"
    report = PhysicsAuditReportV1.model_validate(value)
    assert contract.audit_policy is not None

    decision = derive_physics_audit_decision(contract, contract.audit_policy, report)

    assert decision.outcome == "infrastructure_failure"
    assert decision.rules[0].rule == "report_integrity_error"


def test_conflicting_evidence_follows_explicit_policy() -> None:
    contract, insufficient = _loaded("insufficient_evidence_report.json")
    contract_value = contract.model_dump(mode="json")
    contract_value["audit_policy"] = None
    contract_without_policy = PhysicsTaskContractV1.model_validate(contract_value)
    report_value = insufficient.model_dump(mode="json")
    report_value["evidence_sufficiency"] = "conflicting"
    unresolved = next(
        item for item in report_value["checks"] if item["id"] == "check_trace_free"
    )
    unresolved["evidence_sufficiency"] = "conflicting"
    report = PhysicsAuditReportV1.model_validate(report_value)
    review_policy = PhysicsAuditPolicyV1(
        insufficient_required_evidence="block",
        conflicting_required_evidence="human_review",
        medium_severity="request_repair",
        low_severity="allow_pass",
        informational_severity="allow_pass",
    )
    block_value = review_policy.model_dump(mode="json")
    block_value["conflicting_required_evidence"] = "block"
    block_policy = PhysicsAuditPolicyV1.model_validate(block_value)

    review = derive_physics_audit_decision(
        contract_without_policy, review_policy, report
    )
    block = derive_physics_audit_decision(
        contract_without_policy, block_policy, report
    )

    assert review.outcome == "require_human_review"
    assert block.outcome == "block_insufficient_evidence"


@pytest.mark.parametrize(
    ("severity", "configured_route", "outcome"),
    [
        ("medium", "request_repair", "request_repair"),
        ("medium", "require_human_review", "require_human_review"),
        ("medium", "allow_pass", "pass"),
        ("low", "request_repair", "request_repair"),
        ("low", "allow_pass", "pass"),
        ("informational", "allow_pass", "pass"),
    ],
)
def test_advisory_findings_follow_explicit_policy(
    severity: str,
    configured_route: str,
    outcome: str,
) -> None:
    contract, passing = _loaded("passing_report.json")
    contract_value = contract.model_dump(mode="json")
    contract_value["audit_policy"] = None
    contract_without_policy = PhysicsTaskContractV1.model_validate(contract_value)
    report_value = passing.model_dump(mode="json")
    report_value["findings"] = [
        {
            "id": "advisory_finding",
            "severity": severity,
            "category": "continuum_discrete_mismatch",
            "status": "open",
            "disposition": "repairable",
            "check_ids": ["check_trace_free"],
            "forbidden_claim_ids": [],
            "evidence": [
                {
                    "kind": "source",
                    "reference": None,
                    "path": "src/physics/implementation.py",
                    "line_start": 10,
                    "line_end": 10,
                }
            ],
            "statement": "Synthetic advisory observation.",
            "required_action": "Follow the explicit deterministic policy.",
        }
    ]
    report = PhysicsAuditReportV1.model_validate(report_value)
    policy_value = cast(dict[str, Any], copy.deepcopy(contract.audit_policy.model_dump()))
    policy_value[f"{severity}_severity"] = configured_route
    policy = PhysicsAuditPolicyV1.model_validate(policy_value)

    decision = derive_physics_audit_decision(contract_without_policy, policy, report)

    assert decision.outcome == outcome


def test_policy_mismatch_with_contract_is_infrastructure_failure() -> None:
    contract, report = _loaded("passing_report.json")
    assert contract.audit_policy is not None
    policy_value = contract.audit_policy.model_dump(mode="json")
    policy_value["low_severity"] = "request_repair"
    changed_policy = PhysicsAuditPolicyV1.model_validate(policy_value)

    decision = derive_physics_audit_decision(contract, changed_policy, report)

    assert decision.outcome == "infrastructure_failure"
    assert decision.rules[0].rule == "policy_contract_mismatch"


def test_router_is_pure_and_repeated_output_is_byte_identical(monkeypatch) -> None:
    contract, report = _loaded("passing_report.json")
    assert contract.audit_policy is not None

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("pure routing crossed an external boundary")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(Path, "write_text", forbidden)
    monkeypatch.setattr(Path, "write_bytes", forbidden)
    monkeypatch.setattr(Path, "mkdir", forbidden)
    monkeypatch.setattr(
        "research_automation_supervisor.codex_adapter.run_prepared_codex",
        forbidden,
    )

    decisions = [
        derive_physics_audit_decision(contract, contract.audit_policy, report)
        for _ in range(5)
    ]

    assert len({item.to_canonical_json() for item in decisions}) == 1
    assert len({item.canonical_sha256() for item in decisions}) == 1


def test_schema_invalid_raw_report_never_passes() -> None:
    contract, _ = _loaded("passing_report.json")
    assert contract.audit_policy is not None

    decision = derive_physics_audit_decision(
        contract,
        contract.audit_policy,
        {"schema_version": 1, "verdict": "pass"},
    )

    assert decision.outcome == "infrastructure_failure"
    assert decision.rules[0].rule == "report_schema_invalid"


@pytest.mark.parametrize(
    ("category", "status", "disposition", "outcome"),
    [
        (
            "convention_change_requested",
            "resolved",
            "human_review",
            "require_human_review",
        ),
        (
            "oracle_failure",
            "open",
            "infrastructure_failure",
            "infrastructure_failure",
        ),
    ],
)
def test_router_defensively_rejects_bypass_constructed_findings(
    category: str,
    status: str,
    disposition: str,
    outcome: str,
) -> None:
    contract, passing = _loaded("passing_report.json")
    assert contract.audit_policy is not None
    evidence = passing.checks[0].evidence
    finding = PhysicsFindingV1.model_construct(
        id="bypass_finding",
        severity="low",
        category=category,
        status=status,
        disposition=disposition,
        check_ids=("check_analytic_identity",),
        forbidden_claim_ids=(),
        evidence=evidence,
        statement="A bypass-constructed report is still untrusted.",
        required_action="Route fail closed.",
    )
    bypass = passing.model_copy(update={"findings": (finding,)})

    decision = derive_physics_audit_decision(
        contract,
        contract.audit_policy,
        bypass,
    )

    assert decision.outcome == outcome
