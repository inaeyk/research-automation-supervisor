# RAS 03B-R1 — recovered bounded repair Worker report

## Verdict

**REPAIRED_CANDIDATE**

The interrupted 03B-R1 repair was recovered from the current dirty worktree without reset,
cleanup, revert, checkout, commit, push, web access, or another agent. The frozen systemd user
transient-service/cgroup-v2 architecture remains unchanged. PGID/PID/SID evidence remains
diagnostic and grants no recovery or safe-closure authority.

## Partial R1 work already present

The interrupted Worker had already implemented most of all four required repairs:

- B1: `codex_adapter.py` already reconciled an ambiguous post-`systemd-run` identity by inspecting
  only the durable unique intended unit, binding a safely observed exact identity, requesting a
  bounded unit stop, and suppressing `process_finished` on the terminal launch failure.
- B2: `workflow_engine.py` already invoked durable process-termination reconciliation for a pending
  production Worker/Auditor action before normal artifact recovery, with no pending-action
  relaunch path. `process_enforcement.py` already contained the durable recovery assessment and
  exact-unit stop/persistence flow.
- H1: the production backend already read the bound cgroup's `cgroup.events`, strictly parsed the
  hierarchical `populated` field, and did not use recursive `cgroup.procs` scanning as positive
  closure authority.
- H2: `fake_codex.py` and the marked host test already attempted access through
  `/proc/<outside-same-UID-pid>/root` to the user bus, user-manager paths, and cgroupfs, including
  harmless sibling-unit and cgroup-migration attempts.

## Work completed in this recovery

Three bounded patch cycles completed the remaining gaps:

1. Preserved `termination_failed` as terminal after authoritative cgroup closure. Previously,
   `containment_closed=True` and `cgroup_empty=True` were classified first as `already_closed`,
   which could allow workflow recovery even when `termination_reason=None`.
2. Kept identity mismatch/ambiguity as the stronger fail-closed classification while retaining the
   terminal rule for safely identified failed containment.
3. Fixed absent-after-bound recovery ordering. A disappeared exact unit has no currently observable
   `InvocationID`; recovery now uses the already-durable binding plus the bound cgroup result instead
   of misclassifying that absence as identity reuse.

Tests were extended to cover terminal failure after proven closure, workflow recovery of an exact
bound unit that disappeared, hierarchical `cgroup.events` authority despite contradictory
`cgroup.procs` fixtures, and malformed/missing/unreadable `cgroup.events` failure.

## Required repair results

- **B1 — PASS.** A crossed launch boundary is reconciled only through the durable intended unit.
  An identity-authorized exact live unit is stopped boundedly. Unproven identity or unproven closure
  remains terminal with no `process_finished`, continuation, retry, repair, or relaunch. A B1
  failure that reaches proven closure still remains `termination_failed` during later recovery.
- **B2 — PASS.** Actual pending Worker/Auditor resume loads durable evidence, inspects the exact
  intended unit, assesses recovery, stops only a bound exact live unit, proves and persists closure,
  and only then enters normal artifact recovery. The pending action is never launched again.
  `termination_failed` remains terminal even with `termination_reason=None`, including after closure.
- **H1 — PASS.** Existing cgroups require a readable, well-formed hierarchical `populated 0` in
  their own `cgroup.events`. Malformed, missing, or unreadable events fail closed. Recursive
  `cgroup.procs` contents cannot establish closure. A previously identity-bound cgroup that has
  disappeared is explicitly accepted and durably reconciled.
- **H2 — PASS, not blocked.** On the real host, the `/proc/<outside-same-UID-pid>/root` probes could
  not access the hidden user-manager endpoints, start the sibling unit, or migrate through cgroupfs.
  No hostile escape was observed and no architecture change was made.

## Validation

- Targeted R1/process-enforcement tests excluding the marked host case: **39 passed, 1 deselected**
  in 2.04s.
- Targeted marked real-host hostile-escape test: **1 passed, 33 deselected** in 0.90s. The final
  full Process Enforcement family also executed the marked test successfully after all source
  changes.
- Process Enforcement family: **223 passed** in 7.44s. Historical pre-R1 candidate baseline:
  203 passed.
- Core runtime profile: **321 passed** in 81.25s, preserving the frozen total.
- The first core invocation was collection-only and executed zero tests because the initially
  borrowed Python 3.14 environment lacked `urllib3`. The corrected invocation used an already
  installed complete local project environment; no source or dependency changes were made.
- Ruff on all changed Python files: **PASS**.
- Additional repository-wide Ruff diagnostic: **FAIL**, one pre-existing SIM102 in unmodified
  `src/research_automation_supervisor/semantic_decomposition.py:562`. This recovery did not alter
  that out-of-scope file.
- Strict mypy: **PASS**, no issues in 83 source files.
- `git diff --check`: **PASS**.

## Remaining blocker

None within the four bounded 03B-R1 repairs. The unrelated pre-existing repository-wide Ruff SIM102
finding remains outside this repair's changed-file validation scope.

## Token usage

No authoritative `turn.completed.usage` receipt for this active Worker is available before model
completion, and no durable task ledger for this task was present. No estimate is substituted.

- input_tokens: unavailable
- cached_input_tokens: unavailable
- output_tokens: unavailable
- reasoning_output_tokens: unavailable
- combined_tokens: unavailable
- per-session breakdown: unavailable
- repair/retry token attribution: unavailable

## Repository actions

**NO COMMIT / NO PUSH.**
