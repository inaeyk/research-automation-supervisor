# Physics benchmark exact scoring (PA-5C2)

PA-5C2 is a read-only scoring boundary for completed blind benchmark actions. It does
not orchestrate or recover actions, launch a Physics Auditor, run the 41-session
benchmark, or run the GL pilot. PA-5C1 remains the authority for fixture bytes,
receipts, oracle isolation, scorer exclusion, and the pre-launch manifest.

## Exact run identity

`ExactBenchmarkRunIdentityV1` is canonical JSON and binds one repetition to:

- case, pair, variant, and repetition IDs;
- catalog ID and canonical catalog SHA-256;
- exact variant-visible manifest, scorer authority, complete scorer-root manifest,
  and review-receipt SHA-256s;
- canonical physics-contract SHA-256;
- source-workspace identity and PA-3 projection-manifest SHA-256s;
- every PA-2 completion-proof ID, oracle ID, result SHA-256, proof SHA-256, trusted
  intent SHA-256, and execution-policy SHA-256;
- PA-3 action ID, action-proof SHA-256, complete PA-3 launch-manifest SHA-256, and
  complete PA-5C1 blindness-certificate SHA-256;
- canonical auditor-report SHA-256 and deterministic route, when routing completed;
- the exact finding-category set, every finding ID/category/severity/status, and every
  check/finding/unresolved-question evidence reference; and
- a semantic-observation SHA-256 over the action status, failure reason, complete
  canonical report, and complete canonical routing decision.

`ExactBenchmarkExpectedRunsV1` sorts these identities by
`(case_id, variant_id, repetition_id)`, rejects duplicate keys and reused PA-2/PA-3
proof identities, and hashes the complete expected set. Scoring requires equality of
the full expected and observed identity-hash sets and equality of their repetition-key
sets. Missing, extra, duplicate, or unrelated unique identities therefore fail before
artifact verification or aggregation.

PA-2 completion proof bytes can legitimately have equal semantic hashes across fresh
attempts because the PA-2 proof schema omits the operational action ID. PA-5C2 treats
the closed tuple of completion-proof ID, result hash, proof hash, intent hash, and
policy hash as the proof identity. The completion-proof ID and result hash retain the
exact execution binding; reusing that closed identity across repetitions is rejected.

## Proof-to-report binding

For every observed run the scorer calls the independent PA-3 verifier. That verifier
reconstructs the request, independently verifies every discovered PA-2 completion,
checks the action record/result/proof hash closure, parses the persisted canonical
report against the exact contract and evidence index, recomputes deterministic
routing, and reconstructs the PA-3 proof.

The scorer then independently closes the PA-5C1 certificate against the current
catalog, exact pair and variant manifests, approved receipt, scorer-root manifest,
source-workspace identity, projection, action request, execution configuration,
prompt, output schema, evidence index, PA-2 proof set, Bubblewrap policy and backend,
and the PA-3 proof. It also verifies that every non-control projected byte is exactly
the reviewed visible variant.

The catalog is accepted only from `catalog.json` at the exact scorer-only root that
the catalog declares and the PA-5C1 certificate hashes. A supplied catalog object
must equal those canonical bytes, and each receipt must resolve below that same
scorer-only root. The run identity hashes the complete certificate and its complete
launch manifest, so changing any executable, model, reasoning, argv, mount,
environment, isolation, exclusion, or other launch field changes the expected run
identity even when the PA-3 action proof itself is unchanged.

Only after those checks does it derive the report hash, route, categories, severities,
evidence references, and semantic-observation hash. The derived identity must equal
the observed identity byte-for-byte. A proof for another report, repetition, case,
source, projection, contract, or scorer authority cannot qualify.

## Separate scoring dimensions

`ExactRunSemanticScoreV1` records independent tri-state results (`correct`,
`incorrect`, or `not_applicable`) for:

- defect-category recognition;
- severity correctness;
- deterministic route correctness;
- required-category satisfaction;
- acceptable category alternatives;
- absence of forbidden categories;
- absence of forbidden routes (PA-5C1 declares one expected route and no route
  alternatives, so every other deterministic route is forbidden);
- evidence validity; and
- clean-case pass.

Malformed-report and infrastructure-failure observations are separate booleans and
separate aggregate counts. Criterion aggregates retain numerator, denominator, and
rate; `not_applicable` runs are excluded from that criterion's denominator. No single
success bit replaces these observations.

Any proof, source, contract, projection, report, identity, scorer-authority, or
repetition mismatch raises `PhysicsBenchmarkScoringIntegrityError`. No partial run
score or aggregate is returned.
