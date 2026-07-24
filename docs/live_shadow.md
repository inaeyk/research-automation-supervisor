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

## Bubblewrap quarantine and the persistent queue

Bubblewrap with the required options and functioning unprivileged user
namespaces is a production dependency for `run-live-shadow` (the production
probe records version 0.11.1). Every supervisor turn, including
an exact-UUID resume, is launched in a newly created mount, user, PID, IPC, UTS,
and (when supported) cgroup namespace. It has a new session, fresh `/proc` and
`/dev`, a tmpfs `/tmp`, and dies with its parent. There is no unisolated
fallback.

The namespace has a synthetic root assembled from an explicit allowlist. It
does not bind `/`, `/home`, `/mnt`, the authoritative repository, the
authoritative Stage 2 run, or the Stage 4 run as a whole. Its mounted resources
are:

- read-only system runtime directories `/usr`, `/bin`, `/sbin`, `/lib`, and
  `/lib64` when present;
- individual resolver, NSS, host, user/group, and TLS files or directories
  under `/etc`;
- the canonical Codex executable at `/opt/ras/codex`;
- the empty, engine-owned quarantine workspace read-only at `/workspace`;
- only the current strict schema read-only at
  `/control/output-schema.json`;
- only the current adapter temporary output directory writable at `/action`;
- the run's dedicated Codex runtime home writable at `/home/supervisor`; and
- the host Codex `auth.json` file over-mounted read-only at
  `/home/supervisor/auth.json`.

Resolver symlinks are resolved to their exact canonical file before that file
is bound at `/etc/resolv.conf`; all of `/run`, `/mnt`, or another containing
host filesystem is never exposed. Mount sources and destinations are
canonicalized and checked before each launch. Traversal, symlinks into a
forbidden root, duplicate or unexpected overlapping destinations, and a
quarantine tree overlapping the repository or Stage 2 runs are rejected.

The network namespace is deliberately shared. This preserves the Codex CLI's
transport connection to OpenAI. The semantic Codex policy still disables model
web search, workspace-sandbox network access, and dependency installation;
approval remains `never`, the Codex sandbox remains `read-only`, and user
configuration and rules remain ignored.

Each Stage 4 run owns one persistent `quarantine/codex-home/` and an empty
`quarantine/workspace/`. The runtime home is outside the authoritative
workspace and Stage 2 run, contains no checkout or copied project material, and
is reused only for that run's supervisor turns. It is not blind evidence,
comparison/review input, or an authoritative artifact. `HOME`, `CODEX_HOME`,
and `TMPDIR` are fixed to `/home/supervisor`, `/home/supervisor`, and `/tmp`.
Only the subscription authentication file is exposed read-only; its contents
are not copied into the quarantine tree, prompts, commands, journals, logs,
reports, or errors.

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
supervisor action is finalized from exact durable evidence.

`run-live-shadow` performs an active dependency and capability preflight before
the authoritative Stage 2 launch. It verifies the canonical Bubblewrap
executable is a regular executable under a trusted system executable directory,
checks required options and version, and runs a no-model synthetic-root probe.
The probe proves the repository and Stage 2 location are absent, the action
directory is writable, and `/opt/ras/codex` is visible. A missing or incapable
backend returns dependency exit code 3 and launches neither Stage 2 nor an
ordinary Codex process. `validate-live-shadow-spec` remains read-only and runs
no subprocess.

On resume, Stage 2 is never relaunched or switched to an unisolated supervisor.
If the recorded run still has potential shadow work but Bubblewrap has become
unavailable, recovery records an isolated shadow dependency failure, continues
observing a running authoritative Stage 2 process, and boundedly finalizes
queued decisions at the ordinary shadow completion deadline. The result
converges to `shadow_degraded`; Stage 2 remains unchanged.

`abort-live-shadow` stops only observation and never signals or modifies Stage
2. Status and report are read-only.

## Denial coverage and boundary

The real Bubblewrap/fake-Codex integration test attempts the exact authoritative
workspace and Stage 2 run paths, `/home` and `/mnt` enumeration, a root search
for a future sentinel, `/proc/self/root` and `/proc/1/root` escapes, and reads
from every inherited descriptor above standard input/output/error. It requires
all authoritative reads and proc-root escapes to fail, only the isolated home
to exist under `/home`, no unexpected descriptor, a read-only `/workspace`, a
writable final-message path, exact UUID resume, and a valid structured result.

The test inserts its unique ignored sentinel into the live authoritative
workspace only after the earlier decision intent is durably frozen, then scans
the complete Stage 4 artifact and runtime tree. Neither the sentinel nor test
authentication contents may occur in decision, manifest, schema, Stage 1,
proposal, assessment, comparison, review, report, journal, state, or result
material. On WSL2 kernel 6.18.33.2 with Bubblewrap 0.11.1, the capability and
denial test runs; it is skipped only when Bubblewrap genuinely does not exist
or user namespaces are unavailable.

This boundary contains the unprivileged supervisor process and its descendants.
It is not a claim of containment against a kernel compromise or a privileged
host attacker.

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
snapshots, the Stage 4 journal, `isolation.json` with only the canonical
Bubblewrap path, version, capability result, and isolation schema version, the
two-part `quarantine/`, authoritative identity records, immutable decision
envelopes, proposal artifacts, post-finalization comparisons, reviews, reports,
and escalation directories. The authoritative Stage 2 run remains a separate,
independently readable Stage 2 artifact tree. The default live-shadow run root
is the platform temporary directory so the quarantine is outside a repository;
an explicit `--runs-dir` must preserve the same separation.

Exit 0 means reviewed completion; 2 invalid specification or review input; 3 a
dependency failed before launch; 4 trusted integrity failure; 5 awaiting
reviews, degraded shadowing, or human pause; 8 observer abort; and 1 unexpected
internal failure. Readable status and report commands return zero regardless of
the run status. The authoritative Stage 2 exit code is recorded separately.
