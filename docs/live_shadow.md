# Live quarantined shadow observation

Stage 4 surrounds one ordinary Stage 2 run with an observation-only calibration
layer. The Stage 2 process is launched once with the already validated,
unchanged specification and remains the sole authority. Its worker prompts,
auditor prompts, commands, tests, transitions, result, and exit code do not
depend on Stage 4 or on any supervisor response.

## Observation and temporal blindness

The observer follows the authoritative run's durable hash-chained journal. It
reacts only after a worker or auditor `action_intent` is complete and verified;
it does not add a callback to the Stage 2 critical path. For each intent it
freezes an immutable envelope from the journal prefix ending at that exact
entry. The envelope binds the authoritative run and action identities, decision
kind, ordinal and repair round, baseline, scope, exact acceptance-test argv,
prior typed summaries, and bounded hash-verified evidence.

The envelope excludes the current action's output and everything created later:
workspace changes, tests, audits, transitions, comparisons, and reviews. It
also excludes authoritative source and rendered prompt bytes, paths, and
hashes. The fully rendered blind input is assembled in memory and sent on
standard input; only its component manifest, hash, and byte count are stored.

## Quarantine and the persistent queue

All supervisor turns use one empty, dedicated quarantine directory as their
workspace. It contains no checkout, repository copy, symlink, or bind mount.
The supervisor is read-only, approval is `never`, network and web access are
disabled, user configuration and rules are ignored, and dependency installation
is disabled.

The first turn must emit exactly one canonical, lowercase, non-nil
`thread.started` UUID. Later turns resume that exact UUID, which may not collide
with a Stage 2 worker or auditor UUID. Only one turn is in flight at a time;
later observed decisions queue in journal order. The queue never blocks Stage 2:
the authoritative action proceeds immediately after its own durable intent and
may finish before its shadow proposal.

## Comparison, reviews, and readiness

A proposal is never sent to a worker or auditor and cannot change an
authoritative prompt. Comparison starts only after the supervisor proposal and
its corresponding authoritative action have both finalized and authoritative
prompt reconstruction has been proven. Only then are the authoritative source
prompt, rendered prompt, candidate, and deterministic comparison stored.
Comparison or review material is never included in a later supervisor turn. If
reconstruction cannot be proved, comparison is marked unavailable instead of
being guessed.

Reviews use the immutable Stage 3 review schema. `record-live-shadow-review`
adds one review for a comparison-available proposal and never overwrites it.
The report overlays review status without rewriting the immutable assessment.
Readiness is only `insufficient_data`, `not_ready`, or
`candidate_ready_for_supervised_handoff`. Candidate readiness requires the
configured review thresholds, acceptable consecutive worker and auditor
coverage, a completed authoritative run, and no unresolved temporal or
integrity failure. Every readiness result remains informational and
`automation_enabled` is always false.

## Failure isolation and recovery

Transport, malformed result, UUID/session, confidentiality, temporal-envelope,
and reconstruction failures are shadow-side evidence. A degraded Stage 4 result
reports the authoritative Stage 2 result separately and never rewrites it.
Deterministic proposal disqualification also leaves collection running.

The launch record is written before and after the single detached Stage 2
launch, recording its PID, process-group/session identity, and process start
ticks. Once discovered, the authoritative run identity is immutable. State and
result snapshots are atomic and fsynced, and the Stage 4 journal is strictly
hash chained behind a hardened no-follow lock.

`resume-live-shadow` reattaches to the recorded process and existing Stage 2
run; it never launches a replacement. If a prepared launch cannot be proved,
recovery pauses for a human instead of guessing. A completed external
supervisor action is finalized from exact durable evidence. `abort-live-shadow`
stops only observation and never signals or modifies Stage 2. Status and report
are read-only.

## Commands and artifacts

The seven commands are:

```text
validate-live-shadow-spec PATH [--json]
run-live-shadow PATH [--runs-dir PATH] [--stage2-runs-dir PATH] [--json]
resume-live-shadow RUN_DIR [--json]
live-shadow-status RUN_DIR [--json]
record-live-shadow-review RUN_DIR PROPOSAL_ID REVIEW_PATH [--json]
live-shadow-report RUN_DIR [--json]
abort-live-shadow RUN_DIR --reason TEXT [--json]
```

Each run contains normalized frozen inputs, hash manifests, state/result
snapshots, the Stage 4 journal, an empty `quarantine/`, authoritative identity
records, immutable decision envelopes, proposal artifacts, post-finalization
comparisons, reviews, reports, and escalation directories. The authoritative
Stage 2 run remains a separate, independently readable Stage 2 artifact tree.
The default live-shadow run root is the platform temporary directory so the
quarantine is outside a repository; an explicit `--runs-dir` must preserve that
separation.

Exit 0 means reviewed completion; 2 invalid specification or review input; 3 a
dependency failed before launch; 4 trusted integrity failure; 5 awaiting
reviews, degraded shadowing, or human pause; 8 observer abort; and 1 unexpected
internal failure. Readable status and report commands return zero regardless of
the run status. The authoritative Stage 2 exit code is recorded separately.
