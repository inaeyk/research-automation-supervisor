# PA-5C1 blind fixture and scientific-authority qualification

PA-5C1 prepares scientifically reviewable fixtures without running a Physics Auditor
benchmark. PA-1 through PA-5A behavior and package version `0.2.0` are unchanged.

## Authority boundary

The auditor-visible and scorer-only roots are disjoint. The scorer root contains every
clean/defect label, expected route or interpretation, diagnosis, category constraint,
severity floor, source authority record, and review receipt. None of those files is an
eligible PA-3 projection input. The existing PA-3 Bubblewrap action continues to mount
only its exact read-only projection, isolated runtime home, bounded action output,
schema, authentication file, and system runtime.

Before a future fixture action may launch, `issue_blindness_certificate` checks and
binds:

1. the exact auditor-visible byte manifest;
2. the exact PA-3 projection manifest and projected bytes;
3. the scorer-root manifest and its exclusion from the actual launch namespace;
4. byte equality of paired contracts and oracle programs;
5. equality of paired filenames, titles, and raw-observation schemas;
6. neutral case, pair, and variant identifiers;
7. an empty, canonical PA-3 runtime home; and
8. a detached human-review receipt approving the exact paired manifest; and
9. a canonical launch manifest reconstructed from the real PA-3 Bubblewrap builder,
   including executable/config/model identities, both argv layers, mounts and
   permissions, cwd, environment, projection bytes, runtime home, network policy,
   scratch/output mounts, and evidence/proof identities.

The certificate says explicitly that validation occurred before model launch and that
no model was launched during validation. It embeds and hashes the launch manifest, can
be persisted only once, and is reloaded and reconstructed from the concrete launch
object immediately before the adapter calls `Popen`. Missing, non-approved,
self-authored, stale, differently bound receipts, or any certified-vs-actual launch
difference fail closed.

## Oracle and GL constraints

Generic raw-oracle execution is structurally subject-neutral. The production boundary
accepts only sealed program and declared observation/config bytes, stages them under
fresh neutral host names, and exposes only `/oracle/program.py` and
`/input/payload.json` inside a private Bubblewrap namespace. The oracle has a neutral
writable cwd, fixed cleared environment, private `/proc`, and no source workspace,
fixture path, catalog, scorer authority, case/task ID, or host alias. Python syntax
inspection remains defense in depth rather than the identity-isolation mechanism. The
output schema permits only named raw numeric values, units, and optional uncertainties;
booleans and outcome/status/route/category/diagnosis fields are rejected.

GL preparation reads exact blobs directly from commit
`7d04b5b9882dcd476c1457b8d711ac7b5520b2c1`, verifies byte length and SHA-256,
and materializes only the bounded visible fixture. Preparation does not execute an
oracle, Physics Auditor, benchmark, or GL pilot. Qualification may run the generic
raw-measurement normalizer in the subject-neutral sandbox; that is scripted fixture
verification, not the GL pilot.

## Scientific corrections

All synthetic inputs are paired. Cases 008, 009, 012, 016, and 020 now use,
respectively, a harmonic-potential force identity, the correctly named spherical
Euclidean radial Laplacian with a regular-origin limit, three same-norm factor-two
resolutions, matched-window fitted growth exponents with uncertainties, and explicitly
signed oscillator-frequency estimates with uncertainties. Titles for 018, 020, and
021 are neutral numeric fixture titles. GL contracts 006–009 ask for assessment of
exact sources and raw measurements without embedding the scorer's expected
interpretation.

Scoring, route/proof scoring, benchmark orchestration, recovery, real sessions, prompt
tuning, provider abstraction, and concurrency remain outside PA-5C1.

PA-5C1-T also closes a reproduced PA-2 qualification race without changing the V1
workspace-identity schema: tracked diff inspection uses a private copy of the sealed
Git index, so Git cannot refresh stat-cache bytes in the authoritative index. The real
raw index and canonical staged-entry manifest remain bound and are checked before and
after collection.
