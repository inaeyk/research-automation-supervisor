# Context Economy 03B — systemd containment Worker report

## Verdict

IMPLEMENTED_CANDIDATE

The frozen systemd user transient-service/cgroup-v2 architecture is implemented. Process-group
emptiness remains diagnostic only and cannot authorize safe closure.

## Files changed

- `pyproject.toml`
- `src/research_automation_supervisor/cli.py`
- `src/research_automation_supervisor/codex_adapter.py`
- `src/research_automation_supervisor/codex_models.py`
- `src/research_automation_supervisor/native_rollout_budget.py`
- `src/research_automation_supervisor/process_enforcement.py`
- `src/research_automation_supervisor/systemd_launch_helper.py`
- `src/research_automation_supervisor/workflow_engine.py`
- `src/research_automation_supervisor/workflow_integrity.py`
- `tests/fixtures/fake_codex.py`
- `tests/test_codex_adapter.py`
- `tests/test_process_enforcement.py`
- `docs/campaigns/context-economy-03b-systemd-containment-worker.md`

## Containment lifecycle

- Enforcement allocates a bounded `ras-codex-<128-bit-random>.service` name and durably writes it,
  the backend, task identity, and action identity before the launch boundary.
- The Supervisor preflights cgroup v2, the user manager, and absence of the fresh exact unit using
  bounded `systemctl --user` calls.
- `systemd-run --user --quiet --pipe` launches one `Type=exec` service with
  `KillMode=control-group`, bounded `TimeoutStopSec`, SIGTERM/SIGKILL signals,
  `ProtectControlGroups=yes`, and inaccessible user bus/systemd runtime paths. It does not use
  `--collect`.
- After launch, exact `InvocationID` and `ControlGroup` are inspected, validated against the unit,
  and durably bound. PID/PGID/SID/start ticks are retained only as wrapper diagnostics.
- Budget, accounting-integrity, wall-clock, adapter-timeout, output-limit, and abnormal-cleanup
  stops address the exact unit. systemd performs whole-cgroup TERM-to-KILL escalation.
- Safe closure requires the bound unit identity, a non-live unit outcome, and the exact cgroup path
  proven empty/absent. A cleared post-stop `ControlGroup` display property is accepted only with the
  already-bound path and matching retained `InvocationID`.
- The enforced loop does not poll/reap the wrapper before containment proof. After proof it boundedly
  waits the wrapper, measures the final native-rollout tail, writes `reaped` closure evidence, and
  only then calls `process_finished`.
- Stop or inspection failure writes `termination_failed`, never fires `process_finished`, never
  measures tail as final closure evidence, and cannot produce a successful action result.
- The Process-Enforcement-disabled direct adapter path remains free of containment artifacts.

## Environment transfer

- The transient-service argv contains only the secret-free helper and Codex command.
- The Supervisor sends a magic/versioned length frame containing the sanitized environment over
  inherited stdin, followed immediately by the unchanged prompt bytes.
- The isolated standard-library helper validates the frame and calls `execve()` with exactly that
  environment. Stdout and stderr remain separate, unpolluted pipes.
- Tests prove the sanitized environment and prompt arrive intact and that a sensitive sentinel is
  absent from launch argv and durable/log artifacts.

## Recovery semantics

- Recovery distinguishes never launched, launch outcome unknown, exact live unit requiring stop,
  closed bound containment, absent-after-bound containment, identity mismatch/reuse, and terminal
  termination failure.
- Launch intent with no bound invocation never authorizes relaunch; the exact persisted unit is the
  sole recovery handle.
- InvocationID or ControlGroup mismatch authorizes neither signal nor relaunch.
- `termination_failed` with `termination_reason=None` remains terminal termination failure and is
  not reclassified as generic running.
- Durable safe closure suppresses duplicate stop. PGID evidence grants no recovery signal or closure
  authority for the systemd backend.
- Workflow artifact sealing now includes and verifies containment evidence when enabled. A real
  bounded Worker action pauses as `worker_bounded_continuation_required` after one launch, with no
  Auditor, repair, retry, or continuation.

## Validation

- Focused/adversarial suite: **20 passed** in 2.60s.
- Real-host marked cgroup-v2 slice: **1 passed, 0 skipped** in 0.98s. It proved pipe streaming,
  SIGTERM-ignoring `setsid()` child containment, sibling `systemd-run` denial, and direct cgroup
  migration denial.
- Process Enforcement / execution-budget profile: **203 passed** in 7.22s (historical family: 195).
- Core/quant runtime profile: **321 passed** in 88.44s.
- Ruff on all changed Python files: **PASS**.
- mypy on all changed source files: **PASS** (8 source files).
- `git diff --check`: **PASS**.

## Remaining known limitation

- The backend intentionally supports only Linux hosts with cgroup v2 and a reachable systemd user
  manager; unsupported or hung managers fail closed before Codex launch.
- Failed transient units may remain visible to preserve interrupted-action recovery evidence because
  running actions do not use `--collect`. No automatic repair, relaunch, or PGID fallback exists.

## Token usage

No authoritative current-turn runtime receipt is available inside this Worker before completion;
no estimate is substituted.

- input_tokens: unavailable
- cached_input_tokens: unavailable
- cache_write_input_tokens: unavailable
- output_tokens: unavailable
- reasoning_output_tokens: unavailable
- combined_tokens: unavailable

## Repository actions

NO COMMIT / NO PUSH.
