# R0-SETUP-1 — managed Codex first-run closure

Date: 2026-08-23

Branch: `feature/context-economy-runtime-integration`

Qualified baseline: `df59e2818c3519a5ba7dab69dd067b91b202e936`

Disposition: setup repair implemented and deterministically tested; real-host fresh-machine
requalification remains required. This report does **not** claim R0 PASS.

## Root cause

The qualified product had two incomplete and conflicting setup paths:

- `scripts/install-core-authority-service.sh` installed Core identity, storage, socket,
  and systemd authority, but no qualified Codex executable.
- `scripts/custodian-bootstrap.sh` installed the user-level Python application but no
  `CODEX_HOME` and no Codex prerequisite.
- the separate top-level `bootstrap.sh` was a staged developer bootstrap. It accepted
  arbitrary PATH Codex and recommended a user-local installer/login, but the Windows
  product documentation did not distinguish that developer state from qualified state.

The runtime checks correctly admitted only root-owned, non-writable `/usr/bin/codex` and
correctly refused implicit HOME authentication. A fresh WSL host therefore had no path
from documented product setup to the prerequisites those checks required. The observed
NVM Codex and `~/.codex` login were intentionally insufficient.

## Why prior qualification missed it

The acceptance setup did not construct prerequisites at the real launcher/installer
boundary. In particular, `tests/test_pa5c4_transactional_core.py` created a temporary
`CODEX_HOME` and replaced the trusted-program resolver with `/usr/bin/codex`. Custodian
tests injected ready environment reports, and browser acceptance used its deterministic
acceptance backend. Those tests proved behavior after prerequisite injection, not that a
fresh product install could create the prerequisites.

Preview also resolved PATH Codex in its non-executing view and always deferred the real
fixed-executable/authentication probe until after Core had committed Start authority.
Thus qualification neither proved the system executable installation nor failed missing
setup before Start.

## Architecture selected

### Administrator-owned executable

`scripts/install-research-supervisor.sh` is now the one-time product entry point. It
first invokes `scripts/install-managed-codex.sh`, then the existing Core service
installer. The managed installer:

- has no network/download operation;
- requires one absolute, root-owned, non-group/world-writable, non-symlink standalone
  Linux ELF artifact and an independently approved lowercase SHA-256;
- rejects writable parents, hash mismatch, non-ELF input, and Codex below `0.144.0`;
- copies to a root-owned mode-`0755` temporary file in `/usr/bin`, validates the copied
  bytes before replacement, then atomically installs the regular file as
  `/usr/bin/codex`;
- writes the exact executable, digest, and version to the root-owned mode-`0644`
  `/etc/research-supervisor-core/managed-codex-install-v1` receipt.

The Core component installer now refuses to present itself as complete unless that exact
managed target and receipt already exist. It directs administrators to the product
installer. The product never links to or executes the operator's NVM/npm tree as
qualified authority.

### Operator-owned credential home

`scripts/prepare-managed-codex-home.py` and
`research_automation_supervisor.managed_codex` create exactly
`<application-data>/research-automation-supervisor/codex-home` (or the same child of an
explicit data root). Data root, runtime root, and Codex home are operator-owned mode
`0700`. A mode-`0600` create-once `runtime/managed-codex-home-v1` file binds the exact
absolute path. A moved, substituted, linked, wrong-owner, wrong-mode, or changed binding
fails closed. Relaunch repairs safe owner-only modes but never replaces authentication
content or silently selects another home.

The launcher exports that exact path as `CODEX_HOME` when it starts the Custodian. Health
and launcher reuse compare SHA-256 of the exact path, so an existing backend is reused
only for the same managed home. The qualified runner revalidates the bound home before
every operation and preserves only that path while setting `HOME=/nonexistent` and
`PATH=/usr/bin:/bin`.

### One authentication/execution identity

Shared Python constants and validators define `/usr/bin/codex` as the only qualified
executable. The Sign in operation invokes exactly:

```text
/usr/bin/codex login
```

with the bound managed `CODEX_HOME`. Qualified campaign services explicitly select the
same `/usr/bin/codex` for initial, resumed, and human-response Worker/Auditor execution.
The managed home propagates through Custodian and qualified runner into the existing
Codex adapter. Core never receives `auth.json`, subscription credentials, `CODEX_HOME`,
or HOME access.

### Pre-Start behavior

Preview now executes only fixed, root-owned system probes; arbitrary PATH programs are
not consulted. Missing managed Codex yields `codex_unavailable` with administrator setup
guidance. Missing/broken bound storage yields `managed_codex_home_unavailable`. Sign in
is offered only after both exist. The UI uses **Check Setup Again** until readiness is
true.

Start rechecks full readiness before `create_start_intent`. Failure raises a plain
operator-facing setup error with zero Core Start rows, no qualified campaign directory,
and no runner launch. This is earlier than the old post-Start blocked-card behavior.

## Files changed

- Product/admin setup: `scripts/install-managed-codex.sh`,
  `scripts/install-research-supervisor.sh`,
  `scripts/install-core-authority-service.sh`, `pyproject.toml`.
- Operator first run: `scripts/prepare-managed-codex-home.py`,
  `scripts/custodian-bootstrap.sh`, `launch-research-supervisor.ps1`.
- Runtime contract: `src/research_automation_supervisor/managed_codex.py`,
  `custodian_bootstrap.py`, `custodian.py`, `custodian_server.py`,
  `qualified_runner.py`, `qualified_campaign.py`, and `secure_cli.py`.
- Product/developer documentation: `README.md`, `README_FIRST.md`, `bootstrap.sh`, and
  `docs/campaign_custodian.md`.
- Regression coverage: `tests/test_windows_launcher.py`, `tests/test_custodian.py`,
  `tests/test_custodian_server.py`, and `tests/test_pa5c4_transactional_core.py`.

`Research Supervisor.vbs`, both `.cmd` compatibility wrappers, the Core systemd unit,
Process Enforcement, cgroup/systemd containment, execution budgets, workflow authority,
and authenticated Core socket protocol were inspected and left structurally unchanged.

## Security invariants preserved

- No qualified executable lookup from PATH.
- No user-local/NVM/npm symlink or wrapper accepted as `/usr/bin/codex`.
- Root ownership/non-writability checks remain mandatory.
- No arbitrary HOME login reuse; the bound home is explicit and operator-owned.
- No credential bytes cross into Core or browser projections.
- Browser and Custodian remain non-authoritative; campaign actions still enter only
  through Core intent plus qualified runner.
- No bypass of process containment, budgets, workflow integrity, snapshot verification,
  or socket peer authentication.
- No `sudo`, package install, `/usr/bin` mutation, remote fetch, remote installer, real
  Worker/Auditor, Attempt 005, PA-5D, or PA-5D0 action was executed during this repair.

## Deterministic regression coverage

New/updated coverage proves:

1. first-run construction of the exact managed home and create-once binding;
2. operator ownership and modes `0700`/`0600`;
3. shell launcher propagation and qualified-runner environment preservation;
4. relaunch preserves directory inode, binding, and existing `auth.json` bytes;
5. missing managed Codex appears as pre-Start administrator Action Needed and creates no
   Core authority;
6. Sign in and qualified replay services use exact `/usr/bin/codex` plus exact managed
   home;
7. user-writable executables and changed managed-home bindings are rejected;
8. arbitrary PATH Codex is never resolved or executed;
9. product, component installer, launcher, runtime constant, minimum version, and receipt
   agree;
10. an injected runner that would claim login cannot be reached when the managed
    executable is absent.

Validation results at report time:

- focused setup/Custodian/authentication slices: pass;
- complete relevant Core/Custodian/PA-5C4 family: **148 passed, 3 explicitly skipped,
  1 inherited failure**;
- skipped cases are the existing root-only two-UID/service proofs and explicit network
  intake qualification; no skip was converted to a fake PASS;
- changed/relevant Ruff: pass;
- strict mypy on all changed/relevant source files: pass;
- POSIX shell syntax checks: pass;
- `git diff --check`: pass.

The one failure is
`test_complete_production_git_inventory_has_no_unclassified_callsite`. An archive of the
exact baseline commit independently produces the same failure, the same computed
inventory SHA-256 `f8af9d25eb89712326248105eee732df8dc56a84e8bdcc5b793caea657dc998b`,
and the same four pre-existing unclassified modules/callsites, while the checker expects
`dc2ded7e1c14774af428538bd9a9e3d7157b578166f2505cf91c9f3a325f445e`.
This repair adds no process callsite and does not change that computed inventory. The
unrelated security inventory was not reclassified or rebaselined here.

## Real-host work still required

Unprivileged pytest cannot construct or prove a real root-owned `/usr/bin/codex`, the
root-owned installer receipt, the two-UID service boundary, or a truly fresh Windows/WSL
credential journey. No real administrator installer was run. Existing host Core health
and the operator's NVM login are observations only and are not counted as this repair's
PASS.

## Exact remaining R0 acceptance procedure

1. Use a fresh qualified Windows/WSL machine or reset only through the separately
   authorized fresh-machine qualification procedure. Begin with no `/usr/bin/codex`, no
   product installer receipt, and no Research Supervisor application-data root. Preserve
   the operator's optional NVM Codex only as a negative-control PATH installation.
2. Obtain the official standalone Linux Codex artifact through the approved software
   channel. Independently record its SHA-256. As administrator, stage it at a root-owned,
   non-group/world-writable, non-symlink path whose parents meet the same rule.
3. From qualified source at the candidate commit, run exactly the documented one-time
   `scripts/install-research-supervisor.sh PROJECT_ROOT OPERATOR ARTIFACT SHA256` command.
   Capture installer stdout/stderr, installed-file `lstat`, SHA-256, version, receipt
   ownership/mode/content, Core unit identity/hardening, socket ownership/mode, and
   operator group membership. Any mismatch is FAIL.
4. Sign the ordinary operator out and back in once. Do not run an operator terminal
   setup command and do not copy `~/.codex`.
5. Double-click `Research Supervisor.vbs`. Capture launcher evidence and prove the data
   root, runtime, `codex-home`, and binding ownership/modes/content. Prove the initial
   Preview reports authentication needed—not authenticated—even if the operator NVM
   Codex says logged in. Prove no Core Start exists.
6. Choose **Sign in** in the browser. Observe that the process executable resolves to the
   root-owned `/usr/bin/codex`, its environment has the exact bound `CODEX_HOME` and
   `HOME=/nonexistent`, and Core/browser evidence contains no credential bytes. Complete
   ChatGPT browser sign-in.
7. Choose **Check Setup Again**. Prove the exact managed executable/home reports ready.
   Close and relaunch the browser/Custodian normally; prove the same home binding and
   authentication are reused and launcher health accepts only its matching path digest.
8. Negative controls: place a fake Codex first on PATH; make a separate user-writable
   executable; alter a disposable test binding/mode in an isolated data root. Prove each
   remains insufficient or fails closed before Start. Restore only by the documented
   launcher/admin path, never by symlink or relaxed permissions.
9. Run the authorized minimal qualified acceptance scenario that proves the first
   Worker/Auditor process selects the same `/usr/bin/codex` and managed home, while
   retaining Process Enforcement, budgets, cgroup containment, workflow integrity, and
   socket authentication. Do not infer this from a mocked unit test.
10. Resolve or separately disposition the inherited PA-5C4 process-inventory failure,
    rerun the complete relevant family, and retain exact evidence. Only the designated R0
    authority may then decide R0 PASS. This repair report remains a non-PASS input.

## Token usage

No authoritative `codex-task` `TaskUsageReceipt.json` exists for this interactive model
thread, and final-output usage is not authoritative until the runtime emits
`turn.completed`. Per the repository accounting rule, values are not estimated:

- input_tokens: unavailable
- output_tokens: unavailable
- combined_tokens: unavailable
- cached_input_tokens: unavailable
- cache_write_input_tokens: unavailable
- reasoning_output_tokens: unavailable
- Worker session: unavailable
- Auditor sessions: not applicable (none launched)
- Supervisor/Custodian model sessions: not applicable (none launched)
- model retries/repair audit rounds: not applicable (none launched)

If the hosting runtime appends a machine-generated final token receipt after completion,
that receipt is the authoritative accounting record; cached/cache-write/reasoning values
remain submetrics and are not added to combined tokens.
