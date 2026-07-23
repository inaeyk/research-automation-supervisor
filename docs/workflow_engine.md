# Deterministic single-substage workflow

Stage 2 adds a synchronous, crash-aware workflow around the Stage 1 Codex
adapter. Its trust boundary is deliberately narrow: a human writes the frozen
contract, the initial and repair prompts, the auditor prompt, every continuation
instruction, and every acceptance-test argument vector. The engine transports
those bytes, assembles deterministic local evidence, and applies fixed state
rules. No model writes, selects, rewrites, or summarizes another model's prompt.

## Specification

`research-supervisor validate-substage PATH` validates without writing or
launching Codex or tests. A schema-version-1 YAML file contains exactly the
substage identity and workspace, four human-authored file locators, fixed worker
and auditor model policies, ordered acceptance tests, allowed/protected path
patterns, a repair limit from zero through ten, and `checkpoint_after`.

All referenced paths resolve from the specification directory. Before
resolution, one shared lexical-chain validator applies `lstat` from the supplied
path anchor through every existing component of the specification, contract,
prompt, or continuation locator. Any linked component (including a linked base
or parent), broken link, non-directory intermediate, or non-regular final file
is rejected. The final resolved file must retain the checked leaf identity. For
a supplied human-file locator inside the workspace, protected-pattern matching
is applied first to its normalized supplied workspace-relative path; resolution
and final workspace containment are checked separately. This prevents an
unprotected locator or a linked parent from escaping to an external file. Test
commands are nonempty YAML argv sequences, never shell strings, and each test
cwd must be inside the workspace. Contract and prompt files are nonempty UTF-8
regular files of at most 2 MiB. The workspace must begin at a clean Git `HEAD`,
including no untracked files. Files inside the workspace that define the
contract or prompts must match a protected pattern. Protected paths always win
over allowed paths.

The minimal format is shown in
`examples/workflows/minimal-substage.yaml`. Validation also rejects duplicate or
unknown YAML fields, path traversal, unsafe bounds, duplicate IDs, and any
structural value that mandatory redaction would alter.

## Prompt assembly and sessions

Every model input exists only in memory and begins with the exact bytes of one
human file. The engine then concatenates fixed section labels, the exact frozen
contract bytes, canonical JSON evidence, an engine-owned output schema, and a
fixed reporting instruction. There is no template evaluation, placeholder
substitution, include mechanism, conditional, or model-generated handoff.
Artifacts retain source and rendered hashes, byte counts, and evidence hashes;
they never retain a rendered prompt.

The first worker turn creates one persistent `workspace-write` Codex session.
The engine accepts only one explicit ID from a structured `thread.started`
event. Every repair and human continuation uses `codex exec resume` with that
exact ID. It never uses `--last`, `--all`, names, or recency. Resume preserves
approval `never`, the explicit workspace, model/reasoning, output schema,
disabled web/workspace network, disabled skill dependency installation, ignored
user config/rules, JSONL capture, and stdin prompt delivery. A missing,
ambiguous, changed, or unavailable worker thread pauses the workflow; a new
worker is never substituted automatically.

Every auditor action is a brand-new Stage 1 run with `read-only`, approval
`never`, network disabled, and `--ephemeral`. Auditor actions are never resumed
or reused. Their structured results are advisory until strict deterministic
validation succeeds.

## State machine and exits

The persisted states are:

```text
initialized -> worker_running -> scope_checking -> tests_running
            -> auditor_running -> completed | checkpoint_paused

scope/test/audit repairable failure
            -> repair_pending -> worker_running

worker human status, invalid model output, audit escalation, transport or
evidence uncertainty -> human_paused

repair limit exhausted -> repair_limit_paused
human abort -> aborted
local invariant corruption -> failed
```

Round zero is the initial worker. A repair round increments immediately before
an exact-ID resumed worker turn. `max_repair_rounds: 0` disables automatic
repair. Only `continue-substage --instruction PATH` leaves a human or repair
limit pause; it hashes and records the instruction locator but not its content,
and its worker turn counts as another repair round. Checkpoint pause is terminal
in Stage 2.

Workflow commands return 0 for completed, 2 for invalid input, 3 for a missing
dependency, 4 for unrecoverable workflow failure, 5 for human pause, 6 for
repair-limit pause, 7 for checkpoint pause, and 8 for aborted. Unexpected
internal failures return 1. `substage-status` returns 0 whenever durable state
is readable, regardless of the stored workflow status.

## Git and fixed-test evidence

The clean baseline records repository root, exact `HEAD`, branch or detached
state, and status hash without creating a commit, branch, index update, or
worktree. After every completed worker, shell-free Git commands collect
NUL-delimited status, a no-color/no-pager/no-ext-diff/no-textconv binary diff,
tracked and untracked paths, deletions, renames, type changes, symlinks,
untracked hashes, and bounded safe text. The engine verifies that its evidence
collection did not alter the index. Patch evidence is capped at 25 MiB; an
oversized patch is replaced by its hash and an explicit truncation marker, and
the workflow pauses before audit.

Acceptance tests run in specification order with exact argv and `shell=False`.
Each uses the validated cwd, a copied credential-filtered environment, disabled
Git optional locks, a new process session, bounded streaming logs, redaction,
and TERM/grace/KILL whole-group cleanup. A test is never retried within an
attempt. The first failure stops execution and later tests receive durable
`skipped` records. Auditing starts only after every test passes and scope is
compliant.

Stage 2 does not provide OS-level network isolation for arbitrary test
executables. Specifications must therefore choose fixed tests that are safe to
run offline. The engine neither adds network credentials nor passes flags that
enable network access.

## Durability, recovery, and artifacts

Each run exclusively creates `<runs-dir>/<substage-id>-<random-token>`. A
nonblocking advisory lock records a strict PID, host, and start-time object
while held and clears it on clean release. An unlocked stale same-host record is
recovered only after its PID is proven absent; live local, foreign-host, and
malformed metadata require human action. State and result snapshots use atomic
replacement and directory fsync where supported. The append-only journal has an
exact schema, monotonic sequence numbers and UTC timestamps, legal
prior/new-state transitions, deterministic action identity and kind, reason
codes, artifact mappings, state updates, and a SHA-256 hash chain. Each entry
must match one closed semantic form keyed by event type, prior state, new state,
action kind, action-ID presence, and its one defined reason; syntactically valid
but undefined or state-inappropriate reasons are rejected even after the chain
is rehashed. Evidence reasons are also bound to their exact state-update field
sets. Human, repair-limit, checkpoint, and local-failure pause transitions must
record the same deterministic journal reason in `pause_reason`; a successful
checkpoint therefore stores `auditor_passed_checkpoint`. Action completion
reasons are kind-specific.

Worker, auditor, and test actions have deterministic IDs. Before launch, the
journal freezes the round, run/action identity, role, workspace, model and
reasoning, sandbox/approval/ephemeral/network policy, rendered prompt and output
schema hashes, handoff and artifact directories, exact worker resume ID, or the
test ID/argv/cwd/timeout/output limits. Every intent has at most one matching
completion.

Normal completion and interrupted recovery use the same proof verifier. Worker
and auditor proof requires the exact seven-file Stage 1 core artifact set plus
the Stage-2-only completion manifest written last by the adapter, strict
normalized-request/metadata/result/handoff/manifest models, canonical complete JSONL,
event-derived session IDs, exact command policy, consistent process/timing
fields, prompt/schema hashes, final-message agreement, and a strict structured
model result when present. Auditor proof additionally requires a non-resumed,
read-only, approval-never, network-disabled ephemeral run and rejects worker or
prior-auditor session substitution. Fixed-test proof requires the exact intent,
logical status/pass/exit/signal/timeout/output-limit agreement, complete empty
or bounded stdout/stderr logs, actual hashes and byte counts, environment-name
filtering, redaction metadata, and valid first-failure skip ordering.

Every operation that reads an existing workflow recomputes every journal-cited
hash. Completion action records are recursively checked against all Stage 1 or
test files; test suites are checked against action records; Git evidence is
checked against its exact patch locator, size, and hash; state is replayed from
the journal and compared with both snapshots. Replay independently derives the
latest completed worker, auditor, and fixed-test action lifecycles from verified
typed records. The durable latest-worker and latest-auditor fields must equal
those independently derived values exactly: null if and only if no verified
completion of that kind exists, otherwise the most recent verified completion.
IDs cannot cross roles, cite an earlier or nonexistent action, be erased by
later evidence or transitions, or rely on a prefix alone. Missing, replaced,
truncated, or contradictory evidence cannot report a completed status. An
unmatched intent is never relaunched: fully proved artifacts are finalized
exactly once, while uncertain artifacts cause a durable human pause. A
completion journaled before snapshot replacement is replayed without relaunch.
Frozen source hashes, repository root, `HEAD`, and branch/detached identity are
checked before continued work, and the engine never guesses or rolls back
files.

The run directory contains normalized specification and frozen hashes,
baseline, state/result snapshots, the journal, action records, worker/test/audit
artifacts, Git evidence, hash-only handoff manifests, immutable versioned
escalation packages, and the two engine-owned output schemas. Captured output is
bounded and redacted; prompt and contract content are not persisted by the
workflow.

## Commands and non-goals

The Stage 2 commands are `validate-substage`, `run-substage`, `resume-substage`,
`continue-substage`, `substage-status`, and `abort-substage`. All execution is
synchronous and covers exactly one substage. Stage 2 has no intelligent
supervisor, model-written prompts, contract mutation, multi-substage advance,
Git publishing or worktrees, notifications, background services, package
installation, API calls, or project-specific research logic. A later stage may
place an intelligent planning supervisor outside this deterministic boundary.
Stage 3 now provides only a retrospective blind supervisor: it cannot intercept
a live action, send a proposal, or weaken the frozen human approval and evidence
guarantees described here. `substage-status` remains strict when an external
human-continuation instruction disappears. A separate Stage-3-only trusted read
path reuses the same journal/action/handoff validation while permitting only
that anchored source file to be absent so the retrospective comparison can be
marked unavailable.
