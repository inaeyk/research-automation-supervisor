# R0-SETUP-1R4 — production installer main-boundary closure

Date: 2026-08-24

Repository: `/home/inaeyk/researchrepo/ras-context-integration`

Branch: `feature/context-economy-runtime-integration`

Baseline and current `HEAD`:
`df59e2818c3519a5ba7dab69dd067b91b202e936`

## Scope and candidate disposition

This qualification repair addresses only the remaining R3 Blocker C seam. It
does not redesign Physics Auditor identity, managed-Codex receipt/digest
authority, canonical managed `CODEX_HOME`, protected release authority,
privileged Python isolation, Core/Custodian/runtime behavior, or campaign
behavior.

Worker candidate dispositions:

- **BLOCKER A — Physics Auditor identity: CLOSED.** Bounded security and
  real-adapter/scoring regressions remained green.
- **BLOCKER B — privileged Python trust root: CLOSED.** The fixed interpreter,
  isolation flags, minimal environment, protected CWD, absolute entrypoint,
  and protected release imports are unchanged and remain exercised through
  the newly extended `main()` path.
- **BLOCKER C — production installer main boundary: CLOSED in the
  deterministic candidate.** One subprocess now crosses the actual installed
  shell payload, isolated protected Python, the absolute installed entrypoint,
  `managed_codex_installer.main()`, and real installer orchestration before
  pytest consumes the resulting installed identity and receipt.

These are Worker repair-candidate dispositions, not an independent-audit or
real-host verdict. **THIS IS NOT AN R0 PASS.**

No commit, push, `sudo`, package installation, network access, protected host
mutation, service mutation, real campaign, Attempt 005, PA-5D/PA-5D0 action,
Worker, Auditor, Supervisor/Custodian model session, or nested agent was
performed.

## Recorded starting state

Before editing:

```text
git rev-parse HEAD
df59e2818c3519a5ba7dab69dd067b91b202e936

git branch --show-current
feature/context-economy-runtime-integration

git diff --check
PASS

git diff --stat
27 files changed, 1017 insertions(+), 248 deletions(-)
```

The tracked stat excludes the existing untracked R0 reports, protected-release
payloads/modules, managed-Codex modules, and managed security test. The full
short status was recorded in the task transcript before editing. The working
tree was the stated uncommitted R0-SETUP-1 + R1 + R2 + R3 candidate and was not
reset or rebased.

Both required R3 reports were read in full before source edits:

- `docs/reports/r0/r0-setup-1r3-privileged-python-isolation-closure.md`
- `docs/reports/r0/r0-setup-1r3-independent-audit.md`

The repository has no `.codegraph/` directory, so compact targeted source
reads were used.

## Exact R3 qualification seam

The R3 splice was:

```text
tests/test_managed_codex_security.py::_invoke_protected_import_probe
  -> subprocess.run([
       /bin/sh,
       <installed simulated release>/scripts/install-managed-codex.sh,
       --qualification-import-probe
     ])
  -> installed install-managed-codex.sh
  -> installed run-protected-python.sh
  -> /usr/bin/env -i ... /usr/bin/python3 -I -B
  -> absolute installed protected-managed-codex-entry.py
  -> protected CWD/environment/flag checks
  -> protected managed_codex and managed_codex_installer imports
  -> print import metadata
  -> RETURN 0

pytest resumes
  -> construct a separate ManagedCodexInstallerLayout
  -> checkout-imported install_managed_codex(...)
  -> runtime verification and launch preparation
```

Specifically, the R3 entrypoint's qualification block printed JSON and returned
at old lines 101-120. The next statement, the production-only
`installer_module.main(sys.argv[1:])`, was never reached. The R3 production-wired
test then resumed at old lines 1213-1220 and directly called
`install_managed_codex()` in pytest. This was the prohibited cross-process,
cross-import-authority seam identified by the independent audit.

## Exact repair

The shell-to-entrypoint transition was retained. The installed protected
entrypoint's already authenticated non-root qualification branch now calls:

```python
installer_module.main(
    ["install"], _qualification_release_root=release_root
)
```

`managed_codex_installer.main()` now has one private internal dependency seam,
`_qualification_release_root`. When absent, its construction and behavior are
unchanged: effective UID must be root and `production_installer_layout()` fixes
the canonical `/opt`, `/usr/bin`, and `/etc` authorities. When present:

- effective UID 0 is rejected before layout construction;
- only the exact internal `install` operation is accepted;
- the layout is derived from the already validated installed release root;
- the unprivileged state is the fixed sibling
  `.managed-codex-main-qualification/system`;
- the caller cannot provide a destination, receipt path, release root,
  interpreter, or backend through shell arguments or environment;
- the real `probe_staged_codex_version`, `install_managed_codex`, protected
  receipt rendering, and final runtime verifier execute; and
- `main()` emits a safe structured result only after orchestration succeeds.

The deterministic release fixture approves a real ELF executable copied from
the fixed `/usr/bin/python3` test-host interpreter. On this host its approved
qualification identity was:

```text
release_id: codex-protected-v1
version: 3.14.4
sha256: b8d8288faefdd300201f43fcf00f6f539a27218eeed3a3dff5ab10b9c4c99700
```

This lets the actual production version probe run against the staged bytes;
there is no injected lambda/version-probe replacement in the subprocess path.

The structured success record emitted inside `main()` contains the operation,
disposition, installed executable path, release ID, version, approved digest,
and protected-receipt representation. The test independently reads the durable
receipt and calls the production runtime verifier against that exact
subprocess-created state. That evidence cannot be emitted by the earlier
import-only stage.

No `MAIN_WAS_CALLED` marker, `PYTEST_CURRENT_TEST` dependency, production CLI
destination flag, destination/release/interpreter/backend environment variable,
or mutable-checkout import was added. A focused regression also forces the
internal keyword seam under simulated effective UID 0 and proves it exits 2
without creating qualification state.

## Shell and protected Python command shapes

The successful unprivileged qualification invokes the actual installed shell
payload:

```text
/bin/sh \
  <installed-simulated-release>/scripts/install-managed-codex.sh \
  --qualification-import-probe
```

That payload derives its release from its own installed absolute path and
executes the actual installed launcher. The launcher retains the protected
shape:

```text
cd <installed-simulated-release>
/usr/bin/env -i \
  PATH=/usr/sbin:/usr/bin:/sbin:/bin \
  LANG=C.UTF-8 LC_ALL=C.UTF-8 \
  RAS_PROTECTED_IMPORT_QUALIFICATION=1 \
  /usr/bin/python3 -I -B \
  <absolute-installed-release>/scripts/protected-managed-codex-entry.py \
  --qualification-import-probe
```

The production root command remains:

```text
cd /opt/research-supervisor-release
/usr/bin/env -i \
  PATH=/usr/sbin:/usr/bin:/sbin:/bin \
  LANG=C.UTF-8 LC_ALL=C.UTF-8 \
  /usr/bin/python3 -I -B \
  /opt/research-supervisor-release/scripts/protected-managed-codex-entry.py \
  install
```

The production root shell accepts no qualification argument. The root launcher
accepts only `install`, `verify`, or `bind-home OPERATOR`; it does not export the
qualification environment marker. The root entrypoint rejects that marker if
present, and root `managed_codex_installer.main()` rejects the internal
qualification dependency. Therefore a production shell request or caller
environment cannot select the deterministic backend.

## Production-wired causal contract

The strongest test is now one causal chain:

```text
protected release approval/install
  -> actual installed install-managed-codex.sh subprocess
  -> /usr/bin/python3 -I -B under env -i
  -> absolute installed protected entrypoint
  -> protected installer module
  -> managed_codex_installer.main()
  -> real install_managed_codex orchestration and production version probe
  -> subprocess-created executable and protected receipt
  -> pytest reads that exact durable result
  -> production runtime verifier
  -> canonical managed CODEX_HOME and readiness
  -> Sign in preparation
  -> qualified replay Worker preparation
  -> ordinary Auditor preparation
  -> qualified-runner environment seal
  -> Physics Auditor fresh preparation
  -> Physics Auditor retry preparation
  -> Physics Auditor resume preparation
```

The production-wired test no longer calls `install_managed_codex()` at or after
the shell boundary. Direct installer calls remain only in separate installer
unit/lifecycle tests and pre-existing supplemental Physics tests; they do not
bridge the production-wired subprocess chain.

## Missing-entrypoint negative evidence

The new dedicated subprocess test installs the approved simulated protected
release, prepares only the empty fixed qualification destination, removes the
installed absolute `protected-managed-codex-entry.py`, and invokes the same
`install-managed-codex.sh --qualification-import-probe` shell path.

Its caller state supplies:

- an alternate entrypoint in hostile CWD;
- a shadow installer through `PYTHONPATH`;
- hostile `PYTHONHOME`; and
- a fake PATH-selected `python3` that would create a marker.

The launcher exits 2 with `Protected Python application entrypoint is missing
or unsafe.` before Python starts. Standard output is empty, the fallback marker
does not exist, and no executable, completed receipt, or pending receipt is
created. There is no fallback to the checkout, CWD, PATH, `python3 -m`, an
alternate entrypoint, or caller Python environment.

## Hostile import and closed-boundary regressions

The R3 hostile matrix now continues beyond its old return point through
`managed_codex_installer.main()` and its imports/orchestration. It covers:

- hostile and normal caller CWDs;
- shadow top-level package and installer module;
- shadow transitive `hashlib` import in CWD and `PYTHONPATH`;
- hostile `PYTHONPATH` and `PYTHONHOME`;
- user-site and `.pth` execution attempt;
- `PYTHONSTARTUP`, `PYTHONINSPECT`, `PYTHONWARNINGS`,
  `PYTHONPYCACHEPREFIX`, `PYTHONBREAKPOINT`, `BASH_ENV`, and `ENV`;
- fake PATH-selected `python3`;
- arbitrary normal CWD and the observed protected CWD;
- exact protected module paths, isolated/safe-path/no-user-site flags, minimal
  environment, installed identity, digest, and receipt; and
- both managed-Codex and adjacent Core installed payload transitions.

All hostile markers remain absent. The first invocation reports `installed`;
the following normal/Core invocations report `unchanged` for the exact same
verified identity and receipt. This extends Blocker B coverage through imports
and subprocess activity that occur after the R3 return point.

Blocker A bounded regressions retained protected receipt-backed Physics Auditor
identity and canonical `CODEX_HOME`; hostile request pins, PATH, and environment
remain non-authoritative, and fresh/retry/resume preparation uses the same
verified pair. No Physics code was changed in R4.

Blocker B retains explicit `/usr/bin/python3`, `-I -B`, `/usr/bin/env -i`, fixed
minimal environment, protected CWD, approved absolute entrypoint, and protected
release `src` inserted ahead of protected application imports. No caller CWD,
mutable checkout, `PYTHON*`, user-site, PATH-selected Python, or arbitrary
entrypoint becomes production authority.

## Validation

Exact tools:

```text
/home/inaeyk/researchrepo/ras-regression-venv/bin/python  3.14.4
/usr/bin/python3                                          3.14.4
/home/inaeyk/researchrepo/ras-regression-venv/bin/pytest  9.1.1
/home/inaeyk/researchrepo/ras-regression-venv/bin/ruff    0.16.4
/home/inaeyk/researchrepo/ras-regression-venv/bin/mypy    2.3.1
/usr/bin/bwrap                                            0.11.1
/usr/bin/bash                                             5.3.9
/usr/bin/sh                                               dash
/mnt/c/windows/System32/WindowsPowerShell/v1.0/powershell.exe
                                                          5.1.26100.9168
```

Final results:

- Main-boundary, extended hostile, fail-closed authority,
  missing-entrypoint, root-backend rejection, and production-wired slice:
  **5 passed in 1.22 s**.
- Complete managed-Codex security file: **38 passed, 1 skipped in 1.85 s**.
- Physics real-adapter/scoring slice: **24 passed in 4.90 s**.
- One host-capable 19-file R0/PA-5C4/PA-5C4-U/Custodian/Core/
  qualified-runner/Physics regression family: **390 passed, 8 skipped, 1
  inherited failure in 117.77 s**. The only failure was the known inventory
  test described below.
- Ruff over all 25 changed/new Python source and test files: **PASS**.
- Strict mypy over all 16 changed/new production/script modules: **PASS**, no
  issues.
- Read-only Python `compile()` over all 25 changed/new Python files: **PASS**.
- GNU Bash and POSIX `sh` syntax over all six changed/new shell payloads:
  **PASS**.
- Windows PowerShell parse of `launch-research-supervisor.ps1`: **PASS**.
- Pre-edit and final `git diff --check`: **PASS**.
- Current `HEAD` remains the exact baseline; changes remain uncommitted.

One intermediate five-test run exposed a stale test-local variable after the
fresh/retry/resume assertions were expanded: 4 passed and 1 failed. The local
assertion was corrected, after which the final five-test slice and the complete
security/broad families passed. An initial PowerShell parser command contained
an over-escaped UNC path and failed in the validation harness; the corrected
path parsed successfully. Neither was a product/security failure.

A repository-wide Ruff probe also reported one pre-existing `SIM102` at
`src/research_automation_supervisor/semantic_decomposition.py:562`. That file is
untouched by this candidate and outside R4. It was not suppressed or changed;
the exact required changed/new-file Ruff selection passes.

## Qualification-only skips and inherited inventory

The eight visible broad-family skips remain pending and are not counted as
PASS:

- one real root-owned managed-Codex `/usr/bin` and `/etc` qualification;
- four protected fixed-path/root Windows-launcher cases;
- two actual root/two-UID Core service cases; and
- one explicit network qualification.

The sole broad-family failure reproduced the exact inherited inventory:

```text
inventory_sha256:
f8af9d25eb89712326248105eee732df8dc56a84e8bdcc5b793caea657dc998b

categories: POST-SNAPSHOT 25, PRE-SNAPSHOT 4,
QUALIFIED-SANDBOX-INTERNAL 11, UNCLASSIFIED 4

normalized signature:
7ac0707a865519a1c1f89ec957cbea162896ecb257b60236501e2f0448b7433c
```

The same four normalized identities remain
`process_enforcement._run_systemctl`, `semantic_replay.execute_replay`,
`semantic_replay._git`, and `systemd_launch_helper.main`. R4 adds no production
process callsite. The result is **inherited/unrelated** and was not
investigated further, suppressed, or rebaselined.

## Remaining real-host and distribution assertions

This deterministic repair does not establish:

- the actual externally protected distribution helper/verifier provenance and
  approved bytes;
- actual root UID transition, ownership, groups, modes, links, ancestry, and
  mutability for `/opt`, `/etc`, `/usr/bin`, the protected release, managed
  executable/receipts, and Core venv/site-packages;
- actual `/usr/bin/python3`, standard-library, system-site,
  `.pth`/`sitecustomize`, and distribution `packaging` provenance;
- real root installation, update, interruption/recovery, Core service, and
  two-UID behavior;
- actual canonical managed-home authentication state and end-to-end credential
  containment;
- offline wheelhouse/distribution compatibility; or
- Windows/WSL first-run, relaunch, Sign in, and end-to-end behavior.

Those remain explicit real-host/distribution qualification work and are not
claimed by the subprocess fixture.

## Token usage

### ACCOUNTING OBSERVATION — authoritative counters unavailable

`CODEX_HOME` was unset. The default persistent
`/home/inaeyk/.codex/bin/codex-task` and durable ledger root were present.
`CODEX_THREAD_ID` was `01a033e0-2c83-7521-a6c1-aa8cf4ae2ac7`, but no
`TaskUsageReceipt.json` below the durable ledger root matched that exact thread
identity. No raw rollout transcript was read and no token count was estimated,
derived, or reconstructed. Final-output usage cannot be authoritative until a
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

- Worker interactive session: unavailable; no matching durable receipt.
- Auditor session: unavailable / not applicable; no Auditor was launched.
- Supervisor/Custodian model session: unavailable / not applicable; none was
  launched.
- Other/nested model session: unavailable / not applicable; nested agents were
  prohibited and none were launched.
- Model retries, repair attribution, and repeated audit rounds: unavailable;
  no separate model session receipt exists and no count is estimated.
- Non-model pytest assertion repair and PowerShell path-harness retry: token
  attribution unavailable and not estimated.

If the hosting runtime appends a machine-generated final receipt after turn
completion, that receipt—not an estimate here—is authoritative.

## Terminal disposition

The deterministic Worker candidate closes only the remaining production
installer `main()` test boundary while preserving the already closed Physics
Auditor and privileged Python boundaries. It does not authorize R0, real-host
installation, a campaign, an Auditor, a commit, a push, Attempt 005, PA-5D,
PA-5D0, or another repair round.

**THIS IS NOT AN R0 PASS.**
