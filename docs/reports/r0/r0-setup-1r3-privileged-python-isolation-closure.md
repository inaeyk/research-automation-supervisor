# R0-SETUP-1R3 — privileged Python import-isolation closure

Date: 2026-08-23

Repository: `/home/inaeyk/researchrepo/ras-context-integration`

Branch: `feature/context-economy-runtime-integration`

Qualified baseline and current `HEAD`:
`df59e2818c3519a5ba7dab69dd067b91b202e936`

## Scope and candidate disposition

This micro-repair addresses only the two findings left open by the
R0-SETUP-1R2 independent audit: ambient Python authority at the protected
shell-to-Python transition and the absence of subprocess evidence for that
transition. It does not reopen Physics Auditor identity, canonical managed
`CODEX_HOME`, Codex receipt/digest authority, Core/Custodian design, PA-5D,
Attempt 005, or any campaign behavior.

Worker candidate disposition:

- **BLOCKER A — Physics Auditor identity: CLOSED, unchanged.** The R2 code and
  real-adapter tests remained intact and green.
- **BLOCKER B — privileged Python import authority: CLOSED in the deterministic
  candidate.** The supported protected payload no longer uses
  `PYTHONPATH=... /usr/bin/python3 -m ...` or a PATH-selected `python3`.
- **BLOCKER C — production-wired boundary evidence: CLOSED in the deterministic
  candidate.** Tests invoke both actual protected shell payloads and the shared
  production launcher as subprocesses before continuing through the existing
  production-function trust contract.

These are repair-candidate dispositions, not an independent-audit verdict.
**This is not an R0 PASS.** Real-host and distribution qualification remains
pending.

No commit, push, `sudo`, package installation, network access, `/opt`, `/etc`,
`/usr/bin`, service mutation, campaign, Worker, or Auditor launch was performed.

## Pre-edit state and reconstruction

Before editing:

```text
git rev-parse HEAD
df59e2818c3519a5ba7dab69dd067b91b202e936

git diff --check
PASS

git diff --stat
27 files changed, 962 insertions(+), 244 deletions(-)
```

The working tree was the stated uncommitted R0-SETUP-1 + R1 + R2 candidate.
The complete short status was recorded in the task transcript before edits and
was preserved; unrelated inherited changes were not reset or rewritten.

The exact vulnerable production transitions were:

```text
/usr/libexec/research-supervisor/install-protected-release
  -> /opt/research-supervisor-release/scripts/install-research-supervisor.sh
  -> install-managed-codex.sh
  -> PYTHONPATH=/opt/research-supervisor-release/src
  -> /usr/bin/python3 -m research_automation_supervisor.managed_codex_installer install

install-core-authority-service.sh
  -> the same PYTHONPATH + /usr/bin/python3 -m module for verify/bind-home
  -> PATH-selected python3 -m venv
  -> non-isolated protected-venv Python -m pip
```

The release verifier authenticated the intended tree, but Python performed a
fresh module selection afterward. Under the old `-m` shape, `sys.path[0]` was
the caller CWD ahead of the exported release source. The inherited environment
also reached Python startup and `site` processing.

Harmless unprivileged probes against the old command shape demonstrated:

```text
HOSTILE_CWD_MODULE_SELECTED
HOSTILE_USER_SITE_PTH_EXECUTED
Fatal Python error: Failed to import encodings module
ModuleNotFoundError: No module named 'encodings'
```

The first line came from a caller-CWD package named
`research_automation_supervisor`. The second came from a `.pth` below a
caller-selected `PYTHONUSERBASE`. The fatal startup was induced by a harmless
missing-directory `PYTHONHOME`. Nothing was run with elevated privilege.

## Protected interpreter and import architecture

The repaired chain is:

```text
externally protected fixed helper
  -> exact manifest-approved protected shell payload
  -> exact manifest-approved run-protected-python.sh
  -> fixed /usr/bin/python3 (resolved versioned /usr/bin target)
  -> Python -I -B under env -i and protected-release CWD
  -> absolute manifest-approved protected-managed-codex-entry.py
  -> protected release src inserted at sys.path[0]
  -> protected managed_codex_installer module/function
  -> existing exact Codex installer/receipt/home contract
```

`scripts/run-protected-python.sh` is the sole managed-Codex privileged Python
launcher. In production it:

- accepts only `install`, `verify`, or `bind-home OPERATOR`;
- fixes the release root, launcher, verifier, entrypoint, and interpreter;
- rejects wrong launcher selection, symlinks, unsafe/missing release ancestry,
  an unsafe verifier, a missing/non-absolute/wrong-target interpreter, unsafe
  `/usr` or `/usr/bin`, unsafe interpreter metadata, and an unsafe entrypoint;
- requires `/usr/bin/python3` to resolve to a versioned `/usr/bin/python3.X`
  regular file in the protected system authority;
- reruns the external installed-release verifier immediately before Python;
- changes CWD to `/opt/research-supervisor-release`;
- replaces the Python process environment with exactly fixed `PATH`, `LANG`,
  and `LC_ALL`; and
- executes the absolute approved entry script with `-I -B`.

Python isolated mode implies ignore-environment, safe-path, and disabled user
site semantics. The entry script verifies those flags and rejects an empty/CWD
path entry before it inserts the protected release `src`. System standard
library and distribution site-packages remain available because the existing
managed installer uses the established `doctor.subprocess_runner`, whose
module imports the distribution-owned `packaging` dependency. Those system
locations are beneath the fixed protected system Python authority; the
ordinary operator is not their mutation authority. Their actual root
ownership/modes and distribution provenance remain explicit real-host checks.

The protected approval manifest now requires the launcher, entry script,
package initializer, managed installer, managed runtime, diagnostic process
wrapper, and their error modules. The installed-release verifier still checks
the complete no-extra-files manifest and exact bytes/modes.

The non-root qualification operation is deliberately narrow: it accepts only
`--qualification-import-probe`, derives the simulated release root from the
actual installed payload path, requires its exact safe layout, and cannot be
entered with effective UID 0. Its optional interpreter-failure seam is read
only in that non-root qualification branch. The production root branch fixes
`/usr/bin/python3`, ignores the seam, and removes it with `env -i`.

## Environment sanitization

The protected shell payloads set a fixed minimal system `PATH`, deterministic
locale, `IFS`, and umask. The actual Python `exec` uses `/usr/bin/env -i`, so it
does not inherit caller `PYTHONPATH`, `PYTHONHOME`, `PYTHONSTARTUP`,
`PYTHONUSERBASE`, `PYTHONINSPECT`, `PYTHONWARNINGS`, `PYTHONPYCACHEPREFIX`,
`HOME`, `XDG_CONFIG_HOME`, PATH, or other credential/import redirection.

The managed installer version probe inherits only that minimal production
environment. It retains the previously qualified exact staged executable,
receipt/digest, update, and canonical-home semantics.

The adjacent Core payload was repaired consistently:

- managed-Codex verify and bind-home use the shared protected launcher;
- venv creation uses fixed `/usr/bin/python3 -I -S -B -m venv` under `env -i`
  from the protected release CWD;
- offline pip uses the fixed root-protected venv interpreter with `-I -B`,
  `HOME=/nonexistent`, `PIP_CONFIG_FILE=/dev/null`, a fixed PATH, and no caller
  environment; and
- the venv and its site-packages remain below fixed
  `/opt/research-supervisor-core/venv`, outside ordinary-operator mutation
  authority, then receive root ownership and fixed modes.

## Actual shell-to-Python boundary tests

`tests/test_managed_codex_security.py` now builds an externally approved
simulated release containing the exact production payloads, launcher, entry
script, and required production package modules. Production
`install_approved_release()` copies and verifies those bytes before the test
invokes the installed shell payload with `/bin/sh`.

The hostile test invokes both:

- `install-managed-codex.sh --qualification-import-probe`; and
- `install-core-authority-service.sh --qualification-import-probe`.

It does so from an arbitrary hostile CWD and a normal CWD. The hostile state
contains:

- a package/module matching `research_automation_supervisor` and the protected
  installer name;
- shadow `hashlib.py` transitive dependencies in CWD and `PYTHONPATH`;
- a malicious `PYTHONPATH` package;
- a user-site `.pth` execution hook;
- `PYTHONHOME`, `PYTHONSTARTUP`, `PYTHONUSERBASE`, `PYTHONINSPECT`,
  `PYTHONWARNINGS`, `PYTHONPYCACHEPREFIX`, and hostile HOME/XDG values; and
- a fake PATH-selected `python3` that would leave an unmistakable marker.

No marker is created. Both payloads report the same protected installer and
transitive module paths, the same protected CWD, the same `sys.path`, isolated
and safe-path flags, disabled user site, and the same minimal child
environment from hostile and normal callers.

The failure test proves that a missing interpreter, an operator-owned
interpreter substitute, a writable protected source root, and a missing
protected installer module all fail closed before an installer operation.

These are subprocess/runtime assertions, not source-string substitutes.
Supplemental source/contract assertions remain but are not closure evidence by
themselves.

## Production-wired contract

The existing production-wired test now starts with the actual installed
`install-managed-codex.sh` shell payload crossing the shared isolated Python
boundary with hostile CWD/Python environment values. It confirms the protected
module and CWD, then continues through production functions for:

```text
protected approval and installed release
  -> actual shell -> isolated Python -> protected installer import
  -> simulated exact managed-Codex installation
  -> protected receipt and runtime verification
  -> canonical home and readiness
  -> Sign in preparation
  -> qualified replay Worker / ordinary Auditor preparation
  -> qualified runner environment seal
  -> Schema-v2 Physics Auditor identity/home preparation
```

Separate production tests retained from R2 cover actual generic Worker and
ordinary Auditor verification, Physics fresh/retry/resume, pre-Start ordering,
and real Physics adapter execution. The new evidence extends that contract only
at the missing first transition.

The deterministic probe does not claim root UID, root ownership, `/opt`,
`/usr/bin`, or distribution provenance. Simulated authority uses the current
UID with protected modes and is clearly separate from host qualification.

## Sibling search

A bounded search covered current product scripts and administrator
documentation for `python`, `python3`, `-m`, `PYTHONPATH`, `PYTHONHOME`,
`PYTHONSTARTUP`, and `PYTHONUSERBASE`.

The immediately adjacent Core managed-Codex, venv, and pip launches shared the
defect and were repaired as described above. Remaining occurrences are:

- ordinary-user Custodian bootstrap and developer bootstrap paths;
- unprivileged preparation/report/inventory/build tools;
- acceptance/scientific sandbox Python commands; and
- historical reports, which were intentionally not modified.

No separate out-of-scope privileged administrative subsystem with this exact
defect was discovered. Ordinary-user tooling was not redesigned.

## Validation

Exact tools:

```text
/home/inaeyk/researchrepo/ras-regression-venv/bin/python  3.14.4
/usr/bin/python3                                          3.14.4
/home/inaeyk/researchrepo/ras-regression-venv/bin/pytest  9.1.1
/home/inaeyk/researchrepo/ras-regression-venv/bin/ruff    0.16.4
/home/inaeyk/researchrepo/ras-regression-venv/bin/mypy    2.3.1
/usr/bin/bwrap                                            0.11.1
Windows PowerShell                                       5.1.26100.9168
```

Final-state results:

- New actual shell-to-Python hostile/fail-closed/production-wired slice:
  **3 passed in 0.83 s**.
- Complete focused managed-Codex security file: **36 passed, 1 skipped in
  1.33 s**. The skip is the explicit real-root `/usr/bin`/`/etc`
  qualification and is not counted as PASS.
- Physics Auditor real-adapter/scoring slice: **24 passed in 4.60 s**.
- Ruff across 25 changed/new production and test Python files: **PASS**.
- Strict mypy across 16 production/script modules: **PASS**, no issues.
- Read-only Python compilation across 25 files: **PASS**.
- `bash -n` plus POSIX `sh -n` on all changed/new shell payloads: **PASS**.
- Windows PowerShell parse of `launch-research-supervisor.ps1`: **PASS**.
- Final `git diff --check`: **PASS**.
- Final `HEAD`: exact baseline `df59e2818c3519a5ba7dab69dd067b91b202e936`;
  changes remain uncommitted.

One relevant 19-file R0/PA-5C4/PA-5C4-U/Custodian/Core/qualified-runner/
Physics family was run with host process access: **388 passed, 8 skipped, 1
failed in 111.54 s**. The sole failure exposed a repair-local inventory
regression: an initial attempt to remove the distribution `packaging`
dependency added a direct `subprocess.run` callsite to
`managed_codex_installer.py`, producing transient inventory hash
`bf4389d743d56a1ab4a37482d379e0fa1bb8f5d4c75895dd1b3b45cd28a55565`
and 5 unclassified sites. That attempt was reverted. The affected final
managed-Codex file and boundary tests were rerun green, along with final static
checks; the broad family was not repeated.

The final targeted inventory check returned the established inherited failure:

```text
inventory_sha256:
f8af9d25eb89712326248105eee732df8dc56a84e8bdcc5b793caea657dc998b

categories: POST-SNAPSHOT 25, PRE-SNAPSHOT 4,
QUALIFIED-SANDBOX-INTERNAL 11, UNCLASSIFIED 4

normalized signature:
7ac0707a865519a1c1f89ec957cbea162896ecb257b60236501e2f0448b7433c
```

The final 44 callsites and the four unclassified identities exactly match the
inherited authority: `process_enforcement._run_systemctl`,
`semantic_replay.execute_replay`, `semantic_replay._git`, and
`systemd_launch_helper.main`. It is inherited/unrelated and was not
investigated, suppressed, or rebaselined after the exact match.

An initial combined managed/Windows run inside the restricted sandbox reached
**43 passed, 5 skipped** before two environment-only failures (denied WSL
vsock and loopback socket creation). The host-access family above exercised
those tests successfully. PowerShell version/parse checks likewise required
host access; two parser-harness retries corrected shell quoting and local
execution-policy handling before the parser passed. These were harness/runtime
retries, not product failures.

## Qualification-only skips and pending real-host assertions

The eight visible family skips remain pending and were not hidden or converted
to PASS:

- one real root-owned managed-Codex `/usr/bin`/`/etc` identity test;
- four fixed-path protected/root installer cases in Windows-launcher coverage;
- two actual-root/two-UID Core service qualifications; and
- one explicit network qualification.

Still pending on a qualified real host/distribution:

- actual root UID transition and exact externally installed helper/verifier
  bytes and provenance;
- actual root ownership, groups, modes, links, ancestry, and mutability of the
  helper, `/opt` release, `/usr/bin/python3` plus its system import roots,
  `/usr/bin/codex`, receipts, home authority, and Core venv/site-packages;
- real `/opt`, `/etc`, `/usr/bin`, systemd, service identity, and two-UID
  installation behavior;
- offline wheelhouse compatibility and distribution dependency provenance;
- clean-host interruption/recovery and same/update identity behavior; and
- Windows/WSL first-run, relaunch, Sign in, and end-to-end qualification.

Unprivileged subprocess evidence does not prove any of those root-owned or
distribution assertions.

## Token usage

### ACCOUNTING OBSERVATION

`CODEX_HOME` was unset in this interactive Worker session. The default global
`/home/inaeyk/.codex/bin/codex-task` and durable ledger root were present, but
no task ledger or `TaskUsageReceipt.json` matched the current exact
`CODEX_THREAD_ID`. No nested model session was launched. No raw rollout was
ingested and no token count is estimated or reconstructed.

Authoritative runtime-derived accounting is therefore unavailable before this
interactive turn completes:

```text
input_tokens: unavailable
output_tokens: unavailable
combined_tokens: unavailable
cached_input_tokens: unavailable
cache_write_input_tokens: unavailable
reasoning_output_tokens: unavailable
```

Per-session/retry breakdown:

- Worker interactive session: unavailable (no matching durable receipt).
- Auditor: unavailable / not applicable; no Auditor was launched.
- Supervisor/Custodian model session: unavailable / not applicable; none was
  launched.
- Other/nested session: unavailable / not applicable; none was launched.
- Model retries/repair/repeated audit rounds: unavailable; no additional model
  session existed to attribute.
- Non-model validation/PowerShell/inventory repair retries: token attribution
  unavailable and not estimated.

If the hosting runtime appends a machine-generated final receipt after turn
completion, that receipt—not an estimate in this report—is authoritative.

## Terminal disposition

The R1R3 candidate closes only Blockers B and C while preserving Blocker A's
closed status. It does not authorize R0, real-host installation, a campaign,
an Auditor, another repair round, a commit, or a push.
