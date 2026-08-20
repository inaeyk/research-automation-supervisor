# Semantic Decomposition V1

This stage makes repository state and compact artifacts the durable recovery memory of a
Supervisor-controlled stage. Semantic task boundaries remain explicit, but they are independent
of model-session boundaries. Conversation persists only inside a bounded session epoch and is
never transferred between epochs.

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

`SessionEpochPlanV1` groups each semantic subtask into exactly one ordered `SessionEpochV1`.
Epochs may contain only contiguous subtasks with one role, model/configuration, context-economy
profile, and continuation policy. Every epoch begins fresh. Its first subtask uses
`codex-task run`; later subtasks use `codex-task resume` with the same epoch task ID and verified
Codex thread identity. Resume preserves B4's 64,000-token auto-compaction ceiling and 2,048-token
tool-output ceiling from the initial run.

The deterministic locality policy continues adjacent Worker tasks that share a subsystem,
newly-created interfaces, source/test architecture, an implementation-to-integration-to-
qualification chain, or current-candidate repair context. It starts a fresh epoch on a role
change, required security/blindness independence, genuine subsystem independence, little useful
shared context, an exceeded context-health limit, or qualified recovery requiring another
identity. Each decision and rationale is durable; no model call is used to make it.

Coding Auditor epochs are always fresh, singleton, and read-only. They receive authority,
candidate repository/diff identity, compact completed-epoch handoffs, and relevant valid
receipts, but no Worker conversation. Physics Auditor freshness/security and existing PA-4/PA-5
same-Worker repair semantics remain unchanged.

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
handoff ID. When no exact compatible tokenizer exists, the qualified deterministic policy targets
3,072 UTF-8 bytes, requires justification above the 4,096-byte soft maximum, and rejects more
than 8,192 bytes. Token count is reported unavailable rather than estimated.

Every subtask result is persisted for recovery. Inside an epoch, the resumed model receives only
the new semantic delta: the next objective and its relevant authority/scope changes. The full
`AgentHandoffV1` is not injected back into the same conversation. Between epochs, the compact
handoff is injected; no conversation transcript is transferred.

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

Each Supervisor-launched turn joins the authoritative global `TaskUsageReceipt` with the B4
context-economy receipt, launch decision, and handoff measurement. It records exact input,
cached input, uncached input, output, reasoning output, combined total, inference samples when
available, median and maximum inference context when available, compactions, command/tool
count, model-visible tool-output characters, handoff size, and freshness/continuation reason.
Authoritative totals aggregate each completed resumed turn exactly once and include turn and
session counts. Turn-level cumulative `turn.completed.usage` is never substituted for an
inference-context sample. Inference sample count, median/max context, and compactions are populated
only from genuine rollout/token-count inference events; otherwise they remain null/unavailable.
A combined median remains unavailable when it cannot be reconstructed exactly.

Replay task IDs are deterministic per epoch. An existing incomplete global ledger stops for
qualified recovery instead of launching a duplicate.
The manifest records the authoritative ledger selected by the active `CODEX_HOME`; the launcher
and recovery reader cannot be pointed at different ledger roots.
An already-completed turn is reused only after its epoch identity, workspace, model, exact initial
launch options, required completed turn, completion state, every retained prompt hash, and thread identity
all match. The next semantic subtask resumes that same epoch thread only when the durable epoch
plan requires continuation. Turn files beyond the qualified task state cause a fail-closed stop,
not a duplicate launch. Recovery accepts only a contiguous prefix containing a valid handoff and
matching telemetry for every completed task; conflicting partial artifacts are never overwritten.

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
  recovery/qualification/docs, and coding audit; and
- exactly three epochs: A is fresh Worker `01-runtime-wrapper`; B is one fresh Worker session for
  `02-ras-accounting`, then same-thread resumes for `03-launch-integration` and
  `04-qualification`; C is fresh read-only Coding Auditor `05-coding-audit`.

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
checkout, replay worktree, replay artifacts, and live `$CODEX_HOME`. Epochs A and B receive write
authority to this disposable stand-in because each contains a designated writer subtask; the live
credentials and authoritative replay ledger remain inaccessible to model writes. Epoch C remains
read-only.

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
