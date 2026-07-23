# Stage 2 Contract — Deterministic Single-Substage Workflow

Status: human-approved implementation contract  
Contract schema version: 1  
Stage ID: `AUTOMATION-2`

## Goal

Implement a crash-recoverable deterministic workflow for one human-approved
research-software substage:

```text
human-written contract and prompts
              ↓
persistent worker Codex turn
              ↓
scope checks and fixed tests
              ↓
fresh ephemeral auditor Codex turn
       ↙                         ↘
repairable failure              pass
       ↓                         ↓
resume exact worker         close or checkpoint-pause
       ↓
repeat within fixed limit
```

Stage 2 automates transport, evidence assembly, state transitions, fixed tests,
and worker/auditor handoffs. It does not let any model write another model's
prompt or alter the approved contract.

## Preconditions

- Stage 0 is tagged `stage0-complete`.
- Stage 1 is tagged `stage1-complete`.
- The Stage 1 deterministic Codex adapter and all existing tests remain passing.
- The installed Codex CLI supports persisted `codex exec` sessions,
  `codex exec resume <SESSION_ID>`, and `--output-schema`.

## Non-goals

Do not implement any of the following in Stage 2:

- model-generated prompts;
- an intelligent supervisor model;
- automatic contract or acceptance-criterion changes;
- multi-substage plans or automatic advancement to another substage;
- Git commits, tags, branches, merges, pushes, or worktrees;
- notifications, email, browser control, scheduling, or background services;
- parallel workflows;
- automatic package installation;
- use of `resume --last`, `resume --all`, or session selection by recency;
- auditor session reuse or resume;
- model-based approval review;
- automatic permission escalation;
- API calls or API-key management;
- project-specific Gregory–Laflamme logic.

Stage 2 executes human-approved fixed test argument vectors directly. It must
not add network credentials or deliberately enable network access. OS-level
network isolation for arbitrary fixed test programs is a later stage; projects
using Stage 2 must supply tests that are safe to run offline.

## Required repository changes

Create at least:

```text
src/research_automation_supervisor/
    workflow_models.py
    workflow_engine.py
    workflow_prompts.py
    test_runner.py
    git_evidence.py
tests/
    test_workflow_models.py
    test_workflow_prompts.py
    test_test_runner.py
    test_git_evidence.py
    test_workflow_engine.py
examples/workflows/minimal-substage.yaml
examples/workflows/minimal-stage-contract.md
examples/prompts/worker-initial.md
examples/prompts/worker-repair.md
examples/prompts/auditor.md
docs/workflow_engine.md
```

Update the Stage 1 adapter, CLI, fake Codex, redaction, errors, and documentation
only where necessary. Additional small modules are allowed when they improve
separation of concerns.

Do not edit:

- `STAGE_0_CONTRACT.md`
- `CODEX_STAGE_0_PROMPT.md`
- `STAGE_1_CONTRACT.md`
- `CODEX_STAGE_1_PROMPT.md`
- `STAGE_2_CONTRACT.md`
- `CODEX_STAGE_2_PROMPT.md`

## Required CLI

The installed command remains `research-supervisor`.

### `research-supervisor validate-substage PATH [--json]`

Validate a substage specification, every referenced human-written file, role
policies, test argument vectors, Git state, and path/scope rules. Perform no
writes and do not launch Codex or tests.

### `research-supervisor run-substage PATH [--runs-dir PATH] [--json]`

Create an exclusive workflow run directory and synchronously execute the
substage until one terminal or paused condition is reached:

- `completed`;
- `checkpoint_paused`;
- `human_paused`;
- `repair_limit_paused`;
- `failed`;
- `aborted`.

### `research-supervisor resume-substage RUN_DIR [--json]`

Continue an interrupted nonterminal workflow from its last safe state. This is
for process interruption or machine restart, not for inventing a new
instruction. It must not repeat a completed worker, test, or audit action.

### `research-supervisor continue-substage RUN_DIR --instruction PATH [--json]`

From `human_paused` or `repair_limit_paused`, append an exact human-written
instruction file to the persistent worker session and continue under the same
frozen substage contract. It must:

- validate, read once, and hash the instruction;
- reject structural-redaction collisions;
- record the instruction path and hash, not print or persist its content;
- never modify the frozen contract;
- count the resumed worker turn as a repair round.

It is invalid from completed, checkpoint, failed, aborted, or actively running
states.

### `research-supervisor substage-status RUN_DIR [--json]`

Read durable state without writes or process launches.

### `research-supervisor abort-substage RUN_DIR --reason TEXT [--json]`

Atomically mark a nonterminal, non-running workflow aborted. Sanitize the
reason. Active-process termination through this command is a later-stage
feature.

Human-readable output is the default. JSON output must be stable and agree with
the durable workflow result and state.

## Substage specification model

Implement a strict immutable YAML model containing exactly:

- `schema_version: int`, currently exactly `1`;
- `substage_id: str`;
- `title: str`;
- `workspace: str`;
- `contract_path: str`;
- `worker_initial_prompt_path: str`;
- `worker_repair_prompt_path: str`;
- `auditor_prompt_path: str`;
- `worker_model: str`;
- `worker_reasoning_effort: low | medium | high | xhigh`;
- `worker_timeout_seconds: int`;
- `auditor_model: str`;
- `auditor_reasoning_effort: low | medium | high | xhigh`;
- `auditor_timeout_seconds: int`;
- `acceptance_tests: list[WorkflowTest]`;
- `allowed_paths: list[str]`;
- `protected_paths: list[str]`;
- `max_repair_rounds: int`;
- `checkpoint_after: bool`.

`WorkflowTest` contains exactly:

- `id: str`;
- `argv: tuple[str, ...]`;
- `cwd: str`;
- `timeout_seconds: int`;
- `max_stdout_bytes: int`;
- `max_stderr_bytes: int`.

Validation requirements:

- use the existing strict safe YAML loader;
- reject unknown and duplicate fields at every nesting level;
- trim and validate all strings;
- use conservative identifiers for substage and test IDs;
- require unique test IDs;
- use the existing conservative model-name validation;
- worker/auditor timeouts are 30 through 14,400 seconds;
- test timeouts are 1 through 14,400 seconds;
- `max_repair_rounds` is 0 through 10 inclusive;
- test argv is nonempty, contains no empty element, and is never a shell string;
- test output limits are positive and at most 100 MiB each;
- all referenced paths resolve relative to the specification file;
- workspace exists, is a Git worktree, and is clean at start, including
  untracked files;
- contract and prompt files are regular nonempty UTF-8 files of at most 2 MiB;
- each test cwd resolves inside the workspace;
- path patterns are nonempty normalized POSIX-style relative patterns;
- reject absolute patterns and `..` traversal;
- the same normalized pattern cannot appear in both allowed and protected sets;
- contract and prompt paths inside the workspace must match protected patterns;
- structural-redaction collision checks apply to every externally rendered
  structural string and prospective workflow run path;
- all path, decoding, and Git failures become sanitized input errors with exit
  code 2.

The specification must not expose:

- Codex executable paths;
- arbitrary Codex flags;
- approval, sandbox, network, ephemeral, or session-selection overrides;
- environment variables;
- arbitrary output schemas or artifact paths;
- test shell strings;
- retry algorithms or state-transition overrides.

## Human-written prompt policy

All prompts are authored by humans and frozen by hash.

The engine may create a model input only by concatenating:

1. the exact bytes of one referenced human-written prompt or continuation file;
2. fixed engine-owned section labels;
3. deterministic evidence generated from validated local artifacts.

No model may draft, rewrite, summarize, or choose another model's prompt. The
engine must not use a general template language and may not execute expressions,
conditionals, includes, or arbitrary placeholder substitution.

Rendered prompts are passed through stdin and are not stored verbatim. Record:

- source prompt path and SHA-256;
- contract SHA-256;
- deterministic evidence artifact hashes;
- final rendered-prompt SHA-256 and byte count.

The CLI must not print prompt or contract content.

## Deterministic prompt appendices

### Initial worker turn

Append to the exact worker-initial prompt:

- frozen contract content;
- normalized substage summary;
- allowed and protected paths;
- acceptance-test IDs and exact argv;
- baseline Git commit, branch or detached state, and clean status;
- the engine-owned worker output schema and reporting instruction.

### Fixed-test repair turn

Append to the exact worker-repair prompt:

- frozen contract content;
- repair round number;
- normalized failing test results;
- bounded redacted stdout/stderr artifact paths and hashes;
- current scope findings;
- the engine-owned worker output schema and reporting instruction.

### Audit repair turn

Append to the exact worker-repair prompt:

- frozen contract content;
- repair round number;
- the auditor's validated structured findings;
- current fixed-test and scope results;
- the engine-owned worker output schema and reporting instruction.

### Human continuation turn

Append to the exact human instruction:

- frozen contract content;
- current state and repair round;
- unresolved normalized test, audit, and scope evidence;
- the engine-owned worker output schema and reporting instruction.

### Auditor turn

Append to the exact auditor prompt:

- frozen contract content;
- normalized substage summary;
- baseline and current Git evidence;
- complete bounded patch evidence or a hash plus explicit truncation marker;
- changed-path and scope results;
- latest validated worker result;
- all current fixed-test results and redacted log hashes;
- prior audit findings for re-audits;
- the engine-owned auditor output schema and reporting instruction.

The auditor is instructed to inspect the current workspace directly. The
appendix is evidence, not a substitute for inspection.

## Fixed structured model results

Stage 2 may extend the Stage 1 adapter with engine-owned `--output-schema`
support. The substage specification cannot provide or override schemas.

### Worker result

Require a strict object containing exactly:

- `schema_version: 1`;
- `status: completed | blocked | needs_human`;
- `summary: str`;
- `changed_files: list[str]`;
- `assumptions: list[str]`;
- `questions: list[str]`.

Requirements:

- reject unknown fields;
- normalize relative changed paths and reject duplicates;
- bound and sanitize strings;
- treat reported changed files as informational only and never trust them over
  Git evidence;
- `blocked` or `needs_human` always pauses after evidence is saved.

### Auditor result

Require a strict object containing exactly:

- `schema_version: 1`;
- `verdict: pass | fail_repairable | escalate`;
- `summary: str`;
- `scope_compliant: bool`;
- `contract_satisfied: bool`;
- `findings: list[AuditFinding]`;
- `human_questions: list[str]`.

`AuditFinding` contains exactly:

- `id: str`;
- `severity: critical | high | medium | low`;
- `category: str`;
- `file: str | null`;
- `line: int | null`;
- `evidence: str`;
- `required_fix: str`.

Requirements:

- reject unknown fields;
- require unique finding IDs;
- normalize relative file paths when present;
- require positive line numbers;
- bound and sanitize strings;
- `pass` requires no findings, no human questions, `scope_compliant=true`, and
  `contract_satisfied=true`;
- `fail_repairable` requires at least one finding;
- `escalate` pauses regardless of findings.

Missing, malformed, or schema-invalid worker/auditor results are never repaired
automatically and produce `human_paused`.

## Codex session policy

### Worker

- The initial worker turn creates one persistent Stage 1 Codex run.
- Record one explicit `thread_id` from a structured `thread.started` event.
- Every repair or human-continuation turn uses
  `codex exec resume <EXACT_THREAD_ID>`.
- Never use `--last`, `--all`, recency selection, or a session name.
- Resume preserves global approval `never`, worker `workspace-write`, disabled
  web/workspace network, disabled skill dependency installation, ignored user
  config/rules, explicit workspace, model/reasoning settings, output schema,
  JSONL capture, and stdin prompt delivery.
- If the initial otherwise-successful turn exposes no unambiguous thread ID,
  pause.
- If a resumed turn reports a different thread ID, pause.
- Missing or unavailable stored threads produce `human_paused`; never start a
  replacement worker automatically.

### Auditor

- Every audit round starts a new `read-only`, approval-never, network-disabled,
  `--ephemeral` Stage 1 Codex run.
- Never resume or reuse an auditor.
- Record each audit action and evidence hashes.
- Auditor output is advisory until it passes deterministic schema and state
  validation.

## Baseline and Git evidence

At start:

- require a clean workspace;
- record exact `HEAD`, branch or detached state, repository root, and status;
- hash the frozen specification, contract, and prompt files;
- create no Git commit, branch, index change, or worktree.

After each worker turn:

- collect changed tracked and untracked paths relative to the recorded baseline;
- detect deletions, renames, type changes, and symlinks;
- generate deterministic no-color Git status and diff evidence;
- include untracked file hashes and bounded contents where safe;
- use `--no-ext-diff`, no textconv, no optional locks, argument vectors, no
  pager, and no shell;
- cap stored patch evidence at 25 MiB;
- if complete patch evidence exceeds the limit, store a hash and truncation
  record and pause rather than audit incomplete evidence;
- never modify the index.

Scope rules:

- every changed path must match at least one allowed pattern;
- no changed path may match a protected pattern;
- protected wins over allowed;
- symlink targets must not escape the workspace;
- scope failure becomes a deterministic worker-repair turn while rounds remain;
- repeated scope failure beyond the limit pauses;
- never silently revert files.

## Fixed acceptance-test runner

Run every test after each worker turn that reports `completed` and after Git
scope evidence is collected.

Requirements:

- execute exact argv with `shell=False`;
- use the validated cwd inside the workspace;
- copy the environment and remove the same sensitive names as Stage 1;
- never mutate `os.environ`;
- disable Git optional locks where relevant;
- do not add network credentials or network-enabling flags;
- start a new process group/session;
- stream and bound stdout/stderr;
- redact before durable storage;
- enforce timeout and whole-group TERM/grace/KILL cleanup;
- never retry within the same test attempt;
- record start/end UTC, monotonic duration, argv, cwd, exit/signal, timeout,
  output-limit status, byte counts, removed variable names, and hashes;
- run in specification order;
- stop after the first failed test and record later tests as skipped;
- pass only on exit 0 with no timeout or output-limit breach.

A failed test produces a deterministic worker-repair turn when rounds remain.
The auditor is not launched until all fixed tests pass.

## Workflow state machine

Persist one strict state snapshot plus an append-only journal.

Required states:

- `initialized`;
- `worker_running`;
- `scope_checking`;
- `tests_running`;
- `auditor_running`;
- `repair_pending`;
- `human_paused`;
- `repair_limit_paused`;
- `checkpoint_paused`;
- `completed`;
- `failed`;
- `aborted`.

Required normal transitions:

```text
initialized
  → worker_running
  → scope_checking
  → tests_running
  → auditor_running
  → completed | checkpoint_paused

scope failure
  → repair_pending
  → worker_running

test failure
  → repair_pending
  → worker_running

audit fail_repairable
  → repair_pending
  → worker_running

worker blocked/needs_human
  → human_paused

audit escalate or malformed structured result
  → human_paused

repair limit exhausted
  → repair_limit_paused
```

Additional rules:

- never infer transitions from model prose;
- every transition has a deterministic reason code;
- increment repair round only immediately before a resumed worker turn;
- initial worker turn is round 0;
- `max_repair_rounds=0` means no automatic worker resume;
- auditor `pass` with `checkpoint_after=false` gives `completed`;
- auditor `pass` with `checkpoint_after=true` gives `checkpoint_paused`;
- reserve `failed` for unrecoverable local corruption or invariant failure, not
  ordinary model/test/audit failure;
- terminal states never auto-resume;
- only `continue-substage` may leave human or repair-limit pause;
- checkpoint pause is terminal in Stage 2.

## Crash recovery and exactly-once actions

The workflow must survive process interruption and machine restart.

Requirements:

- create the run directory exclusively;
- use one advisory lock to prevent concurrent mutation;
- lock metadata records PID, host, and start timestamp;
- recover a stale same-host lock only after proving the recorded local PID is
  absent; foreign-host locks require human action;
- state/result snapshots use atomic replacement and directory fsync where
  supported;
- journal records sequence, previous/new state, action ID, timestamps, reason,
  and artifact hashes;
- protect journal integrity with a hash chain or equivalent validation;
- every worker, auditor, and fixed-test action has a deterministic action ID;
- write an intent record before launch and a completion record after finalized
  artifacts;
- on resume, never repeat completed actions;
- intent without completion becomes `human_paused` unless a complete Stage 1
  result or complete test artifact set deterministically proves completion;
- never guess whether a model turn or test completed;
- initial frozen hashes must match on every resume;
- workspace repository identity and baseline must remain consistent;
- unexpected spec, contract, or prompt changes pause;
- never roll back automatically.

## Repair limits and pause behavior

`max_repair_rounds` counts all automatic resumed worker turns caused by scope,
fixed-test, or audit failure.

Before automatic resume, require:

- validated evidence;
- the fixed human-authored repair prompt;
- the exact stored worker thread ID;
- a remaining repair round;
- unchanged frozen hashes;
- consistent workspace and run state.

Pause and create an escalation package when:

- worker says `blocked` or `needs_human`;
- worker or auditor result is missing or invalid;
- thread ID is missing, ambiguous, unavailable, or changes;
- audit says `escalate`;
- patch evidence is too large or incomplete;
- scope, test, or audit failure exceeds the repair limit;
- a permission/sandbox block occurs;
- Stage 1 reports timeout, output limit, malformed stream, missing final message,
  launch failure, or process failure;
- frozen hashes or repository identity change;
- recovery cannot prove exactly-once completion;
- any state invariant fails.

## Workflow artifacts

Create `<runs-dir>/<substage_id>-<run-token>` exclusively. The collision-resistant
run token must pass structural-redaction preflight.

Write at least:

```text
spec.normalized.json
spec.sha256
contract.sha256
prompts.sha256.json
baseline.json
state.json
journal.jsonl
result.json
actions/
worker/
tests/
audits/
git/
handoffs/
escalation/
```

Requirements:

- never persist unredacted captured output;
- never persist rendered model prompts;
- store evidence components and hashes sufficient to reproduce assembly;
- each worker/auditor action references its Stage 1 run directory;
- test logs are bounded and redacted;
- Git evidence is deterministic and hashed;
- escalation includes stable JSON and human-readable Markdown;
- state/result/metadata use atomic replacement;
- artifacts agree on current state, repair round, worker thread ID, latest
  worker result, tests, audit verdict, and pause reason;
- no artifact contains removed sensitive environment values;
- no artifact locator is redacted into a nonexistent path.

## Normalized workflow result

Implement a strict immutable result containing at least:

- `schema_version`;
- `substage_id`;
- `run_token`;
- `status`;
- `repair_round`;
- `max_repair_rounds`;
- `checkpoint_after`;
- `workspace`;
- `baseline_commit`;
- `worker_thread_id`;
- `latest_worker_action_id`;
- `latest_audit_action_id`;
- `tests_passed`;
- `scope_compliant`;
- `contract_satisfied`;
- `artifact_directory`;
- `pause_reason`;
- `summary`;
- `started_at`;
- `updated_at`.

Returned result, persisted `result.json`, human output, and JSON output must
agree. Accepted artifact paths must exist exactly.

## Exit-code contract

- `0`: completed;
- `2`: invalid specification, path, prompt, contract, Git baseline, or CLI input;
- `3`: missing or unsupported environment dependency;
- `4`: unrecoverable workflow failure;
- `5`: human pause;
- `6`: repair-limit pause;
- `7`: checkpoint pause;
- `8`: aborted;
- unexpected internal failure: `1`.

`run-substage`, `resume-substage`, and `continue-substage` return the code for
the resulting durable state. `substage-status` returns 0 when state is readable,
regardless of workflow status.

## Required tests

Tests must use fake Codex and fake test executables. They must not use a real
model, login, network, or user Codex configuration.

Cover at least:

### Specification and prompt assembly

- valid specification;
- unknown, duplicate, nested-invalid fields;
- all timeout, output-limit, and repair boundaries;
- invalid argv, cwd, patterns, paths, UTF-8, and oversized files;
- dirty workspace rejection, including untracked files;
- protected contract/prompt enforcement;
- exact human prompt bytes plus deterministic appendices;
- no template execution or arbitrary substitution;
- prompt/contract contents absent from CLI and stored artifacts;
- stable prompt hashes;
- structural-redaction collision rejection.

### Sessions and roles

- persistent initial worker captures one explicit thread ID;
- exact-ID resume, never `--last` or `--all`;
- resume preserves worker safety flags and output schema;
- missing, ambiguous, changed, or unavailable thread pauses;
- every auditor is fresh, read-only, approval-never, network-disabled, and
  ephemeral;
- auditor sessions are never reused;
- fake parser rejects invalid resume and role policies.

### Git evidence and scope

- clean baseline recording;
- tracked, untracked, deleted, renamed, type-changed, and symlink paths;
- allowed/protected precedence;
- outside-scope repair;
- oversized patch pause;
- deterministic no-color/no-pager/no-ext-diff behavior;
- no index mutation;
- repository identity and frozen-hash drift pauses.

### Fixed tests

- exact argv and cwd;
- no shell;
- environment filtering;
- pass, nonzero, timeout, output limit, launch failure, invalid bytes;
- child cleanup on every outcome;
- first-failure stop and skipped records;
- deterministic repair appendix;
- redacted bounded logs.

### State transitions

- direct pass and checkpoint pass;
- scope failure then repair pass;
- test failure then repair pass;
- audit failure then repair and fresh re-audit pass;
- worker blocked/needs-human;
- audit escalate;
- malformed worker/auditor result;
- permission blocked and all Stage 1 failure statuses;
- repair limit zero and exhausted;
- human continuation from allowed pauses;
- invalid continuation states;
- abort behavior;
- model prose cannot control state.

### Recovery and concurrency

- atomic snapshots;
- journal sequence and integrity;
- concurrent lock rejection;
- safe stale-lock recovery;
- crashes before and after each external action boundary;
- no duplicate worker, test, or auditor action on resume;
- uncertain in-flight action pauses;
- completed workflow cannot resume;
- changed spec/contract/prompt or repository identity pauses;
- returned/persisted/rendered result equality.

### Security and scope regression

- no prompt in argv or artifacts;
- no removed sensitive values anywhere;
- exact real artifact locators;
- Stage 0 and Stage 1 suites remain passing;
- no Git commits, branches, worktrees, pushes, notifications, API use, or
  model-generated prompts.

## Documentation

`docs/workflow_engine.md` must explain:

- deterministic trust boundary and human-written prompt policy;
- specification format;
- state machine and exit codes;
- persistent worker versus fresh auditor policy;
- exact-ID resume;
- test and Git evidence;
- repair limits;
- crash recovery and exactly-once guarantees;
- pause and continuation behavior;
- artifacts;
- Stage 2's direct-test network limitation;
- current non-goals and the later intelligent-supervisor upgrade path.

## Required quality gates

Before completion, all must pass:

```bash
ruff check .
mypy src
pytest -q
```

## Completion report

The worker's final response must state:

- files changed;
- architecture implemented;
- exact state machine;
- worker-session and auditor-freshness behavior;
- deterministic prompt assembly;
- fixed-test and Git-evidence behavior;
- crash recovery and exactly-once design;
- pause and repair-limit behavior;
- exact quality-gate results and test count;
- assumptions;
- every unmet requirement, deviation, or blocker.

A silent deviation is a failure.
