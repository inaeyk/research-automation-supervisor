# Stage 0 architecture

Stage 0 is a deterministic, read-only foundation. It has three layers:

- `contract.py` defines immutable Pydantic models, canonicalizes path patterns,
  rejects schema violations, and safely loads YAML.
- `doctor.py` probes Python, Git, repository state, Codex version, and Codex login
  state. Command execution, executable lookup, working directory, and Python
  version are injectable so tests never require a real Codex login.
- `cli.py` translates those services into human-readable or stable JSON output and
  applies the public exit-code contract.

All subprocesses use argument vectors and explicit timeouts. Diagnostic output
never includes the raw output of `codex login status`; only a normalized status is
reported. Stage 0 performs no workflow execution and owns no mutable global state.

Path patterns are trimmed, backslashes are converted to forward slashes, and
POSIX lexical normalization is applied before allowed/protected overlap checks.
Acceptance-test timeouts are bounded to 1 through 86,400 seconds.
