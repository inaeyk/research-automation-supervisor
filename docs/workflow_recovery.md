# Workflow discovery and recovery

PA-5A adds a model-free operator layer around the existing schema-version-1 and
schema-version-2 workflow engines. It does not change their states, journals, action
records, proofs, routing, repair limits, or completion semantics.

Use one command to locate and recover the unique latest incomplete run:

```bash
research-supervisor resume --runs-dir runs/workflows --latest
```

Inspect the same deterministic plan without writing a cache or receipt and without
launching any process:

```bash
research-supervisor resume --runs-dir runs/workflows --latest --dry-run --json
```

`status --runs-dir DIR` rebuilds the version-1 run index from direct child workflow
journals. `status RUN` builds one recovery plan, and `latest-incomplete --runs-dir DIR`
prints the uniquely newest incomplete run. The index file is only a replaceable cache:
every discovery scans authoritative state and journals again, so a missing, stale, or
malformed cache cannot select a run.

## Safe automatic boundaries

The recovery plan can automatically continue a journal-proven phase before an external
launch, capture a complete Stage-1 action without relaunch, resume the exact stored
Worker thread when the existing engine permits it, reuse recursively verified PA-2 or
PA-3 final proof, apply one already-recorded physics human decision, or finish a derived
state/result snapshot after its authoritative journal transition. A pending human,
evidence, or repair-limit pause is reopened without invoking a model.

Recovery blocks with one stable reason code and one next step when it observes an
ambiguous post-launch intent, an active matching process, a stale or reused child PID,
a live PID behind a legacy lock that lacks start identity, a foreign-host lock, changed
workspace or frozen authority, invalid or missing proof, contradictory records, or an
unprovable Worker/provider session. Recovery never resets a workspace or repairs old
evidence.

For each non-dry recovery attempt, the supervisor writes a create-once plan receipt
before acting and a separate create-once outcome receipt afterward. They live below
`.workflow-recovery-v1` beside the run directories, not inside an earlier evidence tree.
They bind the exact state and journal bytes, journal head, policies, process
reconciliation, proof disposition, reason code, and next step. A power loss can leave a
plan receipt without an outcome; the next invocation reconciles the authoritative run
again and cannot duplicate a completed deterministic action ID.

The concise JSON surface contains only bounded recovery models and applies the standard
credential/session redaction policy. It never includes raw model streams, prompts, or
oracle output.
