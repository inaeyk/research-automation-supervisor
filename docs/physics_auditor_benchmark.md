# Physics Auditor PA-5C benchmark machinery

PA-5C qualifies the public benchmark machinery with scripted agents only. It does not
qualify Physics Auditor scientific performance and it does not change the already
qualified PA-1 through PA-5A semantics. Package version remains `0.2.0`.

The preserved PA-5B run is still scientifically invalid. Its projected evidence
disclosed seeded diagnoses and expected routes, its GL pilot used prepared summaries,
and its source/proof and recovery coverage was incomplete. Its reports remain historical
reproducibility artifacts only.

The remediated synthetic suite is under
[`examples/physics_auditor/benchmark_v1`](../examples/physics_auditor/benchmark_v1/README.md).
The separately prepared GL pilot is under
[`examples/physics_auditor/gl_pilot_v1`](../examples/physics_auditor/gl_pilot_v1/README.md).

## Physical authority boundary

Auditor-visible material is limited to one neutral `case_NNN` contract, candidate
source or claim, raw bounded observations, sealed oracle outputs, and the PA-2 evidence
summaries generated from those outputs. The prompt and evidence index name only those
projected artifacts.

Scorer-only material lives under `benchmark_v1/authority/`: `catalog.json`,
`fixture-authority.json`, and `worker-repair-calibration.json`. It contains the seeded
defect, expected and forbidden routes, required/acceptable/forbidden categories,
minimum severity, review authority, approval records, and source/contract hashes. The
production projection builder excludes the authority directory and oracle program,
then scans the exact prompt and exact projected bytes for authority keys and semantic
answer leakage before a launch. Bubblewrap mounts only that checked projection, so the
authority files are physically absent from its namespace.

The only production case labels are `case_001` through `case_021`; diagnostic names are
not used in auditor-visible paths, contracts, prompts, evidence indexes, or summaries.

## Fixture authority and scoring

Every fixture has an independently reviewed authority entry binding:

- each auditor-visible source path and SHA-256;
- the canonical contract SHA-256;
- the neutral seed identifier and scorer-only defect description;
- expected and acceptable-alternative routes;
- required, acceptable-alternative, and forbidden categories;
- minimum severity and human-review authority; and
- an approval ID, reviewer role, approval decision, review scope, and approval date.

Validation recomputes every binding rather than trusting catalog copies. Case 004 now
declares contravariant cylindrical-vector components under
`g_ij = diag(1, r^2)` and requires `(A^r)^2 + r^2(A^theta)^2`; the earlier
covector/vector ambiguity is removed. Case 020's second oracle is correctly typed as
numerical evidence.

Scoring separately reports category recognition, severity, route, evidence validity,
required-category satisfaction, acceptable alternatives, and forbidden
categories/routes. Missing required facts, under-severity, invalid evidence, or any
forbidden fact fails the associated hard gate. Finding prose is never compared.

Immediately before scoring, the scorer mechanically reverifies the fixture source and
canonical contract hashes, exact PA-3 projection manifest, PA-2 action record and
completion proof, PA-3 action record and completion proof, and the recovery receipt.
The verified identities must equal those bound into the benchmark record. A changed or
missing byte blocks scoring.

## Recovery and finalization

Every benchmark action uses PA-5A resume semantics. Checkpoints cover fixture authority,
PA-2 prelaunch/completion/proof, PA-3 prelaunch/completion/proof, recovery-proof
construction, record persistence, aggregation, and finalization. Existing actions go
through the qualified verifier path and are never relaunched. Ambiguous launch state
fails closed; proof identities are reverified after recovery; action-record hashes and
disjoint run identities prevent duplicate acceptance.

Run records, recovery receipts, JSON aggregation, and Markdown finalization use atomic
replacement with file and parent-directory fsync. Recovery can complete a missing
companion artifact only when already-persisted bytes and all identities still agree.

## GL pilot preparation

GL authority remains scorer-only in `config/pilot.json`. Preparation reads every
declared source with `git show <commit>:<path>`, verifies the exact blob SHA-256, and
creates a dedicated Git workspace containing only those exact bytes, one neutral
contract, and raw candidate observations where needed. PA-2 and PA-3 operate on that
workspace. Prepared evidence summaries and expected classifications were removed.

No PA-5C GL pilot was run. A later real pilot must be a fresh authorized action and may
not reuse the invalid PA-5B result.

## Qualification interpretation

PA-5C qualification means the schemas, authority separation, identity checks, scoring,
isolation, recovery, and finalization behave as designed under deterministic fake-agent
tests. It is not a benchmark score, an open-research claim, or publication authority.

PA-5D should perform one fresh, preregistered 21-case/41-session real-model benchmark
and a fresh ten-task GL pilot from clean action roots, using the frozen PA-5C authority,
exact projections, proof reverification, and unchanged thresholds. It must receive new
independent technical and physics-quality review before any scientific-performance
claim. PA-6 remains unauthorized.

The invalid PA-5B report and audit remain available in
[`docs/validation`](validation/physics_auditor_pa5b.md); they are not PA-5C evidence.
