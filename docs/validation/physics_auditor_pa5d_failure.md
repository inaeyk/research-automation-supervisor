# Physics Auditor PA-5D final calibration

Final verdict: **NOT QUALIFIED — pre-outcome hard stop**

No Physics Auditor benchmark session and no GL-pilot model session was launched. The
hard freeze could not be completed from the qualified tree at
`df3553f584c6e9109c0e4561eab58e480a78b4b5`, so issuing a preregistration receipt or
making any model outcome visible would have violated PA-5D.

## Prelaunch authority verification

The requested base and branch were clean and exact before inspection. PA-5C1, PA-5C2,
PA-5C3, and PA-5C4 qualification commits are ancestors of the base. The mechanical
PA-5C4 frozen-tree verifier found all 526 protected scientific/review files unchanged
from PA-5C3, with tree digest
`8d7d7bb580bc0a14d899a20fc0ccb321c633488691323fd14c252e56c3096ae8`.
The process inventory also passed with digest
`dc2ded7e1c14774af428538bd9a9e3d7157b578166f2505cf91c9f3a325f445e`.

Fresh read-only PA-5C1 qualification approved all 31 detached human-reviewed subjects,
rebound 21 paired synthetic manifests and 10 GL manifests, verified exact GL source
blobs from commit `7d04b5b9882dcd476c1457b8d711ac7b5520b2c1`, and reported both
`model_launched=false` and `gl_pilot_launched=false`.

| Authority | Identity |
| --- | --- |
| Catalog canonical SHA-256 | `f4b52fe2f70baf87ca1ec19dff294490088e5571f45918b112ff00b936abb088` |
| Catalog file SHA-256 | `01e621c06c47cb253be3d03e367b30b976717d4ba7ad2d575d12ab9135995372` |
| Review packet SHA-256 | `4aafec7dd51ab66e8190699a72a9514e46c3228379d0d835e2b1c1220cd34cfb` |
| Fixture qualification SHA-256 | `1fdb54d40ae2828225be35966ab7844ca3114fb6e3f6d730089f0a987576056b` |
| Scorer-root manifest SHA-256 | `4a4ea8e3ea95563381571d1745e8825b8f7a51280827d14a221324ea52436f79` |
| PA-5C4-U operator evidence SHA-256 | `6897d0c4a0d830e49995a9b745f9631182426a3b866e97d97cf5ea597285c385` |

## Hard-stop finding and preregistration

A valid PA-5D preregistration hash is **unavailable because no valid preregistration
receipt was issued**. The current strict PA-5C1 catalog contains fixture, scorer, and
GL source authority, but not the exact 41-run case/variant/repetition schedule,
thresholds, benchmark model/configuration selection, per-run prompt identities, PA-4
child specifications, or expected action/run roots. PA-5C3 freezes a caller-supplied
child set; it does not authorize which of the 42 eligible variants and repetitions form
the 41-run experiment. The current tree also has no qualified GL-pilot runner.

Importing the schedule and thresholds from the invalidated PA-5B diagnostic attempt,
or selecting the generic synthetic execution configuration, would be a new scientific
design choice after prior outcomes were known. It was therefore rejected. The separate
machine-readable prelaunch hard-stop receipt is
[`physics_auditor_pa5d_failure.json`](physics_auditor_pa5d_failure.json); its
self-hash is recorded in that receipt after excluding the `hard_stop_receipt_sha256`
field: `40b08cbaf3b1fd7a68f4555d5b335c1f08e7f75d8f5231e4143bd850f41039f8`.

## Benchmark results and thresholds

Exact benchmark session count: **0 of required 41**. There are no provider session IDs,
PA-5C2 run identities, action roots, duplicate actions, malformed reports,
infrastructure failures, durations, or authoritative model token counters. Freshness
and session uniqueness are proven by the stronger prelaunch fact that no Physics
Auditor process was launched and no PA-5D action root was created.

Aggregate and per-category metrics—including detection, false pass, clean pass,
false-critical findings, repair routing, escalation, insufficient-evidence routing,
required/alternative/forbidden categories, severity, evidence validity, route and
category consistency, repetition consistency, duration, and model usage—are all
**unavailable because zero scientific runs occurred**.

| Required threshold | Qualified value in current authority | Result |
| --- | ---: | --- |
| Clean-case pass minimum | unavailable | missing authority; hard stop |
| Critical-defect detection minimum | unavailable | missing authority; hard stop |
| False-critical-finding maximum | unavailable | missing authority; hard stop |
| Correct-escalation minimum | unavailable | missing authority; hard stop |
| Repeated-route-consistency minimum | unavailable | missing authority; hard stop |
| Infrastructure-failure maximum | unavailable | missing authority; hard stop |

No historical diagnostic threshold was adopted or weakened.

## GL pilot and operational events

GL pilot result: **not started; 0 of 10 model sessions**. The ten fixture/source
authorities and locked GL commit revalidated, but execution was forbidden after the
incomplete freeze. Recovery events: **none**. Human-action events: **none**. Duplicate
external actions: **zero**.

No production GL mode, calibration metric, threshold result, or scientific
interpretation is claimed.

## Independent audits

The fresh independent technical audit found that the qualified tree provides the
PA-5C1 blindness boundary, PA-5C2 exact scorer, PA-5C3 deterministic recovery, and
PA-5C4 operator mechanism, but no complete PA-5D launch authority. Verdict: **HARD
STOP before preregistration or model launch**.

The fresh independent physics-quality audit found that inferring the missing schedule,
thresholds, variant selection, model/configuration, or GL execution design from the
invalidated PA-5B outcome or generic defaults would be retrospective and scientifically
invalid. Verdict: **NOT QUALIFIED**.

Neither auditor launched a model or modified the repository.

## Validation and delivery

Focused PA-5C1/C2/C3/C4 regressions: **138 passed, 1 explicit network-qualification
skip**. Ruff passed. Strict mypy passed over 74 source files. No candidate code changed,
so the complete suite was not required by the PA-5D rule; scientific execution never
began.

This failure report is the evidence intended for commit and push only on
`feature/physics-auditor-final-calibration`. Its final commit and push outcome is
reported in the human handoff because a commit cannot contain its own identity.
No merge, tag, release, or publication is authorized.

Recommendation: **PA-6 release closure is not authorized**. Stop after PA-5D for human
inspection. A future attempt requires separately reviewed, prospective authority that
binds every missing preregistration field before any new model result is visible.

## Token usage

Authoritative runtime token counters are unavailable for the Supervisor/Custodian and
both independent audit sessions. No benchmark or GL model session ran, so there are no
scientific-run counters. Input tokens, output tokens, combined total, and attribution
to retries, repairs, or repeated audit rounds are all **unavailable**; no estimates are
reported.
