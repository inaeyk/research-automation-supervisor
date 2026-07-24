# Stage 0 architecture

Stage 0 is a deterministic, read-only foundation. It has three layers:

- `contract.py` defines immutable Pydantic models with tuple-backed collections,
  canonicalizes path patterns, rejects duplicate YAML mapping keys and schema
  violations, and safely loads YAML.
- `doctor.py` probes Python, Git, repository state, Codex version, and Codex login
  state. Command execution, executable lookup, working directory, and Python
  version are injectable so tests never require a real Codex login.
- `cli.py` translates those services into human-readable or stable JSON output and
  applies the public exit-code contract.

All subprocesses use argument vectors, explicit timeouts, and deterministic UTF-8
decoding with replacement for malformed bytes. Git status disables optional locks
to prevent index refresh side effects. Diagnostic output never includes the raw
output of `codex login status`; only a normalized status is reported. Stage 0
performs no workflow execution and owns no mutable global state.

Diagnostic repository membership is tri-valued: `true` and `false` are confirmed
results, while `null` means the probe failed or could not establish an answer.
Operational probe errors make the environment unready and are included in both
human-readable and JSON output.

Path patterns are trimmed, backslashes are converted to forward slashes, and
POSIX lexical normalization is applied before allowed/protected overlap checks.
Acceptance-test timeouts are bounded to 1 through 86,400 seconds.

Stage 1 adds the exact-prompt Codex transport described in `codex_adapter.md`.
Stage 2 composes that adapter with strict immutable workflow models,
append-only prompt assembly, deterministic Git evidence, a bounded fixed-test
runner, strict action/journal proof models in `workflow_integrity.py`, and the
durable state engine described in `workflow_engine.md`. The composition remains
deterministic: models return schema-validated advice and results, while
engine-owned state rules alone select transitions.

Stage 3 is a separate retrospective reader and calibration engine described in
`shadow_calibration.md`. `shadow_sources.py` reuses trusted Stage 2 integrity
readers and prompt builders to reconstruct point-in-time decisions.
`shadow_prompts.py` assembles non-persisted blind inputs. `shadow_engine.py`
owns canonical-UUID-only persistent read-only supervision, lexical caller and
resolved dependency path preflight, complete recursive serialized-structure
confidentiality invariants, inode-bound locking, post-proposal comparison
ordering, exact state/result agreement, durable state, and exact-once recovery.
`shadow_review.py` is the only semantic
quality boundary: immutable human reviews feed an informational readiness
calculation that cannot enable automation.

Stage 4 is the live, still observation-only layer described in
`live_shadow.md`. `live_shadow_sources.py` validates the immutable live
specification and creates hash-bound envelopes from verified Stage 2 journal
prefixes. `live_shadow_prompts.py` assembles the non-persisted live blind input.
`live_shadow_engine.py` launches one unchanged Stage 2 child independently,
tails only durable action intents, serializes one persistent supervisor queue
in an empty read-only quarantine, and waits until both proposal and
authoritative action finalize before reusing Stage 3 comparison and assessment
semantics. `live_shadow_review.py` reuses immutable Stage 3 reviews to compute
informational readiness. Supervisor failures can degrade Stage 4 but cannot
signal, modify, delay, retry, or reinterpret the authoritative Stage 2 run;
automation remains disabled.
