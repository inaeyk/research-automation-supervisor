# R0-SETUP-1 — independent security / qualification audit

Date: 2026-08-23

Branch: `feature/context-economy-runtime-integration`

Qualified baseline: `df59e2818c3519a5ba7dab69dd067b91b202e936`

Audited state: uncommitted R0-SETUP-1 Managed Codex First-Run Closure

## Overall verdict

**FAIL**

R0-SETUP-1 must not proceed to real-host administrator-installation qualification in
this state. The deterministic changes improve the fixed-path setup flow and correctly
move the ordinary Start readiness check ahead of Core Start creation, but blocking gaps
remain in the executable identity boundary and managed credential-home binding. The
new tests do not exercise those failed contracts.

This verdict does not reclassify the separately inherited PA-5C4 process-inventory
failure. That failure is independently confirmed as baseline-identical and unrelated to
R0-SETUP-1.

## Repository state captured before audit activity

- `HEAD`: `df59e2818c3519a5ba7dab69dd067b91b202e936`
- branch: `feature/context-economy-runtime-integration`
- the current `HEAD` is exactly the qualified baseline, so the tracked working-tree diff
  is directly against that baseline.
- `git diff --check`: PASS.
- tracked `git diff --stat`: 18 files changed, 456 insertions, 181 deletions. Git does
  not include the untracked implementation files in this statistic.

Initial tracked changes:

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
src/research_automation_supervisor/qualified_campaign.py
src/research_automation_supervisor/qualified_runner.py
src/research_automation_supervisor/secure_cli.py
tests/test_custodian.py
tests/test_custodian_server.py
tests/test_pa5c4_transactional_core.py
tests/test_windows_launcher.py
```

Initial untracked files:

```text
docs/reports/r0/r0-setup-1-managed-codex-first-run-closure.md
scripts/install-managed-codex.sh
scripts/install-research-supervisor.sh
scripts/prepare-managed-codex-home.py
src/research_automation_supervisor/managed_codex.py
```

The durable Worker report was read. Every tracked change and every untracked file above
was inspected; conclusions below come from the actual source and tests, not the Worker
summary.

## Findings

### CRITICAL

None.

### MAJOR-1 — the approved Codex artifact identity is not enforced by the qualified runtime

The administrator installer has several sound properties: it uses a fixed
`/usr/bin/codex` destination, never resolves Codex from PATH, rejects a symlinked or
non-regular source, requires root ownership and non-writable source parents, checks the
approved lowercase SHA-256 before copying, checks the copied staging hash and mode,
executes no downloader/package installer, and atomically renames a root-owned mode-0755
staging file into place (`scripts/install-managed-codex.sh:7-119`).

The end-to-end contract nevertheless fails:

1. `managed_codex.trusted_system_executable()` checks only absolute path, resolved
   regular-file type, root ownership, writability, executability, and resolved-parent
   safety (`managed_codex.py:16-35`). It does not require the installer receipt, compare
   the installed bytes with `MANAGED_CODEX_SHA256`, enforce the recorded version, or
   require `/usr/bin/codex` itself to remain the regular file installed by the installer.
2. `install-core-authority-service.sh:10-16` checks only `-f`, not-symlink, `-x`, receipt
   existence, and the executable-path receipt line. It does not validate receipt
   ownership/mode, parse the digest/version, or compare the digest with the installed
   file. A stale or forged receipt with the fixed executable line is sufficient.
3. Sign in uses the shared metadata predicate, but Worker/Auditor campaign execution
   does not. `_qualified_replay_services()` passes the string `/usr/bin/codex` directly
   (`qualified_campaign.py:141-147`). The downstream resolver verifies only that the
   resolved object is a regular file (`workflow_engine.py:4645-4655`), not root ownership,
   non-writability, parent safety, or the approved digest.
4. For a new launch, Core Start consumption occurs at
   `qualified_campaign.py:89-92`, before the replay path resolves the executable. Thus an
   executable that disappears after Custodian readiness can fail only after Start has
   been consumed, while an unsafe regular executable at the fixed path can reach the
   Worker/Auditor resolver. Resume and human-response replay also construct the unchecked
   service directly. `CampaignCustodian.respond()` has no intervening readiness check;
   the qualified runner validates only the credential home before dispatch.
5. The binary rename precedes receipt construction (`install-managed-codex.sh:119-128`).
   A receipt-write failure can leave the new binary paired with an old receipt. The
   runtime ignores that inconsistency, and the component installer checks only the
   executable line. The installer also does not apply the shared runtime predicate after
   replacement, so an abnormal pre-existing destination type can produce inconsistent
   installed state without a final contract check.

Consequences by lifecycle case:

- an absent Codex is correctly blocked by ordinary preview and can be installed from an
  approved artifact;
- an installer rerun replaces the binary, even when the currently installed hash differs;
- a root-owned replacement or Codex update outside this installer is accepted by runtime
  if its metadata/version/login probes pass, despite not matching the receipt;
- an interrupted replacement can leave binary/receipt generations split;
- a RAS-only update leaves the weak existing receipt check as the component prerequisite;
- existing authentication is retained across Codex replacement, but no durable installed
  identity is re-bound to that replacement.

This is a blocking weakening of the executable trust boundary and of the requested
pre-Start fail-closed property.

### MAJOR-2 — managed CODEX_HOME is redirectable and unsafe prior state is repaired rather than rejected

The intended default child name is stable, normal same-root relaunch preserves the home
and `auth.json`, ownership is checked against the operator UID, final modes are 0700/0600,
and direct symlink and wrong-content binding substitutions are rejected. Credentials are
not serialized into Core requests or browser projections, and campaign export uses an
explicit allowlist rooted in the campaign authority/run directories
(`qualified_campaign.py:439-481`). No product path that copies `auth.json` into Core,
snapshots, workspaces, evidence, reports, or export bundles was found.

The binding is not fail closed under the required hostile/stale cases:

1. `_prepare_private_directory()` creates parents recursively and then `fchmod(0700)`s an
   existing same-owner directory before validation (`managed_codex.py:125-150`). It does
   not validate ancestors above the application data root. A data root beneath a
   world-writable non-safe parent is accepted.
2. `_validate_binding()` similarly `fchmod(0600)`s an existing same-owner binding before
   validating content (`managed_codex.py:164-180`). A previously group/world-writable
   binding with expected content is silently accepted after repair. It does not require
   `st_nlink == 1`, so a multiply-linked binding is accepted.
3. If the binding is missing, `prepare_managed_codex_home()` recreates it even when the
   credential home and existing authentication already exist (`managed_codex.py:56-87`).
   First creation and deletion/tampering are therefore indistinguishable; this is not a
   strict create-once state transition.
4. `managed_codex_home_from_environment()` validates any absolute, same-UID, self-bound
   directory named `codex-home`; it has no expected application-data root
   (`managed_codex.py:117-122`). The Custodian readiness check does compare with its
   current `data_root`, but the qualified runner and legacy environment seal call the
   environment-only validator. The shell launcher also derives its root from
   `XDG_DATA_HOME` and accepts the PowerShell `DataRoot` parameter. Changing the selected
   data root creates and accepts a different binding instead of proving continuity with
   the original product binding.

Independent `/tmp` probes against the actual implementation demonstrated all of the
following acceptances:

```text
WORLD_WRITABLE_PARENT_ACCEPTED .../world-writable-parent/data/codex-home 0o777
UNSAFE_MODES_REPAIRED_AND_ACCEPTED ... 0o700 0o700 0o700 0o600
MISSING_BINDING_RECREATED ... True
MULTILINK_BINDING_ACCEPTED ... 2
ALTERNATE_ENVIRONMENT_HOME_ACCEPTED True .../other-data/codex-home
```

An already-authenticated NVM/PATH Codex does not satisfy preview because preview fixes
the executable to `/usr/bin/codex`; that negative control passes. A separately selected,
self-bound CODEX_HOME can nevertheless redirect qualified execution. This is blocking.

### MAJOR-3 — the privileged product wrapper executes an unchecked mutable project root

`scripts/install-research-supervisor.sh:12-20` accepts `project_root` and executes two
subordinate scripts from it as root without requiring an absolute canonical path,
rejecting symlinks, checking ownership/writability of the root and parents, or binding a
qualified source digest. The second installer subsequently runs root `pip install` from
that root and copies its service unit (`install-core-authority-service.sh:44-73`).

The documented administrator command therefore treats “qualified project root” as a
procedural assertion rather than an enforced input contract. If the ordinary operator
can alter or swap that tree while the administrator command runs, the fixed artifact
hash does not protect the privileged subordinate script, package build, or unit file.
The Codex destination itself is fixed, so this is not arbitrary destination selection;
it is unchecked privileged source selection and substitution. It must be closed or made
an explicit, independently protected administrator staging prerequisite before real-host
qualification.

### MAJOR-4 — the regression suite does not close the prior setup-contract gap

The new deterministic tests usefully prove fixed-path selection, same-root relaunch,
binding-content rejection, PATH rejection, pre-Start readiness ordering, exact sign-in
argv/environment, and unprivileged refusal of the root installer.

They do not execute a successful `install-managed-codex.sh` contract. The nominal
installer/runtime agreement test is a source-text assertion
(`test_windows_launcher.py:100-122`). The component-installer harness creates a mocked
user-owned script and a zero-digest receipt (`test_windows_launcher.py:182-272`); it is
not evidence for a real or faithfully isolated root-owned managed Codex installer. The
authentication test mocks the trusted-program resolver to return `/usr/bin/codex`, and
the Worker/Auditor assertion checks only the configured string, not the trust predicate
at process launch (`test_pa5c4_transactional_core.py:247-274`).

There is no deterministic coverage for:

- installed bytes versus receipt digest/version/owner/mode;
- interrupted binary/receipt replacement or reinstall/update semantics;
- Worker/Auditor rejection of unsafe or receipt-mismatched `/usr/bin/codex`;
- unsafe managed-home ancestor, directory, or binding modes;
- a missing or multiply-linked binding;
- an alternate self-bound environment home;
- the privileged project-root source contract.

The explicitly skipped real-host cases remain honestly marked qualification-only; that
is appropriate and is not itself a finding. However, the claim that the previous
mocked-prerequisite gap is closed is not supported.

### MINOR

None separate from the blocking findings above.

## Mandatory security-question results

### A. Trusted Codex installation

**Partial / blocking failure.** The artifact intake and fixed-destination staging are
substantially sound: no PATH Codex, network installer, npm, curl, symlink installation,
operator destination, or pre-hash privileged execution was found. Root/non-writable
source parents, exact hashes before and after copy, deliberate root:root 0755 staging,
and atomic binary rename are present. The unchecked project root, non-transactional
binary/receipt pair, weak receipt consumer, and runtime/Worker/Auditor identity mismatch
fail the complete contract.

`custodian_bootstrap._trusted_system_executable()` and
`qualified_campaign._trusted_system_program()` now delegate to the same shared metadata
predicate. That predicate still does not represent the installer’s hash-approved
identity, and the actual replay service bypasses it.

### B. Managed CODEX_HOME

**Blocking failure.** The expected same-root location, UID, final modes, normal relaunch,
and direct symlink/wrong-content rejection work. Parent safety, strict create-once state,
unsafe-mode rejection, single-link binding identity, and canonical non-environment
binding do not. Credential bytes were not found crossing into Core/browser/campaign
evidence/export code.

### C. Authentication identity trace

| Boundary | Managed executable | Managed home |
|---|---|---|
| Windows launcher | delegates to WSL bootstrap | passes selected data root |
| WSL bootstrap | does not resolve Codex from PATH | prepares `<data-root>/codex-home`, exports it |
| Custodian readiness | shared predicate on `/usr/bin/codex` | compares environment with current data root |
| Sign in | shared predicate, then `/usr/bin/codex login` | runner-validated environment home, `HOME=/nonexistent` |
| Qualified runner | fixed PATH; no Codex check for campaign operations | environment-only validator, then sealed environment |
| Worker/Auditor replay | hard-coded `/usr/bin/codex`, downstream regular-file-only check | inherits the sealed CODEX_HOME |

The intended strings propagate across the real product path and an authenticated
NVM/PATH Codex cannot create readiness. The executable’s approved identity does not
propagate, and the qualified runner accepts another independently self-bound home.

### D. Pre-Start fail-closed property

**Partial / blocking failure.** `CampaignCustodian.start()` performs full executable,
home, version, authentication, Bubblewrap, and filesystem readiness before
`create_start_intent` (`custodian.py:530-560`). The tested missing-Codex and missing-login
paths create zero Core Start rows and launch no runner. `continue_campaign()` also checks
readiness before launch.

The qualified runner does not revalidate the executable before Start consumption, and
the approved digest is never checked. Missing/changed state between Custodian readiness
and qualified dispatch can therefore leave a committed Start and reach consumption
before executable resolution. An unsafe regular fixed-path executable can reach replay.
Home validation occurs before runner dispatch, but its redirect/repair semantics are
themselves insufficient. Human-response replay also lacks a Custodian readiness check.

### E. Update / reinstall / version behavior

- Codex absent: launcher constructs operator storage but preview blocks; administrator
  installer can install an approved artifact. This is correct.
- installed hash differs: installer rerun replaces it; runtime never compares installed
  bytes with the receipt.
- installer rerun: the binary replacement is atomic, but binary plus receipt is not one
  atomic generation and no final shared-predicate/receipt validation occurs.
- RAS update without Codex update: launcher reinstalls the user package when its source
  digest changes and reuses the home. Core component setup accepts only the weak fixed
  executable/receipt-line check.
- Codex update with existing auth: home and auth bytes are retained. The new binary is
  version-probed, but its receipt identity is not enforced later.
- unsafe CODEX_HOME owner: rejected. Unsafe same-owner directory/binding mode: repaired
  and accepted. Unsafe parent: accepted.
- binding wrong content or symlink: rejected. Binding missing: recreated. Hard-linked
  binding: accepted.
- ordinary launcher without administrator rights: works after the fixed system/Core
  prerequisites exist; without them it cannot construct them and stops or reports setup
  needed. That administrator split is expected.

### F. Test-coverage hole

**Not closed.** Deterministic unprivileged behavior tests are valuable, and real-host
proof is honestly deferred, but successful installer/reinstall/receipt semantics and
the actual Worker/Auditor trust boundary are neither faithfully isolated nor host
qualified. A mocked root-owned executable is not counted as real-host evidence.

### G. Inherited process-inventory failure

**Confirmed inherited and unrelated.** The checker was run on the R0-SETUP-1 tree and
from a separate clean local clone detached at exact commit
`df59e2818c3519a5ba7dab69dd067b91b202e936`.

Both runs produced:

```text
return code: 1
inventory_sha256: f8af9d25eb89712326248105eee732df8dc56a84e8bdcc5b793caea657dc998b
expected inventory: dc2ded7e1c14774af428538bd9a9e3d7157b578166f2505cf91c9f3a325f445e
categories: POST-SNAPSHOT 25, PRE-SNAPSHOT 4,
            QUALIFIED-SANDBOX-INTERNAL 11, UNCLASSIFIED 4
normalized failure signature SHA-256:
  7ac0707a865519a1c1f89ec957cbea162896ecb257b60236501e2f0448b7433c
```

The four normalized unclassified callsites and expression hashes were identical:

- `process_enforcement.py::_run_systemctl`
- `semantic_replay.py::execute_replay`
- `semantic_replay.py::_git`
- `systemd_launch_helper.py::main`

The normalized evidence objects were byte-for-byte equal. Full rendered JSON hashes
differed only because R0-SETUP-1 shifted line numbers in existing classified callsites;
a direct diff showed no expression, category, error, or inventory-authority change. No
R0-SETUP-1 file adds a process callsite.

## Validation results

### Focused security/setup slice

```text
12 passed in 0.35s
```

This covered managed-home construction/relaunch, binding-content rejection,
user-writable/PATH executable rejection, installer source contract assertions,
unprivileged installer refusal, pre-Start no-authority behavior, runner environment,
ambient PATH rejection, false-login injection rejection, and exact authentication
argv/environment.

### Relevant PA-5C4 / PA-5C4-U / Custodian / Core family

The family was reproduced as the 120-case Core/Custodian/transactional group plus the
32-case linked-worktree/PA-5C4-U group:

```text
148 passed, 3 skipped, 1 failed
```

The three skips were:

- two existing actual two-UID/root-only service proofs;
- explicit network HTTPS intake qualification.

The sole failure was the inherited process inventory above. An initial sandboxed run
also blocked Unix socket bind and Bubblewrap netlink operations. Per the runtime’s
sandbox guidance, the same family was rerun with local host process access; those
environment-only failures disappeared and are not counted as product failures.

### Static and syntax validation

- Ruff on all changed/relevant Python and test files: PASS.
- strict mypy on eight changed/relevant source files: PASS.
- Bash/POSIX shell syntax on all changed shell scripts: PASS.
- tracked `git diff --check` plus whitespace checks for every untracked audited file:
  PASS.

No administrator installer, `/usr/bin` mutation, package installation, remote fetch,
real Worker/Auditor campaign, Attempt 005, PA-5D, or PA-5D0 action was run. This host had
no `/usr/bin/codex` and no managed Codex receipt; that observation is not counted as
real-host qualification evidence or as an additional product failure.

## Qualification disposition

**R0-SETUP-1 may not proceed to real-host qualification.** Resolve the three product
contract findings and add deterministic tests that exercise their failure modes, then
obtain a new independent audit before spending real-host administrator authority. The
inherited inventory failure remains a separate baseline issue and was neither changed
nor suppressed here.

## Token usage

### ACCOUNTING OBSERVATION

This interactive Independent Auditor runtime exposed thread/session identifiers but no
authoritative `turn.completed.usage` object. `CODEX_HOME` was unset, so the mandated
`$CODEX_HOME/bin/codex-task` entry point and a task-specific durable receipt were not
available to this session. No rollout transcript was inspected and no values are
estimated. Final-output usage cannot be known before the runtime emits turn completion.

- input_tokens: unavailable
- output_tokens: unavailable
- combined_tokens: unavailable
- cached_input_tokens: unavailable
- cache_write_input_tokens: unavailable
- reasoning_output_tokens: unavailable
- Independent Auditor interactive session: unavailable
- prior Worker session: unavailable (its durable report also records no receipt)
- additional Auditor sessions: not applicable; none launched
- Supervisor/Custodian model sessions: not applicable; none launched
- model retries/repair audit rounds: none launched; token attribution unavailable
- validation retry: one non-model rerun after sandbox-only socket/Bubblewrap failures;
  token attribution not applicable

If the hosting runtime appends a machine-generated receipt after completion, that
receipt—not this report—is the authoritative final accounting record.
