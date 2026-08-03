"""Pure deterministic routing for Physics Auditor version-1 reports."""

from __future__ import annotations

from typing import Annotated, Literal, TypeAlias

from pydantic import AfterValidator, BeforeValidator, ConfigDict, Field, ValidationError

from research_automation_supervisor.errors import PhysicsAuditError
from research_automation_supervisor.physics_models import (
    PhysicsAuditPolicyV1,
    PhysicsAuditReportV1,
    PhysicsCanonicalModel,
    PhysicsTaskContractV1,
    PhysicsVerdict,
    validate_physics_audit_report,
)
from research_automation_supervisor.workflow_models import Identifier, _freeze_sequence

PhysicsRoutingOutcome: TypeAlias = Literal[
    "pass",
    "request_repair",
    "require_human_review",
    "block_insufficient_evidence",
    "infrastructure_failure",
]
PhysicsRoutingRule: TypeAlias = Literal[
    "input_contract_invalid",
    "input_policy_invalid",
    "policy_contract_mismatch",
    "report_schema_invalid",
    "report_reference_integrity_invalid",
    "report_integrity_error",
    "convention_change_requires_human",
    "gauge_constraint_ambiguity_requires_human",
    "new_physical_interpretation_requires_human",
    "unresolved_question_requires_human",
    "finding_requires_human",
    "conflicting_required_evidence",
    "insufficient_required_evidence",
    "evidence_blocking_finding",
    "critical_repairable_finding",
    "high_repairable_finding",
    "required_check_failed",
    "required_check_unresolved",
    "medium_finding_policy",
    "low_finding_policy",
    "informational_finding_policy",
    "all_required_checks_passed",
    "report_verdict_overridden",
]

_VERDICT_FOR_OUTCOME: dict[PhysicsRoutingOutcome, PhysicsVerdict] = {
    "pass": "pass",
    "request_repair": "fail_repairable",
    "require_human_review": "human_review",
    "block_insufficient_evidence": "blocked_insufficient_evidence",
    "infrastructure_failure": "infrastructure_failure",
}


def _canonical_subjects(value: tuple[str, ...]) -> tuple[str, ...]:
    if len(value) != len(set(value)):
        raise ValueError("routing proof subjects must be unique")
    return tuple(sorted(value))


class PhysicsRoutingRuleProofV1(PhysicsCanonicalModel):
    """One bounded reason code fired by deterministic routing."""

    rule: PhysicsRoutingRule
    outcome: PhysicsRoutingOutcome
    subject_ids: Annotated[
        tuple[Identifier, ...],
        BeforeValidator(_freeze_sequence),
        AfterValidator(_canonical_subjects),
        Field(max_length=200),
    ]

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        str_strip_whitespace=True,
    )


class PhysicsRoutingDecisionV1(PhysicsCanonicalModel):
    """Canonical authoritative outcome plus the exact deterministic rules fired."""

    schema_version: Literal[1] = 1
    outcome: PhysicsRoutingOutcome
    authoritative: Literal[True] = True
    self_declared_verdict: PhysicsVerdict | None
    contract_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None
    policy_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None
    report_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None
    rules: Annotated[
        tuple[PhysicsRoutingRuleProofV1, ...],
        BeforeValidator(_freeze_sequence),
        Field(min_length=1, max_length=100),
    ]


def derive_physics_audit_decision(
    contract_value: object,
    policy_value: object,
    report_value: object,
) -> PhysicsRoutingDecisionV1:
    """Validate and route without model, subprocess, network, Git, or filesystem access."""
    try:
        contract = PhysicsTaskContractV1.model_validate(contract_value)
    except ValidationError:
        return _invalid_decision("input_contract_invalid")
    contract_hash = contract.canonical_sha256()

    try:
        policy = PhysicsAuditPolicyV1.model_validate(policy_value)
    except ValidationError:
        return _invalid_decision(
            "input_policy_invalid",
            contract_sha256=contract_hash,
        )
    policy_hash = policy.canonical_sha256()
    if contract.audit_policy is not None and contract.audit_policy != policy:
        return _invalid_decision(
            "policy_contract_mismatch",
            contract_sha256=contract_hash,
            policy_sha256=policy_hash,
        )

    try:
        report = PhysicsAuditReportV1.model_validate(report_value)
    except ValidationError:
        return _invalid_decision(
            "report_schema_invalid",
            contract_sha256=contract_hash,
            policy_sha256=policy_hash,
        )
    report_hash = report.canonical_sha256()
    try:
        validate_physics_audit_report(contract, report)
    except PhysicsAuditError:
        return _invalid_decision(
            "report_reference_integrity_invalid",
            self_declared_verdict=report.verdict,
            contract_sha256=contract_hash,
            policy_sha256=policy_hash,
            report_sha256=report_hash,
        )

    return _route_valid_report(contract, policy, report)


def _route_valid_report(
    contract: PhysicsTaskContractV1,
    policy: PhysicsAuditPolicyV1,
    report: PhysicsAuditReportV1,
) -> PhysicsRoutingDecisionV1:
    proofs: list[PhysicsRoutingRuleProofV1] = []

    def fire(
        rule: PhysicsRoutingRule,
        outcome: PhysicsRoutingOutcome,
        subjects: tuple[str, ...] = (),
    ) -> None:
        proofs.append(
            PhysicsRoutingRuleProofV1(
                rule=rule,
                outcome=outcome,
                subject_ids=_canonical_subjects(subjects),
            )
        )

    integrity_ids = tuple(
        item.id
        for item in report.findings
        if item.category == "report_integrity_error"
        or item.disposition == "infrastructure_failure"
    )
    if integrity_ids:
        fire("report_integrity_error", "infrastructure_failure", integrity_ids)
        return _final_decision(contract, policy, report, proofs, "infrastructure_failure")

    human_required = False
    triggers = set(report.human_gate_triggers)
    if "convention_change" in triggers:
        human_required = True
        fire("convention_change_requires_human", "require_human_review")
    if "unresolved_gauge_constraint_ambiguity" in triggers:
        human_required = True
        fire("gauge_constraint_ambiguity_requires_human", "require_human_review")
    if "new_physical_interpretation" in triggers:
        human_required = True
        fire("new_physical_interpretation_requires_human", "require_human_review")

    gauge_questions = tuple(
        item.id
        for item in report.unresolved_questions
        if item.category == "gauge_constraint_ambiguity"
    )
    interpretation_questions = tuple(
        item.id
        for item in report.unresolved_questions
        if item.category == "new_physical_interpretation"
    )
    convention_questions = tuple(
        item.id
        for item in report.unresolved_questions
        if item.category == "convention_change"
    )
    other_questions = tuple(
        item.id
        for item in report.unresolved_questions
        if item.id
        not in {*gauge_questions, *interpretation_questions, *convention_questions}
    )
    if convention_questions:
        human_required = True
        fire(
            "convention_change_requires_human",
            "require_human_review",
            convention_questions,
        )
    if gauge_questions:
        human_required = True
        fire(
            "gauge_constraint_ambiguity_requires_human",
            "require_human_review",
            gauge_questions,
        )
    if interpretation_questions:
        human_required = True
        fire(
            "new_physical_interpretation_requires_human",
            "require_human_review",
            interpretation_questions,
        )
    if other_questions:
        human_required = True
        fire(
            "unresolved_question_requires_human",
            "require_human_review",
            other_questions,
        )

    human_finding_ids = tuple(
        item.id
        for item in report.findings
        if item.disposition == "human_review"
        or item.category
        in {
            "convention_change_requested",
            "gauge_constraint_ambiguity",
            "new_physical_interpretation",
        }
    )
    if human_finding_ids:
        human_required = True
        fire(
            "finding_requires_human",
            "require_human_review",
            human_finding_ids,
        )

    evidence_blocked = False
    conflicting_ids = tuple(
        item.id
        for item in report.checks
        if item.evidence_sufficiency == "conflicting"
    )
    if conflicting_ids:
        evidence_outcome: PhysicsRoutingOutcome = (
            "require_human_review"
            if policy.conflicting_required_evidence == "human_review"
            else "block_insufficient_evidence"
        )
        human_required |= evidence_outcome == "require_human_review"
        evidence_blocked |= evidence_outcome == "block_insufficient_evidence"
        fire("conflicting_required_evidence", evidence_outcome, conflicting_ids)

    insufficient_ids = tuple(
        item.id
        for item in report.checks
        if item.evidence_sufficiency in {"partial", "insufficient"}
    )
    if insufficient_ids:
        evidence_outcome = (
            "require_human_review"
            if policy.insufficient_required_evidence == "human_review"
            else "block_insufficient_evidence"
        )
        human_required |= evidence_outcome == "require_human_review"
        evidence_blocked |= evidence_outcome == "block_insufficient_evidence"
        fire("insufficient_required_evidence", evidence_outcome, insufficient_ids)

    evidence_finding_ids = tuple(
        item.id
        for item in report.findings
        if item.status == "open" and item.disposition == "evidence_blocking"
    )
    if evidence_finding_ids:
        evidence_outcome = (
            "require_human_review"
            if policy.insufficient_required_evidence == "human_review"
            else "block_insufficient_evidence"
        )
        human_required |= evidence_outcome == "require_human_review"
        evidence_blocked |= evidence_outcome == "block_insufficient_evidence"
        fire("evidence_blocking_finding", evidence_outcome, evidence_finding_ids)

    repair_required = False
    critical_ids = tuple(
        item.id
        for item in report.findings
        if item.status == "open"
        and item.disposition == "repairable"
        and item.severity == "critical"
    )
    if critical_ids:
        repair_required = True
        fire("critical_repairable_finding", "request_repair", critical_ids)
    high_ids = tuple(
        item.id
        for item in report.findings
        if item.status == "open"
        and item.disposition == "repairable"
        and item.severity == "high"
    )
    if high_ids:
        repair_required = True
        fire("high_repairable_finding", "request_repair", high_ids)

    failed_ids = tuple(item.id for item in report.checks if item.status == "failed")
    if failed_ids:
        repair_required = True
        fire("required_check_failed", "request_repair", failed_ids)
    unresolved_ids = tuple(
        item.id for item in report.checks if item.status == "unresolved"
    )
    if unresolved_ids:
        unresolved_outcome: PhysicsRoutingOutcome = (
            "require_human_review" if human_required else "block_insufficient_evidence"
        )
        fire("required_check_unresolved", unresolved_outcome, unresolved_ids)

    for severity, policy_route, rule in (
        ("medium", policy.medium_severity, "medium_finding_policy"),
        ("low", policy.low_severity, "low_finding_policy"),
        ("informational", policy.informational_severity, "informational_finding_policy"),
    ):
        finding_ids = tuple(
            item.id
            for item in report.findings
            if item.status == "open"
            and item.disposition == "repairable"
            and item.severity == severity
        )
        if not finding_ids:
            continue
        if policy_route == "request_repair":
            advisory_outcome: PhysicsRoutingOutcome = "request_repair"
        elif policy_route == "require_human_review":
            advisory_outcome = "require_human_review"
        else:
            advisory_outcome = "pass"
        human_required |= advisory_outcome == "require_human_review"
        repair_required |= advisory_outcome == "request_repair"
        fire(rule, advisory_outcome, finding_ids)  # type: ignore[arg-type]

    if human_required:
        outcome: PhysicsRoutingOutcome = "require_human_review"
    elif evidence_blocked:
        outcome = "block_insufficient_evidence"
    elif repair_required:
        outcome = "request_repair"
    else:
        outcome = "pass"
        fire(
            "all_required_checks_passed",
            "pass",
            tuple(item.id for item in report.checks),
        )
    return _final_decision(contract, policy, report, proofs, outcome)


def _final_decision(
    contract: PhysicsTaskContractV1,
    policy: PhysicsAuditPolicyV1,
    report: PhysicsAuditReportV1,
    proofs: list[PhysicsRoutingRuleProofV1],
    outcome: PhysicsRoutingOutcome,
) -> PhysicsRoutingDecisionV1:
    if report.verdict != _VERDICT_FOR_OUTCOME[outcome]:
        proofs.append(
            PhysicsRoutingRuleProofV1(
                rule="report_verdict_overridden",
                outcome=outcome,
                subject_ids=(),
            )
        )
    return PhysicsRoutingDecisionV1(
        outcome=outcome,
        self_declared_verdict=report.verdict,
        contract_sha256=contract.canonical_sha256(),
        policy_sha256=policy.canonical_sha256(),
        report_sha256=report.canonical_sha256(),
        rules=tuple(proofs),
    )


def _invalid_decision(
    rule: PhysicsRoutingRule,
    *,
    self_declared_verdict: PhysicsVerdict | None = None,
    contract_sha256: str | None = None,
    policy_sha256: str | None = None,
    report_sha256: str | None = None,
) -> PhysicsRoutingDecisionV1:
    return PhysicsRoutingDecisionV1(
        outcome="infrastructure_failure",
        self_declared_verdict=self_declared_verdict,
        contract_sha256=contract_sha256,
        policy_sha256=policy_sha256,
        report_sha256=report_sha256,
        rules=(
            PhysicsRoutingRuleProofV1(
                rule=rule,
                outcome="infrastructure_failure",
                subject_ids=(),
            ),
        ),
    )
