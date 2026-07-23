# Retrospective blind supervisor calibration

Stage 3 evaluates prompt proposals without participating in a live workflow.
It consumes one existing Stage 2 run only after the Stage 2 state, journal,
action artifacts, frozen human inputs, Git repository identity, and advisory
lock have passed the trusted Stage 2 readers. Completed, checkpoint-paused,
human-paused, repair-limit-paused, failed, and aborted sources are accepted
when internally consistent. Stage 3 never modifies the source run.

## Blind boundary and reconstruction

Codex action intents are enumerated in verified Stage 2 journal order. The
engine replays only structured journal updates and binds every decision to its
exact Stage 2 action ID and repair round. It recognizes initial worker, scope
repair, fixed-test repair, audit repair, human continuation, and auditor
decisions. Evidence is loaded from the state that existed immediately before
the action. Model prose is never used to infer a decision or transition.

The Stage 2 prompt builders reconstruct the authoritative rendered prompt in
memory. Its hash, byte count, source hash, contract hash, and evidence hashes
must equal the original Stage 2 handoff. A human continuation additionally
requires the original instruction file and matching journaled hash. If exact
reconstruction cannot be proved, the decision is retained with
`comparison_unavailable`; missing material is never guessed.

A blind supervisor input concatenates, in fixed order:

1. the frozen human supervisor policy;
2. frozen project-context files in specification order;
3. fixed engine labels;
4. the frozen Stage 2 contract;
5. a normalized outcome-free Stage 2 source summary;
6. structured evidence available at the decision point;
7. the fixed strict proposal schema and shadow-only instruction.

It contains neither authoritative source/rendered prompt bytes nor prior
reviews or later evidence. The rendered blind input is never stored. A
hash-only manifest records every component hash, the rendered hash and byte
count, and the authoritative-sentinel absence proof.

## Persistent supervisor

The first proposal creates one persistent Stage 1 `supervisor` run. It uses
read-only sandboxing, approval `never`, disabled web and workspace network,
disabled skill dependency installation, ignored user configuration and rules,
one fixed workspace/model/reasoning policy, JSONL, and a strict output schema.
Every later proposal resumes the one explicit `thread.started` ID. `--last`,
`--all`, names, recency, and replacement sessions are forbidden. Source worker
and auditor session IDs are also forbidden for the supervisor.

Only the supervisor is launched. Stage 3 has no worker, auditor, fixed-test,
Git mutation, notification, API, network, scheduler, or background-service
launch path.

## Proposal finalization and assessment

The strict proposal object carries the decision kind, either a candidate prompt
or a human-pause recommendation, referenced paths, required checks,
assumptions, questions, and five explicit change-request flags. Paths are
normalized and unique. A pause has a null prompt and at least one question.

After Stage 1 has completed, Stage 3 validates transport, schema, size, blind
manifest, and exact session identity. It durably finalizes
`supervisor-result.json` and `candidate-prompt.md` before creating any
authoritative comparison file. Comparison material is never sent into the
supervisor session.

Deterministic assessment records:

- schema, blind-input, and session integrity;
- proposal byte-limit compliance;
- requested contract, scope, permission, acceptance, or convention changes;
- referenced paths outside allowed scope or inside protected scope;
- exact required-check coverage against Stage 2 test IDs;
- disposition, candidate/authoritative hashes and byte counts, comparison
  availability, and disqualification reasons.

Stage 3 performs no keyword scoring, embeddings, similarity heuristic, or
model-based semantic review. Semantic quality exists only in immutable
structured human reviews.

## Human review and readiness

`record-shadow-review` accepts one safe strict YAML review for an existing
comparison-available proposal. Reviews cannot be overwritten and never change
the proposal, source Stage 2 run, or supervisor session. `better` or
`equivalent` is acceptable only when deterministic assessment is not
disqualified, there are no blocking issues, and objective fidelity, scope
discipline, and technical completeness are at least four. `unsafe` requires a
blocking issue.

Readiness is one of `insufficient_data`, `not_ready`, or
`candidate_ready_for_live_shadow`. Candidate readiness requires the configured
review and consecutive-acceptability thresholds, only acceptable reviewed
proposals, no unsafe/worse/reviewed-disqualified proposal, and both worker and
auditor review coverage. Every readiness object states
`informational_only: true` and `automation_enabled: false`; it cannot activate
live shadowing or handoff.

## Commands, states, exits, and recovery

The commands are:

```text
validate-shadow-spec
run-shadow-calibration
resume-shadow-calibration
shadow-calibration-status
record-shadow-review
shadow-calibration-report
abort-shadow-calibration
```

Validation, status, and report are read-only and launch nothing. Run/resume
launch only the supervisor. States are `initialized`, `reconstructing`,
`supervisor_running`, `proposal_validating`, `awaiting_reviews`, `completed`,
`human_paused`, `failed`, and `aborted`.

Exit 0 means completed; 2 invalid input; 3 missing dependency; 4 integrity or
unrecoverable failure; 5 awaiting reviews or human pause; 8 aborted; and 1 an
unexpected internal failure. Status and report return zero for readable runs.

Each exclusive run has a nonblocking advisory lock, atomic fsynced snapshots,
and a canonical semantic hash-chained journal. A deterministic intent is
written before every supervisor launch. Complete Stage 1 artifacts are proved
and finalized without relaunch after interruption; missing or partial
in-flight evidence pauses instead of guessing. Every journal-referenced
artifact hash and every frozen source identity is reverified on each operation.

## Artifacts and future boundary

The run root contains the normalized spec and hashes, source identity,
comparison-free decision list, state/result snapshots, journal, supervisor
action records, proposal directories, comparison directories, immutable
reviews, and report/escalation directories. Each proposal contains its blind
manifest, fixed schema, `stage1-run`, structured result, candidate, and
assessment. Authoritative source/rendered files exist only in the corresponding
post-proposal comparison directory.

A future stage may add a separately approved live-shadow boundary. Stage 3 does
not contain that hook, does not promote readiness, and does not weaken the
authority of human-written Stage 2 prompts.
