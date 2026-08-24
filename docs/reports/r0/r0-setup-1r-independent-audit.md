# R0-SETUP-1R independent security closure audit

Date: 2026-08-23

Mode: Independent Auditor

Repository: `~/researchrepo/ras-context-integration`

Branch under review: `feature/context-economy-runtime-integration`
Qualified baseline: `df59e2818c3519a5ba7dab69dd067b91b202e936`

## Verdict

**FAIL**

**May R0-SETUP-1R proceed to real-host qualification? NO.**

The candidate substantially improves the common managed-Codex installation verifier and closes the redirectable/silently repaired credential-home defect. It is not a complete security closure. A reachable schema-v2 Physics Auditor path still chooses Codex from an operator-pinned path/digest or from `PATH` without consulting the protected installation receipt. In addition, the documented privileged entrypoint starts interpreting the release-tree shell script before any checked component establishes the identity or safe metadata of that entrypoint, and the new tests do not exercise the failing schema-v2 path or a complete production-wired install-to-launch chain.

This verdict means only that this candidate is not yet suitable to proceed to explicit real-host administrator installation plus Windows/WSL fresh-launch qualification. It does not alter the status of R0, Attempt 005, PA-5D, or PA-5D0.

## Findings by severity

### CRITICAL

None.

### MAJOR

#### MAJOR-1R-A — The qualified schema-v2 Physics Auditor bypasses the protected Codex identity

The new common path is strong: `verify_managed_codex_installation()` reads a protected strict receipt, reads the fixed executable with no-follow/stability checks, and compares the exact executable digest (`managed_codex.py:114-176`). Readiness, Sign in, qualified-runner entry, the generic replay service, and generic Worker/Code Auditor launches use that verifier.

The claim that **every Auditor** uses the same semantics is false, however:

1. `workflow_engine.run_substage()` dispatches schema-v2 inputs to `run_physics_substage()` (`workflow_engine.py:517-539`).
2. The Physics Auditor is launched/resumed through `context.physics_services.auditor_runner` / `auditor_resumer`; the call receives the software environment and a Physics-Auditor-specific invoker but no managed-Codex identity verifier (`physics_workflow.py:1319-1365`).
3. `_select_codex_executable()` accepts `config.trusted_executable`, otherwise uses `shutil.which("codex")` (`physics_auditor_execution.py:1869-1881`).
4. The so-called trusted executable validator verifies only that an operator-pinned absolute executable matches the digest supplied in that same Physics Auditor configuration. It does not require `/usr/bin/codex`, the protected installation receipt, root authority, or the protected approved-release identity (`physics_auditor_models.py:709-730`). Its own docstring calls this an “operator-pinned” identity.

The sealed production environment makes the default `PATH` fallback likely to resolve `/usr/bin/codex`, but it does not remove the explicit operator-pinned override and it does not turn the Physics Auditor selector into the common receipt-backed verifier. A schema-v2 campaign can therefore launch a different executable identity than the protected receipt authorizes. Recovery/resume uses the same bypassing Physics Auditor service path.

This directly violates mandatory MAJOR-1 checks 9-11 and the blocking condition that execution must not use a different identity than the protected receipt. MAJOR-1 remains open.

TOCTOU assessment for the repaired common path: validation reads a stable opened inode and execution later reopens the fixed path. That is not an fd-bound exec, but an ordinary operator cannot replace `/usr/bin/codex`, its protected ancestry, or the receipt. Concurrent privileged replacement is inside the administrator authority boundary. The gap is acceptable against the stated ordinary-operator boundary and is not the reason for this finding. The Physics Auditor alternate-selector bypass is independently exploitable through campaign configuration.

#### MAJOR-1R-B — The initial privileged release entrypoint is not validated before root interprets it

The downstream installer design is materially improved. It uses fixed release, manifest, artifact, staging, destination, receipt, and pending-record locations; it does not accept an operator hash or project root; it performs no network fetch; and it validates/copies from opened protected files.

The first privileged command documented in `README.md:133-140` is nevertheless:

```text
sudo /bin/sh /opt/research-supervisor-release/scripts/install-research-supervisor.sh OPERATOR
```

At that point `/bin/sh` has already opened and begun interpreting the release-tree script. The script checks its resolved pathname and release directories at `install-research-supervisor.sh:23-32`, and checks the *separate verifier* at lines 33-37, but it never checks its own owner, group, mode, link count, or bytes. The comprehensive tree verifier is not invoked until line 39. Thus an unsafe/mutable file at the expected pathname can execute privileged shell commands before the verifier can reject it.

The documents state that an external administrator/release process staged every entry root-owned and non-writable. That can be a valid trust anchor only if the administrator independently enforces/verifies it before invoking the script. The supplied invocation does not establish that precondition, and no independently trusted bootstrap component verifies the entrypoint before it is interpreted. Merely residing beneath the named “release” directory is insufficient to establish the entrypoint's byte identity or non-writability.

This leaves mandatory MAJOR-3 checks 1, 5, and the special protected-release bootstrap concern incompletely enforced. The arbitrary project-root execution from the prior audit has been removed, but the initial privileged execution boundary is still procedural rather than fail-closed. MAJOR-3 remains open.

#### MAJOR-1R-C — Tests do not close the full qualified install/runtime contract

`tests/test_managed_codex_security.py` is a meaningful improvement over the prior suite. It exercises the real installer/verifier functions against an isolated filesystem for fresh install, identical reinstall, explicit update, rejection of an unapproved update, substituted executable, missing/malformed/unsafe receipt, symlink/hardlink cases, interrupted pending state, and canonical-home initialization/tamper behavior.

The decisive cross-layer gaps remain:

- No test drives a schema-v2 Physics Auditor through the qualified services and proves that it uses the protected receipt/executable pair. The product does not currently satisfy that property.
- `test_worker_and_auditor_reverify_one_configured_codex_identity` covers only the generic schema-v1 Worker/Code Auditor path and supplies a list-appending verifier double (`test_workflow_engine.py:88-105`).
- The Sign-in/replay pair test monkeypatches `_verified_managed_codex_identity` and `_managed_codex_home` (`test_managed_codex_security.py:381-413`); it does not carry an actual simulated installer receipt through readiness, Sign in, qualified runner, Worker, and both Auditor implementations.
- The pre-consumption ordering test monkeypatches `_verify_qualified_runtime_pair` to reject (`test_managed_codex_security.py:415-443`). It proves call ordering at that seam, not agreement among installer, runtime verifier, Custodian, Core, and both execution engines.
- The credential-export test establishes disjoint paths and then source-string-checks that `auth.json` is absent from one module (`test_managed_codex_security.py:446-467`). It is not an artifact traversal/export contract test.
- The fixed installer/runtime location agreement test is a source-string assertion (`test_windows_launcher.py:102-130`), not an execution contract.
- Eight root/network/real-state cases remain explicitly qualification-only. They must remain pending, not PASS.

The suite therefore still makes important seam mocks agree with other mocks and misses a real alternate execution engine. Because the missing property is false in production code, this is a blocking MAJOR-4 closure failure rather than merely a testing preference. MAJOR-4 remains open.

### MINOR

None independently material to the verdict.

## Candidate freeze and scope

At audit freeze:

- `git rev-parse HEAD`: `df59e2818c3519a5ba7dab69dd067b91b202e936`
- HEAD equals the requested qualified baseline; baseline lineage is exact.
- The candidate is uncommitted.
- Initial `git diff --check`: PASS.
- Tracked diff stat: 21 files changed, 718 insertions, 204 deletions.

Tracked modified files:

- `README.md`
- `README_FIRST.md`
- `bootstrap.sh`
- `docs/campaign_custodian.md`
- `launch-research-supervisor.ps1`
- `pyproject.toml`
- `scripts/custodian-bootstrap.sh`
- `scripts/install-core-authority-service.sh`
- `src/research_automation_supervisor/custodian.py`
- `src/research_automation_supervisor/custodian_bootstrap.py`
- `src/research_automation_supervisor/custodian_server.py`
- `src/research_automation_supervisor/qualified_campaign.py`
- `src/research_automation_supervisor/qualified_runner.py`
- `src/research_automation_supervisor/replay_campaign_engine.py`
- `src/research_automation_supervisor/secure_cli.py`
- `src/research_automation_supervisor/workflow_engine.py`
- `tests/test_custodian.py`
- `tests/test_custodian_server.py`
- `tests/test_pa5c4_transactional_core.py`
- `tests/test_windows_launcher.py`
- `tests/test_workflow_engine.py`

Untracked files at freeze (the directory-form status entry was expanded):

- `docs/reports/r0/r0-setup-1-independent-audit.md`
- `docs/reports/r0/r0-setup-1-managed-codex-first-run-closure.md`
- `docs/reports/r0/r0-setup-1r-managed-codex-security-closure.md`
- `scripts/install-managed-codex.sh`
- `scripts/install-research-supervisor.sh`
- `scripts/prepare-managed-codex-home.py`
- `scripts/verify-protected-release.py`
- `src/research_automation_supervisor/managed_codex.py`
- `src/research_automation_supervisor/managed_codex_installer.py`
- `tests/test_managed_codex_security.py`

This audit report is the only file written by the Auditor. No product file, prior report, Git state, privileged host state, or external system was modified.

The three required R0 reports were read completely before the findings were classified. The full candidate diff against the exact baseline was inspected; the Worker closure report was used only as a list of claims, not as proof.

### Security-change map

| Classification | Candidate areas | Assessment |
|---|---|---|
| MAJOR-1 | `managed_codex.py`, `managed_codex_installer.py`, Custodian bootstrap/readiness, qualified runner/campaign, replay/workflow verifier propagation, related tests/docs | Common receipt-backed chain is substantially repaired; schema-v2 Physics Auditor remains outside it. |
| MAJOR-2 | canonical-home authority and validation, explicit prepare script, launcher/bootstrap environment sealing, Custodian/Sign-in/runner propagation, tests/docs | Closed in code for the stated ordinary-operator boundary. |
| MAJOR-3 | fixed protected-release verifier and administrator scripts, managed installer, offline Core installer, approval manifest contract, tests/docs | Downstream staging/install mechanics are strong; initial shell entrypoint trust is not established before execution. |
| MAJOR-4 | new security test module plus Custodian/Core/runner/workflow/launcher tests | Stronger component coverage, but incomplete and it misses the live Physics Auditor bypass. |
| Necessary supporting | packaging metadata, operator documentation, setup UI/status plumbing, development bootstrap compatibility | Consistent with the closure effort. |

No unrelated scope expansion was identified.

## Disposition of the four prior findings

| Prior finding | Disposition | Basis |
|---|---|---|
| MAJOR-1: exact Codex identity not enforced | **OPEN / blocking** | Common path repaired, but schema-v2 Physics Auditor accepts operator-pinned or `PATH`-selected Codex without the protected receipt verifier. |
| MAJOR-2: redirectable/repaired managed home | **CLOSED** | Production derivation is passwd-based, ambient `CODEX_HOME` is ignored, authority is root-protected, initialization is explicit, and verification-only paths fail closed. |
| MAJOR-3: privileged installer executes unchecked mutable project root | **OPEN / blocking, narrowed** | Arbitrary checkout authority was removed, but the initial release-tree shell entrypoint is interpreted before its safety/identity is checked. |
| MAJOR-4: successful install/reinstall/runtime contract untested | **OPEN / blocking, partially repaired** | Actual component simulations were added, but full production wiring and the schema-v2 Auditor are not covered. |

## Detailed re-audit

### MAJOR-1 — protected executable identity chain

For the repaired common path, the observed chain is:

```text
fixed protected approval manifest
  -> protected artifact opened and copied to same-filesystem staging
  -> staged digest/version validation
  -> fixed /usr/bin/codex replacement
  -> root-protected exact-digest receipt
  -> common runtime receipt/executable verifier
  -> readiness / Sign in / qualified runner
  -> generic replay Worker and Code Auditor
```

The common verifier rejects a pending generation, missing/malformed receipt, unsafe receipt metadata, unexpected executable path, unsafe executable metadata, and receipt/executable digest disagreement. Root ownership alone is not treated as byte identity. The installer and verifier use fixed production locations. Protected ancestry and exact file modes/link counts prevent the ordinary operator from forging the receipt or executable.

A stdlib-only local probe of the actual installer/verifier functions confirmed fresh simulated install, identical reinstall, runtime digest identity, rejection of a substituted executable, rejection of a missing receipt, rejection of an unapproved update, and acceptance only after an explicit predecessor-authorized update.

The verifier does not cryptographically authenticate `release_id` or `version` against a retained release manifest at runtime: if a party able to rewrite the protected receipt changes either to another syntactically valid value while retaining the executable digest, the receipt is accepted. The ordinary operator cannot perform that rewrite under the enforced production ownership/mode boundary, so this is not an additional blocking ordinary-operator path. Digest, schema, path, ownership, mode, link, ancestry, and executable disagreement tampering are rejected.

The chain forks at schema-v2 Physics Auditor execution, as detailed in MAJOR-1R-A. Consequently the mandatory “same identity semantics” and “no alternate path” claims fail.

### MAJOR-2 — canonical credential home

There is one production derivation: current UID -> passwd home -> `.local/share/research-automation-supervisor` -> `codex-home` (`managed_codex.py:200-215`). It does not consume caller `CODEX_HOME`, `HOME`, `XDG_DATA_HOME`, PATH, NVM, or normal `~/.codex` state.

The root-protected home-authority receipt binds the exact operator UID, data root, and Codex home (`managed_codex.py:218-252`). Explicit initialization creates the private runtime/home directories and a no-follow/exclusive 0600 binding only when there is no partial or unrelated prior state (`managed_codex.py:267-323`). Normal verification performs no creation or repair (`managed_codex.py:326-337`). Validation enforces canonical location, private directories, exact ownership/modes, and exact binding content (`managed_codex.py:358-390`). The compatibility helper that can construct test authority is documented and confined to test/acceptance use; production launch calls the root-authority path.

The launcher distinguishes first-time initialization from normal verification. Relaunch, readiness, Sign in, qualified-runner sealing, Worker, generic Auditor, resume, and recovery reuse the same home. Even the Physics Auditor identity bypass receives the sealed software environment containing this canonical `CODEX_HOME`; no separate home redirect was found.

The audit searched for `auth.json`, CODEX-home traversal, copying, snapshots, reports, export sources, launcher evidence, and usage-receipt handling. The live-shadow mechanism bind-mounts the source credential file read-only into an isolated runtime location; it does not copy it into Core or campaign evidence. Campaign exports use explicit artifact sources, and the canonical home is outside authority/workspace/export trees. No credential-copy route into Core state, bundle, workspace, snapshots, reports, exports, evidence, or usage ledgers was found.

A stdlib-only local probe confirmed first initialization and exact reuse, 0700 home state, rejection of an unsafe binding mode, and that verification did not repair the unsafe binding.

MAJOR-2 is closed.

### MAJOR-3 — installer and release authority

The managed installer correctly fixes the release root, approval manifest, artifact, executable, completed receipt, pending receipt, and home-authority destinations. It validates protected directory/file ancestry and metadata, disallows symlinks/hardlinks, reads approval/artifact through safe descriptors, detects source changes while copying, hashes the staged bytes, version-probes the staged file, atomically installs it, writes protected state, fsyncs directories, and calls the common runtime verifier. It uses an explicit `update_from_sha256` authority for a changed installation. Same identity is idempotent; missing/split/malformed/pending/unapproved states fail closed. No launcher path implicitly updates privileged state, and updating ordinary RAS checkout code does not change the protected approval.

The administrator scripts use fixed PATHs and fixed destinations, accept no caller-supplied root/hash/artifact/destination, and use offline fixed wheels rather than network installation. No `curl | sh`, `npm install`, remote fetch, or equivalent was found.

The pending marker prevents runtime acceptance during replacement. The completed receipt is atomically written before the pending marker is removed, and the common verifier runs after removal (`managed_codex_installer.py:235-270`). This does not produce a fail-open runtime state because the verifier rejects while pending exists and independently verifies the final bytes afterward, although the literal Worker wording “receipt only after final verification” is not the exact operation order.

The unresolved bootstrap boundary is MAJOR-1R-B. Downstream protected-tree verification cannot retroactively make the already interpreted top-level script trusted.

### MAJOR-4 — required coverage matrix

| # | Required deterministic property | Audit result |
|---:|---|---|
| 1 | Fresh setup without managed Codex blocks before Start | Covered at Custodian seam; actual suite could not be executed here. |
| 2 | Simulated successful approved installation | Covered with actual installer function. |
| 3 | Exact staged digest validation | Installer code enforces it; successful and artifact-safety tests exist, but no clearly isolated wrong-digest negative was identified. |
| 4 | Exact final installed digest validation | Covered by installer/runtime simulation and substitution rejection. |
| 5 | Protected receipt creation | Covered for simulated ownership/mode contract. |
| 6 | Runtime verification of installed identity | Covered directly in installer test. |
| 7 | Root-looking substituted executable rejected | Digest substitution covered; true root metadata remains real-host-only. |
| 8 | Missing receipt rejected | Covered. |
| 9 | Malformed/tampered receipt rejected | Malformed, unsafe-mode, hardlink, and digest disagreement covered; syntactically valid metadata-field mutation is not covered. |
| 10 | Same-identity reinstall | Covered. |
| 11 | Explicit distinct-identity update | Covered. |
| 12 | Unapproved update rejected | Covered. |
| 13 | Staging symlink/path attacks | Artifact/target symlink and mutable ancestry covered; traversal is principally fixed by layout validation. |
| 14 | Fixed destination enforcement | Mostly source/layout assertions; real fixed destinations are qualification-only. |
| 15 | Canonical-home initialization | Covered. |
| 16 | Exact-home reuse | Covered. |
| 17 | Ambient `CODEX_HOME` cannot redirect | Covered. |
| 18 | Missing/tampered binding fails closed | Covered across missing, content, mode, hardlink, symlink, ancestor, and authority cases. |
| 19 | Sign in uses same identity/home | Wiring tested with monkeypatched identity/home, not the actual installed receipt chain. |
| 20 | Worker uses same pair | Generic workflow verifier seam covered; no full production chain. |
| 21 | Auditor uses same pair | **False:** Physics Auditor bypass; generic Code Auditor seam only. |
| 22 | Resume/recovery cannot bypass | Generic verifier propagated; **false/incomplete for Physics Auditor resume**. |
| 23 | PATH/NVM remains irrelevant | Common path covered; Physics Auditor still has a `PATH` fallback and operator-pinned override. |
| 24 | Pre-Start ordering | Seam ordering covered; code trace confirms Custodian check before Core creation. |
| 25 | Credentials excluded from state/artifacts/exports | Only weak path/source-string test; code inspection found no copy route. |
| 26 | Installer/runtime exact contract agreement | Core constants agree; test is substantially source-string based. |
| 27 | Qualification-only tests separated | Yes; eight are explicitly skipped. They remain pending, not PASS. |

The reported `165 passed, 8 qualification-only skipped, 1 inherited failure` is a Worker-reported result, not an independently reproduced result in this environment. The absence of local test tools is recorded below and is not used as a substitute for the code/test findings.

## Pre-Start authority ordering

The actual new-campaign flow is:

```text
browser Preview display
  -> Custodian snapshot-complete environment/readiness
  -> Custodian Start lock and second snapshot-complete readiness check
  -> Core create_start_intent (durable Start authority)
  -> Custodian verifies the returned intent
  -> qualified runner entry verifies managed executable + home
  -> start_qualified_launch verifies the pair again
  -> Core consumes launch intent
  -> workflow execution
```

Browser readiness is informational and not counted as authority. The decisive check is server-side: `Custodian._start_once()` performs `environment(snapshot_complete=True)` and rejects before building/calling `core.create_start_intent()` (`custodian.py:530-560`). Therefore, through the product Custodian Start path, the managed prerequisites verify before Core creates durable Start authority. This is stronger than merely blocking consumption after creation.

The qualified runner also verifies the pair while sealing its production environment (`qualified_runner.py:155-173`). `start_qualified_launch()` verifies again before `consume_start_intent_for_qualified_launch()` (`qualified_campaign.py:82-103`). Thus launch-intent consumption cannot proceed merely because an earlier browser readiness result or durable record exists.

Custodian respond/continue and qualified replay/recovery rebuild/reverify the common pair. Generic Worker/Code Auditor launches call the propagated verifier immediately before model invocation. This accurate ordering does not cure the later schema-v2 Physics Auditor selector: after the campaign has valid authority and the common pair was checked, that component can choose another executable for its actual launch.

## Inherited process-inventory failure

The inventory verifier was run once. It failed with the same four previously classified subprocess callsites. Independent normalization reproduced exactly:

- inventory hash: `f8af9d25eb89712326248105eee732df8dc56a84e8bdcc5b793caea657dc998b`
- normalized failure signature: `7ac0707a865519a1c1f89ec957cbea162896ecb257b60236501e2f0448b7433c`

Both match the prior independent-audit authority exactly. The failure is inherited and unrelated. It was not investigated further, rebaselined, or suppressed.

## Validation results

### Executed successfully

- Initial and final `git diff --check`: PASS.
- Shell syntax:
  - `bash -n bootstrap.sh`: PASS.
  - `sh -n` on all changed/new shell scripts: PASS.
- Read-only Python `compile()` check on all changed/new Python source and test files: PASS (18 files).
- Stdlib-only direct probe of the actual managed installer/runtime/home modules:
  - fresh install: PASS;
  - same-identity reinstall: PASS (`unchanged`);
  - runtime exact digest: PASS;
  - substituted executable rejection: PASS;
  - missing receipt rejection: PASS;
  - unapproved update rejection: PASS;
  - explicit predecessor-authorized update: PASS;
  - home initialize/reuse: PASS;
  - unsafe binding rejection without repair: PASS.
- Process inventory: expected inherited failure, hashes exactly unchanged as above.

### Requested but unavailable in the established environment

- Focused security pytest run: **NOT RUN**; `/usr/bin/python3: No module named pytest`.
- Relevant 11-file R0-SETUP-1 / PA-5C4 / Custodian / Core / qualified-runner family: **NOT RUN** for the same reason. The `165/8/1` result was not independently reproduced.
- Ruff: **NOT RUN**; executable/module unavailable.
- strict mypy: **NOT RUN**; executable/module unavailable.
- PowerShell parse/syntax check: **NOT RUN**; neither `pwsh` nor `powershell` is available.

No packages were installed, no network was accessed, and no real privileged installation or scientific campaign was run, in accordance with the audit constraints. Qualification-only skips were not marked PASS.

## Real-host-only assertions still pending

Even after the blocking code/test findings are repaired, these assertions remain pending for the explicitly separate real-host qualification:

- administrator-staged release ownership, group, modes, no-link status, and external provenance on a fresh host;
- actual standalone ELF/version behavior at the fixed release and `/usr/bin/codex` paths;
- real root-owned receipt, pending record, and home-authority creation under `/etc`;
- service user/group provisioning and Core service ownership/mode behavior across caller umasks;
- fresh Windows/WSL first launch, relaunch, and Sign-in using the same canonical home;
- real-host same-identity reinstall, explicitly approved update, and interrupted-install administrator recovery;
- offline wheel/wheelhouse installation on the target distribution;
- PowerShell launcher parsing and execution in the established Windows environment;
- real credential usability and confidentiality containment without copying credential bytes into campaign/Core artifacts.

None of these pending assertions was credited as PASS, and this audit does not authorize running them.

## Token usage

This interactive Auditor runtime exposes no authoritative non-interactive `turn.completed.usage` receipt. `CODEX_HOME` is unset, so no current `$CODEX_HOME/bin/codex-task` task identity or durable ledger was available to query without guessing. Per the accounting rules, no token value is estimated or reconstructed.

| Metric | Authoritative value |
|---|---:|
| `input_tokens` | unavailable |
| `output_tokens` | unavailable |
| `combined_tokens` | unavailable |
| `cached_input_tokens` | unavailable |
| `cache_write_input_tokens` | unavailable |
| `reasoning_output_tokens` | unavailable |

Per-session breakdown:

- Current independent Auditor session: unavailable.
- Prior interactive Worker session: unavailable (no authoritative receipt exposed).
- Prior interactive Auditor session: unavailable (no authoritative receipt exposed).
- Supervisor/Custodian or other model session: unavailable / none launched by this audit.

Retry/repair/repeated-audit token breakdown: unavailable; this audit launched no additional model session and did not perform a repair round.

**ACCOUNTING OBSERVATION:** exact token accounting is unavailable and therefore not satisfied. This observation does not change the security verdict.
