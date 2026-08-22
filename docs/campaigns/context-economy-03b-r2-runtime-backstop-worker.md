# RAS 03B-R2 Runtime Backstop Worker Report

- Verdict: `REPAIRED_CANDIDATE`
- Starting HEAD: `cc2a36b057d7dd6dbfc09c4ac0a2cb43c456aa93`
- Scope: narrow systemd `RuntimeMaxSec` micro-repair only

## Exact files changed by R2

- `src/research_automation_supervisor/codex_adapter.py`
- `src/research_automation_supervisor/process_enforcement.py`
- `tests/test_process_enforcement.py`
- `docs/campaigns/context-economy-03b-r2-runtime-backstop-worker.md`

## RuntimeMaxSec derivation

Every Process-Enforcement-enabled transient action service receives
`RuntimeMaxSec` in the `systemd-run` launch command before `Popen` crosses the
launch boundary. Its value is exactly:

`min(prepared.request.timeout_seconds, policy.max_wall_clock_seconds)`

The finite positive result is serialized deterministically as decimal seconds.
The request timeout supplies the existing bounded upper limit. `TimeoutStopSec`,
`KillMode=control-group`, `KillSignal=SIGTERM`, and
`FinalKillSignal=SIGKILL` remain unchanged. Disabled/legacy launch behavior and
InvocationID/ControlGroup fail-closed signaling rules remain unchanged.

## Validation

- Targeted A-E qualification: `6 passed in 1.68s`
- Real-host result: PASS; direct `systemd-run` execution (no adapter wall-clock
  loop) ended with unit `Result=timeout`, empty cgroup, and no surviving
  TERM-ignoring `setsid` descendant.
- Process Enforcement family: `226 passed in 8.82s`
- Core runtime profile: `321 passed in 88.56s`
- Ruff on all changed Python files: PASS
- mypy on all changed source files: PASS (`8 source files`)
- `git diff --check`: PASS
- Remaining blocker: none

## Token usage

- input_tokens: unavailable
- cached_input_tokens: unavailable
- cache_write_input_tokens: unavailable
- output_tokens: unavailable
- reasoning_output_tokens: unavailable
- combined_tokens: unavailable
- Reason: authoritative `turn.completed.usage` is unavailable inside this
  active Worker turn; external receipt extraction is required.

NO COMMIT / NO PUSH

## External authoritative completion receipt

Recovered from native Codex rollout after Worker completion.

- session_id: `01a02955-554d-78a2-b750-9094327c3ad1`
- input_tokens: `1103895`
- cached_input_tokens: `1028864`
- cache_write_input_tokens: `0`
- output_tokens: `13286`
- reasoning_output_tokens: `6216`
- combined_tokens: `1117181`
- task_complete_events: `1`

`combined_tokens = input_tokens + output_tokens`; cached input,
cache-write input, and reasoning output are submetrics and are not added again.

## Independent qualification

03B-R2 / Process Enforcement v1: **QUALIFIED** after independent review.
Final qualification gates: targeted R2 6 passed; Process Enforcement family
226 passed; core runtime profile 321 passed; Ruff PASS; mypy PASS;
git diff --check PASS. No blocking finding remains.
