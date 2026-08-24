# R0-SETUP-1R4 final deterministic qualification independent audit

Date: 2026-08-24

Mode: Independent Auditor

Repository: `/home/inaeyk/researchrepo/ras-context-integration`

Branch: `feature/context-economy-runtime-integration`

Baseline: `df59e2818c3519a5ba7dab69dd067b91b202e936`

## Verdict

**PASS**

- **BLOCKER A — Physics Auditor identity: CLOSED**
- **BLOCKER B — privileged Python trust root: CLOSED**
- **BLOCKER C — shell -> main production-wired qualification: CLOSED**
- **May R0-SETUP-1R4 proceed to real-host qualification? YES**

The deterministic candidate closes the R3 test splice. The actual installed simulated
protected shell payload starts an isolated `/usr/bin/python3 -I -B` subprocess under a
minimal environment and protected working directory; the absolute installed entrypoint
then imports the installed protected module and calls `managed_codex_installer.main()`
inside that subprocess. `main()` invokes the real installer orchestration, including the
real staged-version probe, pending-receipt lifecycle, executable replacement, protected
receipt write, and final receipt-backed runtime verification. Pytest subsequently reads
and verifies the executable and receipt made by that subprocess. It does not call the
installer implementation to bridge the end-to-end boundary.

The private qualification dependency seam is not privileged production authority. It is
not selectable by the production shell command, public CLI, request/campaign/schema
data, `CODEX_HOME`, `PATH`, or ordinary environment data. Both the protected entrypoint
and installer `main()` reject qualification under effective UID 0, before qualification
layout construction or filesystem mutation. The privileged path always constructs the
fixed production layout.

This PASS means only that the deterministic R0-SETUP-1R4 candidate may be frozen and
proceed to protected-distribution / real-host administrator qualification. It is not an
R0 PASS or release PASS, does not unblock Attempt 005, and does not resume PA-5D or
PA-5D0.

## Findings by severity

### CRITICAL

None.

### MAJOR

None.

### MINOR

None.

The unavailable token receipt and validation-harness observations recorded below are
accounting/qualification observations, not product findings and do not change the
security verdict.

## 1. Frozen candidate

The initial audit freeze, before this report was created, produced:

```text
git rev-parse HEAD
df59e2818c3519a5ba7dab69dd067b91b202e936

git branch --show-current
feature/context-economy-runtime-integration

git diff --stat
27 files changed, 1017 insertions(+), 248 deletions(-)

git diff --check
PASS
```

The tracked stat does not include untracked candidate files. `HEAD` remained the exact
stated baseline throughout the audit, and all candidate changes remained uncommitted.

Tracked modified files at freeze:

```text
README.md
README_FIRST.md
bootstrap.sh
docs/campaign_custodian.md
launch-research-supervisor.ps1
pyproject.toml
scripts/custodian-bootstrap.sh
scripts/install-core-authority-service.sh
src/research_automation_supervisor/custodian.py
src/research_automation_supervisor/custodian_bootstrap.py
src/research_automation_supervisor/custodian_server.py
src/research_automation_supervisor/live_shadow_isolation.py
src/research_automation_supervisor/physics_auditor_execution.py
src/research_automation_supervisor/physics_auditor_models.py
src/research_automation_supervisor/qualified_campaign.py
src/research_automation_supervisor/qualified_runner.py
src/research_automation_supervisor/replay_campaign_engine.py
src/research_automation_supervisor/secure_cli.py
src/research_automation_supervisor/workflow_engine.py
tests/test_custodian.py
tests/test_custodian_server.py
tests/test_pa5c4_transactional_core.py
tests/test_physics_auditor_execution.py
tests/test_physics_benchmark_blindness.py
tests/test_physics_benchmark_scoring.py
tests/test_windows_launcher.py
tests/test_workflow_engine.py
```

Untracked files at freeze:

```text
docs/reports/r0/r0-setup-1-independent-audit.md
docs/reports/r0/r0-setup-1-managed-codex-first-run-closure.md
docs/reports/r0/r0-setup-1r-independent-audit.md
docs/reports/r0/r0-setup-1r-managed-codex-security-closure.md
docs/reports/r0/r0-setup-1r2-final-trust-path-repair.md
docs/reports/r0/r0-setup-1r2-independent-audit.md
docs/reports/r0/r0-setup-1r3-independent-audit.md
docs/reports/r0/r0-setup-1r3-privileged-python-isolation-closure.md
docs/reports/r0/r0-setup-1r4-production-installer-main-boundary-closure.md
scripts/install-managed-codex.sh
scripts/install-research-supervisor.sh
scripts/prepare-managed-codex-home.py
scripts/protected-managed-codex-entry.py
scripts/run-protected-python.sh
src/research_automation_supervisor/managed_codex.py
src/research_automation_supervisor/managed_codex_installer.py
src/research_automation_supervisor/protected_release.py
tests/test_managed_codex_security.py
```

The complete R4-relevant shell helper, protected launcher, entrypoint, installer module,
qualification fixture, successful production-wired test, missing-entrypoint test, and
root-rejection test were inspected. Surrounding managed executable/home, qualified
campaign/runner, workflow Worker/Auditor, Physics fresh/retry/resume, and protected
release code was retraced far enough to check the previously closed R1-R3 boundaries.
No unexpected non-ignored generated or privileged artifact appeared in the target
worktree. Validation scratch trees were outside the worktree and were removed after the
results were recorded.

The repository has no `.codegraph/` directory, so CodeGraph was not used.

## 2. Exact successful shell -> Python -> entrypoint -> main chain

The successful deterministic test chain is:

```text
tests/test_managed_codex_security.py
  ::test_production_wired_protected_release_to_every_launch_preparation
  -> install_approved_release(simulated protected release)
  -> create only fixed qualification destination directories
  -> subprocess.run([
       /bin/sh,
       <installed-release>/scripts/install-managed-codex.sh,
       --qualification-import-probe
     ], hostile caller cwd/environment)
  -> installed install-managed-codex.sh
  -> exec /bin/sh <installed-release>/scripts/run-protected-python.sh
       --qualification-import-probe
  -> validate installed launcher, release ancestry, fixed system Python, and absolute
     installed entrypoint
  -> cd <installed-release>
  -> exec /usr/bin/env -i
       PATH=/usr/sbin:/usr/bin:/sbin:/bin
       LANG=C.UTF-8 LC_ALL=C.UTF-8
       RAS_PROTECTED_IMPORT_QUALIFICATION=1
       /usr/bin/python3 -I -B
       <installed-release>/scripts/protected-managed-codex-entry.py
       --qualification-import-probe
  -> validate isolated flags, minimal environment, protected CWD/metadata, and import root
  -> import installed managed_codex and managed_codex_installer
  -> managed_codex_installer.main(
       ["install"], _qualification_release_root=<installed-release>
     )
  -> _qualification_installer_layout(<installed-release>)
  -> install_managed_codex(layout, version_probe=probe_staged_codex_version)
  -> verify protected release tree and approval
  -> stage the exact approved ELF artifact
  -> execute the real staged-version probe
  -> write pending receipt
  -> replace the managed executable
  -> write completed protected receipt
  -> remove pending receipt
  -> verify_managed_codex_installation(layout.installation)
  -> emit structured installer result only after success
  -> subprocess exits 0
  -> pytest reads and verifies that subprocess-created executable and receipt
  -> runtime verifier
  -> readiness
  -> Sign in preparation
  -> qualified replay Worker preparation
  -> ordinary Auditor preparation
  -> qualified-runner environment sealing
  -> Physics Auditor fresh preparation
  -> Physics Auditor retry preparation
  -> Physics Auditor resume preparation
```

`scripts/install-managed-codex.sh:22-38` executes the real installed shell payload's
strict non-root qualification branch. It resolves its own installed location, rejects a
symlink or mismatched payload, derives the release root from that installed path, and
execs the installed `run-protected-python.sh`. The root production branch remains fixed
to `/opt/research-supervisor-release`, accepts no arguments, verifies the external
release authority, and invokes the same launcher with `install`.

`scripts/run-protected-python.sh:22-129` uses `/usr/bin/python3` by default, validates
that it resolves to `/usr/bin/python3.X`, validates `/usr` and `/usr/bin`, selects the
absolute entrypoint derived from the installed release, changes to that release root,
and uses `/usr/bin/env -i` plus `-I -B`. The qualification command has one additional
fixed marker needed by the protected entrypoint. The privileged production branch does
not read the test-only interpreter override and emits no qualification marker.

`scripts/protected-managed-codex-entry.py:38-123` validates the entrypoint, effective
UID, CWD, release/source/package metadata, isolation flags, inherited `sys.path`, and
exact environment before adding the installed protected `src`. Its qualification branch
prints import evidence and then returns the result of `installer_module.main(...)`; it
does not return before `main()` as R3 did.

`managed_codex_installer.main()` at lines 600-677 chooses the qualification layout only
when the private keyword is supplied, then calls the same `install_managed_codex()` used
by production with the real `probe_staged_codex_version`. The installer result's second
JSON line is printed only after `install_managed_codex()` has completed its final runtime
verification.

The test's `_managed_layout_from_protected_release()` creates prerequisite directories
only. It does not write an executable, pending receipt, or completed receipt. Those
objects are absent before the subprocess and are produced by the subprocess installer.
After the shell boundary the end-to-end test contains no call to
`install_managed_codex()` or an equivalent installer. Direct installer calls elsewhere
in the file are separate lifecycle/unit and bounded Physics tests; they do not bridge
this path.

Mandatory Blocker C answers:

1. Actual production shell payload executes in the subprocess: **YES**.
2. Same protected Python command shape as supported production (`env -i`, fixed system
   Python, `-I -B`, protected CWD, absolute entrypoint): **YES**.
3. Approved absolute installed entrypoint executes: **YES**.
4. `managed_codex_installer.main()` executes inside that subprocess: **YES**.
5. `main()` calls real production installer orchestration, not an import-only branch:
   **YES**.
6. The executable and receipt later consumed by pytest were produced by that subprocess:
   **YES**.
7. A direct pytest installer bridge has been removed from the end-to-end path: **YES**.
8. Pytest does not reconstruct equivalent installed state independently: **YES**. It
   prepares only destination directories, then reads/verifies subprocess state.
9. The subprocess result contains meaningful installer evidence: **YES**. It binds the
   installed executable, release ID, version, digest, disposition, and exact receipt.
10. Qualification fails if the subprocess exits before orchestration completes: **YES**.
    The parser requires exit 0 and exactly two result lines, while the durable verifier
    independently requires the completed executable/receipt pair and no pending receipt.

**BLOCKER C — shell -> main production-wired qualification: CLOSED.**

## 3. Qualification backend authority

The internal seam is `_qualification_release_root`, an underscore-prefixed keyword on
the protected installer `main()`. The only product call that supplies it is the already
authenticated non-root qualification branch of the protected entrypoint.

Authority checks:

1. The supported privileged shell invocation cannot select it. The root
   `install-managed-codex.sh` branch rejects arguments and the root launcher accepts only
   `install`, `verify`, or `bind-home OPERATOR`.
2. No production/public CLI argument selects it. There is no managed-installer console
   entry point in `pyproject.toml`, and the root shell rejects
   `--qualification-import-probe`.
3. No request, campaign, Pydantic schema, or persisted field references the selector.
4. No ordinary environment variable selects the backend. The entrypoint marker is
   inserted by the non-root launcher after `env -i`; ambient marker data is discarded.
5. `CODEX_HOME` does not participate in layout or backend selection.
6. `PATH` is overwritten, and Python is an absolute fixed path.
7. There is no release-root, destination, receipt, or backend environment override.
   The non-root test release root is derived from the resolved installed shell/entrypoint
   path, and the state is its fixed sibling `.managed-codex-main-qualification/system`.
8. No `PYTEST_CURRENT_TEST` or other pytest ambient condition enables the seam.
9. The protected entrypoint rejects qualification at effective UID 0, and installer
   `main()` independently rejects a non-`None` qualification keyword at effective UID 0.
10. `main()` performs that root rejection before argument-dependent layout construction;
    the five-test slice proves no qualification state path is created.
11. A privileged call without the private keyword always uses
    `production_installer_layout()`.
12. The qualification layout cannot redirect `/usr/bin/codex`, the protected receipt,
    protected release authority, privileged interpreter, or canonical `CODEX_HOME` in
    production. Its writable objects are unprivileged test-state paths only.
13. No public/user-facing API, command help, schema, or documentation exposes it as an
    operator feature.

The non-root shell test mode is deliberately explicit and can mutate only an
unprivileged sibling test tree whose prerequisite directories the test owns. It cannot
gain root ownership or write the fixed production authorities. This is a deterministic
dependency seam beneath the production `main()` boundary, not a new production
authority override.

## 4. Missing-entrypoint fail-closed result

`test_protected_shell_python_boundary_fails_closed_for_missing_entrypoint` uses the same
installed `install-managed-codex.sh --qualification-import-probe` subprocess. After the
approved release is installed, it removes the installed absolute
`scripts/protected-managed-codex-entry.py`, then supplies:

- a hostile CWD alternate entrypoint;
- a `PYTHONPATH` shadow installer;
- a hostile `PYTHONHOME`; and
- a PATH-selected fake `python3` that would create a marker.

The actual launcher exits 2 with `Protected Python application entrypoint is missing or
unsafe.` before Python begins. Standard output is empty; the fallback marker is absent;
and the managed executable, completed receipt, and pending receipt are all absent.

The shell has no fallback to checkout source, CWD, PATH, `python3 -m`, another
entrypoint, or a caller Python environment. The negative case is a real shell
subprocess, not a direct call to a Python existence-check helper.

## 5. Import isolation through installer main

The extended hostile test reaches the installed entrypoint, imports, installer main,
real installer orchestration, staged version subprocess, receipt write, and final
runtime verifier. It supplies and defeats:

- hostile and ordinary caller CWDs;
- a top-level `research_automation_supervisor` shadow package;
- a shadow installed-module name;
- shadow transitive `hashlib` modules in CWD and `PYTHONPATH`;
- `PYTHONPATH` and `PYTHONHOME`;
- `PYTHONUSERBASE` plus a user-site `.pth` import attempt;
- `PYTHONSTARTUP`, `PYTHONINSPECT`, `PYTHONWARNINGS`, `PYTHONPYCACHEPREFIX`, and
  `PYTHONBREAKPOINT`;
- shell startup injection through `BASH_ENV` and `ENV`;
- fake PATH-selected `python3`;
- hostile `HOME`, `XDG_CONFIG_HOME`, locale, and normal arbitrary CWD.

All hostile markers remain absent. The observed protected process reports:

```text
isolated = true
safe_path = true
user_site_disabled = true
cwd = <installed simulated protected release>
environment = [LANG, LC_ALL, PATH, RAS_PROTECTED_IMPORT_QUALIFICATION]
managed_codex = <installed release>/src/research_automation_supervisor/managed_codex.py
managed_codex_installer = <installed release>/src/research_automation_supervisor/managed_codex_installer.py
```

The targeted search over the protected entrypoint and complete installer import chain
found one application import-path change:

```text
scripts/protected-managed-codex-entry.py:92
sys.path.insert(0, str(source_root))
```

That root is derived from the resolved absolute protected entrypoint and is validated
before insertion. There is no `sys.path.append`, `importlib` path loader, `runpy`,
`site.addsitedir`, dynamic external-source `exec`/`eval`, environment-derived import
root, or mutable-checkout reinsertion in the chain. Application imports are the
protected package/modules; other dependencies are standard library plus the system
distribution `packaging.version` import already identified in R3.

`-I` implies environment isolation, safe path, and disabled user site. The fixed minimal
environment prevents caller Python variables and user-site/`.pth` injection. Actual
system-site, standard-library, `.pth`/`sitecustomize`, and distribution `packaging`
provenance remain explicit real-host assumptions; they are not ordinary caller
authority and are not claimed by this deterministic test.

**BLOCKER B — privileged Python trust root: CLOSED.**

## 6. Physics Auditor identity bounded regression

The production fresh path and safe-resume continuation both use
`_invoke_qualified_codex` when no explicit test invoker is injected. Immediately before
launch, `_continue_action()` calls `resolve_qualified_physics_auditor_codex()`, which:

- verifies the protected receipt-backed managed executable;
- verifies the canonical managed home;
- rejects a conflicting legacy request/config executable pin;
- overwrites `PATH`, `HOME`, and `CODEX_HOME`; and
- passes the exact verified executable and home to the real adapter.

The legacy arbitrary executable selector is reachable only through an explicitly
injected non-production test invoker. PATH, an environment executable override, an
arbitrary absolute request pin, and `/usr/bin/codex` named without a matching protected
receipt cannot become schema-v2 production authority. Missing/malformed receipts,
digest substitution, missing/unsafe home state, and conflicting pins fail before model
launch.

`resume_physics_auditor()` re-enters the same `_continue_action()` production
preparation when the durable phase is safe to continue; ambiguous launched/running
states fail closed. The bounded retry/resume tests and the production-wired downstream
preparation all retain the same verified executable/home pair.

**BLOCKER A — Physics Auditor identity: CLOSED.**

## 7. Managed CODEX_HOME bounded regression

The production home remains product-owned and derived from passwd state plus the
root-protected operator/home authority receipt. Ambient `CODEX_HOME` is intentionally
ignored. Normal preparation uses `verified_managed_codex_home()` and is verification
only; it does not silently initialize or repair missing/tampered bindings.

Sign in, qualified replay Worker, ordinary Auditor, qualified runner, and Physics
Auditor all use the same canonical home. Missing authority, malformed/missing binding,
wrong mode, hardlink/symlink/content substitution, and unsafe ancestry fail closed.

Credentials remain in the private managed home. The production-wired preparation test
places a credential marker in `auth.json` and proves it does not enter readiness data,
authentication command/environment serialization, Physics environments, or sealed
environment evidence. Campaign and export roots/source allowlists do not include the
credential file. No regression evidence was found.

## 8. Production-wired test quality and simulation boundary

Real production code exercised deterministically:

- protected-release install/verification logic;
- installed managed-Codex shell payload;
- installed protected Python launcher;
- fixed interpreter command shape and isolated Python flags;
- installed absolute protected entrypoint;
- installed protected module imports;
- `managed_codex_installer.main()`;
- real installer orchestration and real staged executable version probe;
- pending/completed receipt lifecycle and final runtime verifier;
- readiness;
- Sign in preparation;
- Worker and ordinary Auditor service preparation/identity verifier;
- qualified-runner environment seal;
- Physics fresh/retry/resume preparation.

Intentionally simulated below that boundary:

- externally protected release distribution and privileged release ownership;
- prerequisite destination-directory creation;
- root-owned filesystem mutations;
- actual `/opt` release provenance;
- actual `/usr/bin/codex` replacement;
- actual `/etc` receipt installation and ownership.

The simulation supplies unprivileged layout/path/ownership dependencies only. It does
not replace the shell, interpreter, entrypoint, `main()`, installer lifecycle, receipt
rendering, or runtime verifier. Source-string assertions elsewhere are supplemental and
were not counted as primary Blocker C evidence.

## 9. Validation

Exact tools:

```text
/home/inaeyk/researchrepo/ras-regression-venv/bin/python  3.14.4
/usr/bin/python3                                          3.14.4
/home/inaeyk/researchrepo/ras-regression-venv/bin/pytest  9.1.1
/home/inaeyk/researchrepo/ras-regression-venv/bin/ruff    0.16.4
/home/inaeyk/researchrepo/ras-regression-venv/bin/mypy    2.3.1
/usr/bin/bwrap                                            0.11.1
/usr/bin/bash
/usr/bin/sh
/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe
                                                          5.1.26100.9168
```

Final results:

- R4 main-boundary/hostile/missing-authority/missing-entrypoint/root-rejection slice:
  **5 passed in 1.31 s**.
- Complete managed-Codex security file: **38 passed, 1 skipped in 1.90 s**. The
  qualification-only real-root skip is not a pass.
- Physics real-adapter/scoring slice: **24 passed in 5.13 s** in the host-capable
  process environment.
- Broad 19-file family with only the separately verified inherited inventory test
  deselected: **390 passed, 8 skipped, 1 deselected in 351.63 s**.
- Ruff `0.16.4` over all 25 changed/new Python source and test files with `--no-cache`:
  **PASS**.
- Mypy `2.3.1 --strict --no-incremental` over all 16 changed/new production/script
  modules: **PASS**, no issues.
- Read-only built-in `compile()` over all 25 changed/new Python files: **PASS**.
- `/usr/bin/bash -n` and `/usr/bin/sh -n` over all six changed/new shell payloads:
  **PASS**.
- Windows PowerShell parser over `launch-research-supervisor.ps1`: **PASS**.
- Initial, pre-report final, and post-report `git diff --check`: **PASS**.

Harness observations, not product failures:

- The first restricted-process Physics run produced **16 passed, 8 failed** because
  bubblewrap namespace execution was denied. The exact host-capable rerun passed 24/24.
- An initial broad run encountered exhausted global `/tmp` inodes: pytest tmp-path setup
  cascaded, so that run was discarded. A second full run using an external pytest base
  produced 388 passes, eight skips, the inherited inventory failure, and two temporary
  harness failures; those two cases passed 2/2 when isolated away from `/tmp`.
- One attempted aggregate used an external path too long for AF_UNIX sockets and was
  stopped as invalid. The final short-path aggregate above is the authoritative broad
  result and includes both formerly affected tests.
- The first PowerShell parser harness over-escaped the UNC path. The corrected command
  parsed successfully.

No qualification skip is counted as a pass. No package was installed and no network,
root, service, protected host, or campaign operation was used.

## 10. Inherited inventory

The inventory case exited 1 and reproduced:

```text
inventory_sha256:
f8af9d25eb89712326248105eee732df8dc56a84e8bdcc5b793caea657dc998b

categories:
POST-SNAPSHOT 25
PRE-SNAPSHOT 4
QUALIFIED-SANDBOX-INTERNAL 11
UNCLASSIFIED 4

normalized signature:
7ac0707a865519a1c1f89ec957cbea162896ecb257b60236501e2f0448b7433c
```

The four normalized unclassified identities remain
`process_enforcement._run_systemctl`, `semantic_replay.execute_replay`,
`semantic_replay._git`, and `systemd_launch_helper.main`. The inventory hash,
categories, normalized identities, and normalized signature exactly match the supplied
inherited authority. This is **inherited/unrelated**. It was not suppressed,
rebaselined, or investigated further.

## 11. Real-host and distribution assertions still pending

This deterministic PASS does not establish:

- provenance of the externally protected release artifact;
- actual root ownership/modes of the protected release tree;
- actual `/usr/bin/python3`, standard-library, system-site, `.pth`/`sitecustomize`, and
  distribution dependency provenance/host assumptions;
- real root execution of the protected helper;
- real managed-Codex installation in `/usr/bin`;
- an actual protected installation receipt;
- actual reinstall/update/interruption and recovery behavior;
- a real canonical managed `CODEX_HOME`;
- real ChatGPT/Codex authentication using that home;
- Core service and two-UID behavior;
- the actual Windows -> WSL launcher; or
- credential containment on the real host.

These remain required protected-distribution / real-host administrator qualification
work. They are not automatic deterministic failures because the source and tests leave
them explicitly unclaimed.

## 12. Token usage

### ACCOUNTING OBSERVATION — authoritative counters unavailable

`CODEX_THREAD_ID` was `01a033f6-f428-7f91-8f7c-2999997e1c5c`. `CODEX_HOME` was unset;
the default persistent `/home/inaeyk/.codex/bin/codex-task` and durable ledger root were
present. Sixty-four `TaskUsageReceipt.json` files existed, but none matched this exact
thread identity. Receipts belonging to other tasks were not attributed to this audit.
No raw rollout transcript was read and no token count was estimated, reconstructed, or
derived. Final-output usage cannot be authoritative until this turn's
`turn.completed` event exists.

```text
input_tokens: unavailable
output_tokens: unavailable
combined_tokens: unavailable
cached_input_tokens: unavailable
cache_write_input_tokens: unavailable
reasoning_output_tokens: unavailable
```

Per-session/retry breakdown:

- Worker session: unavailable; the prior Worker report is not a matching runtime
  receipt.
- Auditor session (this audit): unavailable; no matching durable receipt.
- Supervisor/Custodian session: unavailable / not applicable; none was launched.
- Other/nested agent session: unavailable / not applicable; launching agents was
  prohibited and none was launched.
- Model retry, repair, or repeated-audit attribution: unavailable; no authoritative
  matching model-session receipt exists. Pytest/harness reruns are not model sessions
  and no token attribution is estimated for them.

Accounting absence does not alter the security verdict.

## Terminal disposition

**R0-SETUP-1R4 may proceed to protected-distribution / real-host administrator
qualification.**

No repair, commit, push, sudo operation, root installation, service change, network
access, real campaign, Attempt 005, PA-5D/PA-5D0 action, or follow-on qualification was
performed.
