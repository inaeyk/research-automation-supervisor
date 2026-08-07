# Physics benchmark campaign orchestration (PA-5C3)

PA-5C3 is a thin, sequential campaign layer over qualified PA-4/PA-5A child
workflows and the unchanged PA-5C2 exact scorer. It does not run PA-2 or PA-3
directly, create Worker or Auditor sessions, interpret scientific findings, or
reconstruct any child recovery rule.

## Frozen manifest and child identity

`PhysicsBenchmarkCampaignManifestV1` is written before child registration. Its
self-hash binds the campaign ID, repository and scorer-catalog authority, dedicated
child-run root, and the complete ordered `CampaignChildAuthorityV1` set. Each child
binds:

- campaign, case, variant, and repetition coordinates;
- a deterministic child run ID and PA-4 run token;
- the exact expected PA-4 run directory;
- the schema-version-2 specification, workspace Git authority, contract, oracle
  catalog, and Auditor configuration paths and hashes.

Case/variant/repetition keys, child IDs, run tokens, run directories, and workspaces
must be unique. The PA-4 substage ID must equal the benchmark case ID. These rules
make the expected child set immutable before any launch.

Every load first validates the manifest self-hash and then requires the state and
origin journal transition to repeat exactly the manifest's campaign ID, manifest
digest, repository root, scorer catalog path/ID/digest, derived scorer authority,
and canonical complete-child-set digest. A different valid manifest is not a new
authority for an existing directory. Manifest replacement and matching edits to the
state snapshot therefore fail before recovery, launch, scoring, or finalization.

## Delegation boundary

The child adapter calls only `run_substage` to create an ordinary schema-version-2
PA-4 run. It supplies the already-qualified PA-5C1 launch authority through PA-4's
existing Physics Auditor service seam. Campaign recovery first calls
`build_recovery_plan`; only `auto_resume` and `finish_finalization` plans are passed
to `execute_recovery_plan`. The exact same blind PA-4 service authority is supplied
to PA-5A.

Blocked, ambiguous, active, stale, reused, and foreign process observations are not
approximated. A matching active process leaves the campaign `running`; other blocked
plans fail closed as `infrastructure_blocked`. PA-4 evidence pauses map without
reinterpretation to `insufficient_evidence` or `human_review_required`. Terminal
failed or aborted children map to `child_failed`.

The campaign never accepts a human decision. A decision is applied only through the
child's existing PA-4/PA-5A path, after which campaign resume rebuilds a new PA-5A
plan.

## Discovery, proof closure, and aggregation

Child discovery is rebuilt from PA-5A's authoritative direct-run inspection. Extra,
duplicate, malformed, substituted, or wrong-schema child runs fail closed. Before
aggregation, the campaign requires all of the following at once:

1. expected run directories exactly equal discovered run directories;
2. every discovered child has a PA-5A `already_terminal` plan;
3. manifest identity equals the PA-4 index, state, journal, and frozen authority;
4. terminal child identities exactly cover every case/variant/repetition key once;
5. every stored terminal observation rebinds identically through PA-5C2;
6. the expected PA-5C2 identity set exactly equals the observed identity set.

Only then is `score_exact_physics_benchmark` called. PA-5C2 independently verifies
the current PA-2/PA-3 proofs, PA-5C1 certificate, scorer authority, source,
projection, report, route, and semantic identity before producing metrics. A partial
result is never a completed campaign.

## Durable state and exactly-once semantic actions

`PhysicsBenchmarkCampaignStateV1` uses these public routing states:
`running`, `resumable`, `human_review_required`, `insufficient_evidence`,
`infrastructure_blocked`, `child_failed`, `ready_to_aggregate`, and `completed`.
Its append-only journal is hash chained and reconciles a state snapshot that was
interrupted after journal fsync.

Registration, launch intent, recovery delegation, terminal observation, scorer,
aggregate, action-tree, and completion records have deterministic IDs. A launch intent is
durable before entering PA-4. If its expected child directory is absent after that
point, the campaign treats launch state as ambiguous and does not relaunch. If the
directory exists, all launch/process/proof decisions are delegated to PA-5A.

Before entering PA-5C2, the campaign atomically persists and journals one scorer
action-start receipt. Its deterministic identity binds the campaign and manifest,
repository and scorer authority, complete child-authority set, complete PA-5C2 input
identity set, and expected-run manifest. Once this receipt exists, the scorer is
never invoked again. On success, the exact score report and its canonical result
hash are embedded together in one atomically persisted and journaled result receipt.
A verified result receipt is reused byte-for-byte. A start receipt without a
verified result is an ambiguous scorer boundary and routes
`infrastructure_blocked`; zero duplicate scorer actions takes precedence over
automatic recovery.

The expected PA-5C2 manifest, scorer receipts, aggregate, action tree, and completion
receipt are each atomically replaced, fsynced, and bound by the exact hash in their
own journal transition before the next lifecycle step. Before aggregation or
finalization, the campaign inventories these fixed paths and rejects missing,
unjournaled, stale, substituted, cross-campaign, or wrong-hash artifacts without
deleting or adopting them. Finally, the hash-chained `completed` transition and state
snapshot are committed last. Thus a completed state is the commit marker for all
prior files, and repeated resume is read-only and returns the same semantic result.

PA-5C3 does not provide parallel scheduling and does not run the real benchmark or GL
pilot.
