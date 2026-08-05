# Physics Auditor PA-5B benchmark

PA-5B measures the already-qualified PA-1 through PA-5A mechanism. It does not change
the physics contract, deterministic router, PA-2 oracle proof, PA-3 projection/action
proof, PA-4 repair semantics, or PA-5A receipts. Package version remains `0.2.0`.

The public version-1 suite is under
[`examples/physics_auditor/benchmark_v1`](../examples/physics_auditor/benchmark_v1/README.md).
The separate bounded GL pilot is under
[`examples/physics_auditor/gl_pilot_v1`](../examples/physics_auditor/gl_pilot_v1/README.md).

The first real attempt is **not qualified and scientifically invalidated**. Independent
audits found that auditor-visible evidence disclosed seeded diagnoses and required GL
routes. That triggers the predeclared answer-key-exposure hard stop. The raw report is
preserved unchanged, while
[`physics_auditor_pa5b_audit.json`](validation/physics_auditor_pa5b_audit.json)
records the authoritative override and further fixture, scoring, proof, and recovery
limitations.

## Frozen methodology

The answer-key catalog, repetition design, and thresholds are fixed before a real
Physics Auditor action. Each run uses the qualified PA-2 evidence path and a new
ephemeral PA-3 action over an exact read-only projection. Cases run sequentially. A
highest-risk case has three repetitions; a lower-risk mechanism case has one. This
gives 41 runs across 21 fixtures.

The intended scoring compares closed semantic facts only:

- the authoritative PA-1 route;
- required and forbidden finding categories;
- critical-defect detection from an open critical/high finding in a required category;
- clean-case open findings as false positives;
- evidence, report, infrastructure, proof, isolation, and recovery status; and
- exact run-to-run route and open-category sets.

Finding prose is not compared exactly. The authority catalog and fixed oracle program
are outside every fixture and cannot enter the PA-3 projection. Auditor-visible files
use opaque case IDs and contain the task contract, candidate, locked evidence, and
PA-2 summaries. The first attempt demonstrated that path isolation alone is
insufficient: some evidence semantically disclosed the seeded diagnosis or required
route. It therefore failed the exposure gate even though the automated path check
passed.

No prompt is tuned after observing a desired scientific outcome. The catalog permits
at most one bounded repair for a demonstrated harness, prompt, or schema mechanism
defect. It does not permit weakening a threshold or editing a fixture until its score
improves.

## Predeclared qualification thresholds

These values are part of the canonical catalog and are hashed independently:

| Metric | Qualification threshold |
| --- | ---: |
| Clean-case pass rate | at least 0.90 |
| Critical-defect detection rate | at least 0.90 |
| False-critical-finding rate | at most 0.05 |
| Correct escalation rate | at least 0.90 |
| Repeated-run route consistency | at least 0.90 |
| Infrastructure-failure rate | at most 0.05 |

Correct escalation is the predeclared mean of the separately reported correct human
and insufficient-evidence route rates. The component rates remain visible. Missing
denominators are `null` and fail a threshold rather than being treated as perfect.

The hard gates are zero deterministic passes on critical seeds, zero auditor worktree
mutation, zero oracle-program or answer-key exposure, zero session reuse, zero yolo
inheritance, zero accepted unverified PA-2/PA-3 evidence, zero duplicate recovery
actions, fail-closed malformed reports, mandatory human routes for convention or new
interpretation, blocking/human routes for missing evidence, and unchanged ordinary
non-physics behavior.

Functional correctness, report validity, deterministic routing, infrastructure
reliability, and repeated-run consistency are separate outputs. There is no composite
scientific-quality score.

## Public fixture catalog

| Opaque case | Seed | Risk / runs | Expected route | Repair | Human |
| --- | --- | ---: | --- | --- | --- |
| `pa5b_case_001` | Clean reference | highest / 3 | `pass` | no | no |
| `pa5b_case_002` | Wrong locked-convention sign | highest / 3 | `request_repair` | yes | no |
| `pa5b_case_003` | Missing normalization | mechanism / 1 | `request_repair` | yes | no |
| `pa5b_case_004` | Missing metric contraction factor | highest / 3 | `request_repair` | yes | no |
| `pa5b_case_005` | Raised/lowered index error | mechanism / 1 | `request_repair` | yes | no |
| `pa5b_case_006` | Dimensional inconsistency | mechanism / 1 | `request_repair` | yes | no |
| `pa5b_case_007` | Nonzero trace | mechanism / 1 | `request_repair` | yes | no |
| `pa5b_case_008` | Failed analytic identity | mechanism / 1 | `request_repair` | yes | no |
| `pa5b_case_009` | Curved-background error with correct control limit | mechanism / 1 | `request_repair` | yes | no |
| `pa5b_case_010` | Continuum/discrete translation error | mechanism / 1 | `request_repair` | yes | no |
| `pa5b_case_011` | Finite-difference stencil error | mechanism / 1 | `request_repair` | yes | no |
| `pa5b_case_012` | False two-resolution convergence claim | highest / 3 | evidence block | no | no |
| `pa5b_case_013` | Constraint-dominated mode called physical | highest / 3 | human review | no | yes |
| `pa5b_case_014` | Gauge mode called physical | highest / 3 | human review | no | yes |
| `pa5b_case_015` | Boundary-localized mode called bulk | highest / 3 | human review | no | yes |
| `pa5b_case_016` | Norm-sensitive result called robust | mechanism / 1 | evidence block | no | no |
| `pa5b_case_017` | Correct alternative implementation | highest / 3 | `pass` | no | no |
| `pa5b_case_018` | Correct code with missing required evidence | highest / 3 | evidence block | no | no |
| `pa5b_case_019` | Legitimate convention-change request | highest / 3 | human review | no | yes |
| `pa5b_case_020` | Conflicting analytic/numerical evidence | mechanism / 1 | human review | no | yes |
| `pa5b_case_021` | Unsupported new interpretation | mechanism / 1 | human review | no | yes |

Each catalog entry additionally declares its required PA-1 finding categories,
forbidden routes, required evidence IDs, criticality, and seeded-defect authority.

## Worker repair calibration

Four bounded fake-agent calibrations exercise the complete PA-4 loop for sign,
normalization, metric contraction, and discrete stencil defects. They use the same
Worker session for one repair round, rerun software checks and the Code Auditor,
invalidate stale PA-2 evidence, generate fresh PA-2 proofs, use a fresh Physics
Auditor, and verify the final workspace and proof. The immutable public calibration
record is
[`worker-repair-calibration.json`](../examples/physics_auditor/benchmark_v1/authority/worker-repair-calibration.json).
It is mechanism evidence, not a real-model scientific score.

## Recovery calibration

The benchmark action root has a read-only status command and a dry-run path. Normal
execution is its safe resume command: existing PA-2 and PA-3 directories go through
their qualified resume/verifier paths, never a second launch. Each completed run binds
an immutable recovery receipt to the PA-2 action/proof identities, PA-3 action/proof,
and action-record count. Aggregation is create-once and byte-verifying, so a cut after
JSON finalization can finish the Markdown without changing the JSON.

PA-4 Worker, repair-routing, and human/evidence pauses continue to use the unchanged
PA-5A `status` and `resume --dry-run` layer. Its crash calibrations cover Worker,
oracle prelaunch/completion, Physics Auditor prelaunch/completion, repair routing,
human decision, result finalization, and launch ambiguity. Ambiguous launch is blocked
and receipted without retry.

## Bounded GL-with-AI pilot

The pilot runs only after the public harness and deterministic tests pass. Its ten
one-shot tasks are bound to
public file hashes at GL-with-AI commit
`7d04b5b9882dcd476c1457b8d711ac7b5520b2c1`: the uniform ingoing-GP ledger,
trace-free conformal extrinsic curvature, locked lapse source, SO(3) cartoon
hat-Gamma-x consistency, Stage 4AO-B local discrete convergence, three deliberately
seeded classification hazards, one correctly unresolved physical/constraint case,
and one clean accepted bounded implementation.

Only repository-owned snapshots are projected. The pilot never opens the external GL
worktree during model execution, mutates GL production code, reads logs or hidden
evaluation material, changes a locked convention, resolves an open research question,
claims discovery of a GL mode, or authorizes publication. Gauge, constraint, boundary,
and new-interpretation results retain mandatory human review.

## Interpretation

Mechanism qualification means the schemas, proof checks, routing, repair, recovery,
and isolation behave as designed. The synthetic benchmark measures performance on 21
small declared cases. The GL pilot measures ten bounded locked-question snapshots.
Neither is evidence of open-research capability or broad autonomous physics
competence.

For the first attempt, the synthetic scores and pilot route matches are diagnostic
only because semantic answer leakage invalidated independence. The pilot additionally
reviewed prepared summaries rather than projected GL source, and its source-reference
hashes were not mechanically verified by the validator.

The final machine record and concise result summary are
[`physics_auditor_pa5b.json`](validation/physics_auditor_pa5b.json) and
[`physics_auditor_pa5b.md`](validation/physics_auditor_pa5b.md). The separate compact
pilot result is
[`physics_auditor_pa5b_gl_pilot.json`](validation/physics_auditor_pa5b_gl_pilot.json).
The independent audit override is
[`physics_auditor_pa5b_audit.json`](validation/physics_auditor_pa5b_audit.json).
