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
runner, and the durable state engine described in `workflow_engine.md`. The
composition remains deterministic: models return schema-validated advice and
results, while engine-owned state rules alone select transitions.
