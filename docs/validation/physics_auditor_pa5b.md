# Physics Auditor PA-5B validation

Final verdict: **NOT QUALIFIED — first scientific-quality attempt invalidated**

An independent technical audit and an independent physics-quality audit found that
auditor-visible evidence disclosed seeded diagnoses and, in the GL pilot, required
human-review routes. This violates the predeclared zero-answer-key-exposure hard gate
and triggers the stage hard stop. The raw deterministic report is retained unchanged
for reproducibility, but its automated
`zero_oracle_or_answer_key_exposure: true` result is superseded by the independent
audit. Its scientific metrics are diagnostic only and cannot qualify PA-5B.

The first attempt was run on 2026-08-05 from base commit
`5741f41bb91cc203152c758a9665e4dad43a2f85` on
`feature/physics-auditor-quality-benchmark`. There is no qualified commit, and the
branch was not pushed because the requested post-PASS condition was not met.

## Frozen design and artifacts

- Public catalog: 21 cases and 41 predeclared repetitions; catalog SHA-256
  `acfdf755ea6b1a9c3af537468947dc91bf485be0ae3266d761b0cec79fc68a2f`.
- Threshold SHA-256:
  `5e021ba97d40d2f9d9d28babe2707637bc4a269612933af9b1d4fffe18d8dd50`.
- GL pilot: 10 one-shot tasks; configuration SHA-256
  `8cde6242ab02b94df571f8ca6ba90e2ffdeefe5525c9089ae1305b52771fd7c8`.
- Raw benchmark report:
  [`physics_auditor_pa5b.json`](physics_auditor_pa5b.json), canonical report SHA-256
  `beb8857647d19f38075286de6c323eff3c50e9fdadbd9f9520ab205a726fe92a`
  and file SHA-256
  `c755697fb3d2ae358ced57382cba925715a6457c7b9d9323a48efc240e22421b`.
- Compact GL report:
  [`physics_auditor_pa5b_gl_pilot.json`](physics_auditor_pa5b_gl_pilot.json),
  full canonical report SHA-256
  `1ca5657a80b6864e814fd298b800f68f68d09d7d9db56e77d93f7e32bd6c82aa`
  and compact-file SHA-256
  `d06e597f15226e998847ddbeeeb6f8b47fe5766fcb15342ff6370fe113b26fe1`.
- Independent audit override:
  [`physics_auditor_pa5b_audit.json`](physics_auditor_pa5b_audit.json).

Three fresh PA-3 sessions were used for each highest-risk case and one for each
lower-risk mechanism case. All 41 Physics Auditor sessions were unique. There was no
post-outcome prompt tuning and no scientific rerun.

## Predeclared qualification thresholds

| Metric | Observed | Required | Raw result |
| --- | ---: | ---: | --- |
| Clean-case pass rate | 1.000 | at least 0.900 | pass |
| Critical-defect detection rate | 0.143 | at least 0.900 | **fail** |
| False-critical-finding rate | 0.000 | at most 0.050 | pass |
| Correct escalation rate | 0.464 | at least 0.900 | **fail** |
| Repeated-run route consistency | 1.000 | at least 0.900 | pass |
| Infrastructure-failure rate | 0.000 | at most 0.050 | pass |

The thresholds were not weakened after observing the results. Even a
severity-independent count of required-category recognition is only 12/28, or 0.429,
and does not rescue qualification.

## Aggregate diagnostic metrics

| Metric | Result |
| --- | ---: |
| Critical-defect detection rate | 0.143 |
| False-pass rate | 0.000 |
| Clean-case pass rate | 1.000 |
| False-critical-finding rate | 0.000 |
| Correct repair-routing rate | 0.857 |
| Correct human-escalation rate | 0.500 |
| Correct insufficient-evidence rate | 0.429 |
| Malformed-report rate | 0.049 |
| Infrastructure-failure rate | 0.000 |
| Repeated-run route consistency | 1.000 |
| Finding-category consistency | 0.867 |
| Median duration (seconds) | 53.992 |
| Median input tokens | 34156 |
| Median output tokens | 1521 |

The 41 raw routes were 22 `request_repair`, 8 `require_human_review`, 6 `pass`,
3 evidence blocks, and 2 malformed reports. Correct expected-route matches were
28/41. Positive controls and the correct alternative implementation passed 6/6 with
no false critical findings. No seeded critical defect routed `pass`.

Failures were stable across repeated cases: the false-convergence and norm-sensitive
claims routed to repair rather than evidence blocking; constraint- and gauge-dominated
modes routed to repair rather than human review; and normalization and
analytic/numerical-conflict reports were malformed. Malformed reports failed closed.

## Per-category diagnostic metrics

| Category | Runs | Detection | Clean pass | Repair route | Human escalation | Evidence route | Infrastructure |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| algebraic tensor | 9 | 0.000 | n/a | 0.778 | n/a | n/a | 0.000 |
| analytic limits | 3 | 0.000 | n/a | 1.000 | n/a | n/a | 0.000 |
| discretization | 2 | 0.000 | n/a | 1.000 | n/a | n/a | 0.000 |
| evidence quality | 4 | n/a | n/a | n/a | 0.000 | 1.000 | 0.000 |
| mode classification | 9 | 0.333 | n/a | n/a | 0.333 | n/a | 0.000 |
| numerical inference | 4 | 0.000 | n/a | n/a | n/a | 0.000 | 0.000 |
| reference correctness | 6 | n/a | 1.000 | n/a | n/a | n/a | 0.000 |
| scientific governance | 4 | 1.000 | n/a | n/a | 1.000 | n/a | 0.000 |

These numbers separate functional detection, report validity, deterministic routing,
infrastructure reliability, and repetition consistency. There is no composite score.
Because the exposure hard gate failed, none is an independently valid estimate of
scientific quality.

## Worker repair calibration

Four scripted/fake-agent PA-4 loops covered sign, normalization, metric contraction,
and finite-difference stencil cases. Each used one repair round with the same Worker,
reran software checks, invalidated stale PA-2 evidence, refreshed proofs, enforced a
fresh Physics Auditor, introduced no recorded regression, and reached a final `pass`.
The raw mechanism rate is therefore 4/4, not a live-Worker scientific success rate.

The physics audit found that case 004's covector contraction authority uses the wrong
or ambiguous variance convention, so that case cannot count as scientifically valid
repair evidence. The record remains useful only for PA-4 mechanism coverage.

## GL-with-AI pilot

The bounded pilot used ten repository-owned snapshots bound to GL commit
`7d04b5b9882dcd476c1457b8d711ac7b5520b2c1`. It did not inspect hidden material,
modify GL production code, change conventions, claim a GL mode, or authorize a
publication conclusion.

Raw results were 9/10 expected routes: six passes, three human-review routes, and one
malformed report. Task 009 failed closed because a passed check described conflicting
rather than sufficient evidence. The pilot outcome was `not_completed`.

The audit invalidated the pilot as independent quality evidence: tasks 006-009 expose
required human-review authority in auditor-visible text, source-reference hashes are
declared but not verified by the pilot validator, and the Auditor reviews authored
summaries rather than projected source content. The six clean passes therefore show
agreement with prepared summaries, not direct scientific review of GL code.

## Recovery calibration and bounded repair

The harness provides one-command status, dry-run, and resume. A real complete resume
initially exposed a multi-oracle receipt comparison defect: canonical receipt arrays
were independently sorted, while resume compared positional tuples. The single
allowed bounded post-attempt repair changed benchmark and pilot comparisons to
set-based identity checks and added a two-oracle regression test. It did not change a
prompt, fixture, threshold, scientific route, or raw model result.

After that repair, resuming all 41 completed records launched no model, oracle, or
repair action. The action-tree hash was identical before and after:
`133b1c9324ee9f11a52a4c5e7e8ce0d8f164ffe919520b03baf239af485205f3`.

The technical audit nevertheless found the requested benchmark-specific interruption
calibration incomplete: generic PA-5A crash tests were reused rather than driving this
harness through every listed phase. Status trusts recorded proof booleans without
independent PA-2/PA-3 artifact verification, ambiguous partial launches may be called
safe to resume, duplicate-action fields are asserted rather than independently
detected, and custom receipt/finalization writes are not atomic. The raw recovery hard
gates therefore do not establish the complete PA-5B recovery claim.

## Hard-gate disposition

| Gate | Final disposition |
| --- | --- |
| Zero deterministic passes on seeded critical defects | pass |
| Zero Physics Auditor worktree mutations | pass |
| Zero oracle-program or answer-key exposure | **fail; hard stop** |
| Zero session reuse and zero yolo inheritance | pass |
| Zero unverified PA-2/PA-3 evidence accepted | **not established by independent verification** |
| Zero duplicate actions after recovery | observed on full resume; broader calibration incomplete |
| All malformed reports fail closed | pass |
| Convention-change and new-interpretation cases require human review | **fail** |
| Missing-required-evidence cases block or require human review | **fail** |
| Ordinary non-physics workflows unchanged | pass |

## Independent audits

The technical audit found one critical, three high, and two medium issues: answer-key
leakage; insufficient independent proof/status verification; incomplete and
non-crash-durable recovery finalization; unverified GL source authority; missing
forbidden finding categories; and a documented pilot-prerequisite mismatch.

The physics-quality audit found one critical, three high, and three medium issues. In
addition to exposure and scoring problems, case 004 has inconsistent covector/vector
authority, case 009 uses the flat Euclidean spherical radial Laplacian rather than a
curved background and requires an inapplicable finding category, and case 020 labels
both conflicting authorities analytic.

Both auditors independently concluded that PA-5B is not qualified and recommended a
bounded remediation stage before any new predeclared run. Neither audit modified the
repository or consulted protected material.

## Validation status and recommendation

The final tree passed 17 focused PA-5B benchmark/repair/pilot tests and the earlier
403-test focused PA-1/2/3/4/5A regression group. The post-repair complete suite passed
1,393 tests with one expected device-permission skip in 540.42 seconds. Ruff passed;
strict mypy passed over 58 source files.

Wheel and sdist builds retained package version `0.2.0`. A clean wheel installation,
installed top-level and PA-5B CLI/help smokes, and package-data inspection passed. The
wheel contains 100 benchmark/GL data entries. Final distribution hashes are reported
in the handoff rather than embedded in the sdist that they identify.

The first installed smoke used a nonexistent shorthand command and was correctly
rejected; rerunning with the registered `validate-physics-benchmark`,
`run-physics-benchmark`, `validate-gl-pilot`, and `run-gl-pilot` names passed.

Do not proceed to PA-6. Preserve this attempt, then authorize at most a separately
scoped remediation that removes semantic answer leakage, corrects scientific fixture
authority, declares genuine forbidden finding categories, independently verifies
proof/source artifacts, and completes crash-boundary recovery calibration. Any rerun
must freeze new catalog and threshold hashes before execution and must not weaken the
thresholds or tune against these outcomes.

This small synthetic suite and summary-based pilot do not establish broad autonomous
physics competence or open-research capability.
