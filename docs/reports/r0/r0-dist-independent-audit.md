# R0-DIST final independent audit

Date: 2026-08-24

Mode: Independent read-only Auditor

Repository: `/home/inaeyk/researchrepo/ras-context-integration`

Branch: `feature/context-economy-runtime-integration`

Baseline / unchanged `HEAD`: `8a3a0297824c16d5ec4c16d5e5a5395f5b394ffb`

## Verdict

**FAIL**

**May R0-DIST proceed to real-host qualification? NO**

The protected-copy, candidate-verification, protected-release, managed-Codex, receipt,
and runtime-verification mechanics passed deterministic inspection and testing. The
supported unprivileged preparation procedure does not, however, run successfully on
the target host: its fixed system-Python invocation is rejected by the host's PEP 668
externally-managed-environment guard before it can establish either candidate output.
The qualification tests bypass that command path. Consequently the documented
bootstrap cannot currently produce the inputs that the administrator procedure
requires.

Previous R0-SETUP blockers A, B, and C remain closed. This audit found no R0-DIST
regression of those boundaries and does not reopen them.

## Findings

### BLOCKING — the documented preparation command cannot create the release candidate

`docs/protected_release_bootstrap.md:12-20` instructs the ordinary user to execute
`./scripts/prepare-protected-release.py prepare ...`. The executable has mode `0755`
and a fixed `#!/usr/bin/python3 -I` shebang (`scripts/prepare-protected-release.py:1`).
Preparation then launches `sys.executable -I -m pip ... install --dry-run
--ignore-installed --no-index --only-binary=:all:`
(`release_preparation.py:509-535`). With the documented direct invocation,
`sys.executable` is the host `/usr/bin/python3`.

An audit reproduction used the documented executable, absolute scratch output paths,
the resolved regular system ELF as the native-artifact stand-in, and a complete
synthetic offline wheelhouse. It failed with:

```text
ERROR: offline wheelhouse does not resolve the complete compatible dependency closure
```

Running the same isolated system-pip command directly exposed the cause:

```text
error: externally-managed-environment
...
You can override this ... by passing --break-system-packages.
```

This failure occurs even with `--dry-run`; no install or network access occurred. The
preparation code does not pass that override, create/use a scratch virtual environment,
or otherwise select a pip execution mode accepted by this host.

The passing distribution test does not cover the supported command. Its `_prepare()`
helper calls `prepare_protected_release()` in-process under the regression virtual
environment (`tests/test_protected_release_distribution.py:73-90`), where pip is not
PEP-668-blocked. The only direct execution of `scripts/prepare-protected-release.py` in
that file selects `verify`, not `prepare`
(`tests/test_protected_release_distribution.py:187-204`). Explicitly invoking the CLI
with the regression-venv Python succeeded diagnostically, confirming the distinction,
but that is not the documented procedure.

This is blocking because the supported procedure cannot produce
`/var/tmp/research-supervisor-release-candidate` and
`/var/tmp/research-supervisor-release-authority-candidate`; therefore it cannot
establish the reviewed helper, verifier, and approval bytes that the first
administrator transition requires. It matches the stated blocking condition that the
bootstrap documentation cannot actually establish the protected authority.

### Non-blocking findings

None.

## Contract results apart from the blocker

1. **Candidate contents and exact identity: PASS under the exercised venv path.**
   Preparation produced identical standalone helper/verifier bytes, strict bootstrap
   inventory with `"authority_is_trusted":false`, canonical approval manifest,
   native-ELF stand-in and managed approval, deterministic product wheel, all package
   sources and configured Physics data, protected payloads/service unit, and the
   supplied offline wheelhouse. Every candidate file was covered by exact relative
   path, `0644`/`0755` mode, and SHA-256. The helper/verifier SHA-256 is
   `f0b1d98f8988540ce0a6ff6d1e8d2c2697032e21c5cfebe730f55764c34bf290`.
2. **Candidate rejection behavior: PASS.** The committed tests rejected stable
   missing, extra, byte-altered, and mode-altered files. Additional audit probes
   rejected malformed JSON, `..` traversal, approval-manifest symlink substitution,
   and candidate-file symlink substitution in both unprivileged verification and the
   protected installer.
3. **First privileged transition design: PASS subject to real-host evidence.** Root is
   instructed to use fixed trusted `/usr/bin/install` only to copy data into fixed
   protected paths, then hash the three root-owned copies and compare them with values
   independently recorded outside mutable staging. Only after the comparison does root
   execute the installed helper. The unprivileged inventory explicitly disclaims
   authority and is not installed as authority.
4. **Fixed production destinations: PASS.** The implementation retains
   `/usr/libexec/research-supervisor/`,
   `/usr/share/research-supervisor-release-authority/`,
   `/var/tmp/research-supervisor-release-candidate`,
   `/opt/research-supervisor-release`, `/usr/bin/codex`,
   `/var/lib/research-supervisor-release-authority/installed-release-v1.json`, and the
   existing managed-Codex receipt locations. Privileged dispatch does not accept a
   release, candidate, authority, interpreter, receipt, or executable destination.
5. **Privileged fetch/build/interpreter boundary: PASS by inspection and regression.**
   No privileged network fetch, npm operation, or package build was found. Privileged
   Python is the fixed `/usr/bin/python3` or the fixed venv interpreter created by it;
   pip is no-index and binary-only. Caller `PATH`, Python environment, and checkout
   import roots do not select the privileged interpreter or application code.
6. **Causal protected-release chain: PASS in simulation below the real-root dispatch.**
   The production `install_approved_release()` path created the exact protected tree
   and protected receipt; the installed protected shell/Python path invoked the real
   managed-Codex installer, created its executable and receipt, and the runtime verifier
   consumed those artifacts. The established R4 test further reached readiness,
   authentication preparation, Worker/Auditor preparation, qualified-runner sealing,
   and Physics fresh/retry/resume identity resolution.
7. **Privileged documentation boundary: PASS.** The administrator procedure never
   directs root to execute a checkout or mutable candidate script. The fixed helper is
   executed only after root-owned copies have been hashed and compared.
8. **Test quality: meaningful but incomplete at the blocking boundary.** The new tests
   exercise actual preparation logic, exact wheel/candidate content, behavioral
   rejection, protected copy/receipt verification, and the installed managed-Codex
   shell/Python/runtime chain. The documentation checks are supplemental source-string
   assertions. The missing direct `prepare` CLI test is material because it concealed
   the blocking PEP 668 incompatibility.

## Validation

Tools came from `/home/inaeyk/researchrepo/ras-regression-venv` except for the explicitly
tested fixed system Python/shell paths and the existing Windows PowerShell parser. No
package, network, root, protected-host, service, campaign, or repository mutation apart
from this required report was performed; validation scratch data was temporary.

- R0-DIST plus complete managed-Codex security tests: **42 passed, 1 skipped in
  3.08 s**. The real-root `/usr/bin` and `/etc` qualification skip is pending, not a
  pass.
- R4 production-wired/hostile/missing-authority/missing-entrypoint/root-rejection
  boundary slice: **5 passed in 1.23 s**.
- Documented direct CLI `prepare` reproduction: **FAIL**, caused by the host system
  Python's PEP 668 externally-managed-environment rejection.
- Explicit regression-venv CLI preparation plus malformed/traversal/manifest-symlink/
  candidate-symlink audit probes: **PASS**. This is diagnostic evidence, not a substitute
  for the failed supported command.
- Focused process-inventory tests: **1 passed, 1 inherited failure in 1.46 s**. The
  reviewed inventory digest is
  `fce827dd9eb5da213c6349979b5e64f0e63dc7b8c68ef3d7a71637644b9343e5`;
  all three R0-DIST subprocess callsites are `PRE-SNAPSHOT`. The failure remains only
  the four baseline-unclassified callsites in `process_enforcement.py`,
  `semantic_replay.py` (two), and `systemd_launch_helper.py`.
- Ruff `0.16.4` on all six changed/new Python files: **PASS**. Whole-repository Ruff
  reproduced only inherited `SIM102` at
  `src/research_automation_supervisor/semantic_decomposition.py:562`; the file is
  untouched from the stated baseline and was not repaired.
- Mypy `2.3.1 --strict --no-incremental` over the package plus the new CLI script:
  **PASS, 88 source files**.
- Read-only Python compilation over all six changed/new Python files: **PASS**.
- GNU Bash and POSIX-shell parsing over the four protected shell payloads: **PASS**.
- Windows PowerShell parsing of `launch-research-supervisor.ps1`: **PASS**.
- Final tracked `git diff --check` and separate no-index checks of every untracked
  candidate file, including this report: **PASS**.

An initial direct-CLI diagnostic supplied `/usr/bin/python3` as a symlink and was
correctly rejected by the plain-file artifact requirement. The corrected reproduction
used its resolved regular ELF and reached the independently reproduced PEP 668 blocker.

## Exact host-only assertions still pending

These assertions remain pending and must not be attempted until the blocking supported
preparation path is corrected and independently re-audited:

1. The supported ordinary-user command succeeds on the target host with the audited
   native Codex ELF and actual complete binary wheelhouse, and creates both fixed
   `/var/tmp` outputs without network access or installation.
2. The actual candidate manifest covers every candidate byte with the expected path,
   mode, and digest; the native Codex identity/version and every wheel's provenance are
   independently approved; and the exact approval/helper/verifier hashes are recorded
   outside mutable staging.
3. The administrator uses only the documented fixed `/usr/bin/install` commands; the
   protected helper, verifier, and approval are root-owned, non-linked, correctly
   moded, beneath safe root-owned ancestry; and all three protected hashes exactly
   match the independent record before either new executable runs.
4. The first newly installed helper executes as real root from exactly
   `/usr/libexec/research-supervisor/install-protected-release`, under the actual
   trusted `/usr/bin/python3`/standard-library/site policy, and rejects helper,
   verifier, approval, candidate, ancestry, and concurrent-substitution failures on the
   real filesystem.
5. The helper creates the exact root-owned `/opt/research-supervisor-release` and
   `/var/lib/research-supervisor-release-authority/installed-release-v1.json`, and the
   fixed verifier validates both after installation and after a repeated invocation.
6. The protected product installer performs an offline-only install, places the exact
   approved executable at `/usr/bin/codex`, creates the actual protected managed-Codex
   receipt, and the runtime verifier accepts that exact executable/receipt pair.
7. Real interruption/recovery, Core service/two-UID behavior, canonical managed
   `CODEX_HOME`, authentication, and the Windows-to-WSL launch path retain the already
   qualified identities on the real host.

No real-root qualification skip is counted as satisfying any item above.

## Token usage

### Authoritative counters unavailable

The exact Auditor thread identity is
`01a0346d-407d-7260-91c4-326956f048bb`. `CODEX_HOME` is unset; the default persistent
`/home/inaeyk/.codex/bin/codex-task` and durable task-ledger root are present. No
`TaskUsageReceipt.json` under that ledger matches this exact thread. Receipts belonging
to other tasks were not attributed, no raw rollout transcript was read, and no count
was estimated or reconstructed. Final-output usage cannot be authoritative before this
turn emits `turn.completed`.

```text
input_tokens: unavailable
cached_input_tokens: unavailable
output_tokens: unavailable
reasoning_output_tokens: unavailable
combined_tokens: unavailable
```

Per-session and retry attribution:

- Worker session: unavailable; the Worker report is not an authoritative matching
  runtime receipt.
- Auditor session: unavailable; no matching completed receipt exists.
- Supervisor/Custodian session: unavailable / not applicable; none was launched.
- Other or delegated model sessions: unavailable / not applicable; none was launched.
- Model retries, repairs, and repeated audit rounds: unavailable. Validation-command
  retries are not model sessions, and no token attribution is fabricated for them.

Accounting unavailability does not alter the blocking verdict.

## Terminal disposition

**R0-DIST may not proceed to real-host administrator qualification.**

No repair, commit, push, sudo operation, protected installation, service change,
network access, campaign, or follow-on work was performed.
