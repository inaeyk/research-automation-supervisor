# Stage 4 Contract — Live Quarantined Shadow Observation

Status: human-approved implementation contract  
Contract schema version: 1  
Stage ID: `AUTOMATION-4`

## Goal

Implement a live shadow-observation layer around one authoritative Stage 2 workflow.

Stage 4 watches the durable Stage 2 journal while the authoritative worker, tests,
and auditor execute normally. At each authoritative decision point, Stage 4
freezes the evidence that existed at that point and asks one persistent read-only
supervisor session what prompt it would have proposed.

The supervisor proposal is quarantined:

- it is never sent to a worker or auditor;
- it never changes Stage 2 state;
- it never changes the authoritative prompt;
- it never blocks, delays, retries, aborts, or overrides authoritative work;
- it is compared with the authoritative human prompt only after both sides have
  finalized.

Stage 4 answers:

> Can the calibrated supervisor produce human-comparable prompts during real
> execution, using only evidence available at the live decision point, without
> influencing that execution?

## Preconditions

- Tags `stage0-complete`, `stage1-complete`, `stage2-complete`, and
  `stage3-complete` exist.
- Current HEAD includes all committed Stage 1–3 production hotfixes.
- The referenced Stage 2 specification is valid and its workspace is initially
  clean.
- Codex supports non-interactive execution, JSONL, fixed output schemas,
  read-only sandboxing, and exact UUID resume.
- Stage 3 proposal, assessment, review, confidentiality, Structured Outputs,
  UUID, and integrity helpers are available.

## Non-goals

Do not implement:

- supervisor-to-worker or supervisor-to-auditor prompt delivery;
- automatic prompt replacement, repair, retry, or fallback;
- supervisor control over Stage 2 transitions;
- model-generated contracts, policies, project context, tests, conventions, or
  permissions;
- semantic scoring by another model, embeddings, keywords, or similarity;
- automatic promotion to handoff;
- automatic Git commit, branch, merge, tag, push, revert, or worktree;
- network-enabled model execution;
- OpenAI API keys or direct API calls;
- notifications, schedules, browser control, daemons, or background services;
- parallel authoritative Stage 2 runs;
- multiple supervisor sessions in one live-shadow run;
- project-specific Gregory–Laflamme behavior.

A later stage may add human-approved supervised handoff. Stage 4 is observation
only.

## Required files

Create at least:

```text
src/research_automation_supervisor/
    live_shadow_models.py
    live_shadow_sources.py
    live_shadow_prompts.py
    live_shadow_engine.py
    live_shadow_review.py
tests/
    test_live_shadow_models.py
    test_live_shadow_sources.py
    test_live_shadow_prompts.py
    test_live_shadow_engine.py
    test_live_shadow_review.py
    test_live_shadow_cli.py
examples/live_shadow/minimal-live-shadow.yaml
examples/live_shadow/supervisor-policy.md
examples/live_shadow/project-context.md
examples/live_shadow/review.yaml
docs/live_shadow.md
```

Update CLI, fake Codex, shared errors/integrity helpers, and architecture
documentation only where necessary.

Do not edit any frozen Stage 0/1/2/3/4 contract or implementation-prompt file.

## Authoritative isolation

The Stage 2 workflow is the sole authority.

Stage 4 must launch the existing Stage 2 engine with the exact validated Stage 2
specification. It must not:

- rewrite or wrap authoritative prompt bytes;
- add text to worker or auditor prompts;
- change Stage 2 model, reasoning, timeout, sandbox, approval, network, schema,
  tests, scope, repair count, or checkpoint settings;
- modify Stage 2 run artifacts;
- convert a Stage 2 pass into failure or a failure into pass;
- delay an authoritative launch waiting for the shadow supervisor;
- interpret supervisor prose as workflow control.

The Stage 2 result and run directory remain independently valid and readable by
the existing Stage 2 commands.

## Observation mechanism

Stage 4 starts two isolated activities:

1. one authoritative Stage 2 workflow;
2. one live shadow observer.

The observer watches the authoritative Stage 2 run's durable journal and reacts
only to verified Stage 2 `action_intent` entries for worker and auditor actions.

The observer must not inspect partially written artifacts. It consumes only
journal-referenced, hash-verified evidence.

Prefer observing durable Stage 2 journal/action artifacts rather than inserting
a blocking callback into the Stage 2 critical path.

If a small optional observer interface is added to Stage 2, it must:

- default to a no-op;
- run only after the relevant Stage 2 intent is durable;
- never propagate observer exceptions into Stage 2;
- never await a supervisor result;
- preserve byte-identical authoritative prompts and commands;
- preserve every existing Stage 2 test.

Stage 4 correctness must not depend on an observer being able to alter Stage 2.

## Temporal blindness

For each Stage 2 action intent, create an immutable live decision envelope from
the verified journal prefix ending at that intent.

The envelope may contain only evidence available at or before that sequence.

It must exclude:

- authoritative human prompt bytes;
- authoritative rendered-prompt bytes;
- authoritative prompt paths and hashes from model input;
- output created by the current action;
- Git changes created after the current intent;
- tests, audits, transitions, comparisons, or reviews created later;
- any live-workspace content that was not frozen before the intent.

### Quarantine workspace

The supervisor must not run with the live Stage 2 workspace as its current
directory and must not receive filesystem access to that workspace.

Every supervisor turn runs in a dedicated Stage 4 quarantine workspace that
contains no checkout, symlink, bind mount, or copy of the authoritative
repository.

The quarantine workspace may contain only engine-owned non-authoritative
scaffolding needed for Codex execution. Blind evidence is delivered through
stdin.

The supervisor remains read-only even inside the quarantine workspace.

## Live decision kinds

Support:

- `worker_initial`;
- `worker_scope_repair`;
- `worker_test_repair`;
- `worker_audit_repair`;
- `worker_human_continuation`;
- `auditor`.

Decision ID:

```text
<kind>-r<repair_round:03d>-a<ordinal:03d>
```

Bind each decision to the exact Stage 2 action ID, journal intent sequence,
repair round, ordinal, evidence hash, authoritative run identity, and proposal
kind.

Decision capture is deterministic and exactly once.

## Live decision envelopes

Each strict envelope contains at least:

- schema version;
- live-shadow run ID;
- authoritative Stage 2 run and substage ID;
- decision ID, kind, ordinal, round, and source action ID;
- Stage 2 journal intent sequence and hash;
- baseline identity;
- allowed and protected paths;
- acceptance-test IDs and exact argv;
- triggering evidence available before the action;
- verified evidence locators, hashes, and bounded safe content;
- prior authoritative action summaries available before the current intent;
- comparison availability;
- envelope hash and timestamp.

Requirements:

- verify the Stage 2 journal prefix;
- verify every referenced action/handoff/Git/test/audit artifact;
- freeze bounded evidence content needed for prompt planning;
- do not read later journal entries while constructing the envelope;
- do not infer from prose when typed evidence exists;
- do not include current workspace reads;
- store the envelope before launching the supervisor;
- never rewrite an envelope.

## Blind prompt construction

Reuse Stage 3 supervisor policy, project context, proposal schema, metadata
semantics, confidentiality preflight, and deterministic assessment rules.

Blind input consists of:

1. frozen human supervisor policy;
2. frozen human project-context files in order;
3. fixed Stage 4 live-shadow instructions;
4. frozen Stage 2 contract;
5. normalized authoritative source summary;
6. immutable live decision envelope;
7. strict supervisor output schema.

The prompt must state:

- this is live shadow observation;
- the proposal is quarantined and will not be sent automatically;
- authoritative execution proceeds independently;
- contract, scope, permissions, tests, conventions, and checkpoints are frozen;
- use only the envelope;
- do not inspect the live repository;
- `referenced_paths` lists only authorized modification targets using normalized
  workspace-relative POSIX paths;
- `required_checks` lists exact Stage 2 acceptance-test IDs;
- recommend human pause when evidence is insufficient;
- return only the strict structured object.

Rendered blind input is not persisted. Persist only its manifest, hash, and byte
count.

## Persistent supervisor session

Use one persistent supervisor session per live-shadow run.

Initial turn:

- role `supervisor`;
- read-only sandbox;
- approval `never`;
- network/web disabled;
- skill dependency installation disabled;
- ignored user config/rules;
- strict config;
- fixed model/reasoning/schema;
- quarantine workspace;
- not ephemeral.

Record exactly one canonical lowercase, hyphenated, non-nil UUID from one
explicit `thread.started` event.

Every later turn uses exactly:

```text
codex exec resume <EXACT_UUID>
```

Require the same UUID. Never use names, `--last`, `--all`, or recency. Reject
reuse of authoritative Stage 2 worker or auditor UUIDs.

Supervisor/session failure is a Stage 4 shadow failure only. It must not alter or
interrupt Stage 2.

## Concurrency and non-blocking behavior

The authoritative Stage 2 workflow proceeds immediately after its own durable
intent. Stage 4 must never wait for the supervisor before allowing Stage 2 to
continue.

Implement supervisor actions in isolated child process groups/sessions.

Under shadow delay or failure:

- authoritative worker/auditor/test behavior and result remain unchanged except
  for negligible polling overhead;
- no Stage 2 timeout is extended;
- no Stage 2 action is retried because of shadow state;
- Stage 2 may reach terminal state while shadow proposals remain pending;
- Stage 4 continues collecting pending shadow results afterward;
- a Stage 4 crash leaves an independently launched Stage 2 child running;
- a supervisor crash leaves Stage 2 running;
- Stage 4 finalizes every envelope already observed even if Stage 2 pauses.

Only one supervisor action may be in flight because the session is persistent
and ordered. Later decisions queue without blocking Stage 2.

## Comparison timing

Create a comparison package only after:

1. the supervisor proposal finalizes;
2. the corresponding authoritative Stage 2 action finalizes or reaches a durable
   terminal result;
3. authoritative prompt reconstruction is proven against the Stage 2 handoff.

Only then load/store the authoritative human source prompt, authoritative
rendered prompt, candidate, and deterministic comparison metadata.

Comparison material and reviews are never fed into later supervisor turns.

If authoritative reconstruction is unavailable, record
`comparison_unavailable`; never guess.

## Shadow failure isolation

Shadow failures include transport failure, invalid UUID/session, malformed
result, deterministic disqualification, confidentiality collision, temporal
envelope failure, and comparison reconstruction failure.

Rules:

- deterministic disqualification is proposal-quality evidence and does not
  pause Stage 2 or the collector;
- malformed/transport/session failure is recorded and observation continues;
- trustworthy incomplete external actions may resume by exact UUID;
- contradictory completed shadow action may produce a Stage 4 human pause only
  after authoritative Stage 2 remains independent;
- Stage 4 trusted-state corruption uses integrity exit 4;
- no shadow failure changes the Stage 2 result.

## Stage 4 specification

Strict immutable YAML fields:

- `schema_version: 1`;
- `live_shadow_id`;
- `title`;
- `stage2_specification_path`;
- `supervisor_policy_path`;
- `project_context_paths`;
- `supervisor_model`;
- `supervisor_reasoning_effort: low|medium|high|xhigh`;
- `supervisor_timeout_seconds`;
- `max_proposal_bytes`;
- `observer_poll_interval_milliseconds`;
- `shadow_completion_timeout_seconds`;
- `minimum_reviewed_proposals`;
- `required_consecutive_acceptable`.

Validation:

- strict safe YAML; duplicate/unknown fields rejected;
- conservative IDs/models;
- supervisor timeout 30–14,400 seconds;
- proposal size 1 KiB–2 MiB;
- poll interval 50–5,000 milliseconds;
- completion timeout 30–86,400 seconds;
- review thresholds 1–100 and consecutive <= minimum;
- lexical parent-component symlink rejection;
- exact raw-locator confidentiality preflight;
- policy/context nonempty UTF-8 regular files <= 2 MiB;
- context paths unique and ordered;
- Stage 2 specification validates with existing Stage 2 validation;
- authoritative workspace is clean;
- all structural-redaction preflights pass;
- validation writes and launches nothing.

The specification cannot override Stage 2 settings, Codex policy flags,
executable paths, schemas, environment variables, sessions, quarantine paths,
promotion, or handoff behavior.

## CLI

Implement:

```text
validate-live-shadow-spec PATH [--json]
run-live-shadow PATH [--runs-dir PATH] [--stage2-runs-dir PATH] [--json]
resume-live-shadow RUN_DIR [--json]
live-shadow-status RUN_DIR [--json]
record-live-shadow-review RUN_DIR PROPOSAL_ID REVIEW_PATH [--json]
live-shadow-report RUN_DIR [--json]
abort-live-shadow RUN_DIR --reason TEXT [--json]
```

`run-live-shadow` creates the Stage 4 run, launches one independent Stage 2
child, records/discovers its run, observes intents, launches quarantined
supervisor turns, waits for authoritative terminal state, then waits up to the
shadow completion timeout for queued shadow turns.

It never kills Stage 2 because of a shadow problem.

Status/report/review launch nothing and do not mutate immutable
proposals/assessments/envelopes.

Abort stops only the Stage 4 observer when safe; it never terminates or modifies
an active Stage 2 run.

## State machine

Required states:

- `initialized`;
- `authoritative_starting`;
- `authoritative_running`;
- `authoritative_terminal_shadow_pending`;
- `awaiting_reviews`;
- `completed`;
- `shadow_degraded`;
- `human_paused`;
- `failed`;
- `aborted`.

Normal flow:

```text
initialized
  → authoritative_starting
  → authoritative_running
  → authoritative_terminal_shadow_pending
  → awaiting_reviews
  → completed
```

`shadow_degraded` means authoritative Stage 2 reached terminal state but one or
more shadow actions did not produce a comparable proposal.

The result separately reports the authoritative Stage 2 status. A degraded
shadow result never rewrites it.

`completed` means all comparison-available proposals have immutable reviews.

## Artifacts

Create:

```text
live-shadow-spec.normalized.json
live-shadow-spec.sha256
policy.sha256
context.sha256.json
state.json
result.json
journal.jsonl
quarantine/
authoritative/
    launch.json
    result.json
    stage2-run.json
decisions/
proposals/
comparisons/
reviews/
reports/
escalation/
```

Per decision:

```text
decisions/<decision-id>/
    envelope.json
    envelope.sha256
    blind-input-manifest.json
    output-schema.json

proposals/<decision-id>/
    stage1-run/
    supervisor-result.json
    candidate-prompt.md
    assessment.json

comparisons/<decision-id>/
    authoritative-source.md
    authoritative-rendered.md
    comparison.json
```

Quarantine contains no authoritative repository content. Stage 2 artifacts are
never modified. Rendered blind input is never stored. Every artifact is
redacted, hashed, and journal-referenced.

## Result model

Include at least:

- schema version;
- live-shadow ID and run token;
- status;
- authoritative Stage 2 specification/run/status/pause reason/result hash;
- supervisor model/reasoning/session UUID;
- observed decision count;
- proposal/comparison/review/disqualification/shadow-failure counts;
- readiness;
- `automation_enabled`, always false;
- artifact directory;
- Stage 4 pause reason;
- summary and timestamps.

## Reviews and readiness

Reuse Stage 3 immutable reviews and acceptance rules.

Readiness statuses:

- `insufficient_data`;
- `not_ready`;
- `candidate_ready_for_supervised_handoff`.

Ready requires configured review count, no unsafe/worse review, no reviewed
disqualification, all reviews acceptable, required consecutive acceptable live
proposals, worker and auditor coverage, authoritative Stage 2 `completed`, and
no unresolved shadow integrity or temporal-blindness failure.

Readiness is informational only. `automation_enabled` is always false.

## Durability and recovery

Use Stage 2/3 patterns:

- exclusive Stage 4 run directory;
- hardened no-follow lock;
- atomic state/result writes and fsync;
- strict hash-chained journal;
- deterministic IDs;
- exact artifact verification;
- exactly-once envelope/action completion;
- frozen-input drift detection;
- authoritative Stage 2 identity verification;
- state/result equality;
- read-only status/report;
- no guessed session replacement.

Record authoritative child PID, start time, process-group/session identity,
Stage 2 run directory once discovered, and terminal result.

On resume:

- if Stage 2 is still running, reattach observation without relaunch;
- if terminated, read its existing result;
- never launch a duplicate Stage 2 run;
- intent without provable launch becomes human pause;
- never kill Stage 2 during Stage 4 recovery.

## Exit codes

- `0`: completed after reviews;
- `2`: invalid Stage 4 specification or review input;
- `3`: missing dependency before launch;
- `4`: Stage 4 or trusted Stage 2 integrity failure;
- `5`: awaiting reviews, shadow degraded, or human pause;
- `8`: observer aborted;
- `1`: unexpected internal error.

Status/report return 0 when readable. The authoritative Stage 2 exit code is
recorded separately.

## Required tests

Use fake Codex and deterministic Stage 2 fixtures only.

Cover:

### Authoritative independence

- Stage 2 result is identical with and without Stage 4;
- scope/test/audit repairs remain unchanged;
- shadow delay does not delay authoritative launch;
- shadow failure does not change Stage 2 state/result;
- Stage 4 crash/abort leaves Stage 2 running;
- no supervisor output reaches worker/auditor;
- byte-identical authoritative prompts/commands.

### Temporal blindness

- envelope uses journal prefix ending at intent;
- current/later action results are absent;
- later Git/test/audit/state/comparison/review evidence is absent;
- quarantine contains no repository files or symlinks;
- direct workspace reads are impossible;
- future sentinels never reach blind stdin or Stage 1 artifacts;
- comparisons/reviews never enter resumed prompts.

### Decision capture

- all six kinds;
- exact IDs/order/round/action binding;
- multiple repairs and auditors;
- exactly once under polling races;
- stable envelope hashes;
- changed journal prefix rejected;
- bounded evidence.

### Persistent supervisor

- canonical UUID initial/resume;
- exact same session;
- read-only quarantine;
- no source worker/auditor UUID reuse;
- ordered one-at-a-time queue;
- failures recorded without Stage 2 impact.

### Comparison/review/readiness

- comparison only after both completions;
- authoritative reconstruction verification;
- unavailable comparison;
- Stage 3 proposal semantics;
- immutable reviews;
- report-only review overlay;
- readiness and automation false.

### Recovery/integrity

- crashes around Stage 2 launch/discovery;
- resume without Stage 2 relaunch;
- crashes around envelope and supervisor boundaries;
- exact-once shadow recovery;
- state/result/journal/artifact mutation;
- lock attacks;
- frozen/source drift.

### CLI/scope

- all seven commands and exit codes;
- validation/status/report read-only;
- no Git automation, network, API, notifications, scheduler, daemon, or handoff;
- Stage 0–3 suites remain passing.

## Documentation

`docs/live_shadow.md` must explain authoritative independence, journal
observation, quarantine, temporal blindness, persistent queue, comparison
timing, degraded shadow versus authoritative result, reviews/readiness,
recovery, and why automation remains disabled.

## Quality gates

Because Stage 4 wraps shared Stage 2/3 execution, run once:

```bash
ruff check .
mypy src
pytest -q
```

## Completion report

Report files changed, authoritative isolation, observation mechanism,
quarantine/temporal-blindness proof, concurrency, session/queue, comparison
timing, failure isolation, state/recovery, CLI/artifacts, exact tests,
assumptions, deviations, and blockers.

A silent deviation is a failure.
