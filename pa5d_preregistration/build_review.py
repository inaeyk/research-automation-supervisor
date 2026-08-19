"""Generate the PA-5D0 machine and human review packets without model execution."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from pa5d_preregistration.authority import (
    HUMAN_AUTHORITY_REQUIRED,
    PA5DHumanDecisionsV1,
    PA5DPreregistrationReviewAuthorityV1,
    build_review_authority,
)


def _table(headers: tuple[str, ...], rows: Sequence[tuple[object, ...]]) -> list[str]:
    output = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    output.extend("| " + " | ".join(str(item) for item in row) + " |" for row in rows)
    return output


def render_markdown(review: PA5DPreregistrationReviewAuthorityV1) -> str:
    candidate = review.candidate_authorities[0]
    lines = [
        "# PA-5D0 calibration preregistration review",
        "",
        f"Status: **{HUMAN_AUTHORITY_REQUIRED}**. This is a prospective draft, not an "
        "approved preregistration receipt. PA-5D1 must not start.",
        "",
        "## Decisions required",
        "",
        "A human must select one exact 41-run schedule, approve the exact already-qualified "
        "PA-3 configuration, approve the one-shot execution and metric/GL-scoring policies, "
        "and approve or replace every proposed numeric performance threshold. The catalog-zero "
        "critical metric may only be approved as NO_GATE; revising it requires a new review. No "
        "proposed scientific choice is active before those exact decisions are rebound.",
        "",
    ]
    lines.extend(
        _table(
            ("Decision", "Subject", "Allowed values"),
            [
                (item.decision_id, item.subject, "; ".join(item.allowed_values))
                for item in review.required_human_decisions
            ],
        )
    )
    lines.extend(
        [
            "",
            "## Benchmark contents",
            "",
            "Catalog: `physics_benchmark_blind_authority_v1`; 21 paired subjects, 42 "
            "qualified variants (21 clean and 21 defective). The table is scorer-side review "
            "authority and must remain inaccessible to the Auditor.",
            "",
        ]
    )
    lines.extend(
        _table(
            (
                "Case",
                "Variant",
                "Label",
                "Expected route",
                "Required categories",
                "Acceptable alternatives",
            ),
            [
                (
                    item.case_id,
                    item.variant_id,
                    item.fixture_label,
                    item.expected_route,
                    ", ".join(item.required_categories) or "none",
                    ", ".join(item.acceptable_alternative_categories) or "none",
                )
                for item in candidate.benchmark_catalog_variants
            ],
        )
    )
    lines.extend(["", "## Exact 41-run schedule alternatives", ""])
    for schedule in review.schedule_alternatives:
        variants = {
            (item.case_id, item.variant_id): item for item in candidate.benchmark_catalog_variants
        }
        scheduled = [variants[(item.case_id, item.variant_id)] for item in schedule.executions]
        route_counts = {
            route: sum(1 for item in scheduled if item.expected_route == route)
            for route in (
                "pass",
                "request_repair",
                "require_human_review",
                "block_insufficient_evidence",
            )
        }
        case_variants: dict[str, set[str]] = {}
        for run in schedule.executions:
            case_variants.setdefault(run.case_id, set()).add(run.variant_id)
        complete_pairs = sum(len(items) == 2 for items in case_variants.values())
        lines.extend(
            [
                f"### {schedule.schedule_id}",
                "",
                f"Status: **{HUMAN_AUTHORITY_REQUIRED} — candidate not selected**.",
                "",
                f"Neutral rule: {schedule.neutral_rule}",
                "",
                f"Tradeoff: {schedule.scientific_tradeoff}",
                "",
                f"Omitted variants: `{', '.join(schedule.omitted_variant_keys)}`. "
                f"Distinct variants: {schedule.distinct_variant_count}; exact repeats: "
                f"{schedule.repeated_execution_count}. Ordering is "
                "`(case_id, variant_id, repetition_id)`.",
                "",
            ]
        )
        lines.extend(
            _table(
                ("Effective denominator", "Exact count"),
                [
                    ("clean runs", sum(item.fixture_label == "clean" for item in scheduled)),
                    (
                        "defective runs",
                        sum(item.fixture_label == "defective" for item in scheduled),
                    ),
                    ("expected pass", route_counts["pass"]),
                    ("expected repair", route_counts["request_repair"]),
                    ("expected human escalation", route_counts["require_human_review"]),
                    (
                        "expected insufficient evidence",
                        route_counts["block_insufficient_evidence"],
                    ),
                    ("complete variant pairs", complete_pairs),
                    ("exact repeat pairs", schedule.repeated_execution_count),
                    (
                        "route-consistency misses allowed by proposed >=0.95",
                        complete_pairs - int(0.95 * complete_pairs + 0.999999),
                    ),
                ],
            )
        )
        lines.append("")
        lines.extend(
            _table(
                ("#", "Case", "Variant", "Rep", "Execution ID", "Fresh PA-3 action root"),
                [
                    (
                        run.ordinal,
                        run.case_id,
                        run.variant_id,
                        run.repetition_id,
                        run.execution_id,
                        f"`{run.pa3_action_root}`",
                    )
                    for run in schedule.executions
                ],
            )
        )
        lines.append("")
    config = candidate.model_configuration
    lines.extend(
        [
            "## Exact model and PA-3 configuration",
            "",
            f"Status: **{HUMAN_AUTHORITY_REQUIRED}** even though the proposed bytes are the "
            "already-qualified PA-3 configuration. The human selection makes its use in this "
            "scientific calibration prospective and explicit.",
            "",
        ]
    )
    lines.extend(
        _table(
            ("Field", "Bound value"),
            [(key, json.dumps(value, sort_keys=True)) for key, value in config.config.items()]
            + [
                ("role_policy_sha256", config.role_policy_sha256),
                ("bubblewrap_policy_sha256", config.bubblewrap_policy_sha256),
                ("codex_cli_version", config.codex_cli_version),
                ("codex_executable_sha256", config.codex_executable_sha256),
                ("bubblewrap_version", config.bubblewrap_version),
                (
                    "bubblewrap_backend_identity_sha256",
                    config.bubblewrap_backend_identity_sha256,
                ),
                ("runtime_identity_drift_allowed", False),
                ("prompt_template_sha256", config.prompt_template_sha256),
                ("output_schema_sha256", config.output_schema_sha256),
                ("rendered_prompt_policy", config.rendered_prompt_policy),
                ("resume_allowed", False),
                ("yolo_or_full_access_allowed", False),
            ],
        )
    )
    lines.extend(
        [
            "",
            "Each benchmark run and GL task gets exactly one fresh ephemeral provider session. "
            "No resume, session reuse, yolo/full-access inheritance, source-worktree mount, "
            "scorer mount, or unverified PA-2/PA-3 evidence is allowed.",
            "The operator may resolve the executable by path, but launch must fail closed unless "
            "the resolved Codex executable and Bubblewrap backend match the exact version and "
            "SHA-256 identities above; runtime drift is not permitted.",
            "",
            "## One-shot execution and scoring policies",
            "",
            "Status: **HUMAN_AUTHORITY_REQUIRED**. The proposal requires dedicated PA-5D1 "
            "one-shot adapters to qualify before launch. Each coordinate produces exactly one "
            "PA-3 session and one verified proof; a route is an observation, not permission to "
            "run an ordinary PA-4 Worker/repair loop. Non-pass routes are persisted and scored "
            "without repair or human scientific override. Ambiguous infrastructure invalidates "
            "the calibration unless the identical action is provably resumable.",
            "",
            "The proposed derived-metric policy fixes declared-category assignment, applies "
            "per-category gates to every nonzero declared category, uses PA-5C2 tri-state failure "
            "eligibility independently for each criterion without a global malformed or "
            "infrastructure override, defines exact pair/repeat comparisons, nearest-rank p95, "
            "and token combination without double-counting reasoning tokens. Benchmark and GL "
            "are separate cohorts: each applicable threshold must pass in each cohort, with no "
            "cross-domain pooling. The GL proposal derives "
            "separate route/category/severity/evidence/malformed/infrastructure criteria from "
            "each locked task. Both policies require explicit human approval.",
            "",
            "## Normative threshold table",
            "",
            "Structural gates are inherited qualified authority. Every performance value is "
            "only a proposal and is highlighted as HUMAN_AUTHORITY_REQUIRED. Duration and token "
            "usage are descriptive and have no acceptance gate.",
            "",
            "Important denominator facts: the catalog contains no critical-minimum benchmark "
            "variant, so `critical_defect_recognition_rate` is not applicable. Only "
            "case_009/variant_002 declares acceptable alternatives; both schedules include it. "
            "Repeat consistency is observable only in the balanced single-repeat schedule.",
            "",
        ]
    )
    lines.extend(
        _table(
            (
                "Metric",
                "Operator",
                "Value",
                "Gate",
                "Application",
                "Authority status",
                "Rationale",
            ),
            [
                (
                    item.metric_id,
                    item.operator,
                    item.value,
                    item.gate_kind,
                    item.application_scope,
                    f"**{item.authority_status}**"
                    if item.authority_status == HUMAN_AUTHORITY_REQUIRED
                    else item.authority_status,
                    item.rationale,
                )
                for item in candidate.thresholds
            ],
        )
    )
    lines.extend(
        [
            "",
            "## Exact ten-task GL pilot",
            "",
            f"Locked source commit: `{candidate.gl_pilot.source_commit}`. Task order is the "
            "qualified catalog order. The pilot uses the exact same proposed PA-3 configuration "
            "and one fresh action/session per task. This authorizes no production GL-mode claim.",
            "",
        ]
    )
    lines.extend(
        _table(
            (
                "#",
                "Task",
                "Expected route",
                "Required categories",
                "Visible manifest",
                "Locked source blobs",
                "Fresh action root",
            ),
            [
                (
                    task.ordinal,
                    task.task_id,
                    task.expected_route,
                    ", ".join(task.required_categories) or "none",
                    task.visible_manifest_sha256,
                    "<br>".join(
                        f"{blob.path} ({blob.byte_length} B, {blob.sha256})"
                        for blob in task.source_blobs
                    ),
                    f"`{task.action_root}`",
                )
                for task in candidate.gl_pilot.tasks
            ],
        )
    )
    lines.extend(
        [
            "",
            "## Failure, recovery, and hard-stop policy",
            "",
            "Only PA-5A/PA-5C3 `auto_resume` and `finish_finalization` may continue an identical "
            "already-authorized action. Ambiguous launch state, stale/reused process identity, "
            "missing proof, changed authority, malformed report, or unverified evidence fails "
            "closed. Recovery may repair infrastructure only; every scientific input stays frozen.",
            "",
            "Any post-outcome prompt, threshold, metric, fixture, expected route, model/config, "
            "schedule, repetition, root, source, or scoring change invalidates the entire "
            "calibration. It cannot be patched or continued under this preregistration.",
            "",
            "## Contamination statement",
            "",
            "Invalidated PA-5B outputs were not used to derive schedule, thresholds, prompt "
            "wording, expected routes, model configuration, repetitions, or scoring policy. "
            "PA-5B appears only in the machine-readable contamination register as historical "
            "non-authority. The alternatives above are derived solely from the current qualified "
            "catalog and explicit SHA-256 ranking rules.",
            "",
            "## Canonical authority hashes",
            "",
        ]
    )
    lines.extend(
        _table(
            ("Authority", "SHA-256"),
            [
                ("PA-5D0 review draft", review.draft_sha256),
                ("Qualified catalog canonical", review.qualified_sources.catalog_canonical_sha256),
                ("Qualified catalog file", review.qualified_sources.catalog_file.sha256),
                (
                    "Qualified PA-5C1 fixture qualification",
                    review.qualified_sources.fixture_qualification_sha256,
                ),
                (
                    "Qualified PA-5C1 scorer root",
                    review.qualified_sources.scorer_root_manifest_sha256,
                ),
                ("Exact model config canonical", config.execution_config_sha256),
                ("GL expected child set", candidate.gl_pilot.expected_child_set_sha256),
            ]
            + [
                (f"Schedule child set: {item.schedule_id}", item.expected_child_set_sha256)
                for item in review.schedule_alternatives
            ]
            + [
                (f"Candidate authority: {item.schedule.schedule_id}", item.authority_sha256)
                for item in review.candidate_authorities
            ],
        )
    )
    lines.extend(
        [
            "",
            "No final approved preregistration receipt exists. Benchmark sessions launched: "
            "**0**. GL-pilot sessions launched: **0**.",
            "",
        ]
    )
    return "\n".join(lines)


def decision_template(review: PA5DPreregistrationReviewAuthorityV1) -> dict[str, object]:
    thresholds = [
        item
        for item in review.candidate_authorities[0].thresholds
        if item.authority_status == HUMAN_AUTHORITY_REQUIRED
    ]
    return {
        "schema_version": 1,
        "review_draft_sha256": review.draft_sha256,
        "reviewer_identity": "REPLACE_WITH_HUMAN_IDENTITY",
        "decided_at": "REPLACE_WITH_RFC3339_TIMESTAMP",
        "selected_schedule_id": "SELECT_EXACT_SCHEDULE_ID",
        "model_configuration_decision": "approve_exact_qualified_pa3_configuration",
        "execution_policy_decision": "approve_one_shot_calibration_execution_policy_v1",
        "metric_and_gl_scoring_decision": "approve_metric_and_gl_scoring_policy_v1",
        "threshold_decisions": [
            {
                "threshold_id": item.threshold_id,
                "decision": (
                    "approve_proposed"
                    if item.operator == "NO_GATE"
                    else "approve_proposed_OR_replace"
                ),
                "replacement_operator": None,
                "replacement_value": None,
                "rationale": "REQUIRED_HUMAN_RATIONALE",
            }
            for item in thresholds
        ],
        "explicit_no_pa5b_derivation_attestation": True,
        "decision_sha256": "COMPUTED_ONLY_AFTER_ALL_DECISIONS_ARE_EXPLICIT",
    }


def main() -> None:
    output_root = Path(__file__).resolve().parent
    repository = output_root.parent
    review = build_review_authority(repository)
    (output_root / "PA5D_PREREGISTRATION_REVIEW.json").write_text(
        json.dumps(review.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (output_root / "PA5D_PREREGISTRATION_REVIEW.md").write_text(
        render_markdown(review), encoding="utf-8"
    )
    (output_root / "PA5D_HUMAN_DECISIONS.template.json").write_text(
        json.dumps(decision_template(review), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (output_root / "PA5D_HUMAN_DECISIONS.schema.json").write_text(
        json.dumps(PA5DHumanDecisionsV1.model_json_schema(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
