# R0 real-launch blocker closure

Date: 2026-08-24

Historical campaign: `campaign-0723d213e3c7c7cd223bf9d0`

Fresh release-gate campaign: `campaign-98312984b86728f2b0f7d1db`

Latest disposition: **STOPPED at the first fresh blocker. R0 PASS was not reached.**

Status: **stopped before a new real recovery attempt because the existing campaign's
legacy frozen acceptance runner now requires a privileged runtime repair**. The r005
nested-Bubblewrap failure was reproduced exactly, fixed narrowly for current qualified
runners, covered by a focused regression, and proven under the production systemd and
managed Codex topology. Pre-attempt validation then showed that this historical
campaign's immutable runner is pinned to `/usr/bin/python3.14`, where the Supervisor
package is unavailable. No safe unprivileged change can repair that sealed runner.

No new campaign, recovery response, model turn, PA-5D/Attempt 005, sudo action,
privileged mutation, Core-service restart, commit, or push was performed in the current
closure. The existing-campaign attempt counter for this mission remains `0/3`; the
fresh release smoke was not started.

## Continuation-rehydration fix

The fix is limited to the existing Stage 2 and replay recovery paths:

- `_rehydrate_accepted_continuation` requires the current durable continuation path,
  SHA-256, repair round, and exact `human_continuation_requested` journal transition;
- it reloads the accepted file through the existing no-follow locator, workspace,
  protected-path, UTF-8, size, redaction-collision, and SHA-256 validation;
- `resume_prompt_source_substage` rehydrates those bytes before driving a human-triggered
  `worker_repair_prompt` boundary;
- the already-sealed sequence-16 consequence is recoverable only when the journal ends
  with the exact prompt-source pause, `prompt_source_human_resume`, and local
  `workflow_state_invariant_failed` transitions, and there is no round-1 Worker intent
  or completion;
- that proof appends `continuation_rehydration_recovery`; every other failed state
  remains terminal;
- replay routing recognizes only that exact failed state at the existing outer
  `worker_continuation` boundary. It does not replace the original continuation with the
  later operator response.

The existing continuation remained anchored at `decision-002-note.md` with SHA-256
`c1b7276c3679a9c5b028c1cb9d828a4734ad640cc0126054d57acaeb6f45a51d`.
The real `worker-r001` metadata cites that same path as its prompt source. No
continuation text was synthesized or re-entered.

## Focused regression coverage

Added coverage proves:

- an accepted continuation survives prompt-source recovery/restart;
- the restored Worker prompt starts with the exact accepted bytes and contains them
  exactly once;
- a missing or replaced accepted note fails before another model action;
- the sealed sequence-16 local failure can recover exactly once;
- `worker-r000` is not duplicated, only `worker-r001` is prepared, and the Auditor is
  fresh in the successful scripted path.

Passing checks:

```text
5 passed in 2.92s
19 passed in 5.93s
Ruff: passed
mypy (workflow_engine.py, replay_campaign_engine.py): passed
git diff --check: passed
```

The five-test set contains the new exact-byte, missing/tampered, sealed-failure, and
continuation-idempotence regressions. The 19-test set is the complete Stage 2 workflow
engine module.

One broader adjacent selection completed `26 passed, 3 failed`; the three failures are
pre-existing fake-Supervisor cases that send an ordinary scripted artifact through the
sealed-prelaunch proof reader without a `stage2-completion.json`. They are outside the
continuation defect and were not changed under this mission.

## Runtime refresh

The user-owned managed package was reinstalled at source digest
`efa263c768ceb30543a535c33317eb818429428f067e8f88ed161bb7a14ca8e0`.
The sandboxed launcher process could not access the Core socket because its synthetic
group set omitted the Custodian group, so the runtime was started through a normal
unprivileged WSL login. Health then reported the same digest and readiness instance
`cdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcd`.
The root-owned Core service was neither modified nor restarted.

## Prior authorized attempt

The active request was `human-000026`, SHA-256
`27d2b5bc717b7c7916e6bab9fc0a4db81de752a0266336cb5e67cc767383658f`.
One `continue_existing` response was accepted. Its note instructed recovery to use the
already accepted durable continuation without replacing or recreating it.

The attempt then proved the fix:

1. Stage 2 validated the sequence-16 failed state and appended
   `continuation_rehydration_recovery` at sequence 17.
2. Supervisor action `supervisor-75016443931a700a` resumed the existing Supervisor
   session `01a034d5-f7e1-70d0-a67b-065927c450f5` and returned an accepted
   `repair_prompt`.
3. Stage 2 sealed exactly one `worker-r001` intent using persistent Worker session
   `01a034db-861a-7b40-a8ab-49e46ea00e00` and the original continuation source.
4. The transient Worker unit failed before `/usr/bin/codex` emitted a thread or turn
   event. Stage 2 sealed `worker_process_failed` at sequence 20 and its escalation at
   sequence 21.
5. No fixed test or Auditor launched.

There are exactly two Worker action records: the earlier completed `worker-r000` and
this one `worker-r001`. There is no Auditor action record.

## Prior distinct blocker

The failed unit was `ras-codex-7fe781536a8f5b1fc92847b3d26d40ae.service`.
Its sealed evidence records:

- `ExecMainStatus=200` (`EXIT_CHDIR`), `Result=exit-code`;
- zero stdout, stderr, and normalized events;
- no Worker thread-start event or final message;
- process exit within 0.061 seconds;
- cgroup and owned process group empty, process reaped, containment closed.

The managed repository path is group-traversable only by
`research-supervisor-custodian` (GID 986). A fresh unprivileged WSL login includes GID
986, but the long-lived `systemd --user` manager (PID 377) has groups
`4 24 27 30 46 100 1000 1001` and omits 986. Its transient service therefore cannot
change directory into the Core snapshot before executing Codex.

This user-manager group state was not repaired or restarted. After the sealed evidence
was collected, the empty failed unit was reset, the Custodian reaped its runner, and
read-only checks found no remaining managed Codex process, runner PID, or
`ras-codex-*` unit.

## One authorized recovery attempt after the host fix

Before recovery, the restarted `systemd --user` manager was PID 1166 and its live
supplementary groups were `4 24 27 30 46 100 986 1000 1001`. The managed repository
remained group-traversable by GID 986, the installed runtime digest remained
`efa263c768ceb30543a535c33317eb818429428f067e8f88ed161bb7a14ca8e0`, and no stale
managed Codex process, qualified runner, or `ras-codex-*` unit existed.

The active request was `human-000033`, SHA-256
`88694287bef0f2c6850b47af727a4098cd05ed6e35018ac3b01562cb13ffca55`.
Exactly one `continue_existing` response was accepted as decision index 5. Its exact
note is SHA-256
`f4e47e989bbc1ed9433c9327253e3dce1c58a1e6f84d62265fa25c4ec06c6d5b`.

The existing campaign then advanced without duplicating prior completed work:

1. Outer journal sequences 34 and 35 sealed and accepted that one human decision.
2. Supervisor action `supervisor-adc18da97a03156f` resumed the existing Supervisor
   session `01a034d5-f7e1-70d0-a67b-065927c450f5` and returned the accepted final
   bounded `repair_prompt` for repair round 2.
3. Stage 2 appended `human_continuation_requested` at sequence 22 and sealed exactly
   one new Worker intent, `worker-r002`, at sequence 23. It resumed the existing Worker
   session `01a034db-861a-7b40-a8ab-49e46ea00e00`; neither `worker-r000` nor
   `worker-r001` was relaunched.
4. Unit `ras-codex-f44895bfe47eeba0ba0567320ec34df6.service` entered
   `systemd_user_cgroup_v2` containment with the existing campaign repository as its
   working directory. `/usr/bin/codex` emitted the expected persistent thread ID and a
   complete `turn.completed.usage` event, then exited zero. Sealed termination evidence
   records `Result=success`, an inactive/dead unit, an empty cgroup and owned process
   group, a reaped process, and closed containment.
5. The Worker returned `status=blocked` because Codex emitted the error
   `Code Mode is unavailable because code-mode host is disabled`. The sealed invocation
   metadata records `features.code_mode_host=false`, and stderr records
   `error=code-mode host is disabled`.
6. The Worker changed no files. Round-2 Git evidence has an empty changed-path set and
   the empty-diff SHA-256
   `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`.

Stage 2 validated the blocked Worker result at sequence 25, stored unchanged Git
evidence at sequence 26, paused with `worker_blocked` at sequence 27, and sealed the
escalation package at sequence 28. The outer campaign paused at sequence 40. No fixed
test, fresh Auditor, candidate finalization, or export was reached. Because this was a
distinct blocker, no response to the new request and no further recovery action was
taken.

## Code-mode-host diagnosis and release-blocker fix

The exact production cause of the `worker-r002` failure was the frozen managed Codex
command, not an assumed missing executable: both new and resumed invocations explicitly
set `features.code_mode_host=false`. The installed `/usr/bin/codex` reported Codex CLI
`0.149.1`, `code_mode_host` as stable and enabled, and `code_mode_only` as disabled;
the command-line override disabled the otherwise-enabled host router. The sealed
`worker-r002` metadata and stderr independently record the false override and
`error=code-mode host is disabled`.

The narrow fix changes new and resumed managed commands to the exact override
`features.code_mode_host=true`. It does not enable `code_mode_only`, so ordinary direct
tools remain available as fallback. Recovery verification accepts only three exact
sealed command epochs: current `true`, prior `false`, and the older command without a
code-mode-host override. Arbitrary values or extra flags still fail closed. This
preserves historic recovery evidence without weakening command sealing.

The managed executable identity did not change. `/usr/bin/codex` remained version
`0.149.1` with SHA-256
`73dc5888888f411c1f0fa7b81d866e721dcc86b527ce8e3b2cf4708661e823ba`, matching the
root-owned managed-install record. Systemd/cgroup containment, resumed thread identity,
direct-tool fallback, and existing usage-receipt behavior were preserved.

## Focused code-mode regression and local validation

The focused regression reproduces the exact production path: a frozen
`features.code_mode_host=false` command emits the production-style fail-closed error,
blocked final result, and stderr marker; the corrected managed command succeeds and
contains `features.code_mode_host=true`, with neither the false override nor
`features.code_mode_only=true`. Command-integrity coverage also proves that only the
three exact policy epochs above are accepted during recovery.

Validation completed before the real recovery attempt:

```text
Focused code-mode regressions: 2 passed in 0.42s
Full adapter and workflow-engine modules: 99 passed in 10.87s
Scoped replay/recovery gate: 9 passed in 5.15s
Adjacent managed-runtime selection: 289 passed, 17 failed, 1 skipped in 55.65s
Ruff (all changed Python files): passed
mypy (codex_adapter.py, workflow_integrity.py): passed
git diff --check: passed
```

All 17 adjacent-selection failures are the already-documented fake-Supervisor
prelaunch-proof cases in `test_replay_campaign.py`: their scripted artifacts omit
`stage2-completion.json`. They do not execute the code-mode path and were not broadened
into this release fix. The single skip is the qualification-only real root-owned-state
case. The exact scoped replay/recovery gate passed.

The unprivileged managed runtime was reinstalled at source digest
`3f4511eb41d4fe40fb9a88ee4af8d048c80876c9f95398b6fe7b9a00f308c6a8`; its installed
stamp and source matched. Only the user-owned Custodian runtime was restarted for the
attempt. The root-owned Core service remained unchanged at PID 137. Before recovery,
the existing `systemd --user` manager was PID 1166 and included GID 986.

## Single final code-mode recovery attempt

The active request was `human-000040`, SHA-256
`b2c914f0649d5cc018f79c05984c729fd9dcd9bf800725f0994a23f02e56b4ff`.
Exactly one `continue_existing` response was accepted as decision index 6. The durable
decision has SHA-256
`83e22a8356d85f5aafc50942ef4dad17a7c915b7aab6e27b79f7fdb273838ce0`; its note has
SHA-256 `6f6c5f00680bf060a78652fc18306ef7a2c66dcabfc163cd92c85c811b16c285`.

The existing campaign advanced without duplicating prior completed work:

1. Supervisor action `supervisor-2f276d8da7a527ae` resumed the original Supervisor
   session `01a034d5-f7e1-70d0-a67b-065927c450f5` and accepted the bounded repair-round-3
   prompt.
2. Stage 2 sealed only `worker-r003` and resumed the original Worker session
   `01a034db-861a-7b40-a8ab-49e46ea00e00`. It did not relaunch `worker-r000`,
   `worker-r001`, or `worker-r002`.
3. The exact Worker command retained `/usr/bin/codex`, used
   `features.code_mode_host=true`, and entered unit
   `ras-codex-2ee5a28f3e6d0c92c7dbf58305d1a1ce.service` under
   `systemd_user_cgroup_v2` containment. Sealed termination evidence records return
   code zero, `Result=success`, an inactive/dead unit, empty cgroup and owned process
   group, a reaped process, and closed containment.
4. With the false override removed, Codex reached host spawning and exposed a distinct
   production blocker: `failed to spawn code-mode host
   /usr/bin/codex-code-mode-host: host executable was not found`. Stderr records
   `No such file or directory (os error 2)` for that exact path.
5. The Worker inspected, edited, and tested no repository file. Round-3 Git evidence
   has an empty changed-path set and empty-diff SHA-256
   `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`.

This missing host executable is distinct from the diagnosed and fixed false feature
override. In accordance with the stop rule, it was not repaired and no second response,
retry, Worker action, or Auditor action was launched.

## Historical `worker-r001` accounting audit

The narrow durable-telemetry audit found no authoritative usage object to recover.
`worker-r001/events.jsonl` and stderr are zero bytes; metadata records a 0.060911-second
`200/EXIT_CHDIR` pre-exec failure, `thread_id=null`, no thread-start IDs, zero valid
events, and `usage_complete=false`. Receipt
`b76a213304b2f107893822fbed74584631a428d030be8d2ebe1b35bbf09b4985` has zero events
and zero completed turns, with `missing_or_ambiguous_thread_id` and
`missing_turn_completed_event`.

There is therefore no exact `turn.completed.usage` telemetry from which to recover
`worker-r001`. Its usage remains **unavailable**; no estimate or synthetic receipt was
created. The campaign ledger remains correctly fail-closed.

## Durable state at stop

- Projection: `needs_input / worker_requires_human`.
- Active request:
  `6cf60161826c2b61bb01d00ab1b16410290993cebd7246f3df59b2c0c380fb48`
  (`human-000047`).
- Outer campaign: `human_paused / worker_continuation`, journal sequence 47, seven
  accepted human decisions.
- Stage 2: `human_paused / worker_blocked`, journal sequence 35,
  `repair_round=3`, `repair_trigger=human`.
- Worker: `worker-r000` preserved; `worker-r001` remains the pre-exec failed launch;
  `worker-r002` remains the completed false-flag turn; `worker-r003` is the one newly
  completed, contained, blocked turn. The persistent Worker session is unchanged.
- Supervisor: original session preserved; exactly one new accepted repair-prompt turn.
- Auditor: no session or action.
- Smoke change: absent; round-3 diff empty. Fixed tests: not reached.
- Completion: not verified; no candidate export.
- Surviving managed Codex process, qualified runner, or `ras-codex-*` unit: none.
  The temporary unprivileged Custodian used for recovery was stopped; the final user-unit
  listing was empty and the process table contained no campaign-managed child process.

## Token usage

All available numbers below come from verified `turn.completed.usage` receipts.
Combined tokens are exactly input plus output; cached input is retained as a submetric
and is not added a second time.

| Scope | Input | Cached input | Output | Reasoning output | Combined |
|---|---:|---:|---:|---:|---:|
| Valid-receipt cumulative before this final attempt | 146,040 | 74,496 | 2,552 | 756 | 148,592 |
| New Supervisor `supervisor-2f276d8da7a527ae` | 19,594 | 17,152 | 519 | 243 | 20,113 |
| New Worker `worker-r003` | 41,482 | 37,376 | 256 | 27 | 41,738 |
| Exact final-attempt delta | 61,076 | 54,528 | 775 | 270 | 61,851 |
| Final valid-receipt cumulative subtotal | 207,116 | 129,024 | 3,327 | 1,026 | 210,443 |
| Historical Worker `worker-r001` | unavailable | unavailable | unavailable | unavailable | unavailable |

Both model actions in the final attempt have complete authoritative receipts.
Supervisor receipt
`0ea2f1c5a06ae8e55cb43e52491bca8a0d8b0db867648728e8d04023dd2839ae` and Worker
receipt `1565ac2cd898eb64b82c9f990c85658bf42bbeaf204bfd4d5a8919df6a4ebdb1`
produce the exact attempt delta above.

Valid-receipt role breakdown at stop:

- Worker: input `122,671`, cached input `82,688`, output `1,349`, reasoning output
  `580`, combined `124,020` across three completed turns, plus unavailable
  `worker-r001`.
- Supervisor/other model sessions: input `84,445`, cached input `46,336`, output
  `1,978`, reasoning output `446`, combined `86,423` across five completed turns.
- Repair/retry attribution: input `148,132`, cached input `100,864`, output `2,114`,
  reasoning output `506`, combined `150,246` across six completed turns, plus
  unavailable `worker-r001`.
- Auditor: no session and no tokens.

The exact final attempt used input `61,076`, cached input `54,528`, output `775`,
reasoning output `270`, and combined `61,851`. Across all eight valid completed turns,
the authoritative subtotal is input `207,116`, cached input `129,024`, output `3,327`,
reasoning output `1,026`, and combined `210,443`. The ledger is `complete=false`
solely because historical `worker-r001` has no valid usage object. Consequently, the
complete cumulative campaign total and final authoritative complete ledger are
**unavailable**.

## Final test-runner blocker closure

This section supersedes the earlier stop-state and token-accounting sections above for
the final test-runner recovery. The preserved entry boundary was `human-000054`
(`60e2b63dd87c…`). Stage 2 journal sequence 42 already contained `worker-r004`'s
completed repository work. Its Git evidence named exactly `src/message.py` and
`tests/test_message.py`, with patch SHA-256
`ad9aef615fc64435e46a7f720e9ca4a4773f51ba1454cefd665537201cf2fab8`.
Those files were inspected but not edited or regenerated during this closure.

### Release-correct test execution

The no-`pytest` failure had two production causes:

- prelaunch canonicalized `sys.executable` with `realpath`, turning an otherwise
  qualified virtual-environment interpreter into the system interpreter; and
- automatic acceptance selected `repository_integrity` for a bare Python repository
  with `tests/test_*.py` but no `pyproject.toml`, `pytest.ini`, or `setup.cfg`, so simply
  fixing interpreter qualification would still not execute the smoke tests.

The smallest repository-independent production behavior implemented for that case is
an internal `python_bare` acceptance profile. It discovers sorted `tests/test_*.py`
modules, loads them with the standard library, and executes sorted module-level,
zero-argument `test_*` functions. It fails closed for no tests, parameterized/fixture
tests, or async tests, directing such repositories to declare a test environment. The
frozen runner uses the qualified `sys.executable` without resolving away a virtual
environment and sets `PYTHONDONTWRITEBYTECODE=1`. Existing configured Python projects
continue to use `python -m pytest`; non-Python/otherwise undeclared repositories retain
the integrity profile.

Regression coverage in `tests/test_qualified_campaign.py` creates a no-pip virtual
environment, proves `pytest` is unavailable there, freezes that exact interpreter into
acceptance, executes two actual bare tests through the production Bubblewrap topology,
and proves no bytecode cache escapes. Validation completed before the recovery response:

```text
Focused production regressions: 2 passed in 0.58s
Adjacent campaign/prelaunch/custodian regressions: 45 passed in 1.93s
Protected smoke, production-like Bubblewrap: 2 bare Python test(s) passed
Ruff: passed
mypy: passed
git diff --check: passed
```

The protected workspace remained exactly the `worker-r004` two-file diff after that
validation, with no `__pycache__` residue. The unprivileged runtime was refreshed to
source digest
`6dbbfbf6c3ae9a2b0a667bf7ac9a1a156eee9b3d25742be943c0c58b28953edc`;
the privileged Core was neither modified nor restarted.

### Exactly one recovery attempt

Exactly one valid response was accepted on the existing campaign. The response file
SHA-256 was `e4de784af1df8febbd67bbb18446f774f78c44ca13a75b6d01b060dcf9c5493e`;
outer decision 8 was durably sealed with SHA-256
`6fa8e59d907e76e4f23ac6f4909d5d8be3b00faa51b9c4b2b485ca1e5343aec7`.
The resumed Supervisor action `supervisor-3e386adb3a47e5ec` reused its persistent
session and launched only `worker-r005`. That Worker reused the persistent Worker
session and made no repository edit; round-5 Git evidence retains the exact
`worker-r004` changed paths and patch SHA-256 above.

Before the requested `/usr/bin/python3` standard-library smoke command could execute,
the managed command sandbox failed with the new blocker:

```text
bwrap: loopback: Failed to create NETLINK_ROUTE socket: Operation not permitted
```

This is distinct from the corrected test-command-selection defect. In accordance with
the mission's stop rule, it was not diagnosed further, repaired, retried, or answered
with another human continuation. Consequently, no frozen acceptance action or fresh
Auditor launched, and campaign completion is not verified.

### Durable state and containment at final stop

- Projection: `needs_input / worker_requires_human`.
- Active request: `human-000061`, SHA-256
  `1536e7c444a5a6fde665a31cb1267929e6215a81477e29c21958676f445edd2d`.
- Outer campaign: `human_paused / worker_continuation`, journal sequence 61.
- Stage 2: `human_paused / worker_blocked`, journal sequence 49,
  `repair_round=5`; fixed tests remain unsealed and `latest_audit` remains absent.
- Worker: `worker-r005` is the single newly completed blocked turn; no edit was made.
- Auditor: no action or session.
- Both new managed model units are inactive/dead with empty cgroups and owned process
  groups, reaped processes, and closed containment. No campaign runner, qualified
  runner, temporary Custodian, or `ras-codex-*` unit survives. The protected workspace
  contains only `worker-r004`'s two tracked modifications.

### Token usage for the final test-runner attempt

All values are exact `turn.completed.usage` receipt counters. Combined tokens are
input plus output; cached input and reasoning output are retained submetrics and are
not added again.

| Scope | Input | Cached input | Output | Reasoning output | Combined |
|---|---:|---:|---:|---:|---:|
| Valid-receipt subtotal supplied at `human-000054` | 323,417 | 197,888 | 4,554 | 1,372 | 327,971 |
| Supervisor `supervisor-3e386adb3a47e5ec` | 23,424 | 20,224 | 575 | 257 | 23,999 |
| Worker `worker-r005` | 53,777 | 49,664 | 481 | 189 | 54,258 |
| Exact final-attempt delta | 77,201 | 69,888 | 1,056 | 446 | 78,257 |
| Updated valid-receipt subtotal | 400,618 | 267,776 | 5,610 | 1,818 | 406,228 |
| Historical Worker `worker-r001` | unavailable | unavailable | unavailable | unavailable | unavailable |

Supervisor receipt
`9ddad8d4da94aa6a8092c6ec31b3148a7325de385e568cb1cec0b7fc0eb96528`
and Worker receipt
`8ebd3c70a33c6818734e9157a23fcbf514d7394b303bcb7a7ff75a5e5f499eb8`
each contain one valid completed-turn usage object. The entire final-attempt delta is
attributable to the required recovery/repair round. Auditor usage is zero because no
Auditor launched. Historical `worker-r001` remains permanently unavailable, so a
complete all-session campaign total is unavailable and has not been estimated.

## Current end-to-end closure

This section supersedes the status of the historical attempts above for the current
`R0 END-TO-END CLOSURE` mission.

### Exact r005 reproduction and actual conflict

The failing r005 shell command was replayed without a model turn through all of the
production layers:

- `/usr/bin/codex` `0.149.1` and its managed `CODEX_HOME`;
- a transient `systemd-run --user --pipe` service with the production `Type=exec`,
  `KillMode=control-group`, bounded stop/runtime, signal, `ProtectControlGroups`,
  `InaccessiblePaths`, working-directory, and environment-frame helper settings;
- Codex `workspaceWrite` sandboxing for the protected campaign repository with
  `networkAccess=false`; and
- the exact r005 standard-library test, qualified acceptance, diff-check, status, and
  diff shell command.

The command reproduced exactly:

```text
bwrap: loopback: Failed to create NETLINK_ROUTE socket: Operation not permitted
```

The transient service itself closed `inactive/dead` with `Result=success`; the nested
command returned `exitCode=1`. A direct managed-Codex environment probe showed
`CODEX_SANDBOX_NETWORK_DISABLED=1`. Running Bubblewrap directly and running the managed
Codex sandbox under the same user systemd manager both succeeded, ruling out the user
manager, cgroup containment, `RestrictAddressFamilies`, `RestrictNamespaces`, and the
ordinary outer sandbox. The error occurs only when the immutable acceptance script,
already inside Codex's network-disabled Bubblewrap, starts a second
`bwrap --unshare-all` and that inner Bubblewrap attempts to configure loopback through
`NETLINK_ROUTE`.

The narrow fix has two parts:

- a newly rendered qualified acceptance runner adds `--share-net` only when the trusted
  managed outer sandbox supplies the exact `CODEX_SANDBOX_NETWORK_DISABLED=1` marker;
  every other execution retains `--unshare-all` including its own network namespace;
- the engine-owned Worker wrapper states that fixed acceptance argv runs after the
  Worker returns and must not be executed redundantly inside the Worker sandbox.

This does not enable network access. In the nested case the inner runner reuses the
already isolated outer network namespace; mount, user, PID, IPC, UTS, and cgroup
isolation remain nested. The normal fixed-test path still creates its own isolated
network namespace.

### Production-like proof and regression

A corrected current runner executed a real bare-Python test through the exact managed
Codex and user-systemd topology above:

```text
command exitCode: 0
stdout: 1 bare Python test(s) passed
unit: inactive/dead, Result=success, ExecMainStatus=0
```

Focused regression coverage executes the rendered acceptance program with the managed
outer-sandbox marker and asserts that `--share-net` immediately follows
`--unshare-all`. Replay coverage also asserts the engine-owned Worker execution rule.
The final local gate was:

```text
Relevant pytest selection: 8 passed in 1.17s
Final focused marker/ownership rerun: 2 passed in 0.85s
Ruff (four touched Python files): passed
mypy (custodian_models.py, workflow_engine.py): passed
git diff --check: passed
```

The unprivileged managed runtime was refreshed to source digest
`6dad68477639d59fb8a24dfd0f3920471ce47de37a9febbc460eece81aacccfd`.
The privileged Core was neither modified nor restarted.

### Pre-attempt privileged stop

Before answering `human-000061`, the existing campaign's exact immutable acceptance
was also run directly from the normal unprivileged WSL host context. Bubblewrap passed
the former loopback point, then its inner interpreter failed:

```text
ModuleNotFoundError: No module named 'research_automation_supervisor'
```

The historical runner hard-codes `/usr/bin/python3.14` and the
`repository_integrity` profile. That interpreter has no Supervisor package in its
system paths. The runner also deliberately supplies only `PATH` and `LANG` to
Bubblewrap and sets `PYTHONNOUSERSITE=1`, so a user-site or environment override cannot
repair it. Current source fixes new campaigns by freezing the managed virtual-environment
interpreter and selecting `python_bare` for this repository, but the existing runner is
part of the sealed `.research-supervisor/**` snapshot and frozen Git/provenance identity.
Replacing it would mutate protected campaign authority. The remaining supported repair
would install the package into a root-owned `/usr` Python path, which requires
privileged host mutation.

The mission therefore stopped under the explicit privileged-blocker rule before a
campaign response was submitted. Attempts consumed: `0/3`. No Worker, fixed test,
Auditor, or Supervisor model turn was launched in this closure. Because the existing
campaign did not complete, the one fresh release smoke was not started.

### Preserved workspace and final containment

The protected workspace still reports exactly:

```text
 M src/message.py
 M tests/test_message.py
```

`git diff --check` passes there, there is no `__pycache__`, and no protected file was
edited. Final normal-host inspection found no `ras-codex-*` unit, qualified runner,
campaign runner, temporary Custodian, or managed `/usr/bin/codex` process. The temporary
runtime Custodian was identity-checked and terminated; the one intentionally failed
diagnostic transient unit was cleared. The root-owned Core Authority service and the
interactive operator's unrelated Codex host remained untouched.

### Token usage

All campaign values below are exact authoritative `turn.completed.usage` receipt
counters. No campaign model turn was launched in the current closure, so the new delta
is exactly zero. Cached input and reasoning output remain submetrics and are not added
again.

| Scope | Input | Cached input | Output | Reasoning output | Combined |
|---|---:|---:|---:|---:|---:|
| Historical valid-receipt subtotal | 400,618 | 267,776 | 5,610 | 1,818 | 406,228 |
| Current closure recovery/repair delta | 0 | 0 | 0 | 0 | 0 |
| Updated valid-receipt subtotal | 400,618 | 267,776 | 5,610 | 1,818 | 406,228 |
| Historical Worker `worker-r001` | unavailable | unavailable | unavailable | unavailable | unavailable |
| Fresh campaign | unavailable — not run | unavailable — not run | unavailable — not run | unavailable — not run | unavailable — not run |

The fresh-campaign complete total is unavailable because the fresh gate was correctly
not entered. The enclosing operator turn's final runtime usage is unavailable while
this report is being written; it must be supplied only by the post-completion runtime
receipt and is not estimated here.

## Fresh R0 release gate

This section supersedes the earlier statement that the fresh gate was not entered. The
historical campaign was not read through the product, resumed, responded to, or
modified. No PA-5D, Attempt 005, sudo command, commit, push, optional improvement, or
recovery intervention was performed.

### Focused validation and unprivileged runtime

The current source passed the regressions added for the release-gate fixes:

```text
Focused current-fix regressions: 18 passed in 2.95s
Ruff (all changed and new Python files): passed
mypy (seven affected runtime modules): passed
git diff --check: passed
```

The 18 tests cover managed Codex and code-mode-host identity, code-mode enablement,
systemd user-bus/group handling, prelaunch continuation, resumed per-turn accounting,
the bare-Python runner, nested-Bubblewrap network namespace reuse, engine-owned
acceptance execution, and exact recovery command epochs.

The checkout source digest was
`6dad68477639d59fb8a24dfd0f3920471ce47de37a9febbc460eece81aacccfd`, exactly matching
the installed user runtime and its health identity. Reinstallation was therefore not
needed. The recorded backend PID was stale, so the ordinary unprivileged bootstrap
started the matching runtime with readiness instance
`f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1`. The first
connection-refused probe reached neither Preview nor Start and created no campaign.

### One fresh Preview and Start

The one real product Preview used the clean baseline repository
`/home/inaeyk/researchrepo/ras-r0-smoke` at source commit
`d20270ae55489e6cc19a73d62098c71fefd7bd6e`. It reported every environment predicate
ready, including managed Python, Supervisor package, Git, managed Codex,
authentication, isolation, and filesystem checks. Preview ID
`preview-091b3d1541d0e540204685c5` froze:

- exactly `src/message.py` and `tests/test_message.py` as editable areas;
- automatic qualified acceptance, which selects the current `python_bare` profile for
  this repository;
- zero repair rounds; and
- the tiny task to add `version() -> str` returning `"r0"` and exactly one
  corresponding test.

The one Start created `campaign-98312984b86728f2b0f7d1db`, input-bundle SHA-256
`d3f21be7c7289a4682c1c03988b0b636cf6325aced1e6a63b58601524c3a9395`, and launch-intent
SHA-256 `8ad4e84bf1ba2f3f698b4a15cf65052f8a94f38b06cf446e0d01222246f31c40`.

### First fresh blocker

The campaign failed closed during qualified prelaunch with:

```text
QualifiedCampaignInputError: prepared repository snapshot binding is invalid
projection: blocked / verified_status_unavailable
```

The failure occurred before visible campaign authority, Supervisor, Worker, fixed
tests, fresh Auditor, diff collection, or candidate completion. The fresh authority
directory contains only `campaign-input-bundle-v1.json` and
`qualified-failure-v1.json`; there is no run directory or model-action record. The
source repository remains clean and no smoke change exists.

Read-only inspection identified an exact installed-runtime split:

- current checkout and protected-release `gitless_repository.py` are byte-identical at
  SHA-256 `995b13240e6425994648ccf91eebf1eae0bc8cf5d27c5929ea63b5aa8d39a41d`;
- the running Core venv has the stale file SHA-256
  `4ee782b022a806118820a619df5827055ab78822b5296a272f6a884f29aacbbb`;
- that stale Core code explicitly publishes the campaign workspace root as mode
  `0750`, and the fresh root was in fact `drwxr-x---`; and
- the current qualified verifier requires the safely delegated Core-owned,
  Custodian-group workspace root at exact mode `03770`.

Thus the current user-owned product correctly rejected a snapshot created by the stale
running privileged Core package. Repair requires refreshing the root-owned Core venv
and service through its protected administrator path. That is outside this no-sudo,
first-blocker gate, so it was not attempted. The campaign was not continued, responded
to, resumed, retried, or otherwise recovered.

### Final containment and gate result

The Custodian record is `runner_operation=idle` with `runner_pid=null`. The final real
WSL process inventory contains no campaign-managed `/usr/bin/codex`, qualified runner,
campaign runner, or replay runner. The user-unit listing contains no `ras-codex-*`
unit. The unrelated interactive operator's NVM Codex and code-mode host remain outside
the managed campaign path and were not touched. The Core service remains active at its
pre-existing PID `57788`; the user-owned Custodian remains available for inspection.

R0 PASS requirements for a real Worker, tests, an accepting fresh Auditor, exact
two-file change, and verified completion are unmet. The required stop condition was
the first new blocker above.

### Token usage

No fresh-campaign model process or turn was launched, so there is no
`turn.completed.usage` object to aggregate and no missing completed-turn receipt. The
fresh gate added zero model turns, retries, repairs, and audit rounds. Cached input and
reasoning output remain submetrics and are not added to combined tokens.

| Scope | Input | Cached input | Output | Reasoning output | Combined |
|---|---:|---:|---:|---:|---:|
| Historical valid-receipt subtotal before fresh gate | 400,618 | 267,776 | 5,610 | 1,818 | 406,228 |
| Fresh Supervisor | 0 — not launched | 0 — not launched | 0 — not launched | 0 — not launched | 0 — not launched |
| Fresh Worker | 0 — not launched | 0 — not launched | 0 — not launched | 0 — not launched | 0 — not launched |
| Fresh Auditor | 0 — not launched | 0 — not launched | 0 — not launched | 0 — not launched | 0 — not launched |
| Fresh retries/repairs/repeated audits | 0 | 0 | 0 | 0 | 0 |
| Updated valid-receipt subtotal | 400,618 | 267,776 | 5,610 | 1,818 | 406,228 |
| Historical Worker `worker-r001` | unavailable | unavailable | unavailable | unavailable | unavailable |

The fresh campaign's zero-turn accounting is complete. Historical `worker-r001`
remains unavailable and irrelevant to this fresh gate. The enclosing operator turn's
runtime usage is unavailable until its post-completion receipt and is not estimated
here.

## Acceptance-profile parity closure

This section supersedes the prior fresh-gate stop state. Campaign
`campaign-48b483613ca5c74ec4221a62` was inspected read-only and was not resumed,
responded to, recovered, or otherwise modified. Its sealed workspace and campaign
evidence were not edited. No PA-5D, Attempt 005, sudo command, commit, push, broad
refactor, or fresh campaign was performed.

### Exact parity diagnosis

Current qualified user-runtime planning already selects the internal `python_bare`
profile for a repository containing `tests/test_*.py` without Python project runner
configuration. Current source and the installed unprivileged runtime also already
contain the matching sealed acceptance-runner branch. Their exact
`custodian_models.py` SHA-256 is
`f92bc493e96fe361c962fe2aceab6ec93b98d1895fb9bbc155a00b6e9b56c9e6`.
The unprivileged installed-source digest is
`6dad68477639d59fb8a24dfd0f3920471ce47de37a9febbc460eece81aacccfd`,
exactly equal to the checkout source digest.

The protected release and installed privileged Core are stale in the same precise
way. Their `custodian_models.py` files are byte-identical at SHA-256
`9d4d5ea20af10b56bf266f90dce5732d35e46a2d71fd945a24c3f84cbae0505f`
and both lack the `python_bare` branch. The failed campaign's Core-frozen
`.research-supervisor/acceptance.py` has SHA-256
`bd44a1b2016552768a8e66d92197749c29c17dd447b11abfba5885c5e7123752`
and likewise lacks that branch. It therefore exits 64 on the user-runtime-selected
profile before launching Bubblewrap or any test. The defect is deployment skew, not a
missing implementation in current source.

The current implementation preserves acceptance authority: user input exposes only
`standard`, `python_pytest`, and `python_unittest`; `python_bare` is selected internally
from the Core-prepared repository; the Core renders the runner into the sanitized Git
snapshot; the frozen manifest names only that runner and profile; and the Worker cannot
supply a replacement command. The bare behavior uses the qualified frozen interpreter
and the standard library to execute sorted, zero-argument module-level `test_*`
functions. It assumes neither pytest nor a system-Python package installation and
continues to exit 64 for an unknown profile.

### Focused coverage and validation

`tests/test_qualified_campaign.py` now makes all four parity properties explicit. The
integrated fresh-Start fixture proves automatic planning chooses `python_bare`, proves
the executable runner is byte-identical to
`HEAD:.research-supervisor/acceptance.py` in the Core-prepared snapshot, executes two
real `test_*` functions through the qualified no-pip interpreter, and observes a zero
exit instead of 64. A separate regression executes the same rendered runner with an
unsupported profile and requires exact exit 64. Existing integrity-profile coverage
continues to prove repositories without declared tests retain the sealed integrity
check.

Validation before the protected handoff was:

```text
Focused profile selection/freeze/execution/fail-closed tests: 3 passed in 0.50s
Complete qualified-campaign module: 8 passed in 0.72s
Ruff (qualified-campaign source and tests): passed
git diff --check: passed
```

No production module was changed in this closure because the correct production
implementation was already present. The only code edit was the focused regression
coverage above.

### Prepared protected update and privileged stop

The old ordinary-user candidate directories were preserved, not deleted, as:

```text
/var/tmp/research-supervisor-release-candidate-pre-python-bare-parity
/var/tmp/research-supervisor-release-authority-candidate-pre-python-bare-parity
```

A new offline, unprivileged update candidate was prepared from the current checkout
using the already-approved Codex 0.149.1 binaries and protected offline wheelhouse. It
retains release ID `ras-8a3a029-codex-0.149.1` and binds the installed manifest
SHA-256 `5bea7b17f0b97fe2c762b937a44b61147534dfafac239f3b018a94358b038848`
as `update_from_manifest_sha256`. Candidate verification passed with these exact
review values:

```text
install-protected-release  791701fc61f0152c5c6a76bb901c1f84e1c98eb9c0e10a2a3008de59eacfb70d
verify-protected-release   791701fc61f0152c5c6a76bb901c1f84e1c98eb9c0e10a2a3008de59eacfb70d
update approval            dcf705375585612035e72b3d1a879830f5ac927b287180c849a10b5490ca651c
product wheel              a2cb7859d48b865982cf42a26e3f59733afcf060829886971dfb0cc5452afe9e
candidate custodian_models f92bc493e96fe361c962fe2aceab6ec93b98d1895fb9bbc155a00b6e9b56c9e6
```

The protected release and Core must therefore be refreshed. The mission stops before
sudo. From this checkout, the exact minimal administrator update is:

```bash
sudo /usr/libexec/research-supervisor/verify-protected-release
sudo /usr/bin/install -o root -g root -m 0755 /var/tmp/research-supervisor-release-authority-candidate/install-protected-release /usr/libexec/research-supervisor/install-protected-release
sudo /usr/bin/install -o root -g root -m 0755 /var/tmp/research-supervisor-release-authority-candidate/verify-protected-release /usr/libexec/research-supervisor/verify-protected-release
sudo /usr/bin/install -o root -g root -m 0644 /var/tmp/research-supervisor-release-authority-candidate/approved-release-v1.json /usr/share/research-supervisor-release-authority/approved-release-update-v1.json
sudo /usr/bin/sha256sum /usr/libexec/research-supervisor/install-protected-release /usr/libexec/research-supervisor/verify-protected-release /usr/share/research-supervisor-release-authority/approved-release-update-v1.json
sudo /usr/libexec/research-supervisor/verify-protected-release
sudo /usr/libexec/research-supervisor/install-protected-release --update inaeyk
```

The three hashes printed before the final two commands must exactly match the first
three review values above. Stop on any mismatch. After the update, verify the protected
release, active restarted Core, and exact parity bytes with:

```bash
sudo /usr/libexec/research-supervisor/verify-protected-release
sudo /usr/bin/systemctl is-active research-supervisor-core-authority.service
sudo /usr/bin/systemctl show research-supervisor-core-authority.service --property=ActiveState,SubState,MainPID,ExecMainStartTimestamp --no-pager
sudo /usr/bin/sha256sum /opt/research-supervisor-release/src/research_automation_supervisor/custodian_models.py /opt/research-supervisor-core/venv/lib/python3.14/site-packages/research_automation_supervisor/custodian_models.py
sudo /usr/bin/grep -n 'elif profile == "python_bare":' /opt/research-supervisor-release/src/research_automation_supervisor/custodian_models.py /opt/research-supervisor-core/venv/lib/python3.14/site-packages/research_automation_supervisor/custodian_models.py
```

Both `sha256sum` results must be
`f92bc493e96fe361c962fe2aceab6ec93b98d1895fb9bbc155a00b6e9b56c9e6`,
both grep targets must contain the branch, and the Core MainPID/start timestamp must be
newer than pre-update PID `93527` started at `2026-08-24 18:22:27` local time.

### Narrow qualified-runner zombie inspection

The only campaign-related survivor is PID `94152`, state `Z`, parent PID `90567` (the
ordinary user Custodian), in `/init.scope`; all managed Codex units/cgroups and their
processes are already gone. The Custodian launched the asynchronous qualified runner
with `Popen`, retained only its PID, and uses signal-0 liveness, which reports a zombie
as present. No process-management source was changed because that would exceed this
parity-only closure.

After the protected update, reap this one stale child by restarting only its verified
ordinary-user parent before a new smoke campaign:

```bash
/usr/bin/ps -p 90567 -o pid=,ppid=,stat=,args=
/usr/bin/kill -TERM 90567
/usr/bin/timeout 10 /usr/bin/tail --pid=90567 -f /dev/null
/bin/sh /home/inaeyk/researchrepo/ras-context-integration/scripts/custodian-bootstrap.sh /home/inaeyk/researchrepo/ras-context-integration normal bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
/usr/bin/ps -eo pid,ppid,state,user,cgroup,args | /usr/bin/awk '$3 ~ /^Z/ || /qualified_runner|ras-codex-|\/usr\/bin\/codex/'
/usr/bin/systemctl --user list-units 'ras-codex-*' --all --no-legend --no-pager
```

The first command must still identify PID `90567` as the same
`research-supervisor-custodian`; stop rather than signal it if identity differs. The
final two commands must print no campaign-managed process, zombie, or unit before the
single new fresh R0 smoke is started. Do not continue or respond to
`campaign-48b483613ca5c74ec4221a62`.

### Token usage

The following are exact authoritative `turn.completed.usage` counters from the failed
fresh campaign's complete durable ledger. Cached input and reasoning output are
submetrics and are not added again. No retry, repair, repeated audit, or Auditor turn
was launched.

| Session | Input | Cached input | Output | Reasoning output | Combined |
|---|---:|---:|---:|---:|---:|
| Supervisor/other | 13,797 | 0 | 399 | 141 | 14,196 |
| Worker | 73,498 | 41,216 | 922 | 171 | 74,420 |
| Auditor | 0 | 0 | 0 | 0 | 0 |
| Retries/repairs/repeated audits | 0 | 0 | 0 | 0 | 0 |
| Fresh campaign total | 87,295 | 41,216 | 1,321 | 312 | 88,616 |

All two completed model turns have valid receipts; campaign accounting is complete.
This closure launched no model session, so its campaign-model delta is zero. The
enclosing operator turn's final runtime usage is unavailable until its post-completion
receipt and is not estimated here.

## Post-refresh fresh smoke stop before Preview

This section supersedes the pending protected-update handoff above. The protected
release and installed Core `custodian_models.py` now both have exact SHA-256
`f92bc493e96fe361c962fe2aceab6ec93b98d1895fb9bbc155a00b6e9b56c9e6`
and contain the `python_bare` branch. The Core service was active/running as new PID
`101764`, started at `2026-08-24 18:44:38` local time. The unprivileged runtime had the
same module hash. The smoke repository was clean at commit
`d20270ae55489e6cc19a73d62098c71fefd7bd6e`, tree
`2222b64080fe530482fb2e6b2c3bdb800e9cd0f2`.

The previous Custodian PID and its qualified-runner zombie were gone, but the restart
had left no Custodian process listening and the readiness file still named dead PID
`90567`. A read-only host health request returned connection refused. One ordinary,
unprivileged bootstrap was then invoked with the already-matching runtime and readiness
instance `cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc`. It
reported `RAS_LAUNCH_READY`, wrote valid launcher evidence, and briefly served a
successful health request as PID `102206`.

The single fail-closed smoke client was deliberately ordered as health, application
session/CSRF, Preview, and only then conditional Start. Its first health request found
that PID `102206` had already exited and returned:

```text
ConnectionRefusedError: [Errno 111] Connection refused
urllib.error.URLError: <urlopen error [Errno 111] Connection refused>
```

This is the first distinct new blocker. The launch path stopped immediately. There
was no second bootstrap, health retry, Preview, Start, campaign ID, response, continue,
repair, recovery, Worker, acceptance process, Auditor, or model session. The preview
directory's newest entry remains `preview-6eb1b4094a776728938020d9` from 18:23:31, and
the campaign directory's newest entry remains the preserved
`campaign-48b483613ca5c74ec4221a62` from 18:24:23. No previous campaign was resumed or
modified.

The available backend log contains the one successful 18:46:20 health request but no
traceback or explicit shutdown reason, so the cause of the post-readiness Custodian
exit is not established in this no-repair stop. Final host containment found no
Custodian, qualified runner, campaign-managed `/usr/bin/codex`, zombie, or
`ras-codex-*` user unit. Only the expected active privileged Core and the unrelated
interactive operator Codex remained. The source smoke repository remained clean.

R0 PASS is therefore not claimed: the fresh campaign did not reach Preview or Start,
and no PASS criterion after those boundaries was exercised.

### Token usage

No fresh campaign or model process was created. There is no `turn.completed.usage`
object to aggregate and no missing completed-turn receipt; the complete fresh-attempt
campaign-model delta is exactly zero input, zero output, and zero combined tokens, with
zero Worker, Auditor, Supervisor, retry, repair, or repeated-audit sessions. The
enclosing operator turn's final runtime usage remains unavailable until its
post-completion runtime receipt and is not estimated here.

## Custodian lifecycle closure and fresh-campaign stop

This section supersedes the post-refresh lifecycle blocker above. The Custodian
lifecycle blocker is fixed and verified. One subsequent fresh R0 campaign crossed
Preview, Start, Worker, fixed acceptance, and a fresh accepting Auditor without any
recovery intervention, but encountered a distinct candidate-finalization blocker. Per
the mission stop rule, that campaign was not continued, retried, repaired, responded
to, or recovered. R0 PASS is not claimed.

### Lifecycle root cause

PID `102206` had been started by plain `nohup ... &` in `/init.scope`. The bootstrap
returned as soon as readiness matched and retained no durable service identity.
`nohup` changes only SIGHUP handling; it does not move the backend out of a launch
caller's scope or protect it from scope/cgroup cleanup with SIGTERM/SIGKILL. The
backend's normal `serve_forever()` path has no idle shutdown and its log contained no
application exception, so the post-readiness loss was external process containment,
not a Custodian request or timeout decision.

A differential real-host reproduction established the boundary:

- the exact Windows `powershell.exe -> wsl.exe --exec /bin/sh -> bootstrap` command
  started legacy PID `106200`, parented through WSL `/init`, with session/PGID
  `106189`, cgroup `/init.scope`, and no user-service unit;
- an unprivileged transient `systemd --user` probe survived the complete Windows
  caller exit in its own cgroup; and
- the original stopped attempt had invoked the same bootstrap directly through the
  transient command runner, whose scope cleanup was therefore able to terminate the
  background process despite `nohup`.

The product defect was reliance on caller-dependent daemonization. It also contained
a separate unsafe reuse primitive: a stale readiness PID plus a command-line substring
could authorize `kill -TERM`, without a durable process identity.

### Narrow lifecycle fix

Only the launcher lifecycle was changed:

- `scripts/custodian-bootstrap.sh` serializes readiness/replacement under a private
  `flock`, reuses a matching healthy backend before replacement, and no longer reads
  or signals a PID from stale readiness;
- `custodian_lifecycle.py` derives one deterministic user-service unit from the
  canonical application-data root, verifies the exact unit description before any
  stop, fails closed on ambiguity, and launches with `/usr/bin/systemd-run --user`;
- the transient service uses `Type=exec`, `KillMode=control-group`, bounded TERM/KILL
  stop policy, `UMask=0077`, `NoNewPrivileges=yes`, explicit managed environment, and
  append-only backend-log routing; and
- a normal matching relaunch remains a health-based reuse, while a changed runtime
  replaces only that verified service unit. There is no PID-reuse signal path.

Focused regression coverage is in `tests/test_custodian_lifecycle.py`, with the two
existing launcher contract assertions updated in `tests/test_windows_launcher.py`.
The real-host integration test launches a loopback health backend, lets the launcher
process exit, verifies health, replaces the same verified unit, proves its PID changed,
and proves a sentinel PID written into stale readiness remains alive.

Validation results:

```text
Focused lifecycle suite on real WSL host: 5 passed
Windows launcher suite on real WSL host: 9 passed, 4 qualification-only skipped
Adjacent Custodian/launcher/managed-Codex/prelaunch/campaign suite:
  104 passed, 5 qualification-only skipped
Ruff on lifecycle source/tests and launcher tests: passed
mypy on lifecycle source/tests: passed
POSIX shell syntax and Python byte compilation: passed
git diff --check: passed
```

### Real launcher persistence and reuse

The identity-verified legacy PID `106200` was terminated for the requested clean
restart. The fixed inner product launch installed and served source digest
`159220ac09c4d498d28906aac7564681595edcb67592d912020b0310d7f8214e` as PID
`109050` in:

```text
research-supervisor-custodian-117f321c9209aa9f67e5c907369fe82a00ea21b41026282ba87fec9a3c7aa453.service
InvocationID=922655851b6544a0914ae2d20c6b5b90
ControlGroup=/user.slice/user-1000.slice/user@1000.service/app.slice/research-supervisor-custodian-117f321c9209aa9f67e5c907369fe82a00ea21b41026282ba87fec9a3c7aa453.service
```

A Windows-side authenticated session made three recorded `/api/health` and
`/api/campaigns` pairs over `26.594` seconds. Checks completed at elapsed `2.219`,
`14.417`, and `26.593` seconds; every health result was ready, every campaigns request
succeeded, and PID, invocation ID, cgroup, readiness instance, and active/running state
remained unchanged. A subsequent normal bootstrap with requested readiness instance
`ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff` emitted
`RAS_LAUNCH_READY`; its evidence records `backend_reused: true`, observed the original
`eeee...` instance, and retained PID `109050` and the same invocation ID.

The noninteractive WScript/UNC harness returned the launcher's generic WSL error and
wrote no random-instance launcher evidence; successful top-level VBS/browser opening
is therefore not claimed from that harness. The exact Windows
`powershell.exe -> wsl.exe --exec` backend command used by the launcher did pass and is
the boundary relevant to the Custodian lifetime. This distinction does not change the
stop result below: the fresh campaign was created only after the backend command,
persistence window, authenticated campaigns calls, and reuse check had passed.

### One fresh R0 campaign

The source smoke repository was clean at commit
`d20270ae55489e6cc19a73d62098c71fefd7bd6e`, tree
`2222b64080fe530482fb2e6b2c3bdb800e9cd0f2`. One malformed HTTP Preview request was
rejected with status 400 before a preview draft or campaign existed because the
operator harness lost its `initial_task` field during PowerShell quoting. No Start,
campaign, model, repository mutation, or recovery resulted from that rejected request.

The one successful Preview was `preview-b8f1ac8f16f98467646e177c`. It reported every
environment predicate ready, baseline `d20270ae5548`, automatic qualified acceptance,
zero repair rounds, and exactly these editable paths:

```text
src/message.py
tests/test_message.py
```

The single Start created `campaign-0aa5c56e37022c1e772099c9`, input-bundle SHA-256
`fe01564673e2d56406b8a1326d40bd39be7d472a1b5a83bde60507a0b5ef3adf`, and
launch-intent SHA-256
`80c834b3b7d51176670b81bbccb8290a2e1b305361f2132d21fa7b478d3e1893`.
There was no Continue, response, recovery, model retry, Worker repair, or repeated
Auditor round.

The managed Worker ran `/usr/bin/codex` in
`ras-codex-6dd06dd39f04725e25696abaa7005b29.service` under
`systemd_user_cgroup_v2`. Its termination evidence is `phase=reaped`, return code 0,
`containment_closed=true`, `cgroup_empty=true`, and `owned_process_group_empty=true`.
The exact qualified diff contains only:

```diff
--- a/src/message.py
+++ b/src/message.py
@@
 def message() -> str:
     return "hello"
+
+
+def version() -> str:
+    return "r0"
--- a/tests/test_message.py
+++ b/tests/test_message.py
@@
-from src.message import message
+from src.message import message, version
@@
 def test_message() -> None:
     assert message() == "hello"
+
+
+def test_version() -> None:
+    assert version() == "r0"
```

Git scope evidence is compliant with exactly those two paths, no scope findings, and
patch SHA-256 `ad9aef615fc64435e46a7f720e9ca4a4773f51ba1454cefd665537201cf2fab8`.
The engine-owned acceptance command was:

```text
/usr/bin/python3 .research-supervisor/acceptance.py python_bare
```

It exited 0 in `0.03981` seconds and reported `2 bare Python test(s) passed`. The one
fresh Auditor ran read-only in
`ras-codex-da30201768c671500a76964ceccaa14d.service`, exited 0, was reaped with an empty
closed cgroup, and returned `verdict=pass`, `scope_compliant=true`,
`contract_satisfied=true`, no findings, and no human questions. The persistent
Supervisor then accepted `finish`, citing the exact two paths and the passing
`repository-acceptance` result.

### First distinct new blocker

After the task had completed, acceptance had passed, the fresh Auditor had accepted,
and Supervisor had recorded `finish`, final candidate processing failed closed. The
authoritative `qualified-failure-v1.json` contains exactly:

```json
{"error": "QualifiedCampaignStateError", "message": "candidate source provenance is invalid"}
```

The campaign never produced verified completion. Its task result is completed and its
complete model terminal exists, but the campaign journal stops at sequence 9 in
`running`; the operator projection remains `running / Current task passed its qualified
checks`, and `completion_verified` is false. The Custodian runner is already
`runner_operation=idle` with `runner_pid=null`.

This is distinct from the Custodian lifecycle blocker. It appeared after all required
model and acceptance work and before verified candidate completion. Per instruction,
no diagnosis-driven mutation, repair, retry, Continue, response, recovery, second
campaign, PA-5D, Attempt 005, sudo, commit, or push was performed.

Final containment found the Custodian still healthy as PID `109050`, no live managed
`/usr/bin/codex`, no actual qualified runner, no zombie, and no `ras-codex-*` user
unit. All five model-process termination records report reaped processes and empty,
closed systemd cgroups. The source smoke repository remains clean at its original
commit; the exact two-file result remains only in the Core-prepared campaign workspace.

R0 PASS is therefore not claimed. The lifecycle mission itself passes; the overall
fresh gate stops on the new candidate-source-provenance blocker.

### Token usage

The campaign and task ledgers are complete, have no incomplete receipt IDs, and bind
five exact receipt IDs across three sessions and five turns. Values below are the
authoritative `turn.completed.usage` aggregates. Cached input and reasoning output are
submetrics and are not added again; combined is exactly input plus output.

| Session | Input | Cached input | Output | Reasoning output | Combined |
|---|---:|---:|---:|---:|---:|
| Supervisor/other (3 turns) | 49,096 | 29,184 | 859 | 266 | 49,955 |
| Worker `worker-r000` | 89,921 | 71,424 | 1,234 | 274 | 91,155 |
| Fresh Auditor `auditor-r000` | 35,009 | 15,104 | 1,044 | 169 | 36,053 |
| Fresh campaign total | 174,026 | 115,712 | 3,137 | 709 | 177,163 |

The ledger's `repairs_retries` attribution is a subset of the Supervisor/other row,
not an additional total: two later turns in the persistent Supervisor session account
for input `35,292`, cached input `29,184`, output `486`, reasoning output `127`, and
combined `35,778`. Workflow `repair_round` remained exactly zero, Worker automatic
retry/repair was false, and there was only one fresh Auditor round. The enclosing
operator turn's final runtime usage remains unavailable until its post-completion
runtime receipt and is not estimated here.

## Finalization provenance closure

### Exact durable mismatch

The failed finalization did not contain a source identity, path, commit, tree, or
binding substitution. The complete durable chain agrees:

| Evidence | Repository/path | Commit | Tree/binding |
|---|---|---|---|
| Selected source | `ras-r0-smoke-69ca11217212`; `/home/inaeyk/researchrepo/ras-r0-smoke` | `d20270ae55489e6cc19a73d62098c71fefd7bd6e` | `2222b64080fe530482fb2e6b2c3bdb800e9cd0f2` |
| Core sanitized snapshot | `/var/lib/research-supervisor-core/snapshots/workspaces/campaign-0aa5c56e37022c1e772099c9/repository` | `42402b9135a09c405d27beab5e3f0f1f10a7b15b` | `925613f44edccdb28bc2818fd4895ac758621b50` |
| Frozen bundle | same prepared path | `42402b9135a09c405d27beab5e3f0f1f10a7b15b` | tree `925613f44edccdb28bc2818fd4895ac758621b50`; bundle `fe01564673e2d56406b8a1326d40bd39be7d472a1b5a83bde60507a0b5ef3adf` |
| Signed workspace binding | same prepared path | `42402b9135a09c405d27beab5e3f0f1f10a7b15b` | same tree and bundle SHA-256 |
| Frozen campaign source provenance | repository ID above | `42402b9135a09c405d27beab5e3f0f1f10a7b15b` | `925613f44edccdb28bc2818fd4895ac758621b50` |
| Stage-2 baseline and live Git object | same prepared path | baseline and `HEAD` both `42402b9135a09c405d27beab5e3f0f1f10a7b15b` | `42402...^{tree}` resolves to `925613f44edccdb28bc2818fd4895ac758621b50` |

The sanitized commit has parent `d20270ae...` and records the Core-owned snapshot
construction. The finalizer correctly compares against that prepared baseline, not
against the pre-snapshot source tree.

The mismatch was instead between the cross-UID workspace and the finalizer's Git
process configuration. Core owns the prepared repository as `nobody:nogroup`; the
qualified finalizer runs as the ordinary operator. The runner establishes a
command-scoped `safe.directory` qualification for that already verified workspace,
but `candidate_export._git_value()` created a fresh environment and omitted it. Its
exact `git rev-parse 42402...^{tree}` therefore exited 128 with Git's `dubious
ownership` rejection, and the generic wrapper emitted `candidate source provenance is
invalid`. Adding the same path qualification made the unchanged object resolve to the
expected tree immediately.

### Narrow fix and local proof

`candidate_export._git_value()` now canonicalizes the supplied workspace to an
absolute path and invokes Git with one path-scoped
`-c safe.directory=<qualified-workspace>` option. No campaign state, provenance value,
Git object, receipt, acceptance authority, model adapter, or recovery policy changed.

Focused coverage proves both sides of the defect:

- `test_candidate_source_provenance_qualifies_cross_uid_workspace` forces Git's
  different-owner behavior, proves the unqualified command is rejected for dubious
  ownership, and proves the path-qualified provenance read returns the exact tree;
- `test_provenance_failure_recovers_existing_terminal_task_without_model_work`
  reproduces the exact finalization exception after the terminal Worker/Auditor result,
  then proves resume publishes the candidate without another model call.

Before the real recovery request, the patched code loaded this existing frozen
manifest read-only and asserted equality across the bundle, workspace path, signed
binding payload, campaign provenance, Stage-2 baseline, live `HEAD`, and resolved Git
tree. The observed commit was `42402b9135a09c405d27beab5e3f0f1f10a7b15b`
and the observed tree was `925613f44edccdb28bc2818fd4895ac758621b50`.

Validation before recovery:

```text
Focused exact-mismatch tests: 2 passed
Candidate publication/recovery regression subset: 13 passed
Adjacent replay and qualified-projection regressions: 4 passed
Ruff on changed source/test: passed
Configured strict mypy: 95 source files passed
git diff --check: passed
```

An exploratory run of the entire campaign/evaluation split module produced 45 passes
and three offline-evaluator process failures. All three were outside the changed path
and shared the current tool sandbox's nested-bubblewrap restriction; a direct probe
reported `bwrap: loopback: Failed to create NETLINK_ROUTE socket: Operation not
permitted`. The focused candidate/recovery suite is clean, and the real qualified
runner executed outside that diagnostic tool sandbox.

### Existing campaign recovery and verified completion

The normal no-sudo bootstrap installed source digest
`bed009a2bb487fc5fe084ab1104beab133f9a1bca8005b68c200d755a9e0b171` and
replaced only the verified Custodian user service. One authenticated Continue was then
issued for `campaign-0aa5c56e37022c1e772099c9`. No new campaign, Preview, Start,
response, repair, test, Worker, Auditor, or Supervisor model action occurred.

Recovery appended only reconciliation/finalization journal evidence: the known Worker
session binding at sequence 10, `visible_task_completed` at sequence 11, and
`candidate_exported` at sequence 12. The model-terminal and task-report SHA-256 values
remain exactly `64b213d1f51b6a80c7716d97903cba65c5822e91cab90eeea924bbe89fa98a84`
and `a6f1de226dd9042b768d2aaa9a2bbfd5a9586c01e98ba0b3d6b5ef79fd14bd6c`.
There are still exactly three Supervisor actions, one Worker action, one acceptance
action, one Auditor action, five model turns, and five usage receipts.

The campaign is now `completed` with `completion_verified=true`. Its immutable final
candidate has candidate identity
`080c6049edc1227a5ecb3af4ba99bfd7e57be5240a9b17d1f420cee0d9d15658`,
manifest-file SHA-256
`2d39dcb7b537cc6d0d2184d99f67943439c0accd37f1ba8c607fc3aea9f17589`,
patch SHA-256 `ad9aef615fc64435e46a7f720e9ca4a4773f51ba1454cefd665537201cf2fab8`,
and exactly these changed paths:

```text
src/message.py
tests/test_message.py
```

The candidate bytes match the Core-prepared workspace exactly and contain `version()`
returning `"r0"` plus the corresponding passing test. The selected source repository
remains clean at its original commit, as required by candidate publication rather than
direct source mutation. The historical `qualified-failure-v1.json` remains preserved
as evidence of the stopped attempt; verified projection and candidate authority now
come from the completed journal and final candidate.

### Ledger and containment closure

All five receipts were reloaded and checked against their retained `events.jsonl`, then
re-aggregated. The recomputed campaign and task ledgers exactly equal the durable
ledgers. Their SHA-256 values remain unchanged:

```text
campaign-token-ledger.json  5920ca6b86166c88d87ed9d104514b616bd5ee837a41a2dc0dee8f565e32f486
task token ledger           81366ebec1b16e2633277fe8942785488e857b770323294129e3bcff2263c6c3
```

The ledger remains complete with no incomplete receipt IDs and the original exact
totals: input `174,026`, output `3,137`, combined `177,163`, cached input `115,712`,
and reasoning output `709`. No recovery/finalization model session was launched, so
there is no new campaign usage receipt or additional campaign token usage.

Final host inspection found `runner_operation=idle`, `runner_pid=null`, no managed
`/usr/bin/codex`, no qualified runner, no zombie process, and no `ras-codex-*` or
qualified-runner transient user unit. The healthy Custodian remains PID `124331`,
InvocationID `4e3531d98c334975b4677093a5e664c6`, in its verified deterministic
user-service cgroup and bound to the patched source digest above.

R0 functional restoration is complete. The enclosing operator model turn's runtime
usage is unavailable until its authoritative post-completion `turn.completed` event;
it is not estimated.
