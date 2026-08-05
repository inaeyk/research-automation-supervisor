"""Deterministic PA-5B fixture validation, semantic scoring, and reporting."""

from __future__ import annotations

import hashlib
import statistics
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Literal

from research_automation_supervisor.durable_state import render_json_bytes
from research_automation_supervisor.errors import (
    PhysicsBenchmarkInputError,
    PhysicsBenchmarkIntegrityError,
    PhysicsBenchmarkStateError,
)
from research_automation_supervisor.physics_benchmark_models import (
    PhysicsBenchmarkCaseAuthorityV1,
    PhysicsBenchmarkCatalogV1,
    PhysicsBenchmarkCategoryMetricsV1,
    PhysicsBenchmarkHardGatesV1,
    PhysicsBenchmarkMetricSetV1,
    PhysicsBenchmarkReportV1,
    PhysicsBenchmarkRunRecordV1,
    PhysicsBenchmarkThresholdOutcomeV1,
)
from research_automation_supervisor.physics_models import load_physics_task_contract

_ANSWER_KEY_FIELD_NAMES = frozenset(
    {
        "critical_seeded_defect",
        "expected_route",
        "forbidden_routes",
        "required_finding_categories",
        "seeded_defect_authority",
        "worker_repair_appropriate",
    }
)
_HUMAN_SEED_KINDS = frozenset(
    {
        "boundary_localization_claim",
        "conflicting_evidence",
        "constraint_mode_claim",
        "convention_change_request",
        "gauge_mode_claim",
        "unsupported_interpretation",
    }
)


def seeded_authority_sha256(case: PhysicsBenchmarkCaseAuthorityV1) -> str:
    """Hash only the hidden semantic answer-key fields for one public case."""
    value = {
        "case_id": case.case_id,
        "critical_seeded_defect": case.critical_seeded_defect,
        "expected_route": case.expected_route,
        "forbidden_routes": list(case.forbidden_routes),
        "human_review_mandatory": case.human_review_mandatory,
        "required_evidence_ids": list(case.required_evidence_ids),
        "required_finding_categories": list(case.required_finding_categories),
        "seed_kind": case.seed_kind,
        "seeded_defect_authority": case.seeded_defect_authority,
        "worker_repair_appropriate": case.worker_repair_appropriate,
    }
    from research_automation_supervisor.durable_state import canonical_json

    return hashlib.sha256(canonical_json(value)).hexdigest()


def fixture_sha256(root: Path) -> str:
    """Hash names, modes, and contents of one bounded public fixture tree."""
    try:
        resolved = root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise PhysicsBenchmarkInputError("benchmark fixture root is unavailable") from exc
    if not resolved.is_dir() or root.is_symlink():
        raise PhysicsBenchmarkInputError("benchmark fixture root must be a real directory")
    hasher = hashlib.sha256()
    for count, path in enumerate(sorted(resolved.rglob("*")), start=1):
        if path.is_symlink():
            raise PhysicsBenchmarkInputError("benchmark fixtures cannot contain symlinks")
        relative = path.relative_to(resolved).as_posix()
        mode = path.stat().st_mode & 0o7777
        kind = "directory" if path.is_dir() else "regular" if path.is_file() else "other"
        if kind == "other":
            raise PhysicsBenchmarkInputError("benchmark fixtures contain an unsupported object")
        if kind == "regular" and path.stat().st_size > 16 * 1024 * 1024:
            raise PhysicsBenchmarkInputError("benchmark fixture file exceeds its size limit")
        payload = path.read_bytes() if kind == "regular" else b""
        hasher.update(f"{relative}\0{kind}\0{mode:o}\0{len(payload)}\0".encode("ascii"))
        hasher.update(payload)
        if count > 1_000:
            raise PhysicsBenchmarkInputError("benchmark fixture contains too many objects")
    return hasher.hexdigest()


def validate_benchmark_authority_separation(
    catalog: PhysicsBenchmarkCatalogV1,
    *,
    repository_root: Path,
    catalog_path: Path,
) -> dict[str, str]:
    """Validate that answer keys and oracle executables cannot enter PA-3 projections."""
    try:
        root = repository_root.resolve(strict=True)
        authority = catalog_path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise PhysicsBenchmarkInputError("benchmark repository or catalog is unavailable") from exc
    if not root.is_dir() or authority.is_symlink() or not authority.is_file():
        raise PhysicsBenchmarkInputError("benchmark authority path is not a regular file")
    try:
        authority.relative_to(root)
    except ValueError as exc:
        raise PhysicsBenchmarkInputError(
            "public benchmark authority must be repository-bound"
        ) from exc

    fixture_hashes: dict[str, str] = {}
    for case in catalog.cases:
        fixture = _inside(root, root / case.fixture_root, "fixture root")
        contract_path = _inside(root, root / case.contract_path, "contract")
        if authority == fixture or fixture in authority.parents:
            raise PhysicsBenchmarkIntegrityError("answer-key authority overlaps a fixture root")
        if fixture not in contract_path.parents:
            raise PhysicsBenchmarkIntegrityError("case contract must be inside its fixture root")
        contract = load_physics_task_contract(contract_path)
        raw_contract = contract.model_dump(mode="json")
        exposed_keys = _mapping_keys(raw_contract)
        leaked = sorted(_ANSWER_KEY_FIELD_NAMES & exposed_keys)
        if leaked:
            raise PhysicsBenchmarkIntegrityError(
                "auditor-visible contract contains benchmark answer-key fields: "
                + ", ".join(leaked)
            )
        declared_paths = {item.path for item in contract.evidence if item.path is not None}
        for relative in declared_paths:
            evidence = _inside(root, root / relative, "declared fixture evidence")
            if fixture not in evidence.parents:
                raise PhysicsBenchmarkIntegrityError(
                    "auditor-visible evidence escapes its opaque fixture root"
                )
            if evidence == authority:
                raise PhysicsBenchmarkIntegrityError("answer-key authority is declared evidence")
        for relative in case.oracle_program_paths:
            program = _inside(root, root / relative, "oracle program")
            if not program.is_file() or program.is_symlink():
                raise PhysicsBenchmarkInputError("benchmark oracle program is not a regular file")
            if relative in declared_paths:
                raise PhysicsBenchmarkIntegrityError(
                    "oracle executable is declared as Physics Auditor evidence"
                )
        fixture_hashes[case.case_id] = fixture_sha256(fixture)
    return fixture_hashes


def verify_projection_excludes_benchmark_authority(
    projected_paths: Iterable[str],
    *,
    case: PhysicsBenchmarkCaseAuthorityV1,
    catalog_relative_path: str,
) -> None:
    """Fail closed if a finalized PA-3 projection names an answer key or oracle program."""
    visible = frozenset(projected_paths)
    forbidden = {catalog_relative_path, *case.oracle_program_paths}
    overlap = sorted(visible & forbidden)
    if overlap:
        raise PhysicsBenchmarkIntegrityError(
            "Physics Auditor projection exposes forbidden benchmark authority: "
            + ", ".join(overlap)
        )


def score_physics_benchmark(
    catalog: PhysicsBenchmarkCatalogV1,
    records: Sequence[PhysicsBenchmarkRunRecordV1],
    *,
    ordinary_nonphysics_unchanged: bool,
    limitations: Sequence[str],
) -> PhysicsBenchmarkReportV1:
    """Score semantic outcomes without comparing model prose."""
    validated = _validate_records(catalog, records)
    expected_count = sum(case.repetitions for case in catalog.cases)
    complete = len(validated) == expected_count
    aggregate = _metrics(catalog, validated)
    by_category = tuple(
        PhysicsBenchmarkCategoryMetricsV1(
            category=category,
            metrics=_metrics(catalog, category_records),
        )
        for category, category_records in sorted(_group_by_category(validated).items())
    )
    hard_gates = _hard_gates(
        catalog,
        validated,
        ordinary_nonphysics_unchanged=ordinary_nonphysics_unchanged,
    )
    threshold_outcomes = _threshold_outcomes(catalog, aggregate)
    qualified = complete and hard_gates.passed() and all(
        outcome.passed for outcome in threshold_outcomes
    )
    return PhysicsBenchmarkReportV1(
        benchmark_id=catalog.benchmark_id,
        methodology_version=catalog.methodology_version,
        catalog_sha256=catalog.canonical_sha256(),
        thresholds_sha256=catalog.thresholds.canonical_sha256(),
        thresholds_predeclared=True,
        complete=complete,
        qualification_verdict="qualified" if qualified else "not_qualified",
        aggregate=aggregate,
        by_category=by_category,
        threshold_outcomes=threshold_outcomes,
        hard_gates=hard_gates,
        records=tuple(validated),
        limitations=tuple(limitations),
    )


def validate_physics_benchmark_records(
    catalog: PhysicsBenchmarkCatalogV1,
    records: Sequence[PhysicsBenchmarkRunRecordV1],
) -> tuple[PhysicsBenchmarkRunRecordV1, ...]:
    """Validate run-record identity and semantic scoring fields without aggregating."""
    return _validate_records(catalog, records)


def render_physics_benchmark_markdown(report: PhysicsBenchmarkReportV1) -> str:
    """Render a concise deterministic companion to the complete JSON result."""
    metric = report.aggregate
    lines = [
        "# Physics Auditor PA-5B benchmark summary",
        "",
        f"Qualification verdict: **{report.qualification_verdict.replace('_', ' ')}**",
        "",
        f"Runs recorded: {metric.run_count}",
        f"Complete repetition design: {'yes' if report.complete else 'no'}",
        "",
        "## Aggregate metrics",
        "",
        "| Metric | Result |",
        "| --- | ---: |",
    ]
    for label, value in _display_metrics(metric):
        lines.append(f"| {label} | {_format_metric(value)} |")
    lines.extend(
        [
            "",
            "## Qualification thresholds",
            "",
            "| Threshold | Observed | Required | Result |",
            "| --- | ---: | ---: | --- |",
        ]
    )
    for item in report.threshold_outcomes:
        comparator = ">=" if item.comparator == "at_least" else "<="
        lines.append(
            f"| {item.name.replace('_', ' ')} | {_format_metric(item.observed)} | "
            f"{comparator} {item.threshold:.3f} | {'pass' if item.passed else 'fail'} |"
        )
    lines.extend(
        [
            "",
            "## Hard gates",
            "",
        ]
    )
    for name, passed in report.hard_gates.model_dump(mode="json").items():
        lines.append(f"- {'PASS' if passed else 'FAIL'} — {name.replace('_', ' ')}")
    lines.extend(
        [
            "",
            "## Per-category metrics",
            "",
            "| Category | Runs | Detection | Clean pass | Repair route | Human escalation | "
            "Evidence route | Infrastructure |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for category_item in report.by_category:
        category_metric = category_item.metrics
        lines.append(
            f"| {category_item.category} | {category_metric.run_count} | "
            f"{_format_metric(category_metric.critical_defect_detection_rate)} | "
            f"{_format_metric(category_metric.clean_case_pass_rate)} | "
            f"{_format_metric(category_metric.correct_repair_routing_rate)} | "
            f"{_format_metric(category_metric.correct_human_escalation_rate)} | "
            f"{_format_metric(category_metric.correct_insufficient_evidence_rate)} | "
            f"{_format_metric(category_metric.infrastructure_failure_rate)} |"
        )
    lines.extend(["", "## Limitations", ""])
    lines.extend(f"- {item}" for item in report.limitations)
    lines.extend(
        [
            "",
            "Functional correctness, report validity, deterministic routing, infrastructure "
            "reliability, and run-to-run consistency are reported separately. This synthetic "
            "benchmark does not establish broad autonomous physics competence.",
            "",
        ]
    )
    return "\n".join(lines)


def finalize_physics_benchmark_report(
    output_directory: Path,
    report: PhysicsBenchmarkReportV1,
    *,
    validation_layout: bool = False,
) -> tuple[Path, Path]:
    """Create or verify immutable JSON/Markdown aggregates; safe after interruption."""
    output_directory.mkdir(parents=True, exist_ok=True)
    json_name = "physics_auditor_pa5b.json" if validation_layout else "benchmark-report.json"
    markdown_name = (
        "physics_auditor_pa5b.md" if validation_layout else "benchmark-summary.md"
    )
    json_path = output_directory / json_name
    markdown_path = output_directory / markdown_name
    _write_once_or_verify(
        json_path,
        render_json_bytes(report.model_dump(mode="json")),
        "benchmark JSON report",
    )
    _write_once_or_verify(
        markdown_path,
        render_physics_benchmark_markdown(report).encode("utf-8"),
        "benchmark Markdown summary",
    )
    return json_path, markdown_path


def _validate_records(
    catalog: PhysicsBenchmarkCatalogV1,
    records: Sequence[PhysicsBenchmarkRunRecordV1],
) -> tuple[PhysicsBenchmarkRunRecordV1, ...]:
    if len(records) > sum(case.repetitions for case in catalog.cases):
        raise PhysicsBenchmarkInputError("benchmark contains more runs than predeclared")
    seen: set[tuple[str, int]] = set()
    validated: list[PhysicsBenchmarkRunRecordV1] = []
    session_hashes: set[str] = set()
    for record in records:
        key = (record.case_id, record.repetition)
        if key in seen:
            raise PhysicsBenchmarkInputError("benchmark repeats a case/repetition key")
        seen.add(key)
        try:
            case = catalog.case(record.case_id)
        except KeyError as exc:
            raise PhysicsBenchmarkInputError("benchmark record names an unknown case") from exc
        if record.repetition > case.repetitions:
            raise PhysicsBenchmarkInputError("record exceeds the predeclared repetition count")
        if (
            record.benchmark_id != catalog.benchmark_id
            or record.category != case.category
            or record.expected_route != case.expected_route
            or record.required_finding_categories != case.required_finding_categories
            or record.seeded_defect_authority_sha256 != seeded_authority_sha256(case)
        ):
            raise PhysicsBenchmarkIntegrityError("run record disagrees with benchmark authority")
        finding_ids = {
            item.finding_id
            for item in record.findings
            if item.category not in case.required_finding_categories
        }
        if case.clean_case:
            finding_ids = {item.finding_id for item in record.findings if item.status == "open"}
        if set(record.false_positive_finding_ids) != finding_ids:
            raise PhysicsBenchmarkIntegrityError("false-positive findings were scored incorrectly")
        expected_detected = False
        if case.critical_seeded_defect and record.actual_route != "pass":
            expected_detected = any(
                item.status == "open"
                and item.category in case.required_finding_categories
                and item.severity in {"critical", "high"}
                for item in record.findings
            )
        if record.critical_defect_detected != expected_detected:
            raise PhysicsBenchmarkIntegrityError("critical-defect detection was scored incorrectly")
        session = record.fresh_session_identity_sha256
        if session is not None:
            reused = session in session_hashes
            if record.session_reused != reused:
                raise PhysicsBenchmarkIntegrityError("fresh-session reuse flag is incorrect")
            session_hashes.add(session)
        elif record.session_reused:
            raise PhysicsBenchmarkIntegrityError("session reuse lacks an identity")
        if record.actual_route is not None and record.actual_route in case.forbidden_routes:
            # Forbidden routes remain valid observations; qualification gates score them.
            pass
        validated.append(record)
    return tuple(sorted(validated, key=lambda item: (item.case_id, item.repetition)))


def _metrics(
    catalog: PhysicsBenchmarkCatalogV1,
    records: Sequence[PhysicsBenchmarkRunRecordV1],
) -> PhysicsBenchmarkMetricSetV1:
    critical = [item for item in records if catalog.case(item.case_id).critical_seeded_defect]
    clean = [item for item in records if catalog.case(item.case_id).clean_case]
    repair = [
        item
        for item in records
        if catalog.case(item.case_id).expected_route == "request_repair"
    ]
    human = [
        item
        for item in records
        if catalog.case(item.case_id).expected_route == "require_human_review"
    ]
    insufficient = [
        item
        for item in records
        if catalog.case(item.case_id).expected_route == "block_insufficient_evidence"
    ]
    attempted_repairs = [
        item.worker_repair
        for item in records
        if item.worker_repair is not None and item.worker_repair.attempted
    ]
    duration = [item.duration_seconds for item in records]
    input_usage = [
        item.usage.input_tokens
        for item in records
        if item.usage.input_tokens is not None
    ]
    output_usage = [
        item.usage.output_tokens
        for item in records
        if item.usage.output_tokens is not None
    ]
    route_consistency, finding_consistency = _consistency(records)
    return PhysicsBenchmarkMetricSetV1(
        run_count=len(records),
        critical_defect_detection_rate=_rate(
            sum(item.critical_defect_detected for item in critical), len(critical)
        ),
        false_pass_rate=_rate(
            sum(item.actual_route == "pass" for item in critical), len(critical)
        ),
        clean_case_pass_rate=_rate(
            sum(item.actual_route == "pass" for item in clean), len(clean)
        ),
        false_critical_finding_rate=_rate(
            sum(
                any(
                    finding.status == "open"
                    and finding.severity in {"critical", "high"}
                    for finding in item.findings
                )
                for item in clean
            ),
            len(clean),
        ),
        correct_repair_routing_rate=_route_rate(repair, "request_repair"),
        correct_human_escalation_rate=_route_rate(human, "require_human_review"),
        correct_insufficient_evidence_rate=_route_rate(
            insufficient, "block_insufficient_evidence"
        ),
        malformed_report_rate=_rate(
            sum(item.malformed_report for item in records), len(records)
        ),
        infrastructure_failure_rate=_rate(
            sum(item.infrastructure_failure for item in records), len(records)
        ),
        repair_success_rate=_rate(
            sum(item.success is True for item in attempted_repairs), len(attempted_repairs)
        ),
        repeated_run_route_consistency=route_consistency,
        finding_category_consistency=finding_consistency,
        median_duration_seconds=_median(duration),
        median_input_tokens=_median(input_usage),
        median_output_tokens=_median(output_usage),
    )


def _consistency(
    records: Sequence[PhysicsBenchmarkRunRecordV1],
) -> tuple[float | None, float | None]:
    groups: dict[str, list[PhysicsBenchmarkRunRecordV1]] = defaultdict(list)
    for item in records:
        groups[item.case_id].append(item)
    repeated = [items for items in groups.values() if len(items) > 1]
    if not repeated:
        return None, None
    route_numerator = 0
    category_numerator = 0
    denominator = 0
    for items in repeated:
        routes = [item.actual_route or item.run_status for item in items]
        category_sets = [
            tuple(
                sorted(
                    {
                        finding.category
                        for finding in item.findings
                        if finding.status == "open"
                    }
                )
            )
            for item in items
        ]
        route_numerator += Counter(routes).most_common(1)[0][1]
        category_numerator += Counter(category_sets).most_common(1)[0][1]
        denominator += len(items)
    return route_numerator / denominator, category_numerator / denominator


def _hard_gates(
    catalog: PhysicsBenchmarkCatalogV1,
    records: Sequence[PhysicsBenchmarkRunRecordV1],
    *,
    ordinary_nonphysics_unchanged: bool,
) -> PhysicsBenchmarkHardGatesV1:
    critical = [item for item in records if catalog.case(item.case_id).critical_seeded_defect]
    human = [item for item in records if catalog.case(item.case_id).seed_kind in _HUMAN_SEED_KINDS]
    missing = [
        item
        for item in records
        if catalog.case(item.case_id).seed_kind in {
            "insufficient_evidence",
            "false_convergence_claim",
            "norm_sensitivity_claim",
        }
    ]
    return PhysicsBenchmarkHardGatesV1(
        zero_critical_defect_passes=not any(item.actual_route == "pass" for item in critical),
        zero_auditor_worktree_mutations=not any(
            item.workspace_integrity == "changed" for item in records
        ),
        zero_oracle_or_answer_key_exposure=not any(
            item.oracle_program_access_detected or item.answer_key_exposure_detected
            for item in records
        ),
        zero_session_reuse=not any(item.session_reused for item in records),
        zero_yolo_inheritance=not any(item.yolo_inheritance_detected for item in records),
        zero_unverified_pa2_or_pa3_evidence=not any(
            not item.pa2_proofs_verified or not item.pa3_proof_verified for item in records
        ),
        zero_duplicate_recovery_actions=not any(
            item.duplicate_recovery_action_detected for item in records
        ),
        all_malformed_reports_failed_closed=all(
            item.actual_route is None for item in records if item.malformed_report
        ),
        all_convention_and_interpretation_cases_human=all(
            item.actual_route == "require_human_review" for item in human
        ),
        all_missing_evidence_cases_blocked_or_human=all(
            item.actual_route in {"block_insufficient_evidence", "require_human_review"}
            for item in missing
        ),
        ordinary_nonphysics_unchanged=ordinary_nonphysics_unchanged,
    )


def _threshold_outcomes(
    catalog: PhysicsBenchmarkCatalogV1,
    metric: PhysicsBenchmarkMetricSetV1,
) -> tuple[PhysicsBenchmarkThresholdOutcomeV1, ...]:
    thresholds = catalog.thresholds
    values: tuple[tuple[str, Literal["at_least", "at_most"], float, float | None], ...] = (
        (
            "clean_case_pass_rate",
            "at_least",
            thresholds.clean_case_pass_rate_min,
            metric.clean_case_pass_rate,
        ),
        (
            "critical_defect_detection_rate",
            "at_least",
            thresholds.critical_defect_detection_rate_min,
            metric.critical_defect_detection_rate,
        ),
        (
            "false_critical_finding_rate",
            "at_most",
            thresholds.false_critical_finding_rate_max,
            metric.false_critical_finding_rate,
        ),
        (
            "correct_escalation_rate",
            "at_least",
            thresholds.correct_escalation_rate_min,
            _combined_escalation(metric),
        ),
        (
            "repeated_run_route_consistency",
            "at_least",
            thresholds.repeated_run_route_consistency_min,
            metric.repeated_run_route_consistency,
        ),
        (
            "infrastructure_failure_rate",
            "at_most",
            thresholds.infrastructure_failure_rate_max,
            metric.infrastructure_failure_rate,
        ),
    )
    return tuple(
        PhysicsBenchmarkThresholdOutcomeV1(
            name=name,
            comparator=comparator,
            threshold=threshold,
            observed=observed,
            passed=(
                observed is not None
                and (
                    observed >= threshold
                    if comparator == "at_least"
                    else observed <= threshold
                )
            ),
        )
        for name, comparator, threshold, observed in values
    )


def _combined_escalation(metric: PhysicsBenchmarkMetricSetV1) -> float | None:
    values = [
        item
        for item in (
            metric.correct_human_escalation_rate,
            metric.correct_insufficient_evidence_rate,
        )
        if item is not None
    ]
    return sum(values) / len(values) if values else None


def _group_by_category(
    records: Sequence[PhysicsBenchmarkRunRecordV1],
) -> dict[str, list[PhysicsBenchmarkRunRecordV1]]:
    groups: dict[str, list[PhysicsBenchmarkRunRecordV1]] = defaultdict(list)
    for item in records:
        groups[item.category].append(item)
    return groups


def _route_rate(
    records: Sequence[PhysicsBenchmarkRunRecordV1], expected: str
) -> float | None:
    return _rate(sum(item.actual_route == expected for item in records), len(records))


def _rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _median(values: Sequence[int | float]) -> float | None:
    return float(statistics.median(values)) if values else None


def _mapping_keys(value: object) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            keys.add(str(key))
            keys.update(_mapping_keys(item))
    elif isinstance(value, list):
        for item in value:
            keys.update(_mapping_keys(item))
    return keys


def _inside(root: Path, path: Path, label: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise PhysicsBenchmarkInputError(f"benchmark {label} escapes the repository") from exc
    return resolved


def _write_once_or_verify(path: Path, value: bytes, label: str) -> None:
    if path.exists():
        try:
            current = path.read_bytes()
        except OSError as exc:
            raise PhysicsBenchmarkStateError(f"could not verify existing {label}") from exc
        if current != value:
            raise PhysicsBenchmarkStateError(f"existing {label} contradicts recovered result")
        return
    try:
        path.write_bytes(value)
    except OSError as exc:
        raise PhysicsBenchmarkStateError(f"could not finalize {label}") from exc


def _format_metric(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.3f}"


def _display_metrics(
    metric: PhysicsBenchmarkMetricSetV1,
) -> tuple[tuple[str, float | None], ...]:
    return (
        ("Critical-defect detection rate", metric.critical_defect_detection_rate),
        ("False-pass rate", metric.false_pass_rate),
        ("Clean-case pass rate", metric.clean_case_pass_rate),
        ("False-critical-finding rate", metric.false_critical_finding_rate),
        ("Correct repair-routing rate", metric.correct_repair_routing_rate),
        ("Correct human-escalation rate", metric.correct_human_escalation_rate),
        ("Correct insufficient-evidence rate", metric.correct_insufficient_evidence_rate),
        ("Malformed-report rate", metric.malformed_report_rate),
        ("Infrastructure-failure rate", metric.infrastructure_failure_rate),
        ("Repair success rate", metric.repair_success_rate),
        ("Repeated-run route consistency", metric.repeated_run_route_consistency),
        ("Finding-category consistency", metric.finding_category_consistency),
        ("Median duration (seconds)", metric.median_duration_seconds),
        ("Median input tokens", metric.median_input_tokens),
        ("Median output tokens", metric.median_output_tokens),
    )
