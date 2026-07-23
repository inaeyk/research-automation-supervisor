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

All referenced paths resolve from the specification directory. Test commands
are nonempty YAML argv sequences, never shell strings, and each test cwd must be
inside the workspace. Contract and prompt files are nonempty UTF-8 regular files
of at most 2 MiB. The workspace must begin at a clean Git `HEAD`, including no
untracked files. Files inside the workspace that define the contract or prompts
must match a protected pattern. Protected paths always win over allowed paths.

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
nonblocking advisory lock records PID, host, and start time. An unlocked stale
same-host record is recovered only after its PID is proven absent; foreign-host
metadata requires human action. State and result snapshots use atomic
replacement and directory fsync where supported. The append-only journal has
monotonic sequence numbers and a SHA-256 hash chain.

Worker, auditor, and launched test actions have deterministic IDs. The journal
records an intent before launch and completion only after the complete artifact
set exists. Recovery replays hash-validated journal updates omitted by an
interrupted snapshot write. It accepts a completed action only from a complete,
strictly validated Stage 1 or test artifact set; otherwise it enters
`human_paused`. Completed action IDs are never launched again. Frozen source
hashes, repository root, `HEAD`, and branch/detached identity are checked before
continued work, and the engine never guesses or rolls back files.

The run directory contains normalized specification and frozen hashes,
baseline, state/result snapshots, the journal, action records, worker/test/audit
artifacts, Git evidence, hash-only handoff manifests, escalation packages, and
the two engine-owned output schemas. Captured output is bounded and redacted;
prompt and contract content are not persisted by the workflow.

## Commands and non-goals

The Stage 2 commands are `validate-substage`, `run-substage`, `resume-substage`,
`continue-substage`, `substage-status`, and `abort-substage`. All execution is
synchronous and covers exactly one substage. Stage 2 has no intelligent
supervisor, model-written prompts, contract mutation, multi-substage advance,
Git publishing or worktrees, notifications, background services, package
installation, API calls, or project-specific research logic. A later stage may
place an intelligent planning supervisor outside this deterministic boundary;
it must not weaken the frozen human approval and evidence guarantees described
here.
