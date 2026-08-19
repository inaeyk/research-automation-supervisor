# Semantic Decomposition V1

This stage makes repository state and compact artifacts the durable memory of a
Supervisor-controlled stage. Model conversation is ephemeral and is not forwarded across
semantic boundaries.

PA-5D remains paused. This implementation and its replay harness do not regenerate PA-5D0,
launch PA-5D scientific sessions, merge, tag, or release anything.

## Decomposition policy

`SemanticTaskPlanV1` is strict and versioned. A substantial stage has two through six ordered
`SemanticSubtaskV1` records; a non-substantial stage remains one task. The planner records the
plan before launch and chooses the smallest number of tasks that materially reduces irrelevant
context. Each task contains one objective, a bounded scope, authority path/hash references,
required predecessor artifacts, deliverables, independently checkable completion conditions,
validation requirements, and stop conditions.

Valid boundaries are an independent component or interface, investigation to implementation,
implementation to independent audit, implementation to qualification, and a logically
independent repair. The schema rejects obvious single-operation objectives and adjacent tasks
with the same role, boundary, and coherence key. Reads, searches, tiny edits, and individual
tests sharing the same context are operations inside a task, not semantic tasks.

The B4 policy remains the default: `model_auto_compact_token_limit=64000` and
`tool_output_token_limit=2048`. Tool-call thresholds are measured batching signals. They never
deny an otherwise justified call or create a retry loop.

## Session and handoff policy

Every independent semantic task starts with a fresh `codex-task run`. No prior thread or model
conversation is passed. `SessionLaunchV1` permits continuation only for:

- qualified recovery tied to the exact previous thread identity; or
- unresolved working context that cannot safely be represented by a compact artifact.

A continuation records the exact prior thread, a typed reason, a durable explanation, and the
governing authority reference. Coding and physics auditors cannot continue another session.
Existing PA-4/PA-5 same-Worker repair requirements therefore remain intact: the fresh auditor
hands concise findings back to the exact qualified Worker thread rather than transferring the
auditor conversation.

`AgentHandoffV1` contains only:

1. completed objective;
2. changed paths and interfaces;
3. repository, base commit, HEAD, diff hash, and dirty-state identity;
4. authority paths and hashes;
5. still-valid evidence or test receipts;
6. established decisions and invariants;
7. unresolved findings;
8. remaining work;
9. next-task requirements; and
10. things that should not be rediscovered or retested.

Unknown fields fail validation and `transcript_included` can only be `false`. The model authors
only the semantic draft; the Supervisor binds repository identity, authority hashes, and the
handoff ID. Handoffs target at most 1,000 tokens and have a 2,000-token soft maximum. Exceeding
the soft maximum requires an explicit justification; the defensive absolute bound is 8,000
tokens. When an exact tokenizer is unavailable, enforcement uses UTF-8 byte count as a proven
upper bound and reports the token count itself as unavailable, never estimated.

Worker-to-Auditor prompts contain the current task, candidate-tree identity, compact Worker
handoff references, valid receipts, and audit scope. They contain no Worker transcript,
reasoning history, unrelated logs, or repeated stage plan. Auditor-to-repair transfer follows
the same rule for findings and evidence.

## Validation receipt reuse

`ValidationReceiptV1` is deliberately narrow. A downstream task may cite a deterministic PASS
only when the receipt has exit code zero and the complete ordered fingerprints of test code,
relevant source, configuration, and environment assumptions remain identical. A failed result
or any changed or uncertain fingerprint requires a rerun. This is not a general DoneTestLibrary.

## Telemetry and recovery

Each Supervisor-launched task joins the authoritative global `TaskUsageReceipt` with the B4
context-economy receipt, launch decision, and handoff measurement. It records exact input,
cached input, uncached input, output, reasoning output, combined total, inference samples when
available, per-session median and maximum context when available, compactions, command/tool
count, model-visible tool-output characters, handoff size, and freshness/continuation reason.
Aggregation is by role, repair/retry, and total stage. Missing authoritative counters remain
unavailable. A cross-session median is unavailable because it cannot be reconstructed exactly
from per-session medians.

Replay task IDs are deterministic and every independent task uses `run`, never `resume`. An
existing incomplete global ledger stops for qualified recovery instead of launching a duplicate.
The manifest records the authoritative ledger selected by the active `CODEX_HOME`; the launcher
and recovery reader cannot be pointed at different ledger roots.
An already-completed ledger is consumed only after its task identity, workspace, model, exact
launch options, one-turn shape, completion state, and durable prompt hash all match. External
replay recovery accepts only a contiguous prefix containing both a valid handoff and matching
telemetry for every completed task; partial current-task artifacts are verified or reconstructed
without overwriting conflicting bytes. Handoffs are exclusive-created, and receipts retain the
global wrapper's existing exact-turn and deduplication behavior.

## Controlled bootstrap replay

The recorded plan is
[`bootstrap_replay_control.json`](validation/semantic_decomposition_v1/bootstrap_replay_control.json).
It freezes:

- implementation authority commit `4c1df104a861d638275e573b996b6d7a29b1297f`;
- clean workload start commit `aeaef976c5990245c3d72f0b0cf41bc76fd8d415`;
- original workload prompt SHA-256
  `2da7e7a7e3e67b4f70200846a07bf5eea9190ad800d86d691be3992776d6bbde`;
- model `gpt-5.6-sol`, reasoning effort `high`, and B4 64k auto-compaction; and
- five semantic boundaries: global runtime wrapper, RAS schema/aggregation, launch integration,
  recovery/qualification/docs, and a fresh read-only coding audit.

The original evidence did not authoritatively retain model and reasoning metadata, so those two
runtime controls are explicit replay controls rather than reconstructed claims about the original
run. The workload prompt and start tree are exact.

The harness is dry-run by default. From the qualified implementation checkout, a human creates a
separate detached worktree and keeps replay artifacts outside it:

```bash
git worktree add --detach \
  /absolute/path/to/bootstrap-semantic-replay-worktree \
  aeaef976c5990245c3d72f0b0cf41bc76fd8d415

.venv/bin/python -m research_automation_supervisor.semantic_replay \
  --control "$PWD/docs/validation/semantic_decomposition_v1/bootstrap_replay_control.json" \
  --authority-root "$PWD" \
  --workspace /absolute/path/to/bootstrap-semantic-replay-worktree \
  --artifact-root /absolute/path/to/bootstrap-semantic-replay-artifacts \
  --global-target /absolute/path/to/disposable-bootstrap-codex-home
```

The dry run verifies all authority and repository identities and prints the exact commands without
creating artifacts or launching Codex. Human execution is the same command with the explicit final
argument. The global target must initially be absent or empty and must be outside the authority
checkout, replay worktree, replay artifacts, and live `$CODEX_HOME`. Only the runtime-wrapper and
qualification Workers receive write authority to this disposable stand-in; the live credentials
and authoritative replay ledger remain inaccessible to model writes.

```bash
.venv/bin/python -m research_automation_supervisor.semantic_replay \
  --control "$PWD/docs/validation/semantic_decomposition_v1/bootstrap_replay_control.json" \
  --authority-root "$PWD" \
  --workspace /absolute/path/to/bootstrap-semantic-replay-worktree \
  --artifact-root /absolute/path/to/bootstrap-semantic-replay-artifacts \
  --global-target /absolute/path/to/disposable-bootstrap-codex-home \
  --execute I_ACKNOWLEDGE_EXTERNAL_REPLAY
```

That acknowledgement is intentionally not used by this implementation task.
