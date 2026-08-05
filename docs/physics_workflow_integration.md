# Physics-enabled single-substage workflow

PA-4 integrates the qualified PA-1 router, PA-2 trusted oracles, and PA-3 isolated
Physics Auditor into normal synchronous single-substage execution. It is an explicit,
versioned opt-in: schema-version-1 substages, states, results, and journals retain their
0.2.0 models, serialization, hashes, exit behavior, and commands.

## Versioned opt-in

A physics workflow uses `schema_version: 2`, all existing substage fields, a required
`physics_contract_path`, and this strict block:

```yaml
physics:
  schema_version: 1
  enabled: true
  required: true
  trusted_oracle_catalog_path: ../project/control/oracle-catalog.json
  auditor_execution_config_path: ../project/control/physics-auditor.yaml
  max_repair_rounds: 2
  human_review_triggers:
    - convention_change
    - unresolved_gauge_constraint_ambiguity
    - new_physical_interpretation
    - conflicting_evidence
    - contract_weakening_attempt
  insufficient_evidence_policy: block
  conflicting_evidence_policy: human_review
  medium_finding_policy: request_repair
  low_finding_policy: allow_pass
```

All five scientific-review triggers are mandatory. The finding and evidence policies
must exactly equal the physics contract's PA-1 audit policy; PA-4 cannot reinterpret or
weaken the authoritative router. The optional physics repair limit may only narrow the
existing substage limit, so there is one shared repair counter.

Validation requires every contract-required oracle ID to exist in the trusted PA-2
catalog. Each oracle program must be protected and must not be Worker-writable. No
undeclared catalog intent is executed.

## Execution and completion gate

The engine dispatches by specification or durable-state version and runs:

```text
persistent Worker -> visible tests -> fresh Code Auditor
  -> required trusted PA-2 oracles -> fresh isolated PA-3 Physics Auditor
  -> unchanged deterministic PA-1 router
```

A schema-version-2 run completes only when visible tests and Code Auditor checks pass,
all required PA-2 completion proofs verify against one current workspace identity, the
PA-3 result/action proof/report/routing record strictly verify, PA-1 routes `pass`, no
human or evidence pause remains, and a newly collected final workspace identity equals
the accepted oracle and Physics Auditor evidence. `substage-status` also detects a
workspace mutation after completion.

PA-3 is invoked through its existing standalone interface. Each round gets a new
read-only projected Bubblewrap session with approval `never`, no resume, no Worker or
Code Auditor session identifier, no original worktree, and no oracle program or
protected-data mount. PA-4 records and rejects any repeated Physics Auditor provider
thread ID. It never adds or inherits `--yolo`, `--full-auto`, or
`danger-full-access`.

## Routes, repair, and evidence

The unchanged PA-1 route has these workflow effects:

| PA-1 outcome | PA-4 effect |
| --- | --- |
| `pass` | verify the completion gate and complete |
| `request_repair` | queue bounded repair to the same Worker session |
| `require_human_review` | write a review packet and durably pause |
| `block_insufficient_evidence` | write an evidence packet and durably pause, without Worker blame |
| `infrastructure_failure` | stop as infrastructure failure, without candidate blame |

An automatic repair prompt contains only validated finding IDs, bounded summaries,
evidence references, and required actions. It never contains route metadata, round or
decision metadata, engine prose, model reasoning, raw streams, or protected material.
The existing Worker
thread is resumed; visible tests and a fresh Code Auditor rerun. A workspace change
invalidates all PA-2 acceptance evidence bound to the old complete workspace identity,
while the old records remain historical. Evidence is preserved only when the exact
full PA-2 identity is unchanged. A new Physics Auditor session is always launched.

Functional oracle failures remain candidate evidence for PA-1. Missing or malformed
evidence pauses, workspace/proof integrity failures fail closed, and transport or local
capability failures stop as infrastructure. None consumes an automatic Worker repair
unless PA-1 returns `request_repair`.

## Human scientific gate

Every human/evidence/repair-limit pause contains an immutable
`PhysicsHumanReviewPacketV1` binding the run, frozen authorities, current workspace,
software result, oracle results/proofs, Physics Auditor proof/report/route, findings,
and unresolved questions. `PhysicsReviewDecisionV1` must bind the packet hash and
supports:

- `approve_existing_contract`: retain frozen authority and request bounded Worker work;
- `revise_contract`: abort this run because revised authority requires a new run;
- `request_additional_evidence`: resume the same Worker for bounded evidence work;
- `accept_with_caveat`: record the caveat but do not override a non-pass PA-1 route;
- `reject_candidate`: durably abort without making a scientific-failure claim.

If a Worker changed frozen authority, the engine records the attempt and review packet
before pausing. The operator must restore the exact frozen authority bytes before the
strict status/review command will accept a decision; PA-4 never repairs or adopts the
changed contract.

Use `research-supervisor review-physics-substage RUN --decision DECISION.yaml`. Existing
`validate-substage`, `run-substage`, `resume-substage`, `substage-status`, and dispatch
also recognize schema version 2. `continue-substage` dispatches a version-2 run to the
same strict decision parser for programmatic compatibility.

## Durability and recovery

PA-4 uses separate `PhysicsWorkflowStateV2`, `PhysicsWorkflowResultV2`, and
`PhysicsWorkflowJournalEntryV2` records. Its journal forms are disjoint from the frozen
version-1 forms. Per-round software results are copied into create-once evidence files;
old journal hashes never point at the mutable nested version-1 result snapshot.

Every software, oracle, and Physics Auditor action has one deterministic intent and at
most one completion. State/result snapshots reconcile from the hash-chained journal.
Recovery covers software gating, oracle intent/completion/proof refresh, Physics Auditor
launch/completion/route, repair routing, evidence invalidation or preservation, human
decision recording, pauses, and final completion. A proved completion is finalized
without relaunch; an ambiguous external launch stops as infrastructure instead of
guessing or retrying.

PA-4 does not add provider-neutral adapters, parallel scheduling, campaign integration,
protected historical evaluation, publication approval, or any change to PA-1, PA-2,
or PA-3 canonical proof semantics.

## PA-5A recovery UX

The separate PA-5A [workflow recovery layer](workflow_recovery.md) discovers both
schema-version-1 and schema-version-2 runs and delegates only a verified safe plan back
to this unchanged engine. It recursively verifies current PA-2 and PA-3 evidence,
checks their Linux PID/start identities before invoking a resumer, reopens physics
human/evidence pauses, and can replay one journaled `PhysicsReviewDecisionV1` exactly
once. It never changes the physics contract, catalog, auditor configuration, routing,
Worker thread, model, approval, sandbox, oracle policy, or repair limit.

The layer accepts PA-4's qualified sequence-zero initial snapshot without adding a new
journal form. Before continuing past the software gate, it recursively verifies the
nested software workflow and compares the current worktree with its accepted Git
evidence; once PA-4 has journaled a full physics workspace identity, that identity must
match as well. Terminal public-result reconstruction ignores corrupt result bytes,
reverifies the authoritative state and proofs, writes atomically, and verifies the
replacement before declaring finalization complete.
