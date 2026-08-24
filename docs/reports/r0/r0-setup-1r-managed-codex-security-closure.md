# R0-SETUP-1R — Managed Codex security closure

Date: 2026-08-23  
Repository: `ras-context-integration`  
Branch: `feature/context-economy-runtime-integration`  
Qualified baseline and current HEAD: `df59e2818c3519a5ba7dab69dd067b91b202e936`  
Disposition: the four blocking findings in the independent R0-SETUP-1 audit are repaired in the uncommitted working tree. This report does **not** claim R0 PASS.

## Scope and reconstruction

Before editing, the exact HEAD, branch, `git status --short`, tracked diff stat, `git diff --check`, both R0-SETUP-1 reports, and the complete current R0-SETUP-1 diff were inspected. The initial tracked diff was 18 files with 456 insertions and 181 deletions; `git diff --check` was clean. The independent report remained authoritative and was not modified.

The blocking defects and the repair plan reconstructed from the audit were:

| Finding | Defect located before repair | Repair target | Contract tests |
| --- | --- | --- | --- |
| MAJOR-1 | Readiness and Sign in accepted a fixed root-owned/non-writable `/usr/bin/codex`, while Worker and Auditor had no shared exact-byte verifier. A different root-owned executable could therefore retain qualification. | One receipt-backed verifier shared by setup/readiness, Sign in, qualified-runner, Worker, and Auditor paths. | Fresh install/runtime identity, substitution and receipt failures, exact pair propagation, pre-Start ordering, Worker/Auditor re-verification. |
| MAJOR-2 | The product path was derived from caller-influenced data-root/environment state, and the preparation helper could create or repair home/binding state during normal launch. | Protected operator/home authority, passwd-derived canonical product data root, explicit initialize operation, verification-only normal launch. | Initialization/reuse, environment override irrelevance, missing/malformed/symlink/wrong-mode/wrong-content/hard-link failures without repair. |
| MAJOR-3 | The administrator entry point accepted a mutable project root and caller-supplied artifact/hash, then loaded project Python under privilege. | Fixed protected release authority with a protected approval manifest, standalone pre-import release validation, fixed destination, staged-byte validation, atomic generation receipt. | Deterministic install/update simulation, mutable/symlink staging rejection, unsafe destination rejection, interrupted-generation failure. |
| MAJOR-4 | Tests injected already-qualified prerequisites at unit seams and did not cross install, reinstall, runtime, home, Sign in, Worker, Auditor, and Start-authority layers. | Add one deterministic cross-layer security suite plus actual workflow and Custodian journey assertions. | The 20 requested contract areas are covered as described below; root-only checks remain explicit skips. |

No PA-5D, Attempt 005, scientific workflow, UI, network, package installation, privileged host mutation, real campaign, or independent Auditor run was performed.

## Resulting trust chain

The production authority chain is now:

1. An external administrator/release process stages a complete release at the fixed `/opt/research-supervisor-release` path. An ordinary mutable checkout is explicitly **not** an installation authority.
2. The release tree, its ancestry, standalone verifier, approval manifest, and Codex artifact must be root-owned, non-group/world-writable, non-symlinked, and fixed to the release contract. The approval is `/opt/research-supervisor-release/managed-codex-approval-v1.json`; the only Codex artifact is `/opt/research-supervisor-release/artifacts/codex`.
3. Before protected project Python is imported, `scripts/verify-protected-release.py` validates the protected release tree. The administrator scripts only execute from that fixed protected release. They do not accept a project root, artifact, digest, destination, or `CODEX_HOME` from the caller.
4. The approval manifest names the release ID, exact SHA-256 digest, exact version, fixed artifact relative path, and any explicit predecessor digest authorized for an update. A digest supplied by the ordinary operator has no authority.
5. The installer stages from one safely opened source descriptor into a destination-directory temporary file, hashes the copied bytes, checks source stability and metadata, checks the staged executable/version, records a protected pending generation, atomically replaces the fixed executable and protected receipt, fsyncs the relevant directories, removes the pending marker, and invokes the same runtime verifier before reporting success.
6. Runtime qualification reads the protected receipt and executable with no-follow opens, validates fixed paths, complete ancestry, owner/group, exact mode, link count, stable file metadata, strict receipt schema, supported exact version/release fields, and the SHA-256 of the opened executable bytes. The returned identity also records device and inode.
7. Setup/readiness, Sign in, qualified-runner entry, replay/resume, and each Worker and Auditor launch consume this common verifier and the same verified canonical credential home.

The trusted administrator/root release authority remains inside the threat model. Runtime validation is performed immediately before the launch chain and again before each Worker/Auditor model invocation. Because the ordinary operator cannot mutate the executable, receipt, pending marker, or their protected ancestry, the verified bytes cannot be substituted by that operator between validation and launch. This does not claim protection against a malicious administrator concurrently replacing root-protected state; such an administrator is the installation authority.

## Managed Codex installation identity lifecycle

The canonical production contract is centralized in `managed_codex.py`:

- executable: `/usr/bin/codex`;
- completed receipt: `/etc/research-supervisor-core/managed-codex-install-v1.json`;
- pending-generation marker: `/etc/research-supervisor-core/managed-codex-install-pending-v1.json`;
- approving release: the fixed protected manifest and artifact above.

Lifecycle behavior is explicit:

- **Absent identity:** readiness is setup-required and no qualified Start can consume Core authority.
- **Fresh install:** both destination and receipt must be absent, and the protected approval must be a fresh approval rather than an update. Only the validated staged bytes are installed. A protected receipt containing their digest, version, and release ID is recorded.
- **Identical reinstall:** an already verified installation with the exact approved digest/version returns deterministic `unchanged` behavior.
- **Distinct update:** accepted only when the new protected approval explicitly names the current verified digest in `update_from_sha256`; it returns `updated` behavior.
- **Unexpected executable, split executable/receipt state, or stale pending marker:** fails closed. There is no implicit recovery.
- **Failed/interrupted installation:** the pending marker remains, so neither runtime verification nor another normal install can treat the partial generation as complete. Recovery is an explicit administrator operation.
- **Runtime substitution or receipt failure:** absent, malformed, writable, linked, wrongly owned/mode receipt; digest disagreement; executable substitution; unsafe ancestry; or a pending generation all fail closed.

Version remains a release-contract field and staged sanity check; it is not used as a substitute for exact installed-byte identity.

## Canonical `CODEX_HOME` lifecycle

The installer creates a protected create-once authority at `/etc/research-supervisor-core/managed-codex-home-v1.json`. It binds the selected ordinary operator UID to exactly:

`<passwd home>/.local/share/research-automation-supervisor/codex-home`

The path is derived from the protected setup operation and the system account database, not from ambient `CODEX_HOME`, `HOME`, `XDG_DATA_HOME`, a launcher data-root option, or a campaign record. Production launch rejects a data-root override; the isolated acceptance backend retains an explicitly gated fake-root seam only for deterministic tests.

The two phases are separate:

- **Explicit first initialization:** the first-run command validates the protected authority and safe data ancestry, requires a clean application data root, then exclusively creates the private runtime directory, `codex-home`, and exact `runtime/managed-codex-home-v1` binding. Partial prior state is rejected rather than completed.
- **Normal relaunch/reuse:** validation only. It requires the protected authority, exact canonical path, operator ownership, private directory modes, exact single-link binding content/mode/owner, and safe non-symlink ancestry. It does not create, chmod, rewrite, or repair anything.

Missing, malformed, redirected, linked, wrongly owned/mode, or tampered authority/home/binding state therefore fails before Start. An environment `CODEX_HOME` override is ignored and cannot redirect Sign in, qualified-runner, Worker, or Auditor execution.

Credential material remains exclusively beneath this external private home. `auth.json` is not an allowed campaign input, snapshot source, artifact/report/bundle/export source, or Core-private campaign-state path.

## Privileged installer threat model

The old trust inversion—running Python or shell from an operator-selected mutable checkout under privilege—has been removed. The fixed administrator entry points require their own resolved paths and complete release ancestry to match the root-protected staging contract. The standalone verifier runs before `PYTHONPATH` is set to the already-validated protected release. Product installation uses a prebuilt fixed wheel and protected offline wheelhouse with `--no-index` and binary-only resolution; there is no `curl`, npm, network bootstrap, or privileged build from the ordinary project root.

Destination paths are fixed by code. The installer rejects symlinked/mutable release content, linked artifacts/receipts, unsafe ancestors, and unsafe destination directories. It validates and copies from the same opened source descriptor, installs from the validated target-directory staging file, uses atomic replacement, records incomplete generations, and verifies final ownership, mode, receipt, and digest with the common runtime verifier.

The protected release must be provisioned by an auditable administrator/release mechanism outside this ordinary checkout. This repair deliberately makes that boundary explicit; it does not pretend that code merely present in a user-writable checkout has privileged authority.

## Pre-Start and resume ordering

The qualified path now verifies the exact managed executable identity and canonical home before consuming Core Start authority. Custodian `start`, `continue`, and response/recovery paths re-evaluate the complete environment gates before launching the qualified runner. The qualified runner again verifies the pair before every operation and supplies only the verified home after clearing the ambient environment. Authentication readiness is determined only using that pair.

Replay/resume services carry the same fixed executable and verifier. The workflow engine calls the verifier immediately before Worker and Auditor invocations. Tests prove that an identity failure occurs before the Core start-consumption call, that a later readiness failure prevents runner launch during both continuation and response, and that neither recovery nor a stored campaign record creates an alternate authority route.

Thus no campaign action becomes authorized/launched until managed installation identity, canonical home, authentication, and the inherited setup gates all pass.

## Contract coverage

`tests/test_managed_codex_security.py` and the updated family tests cover the requested gap as follows:

1. absent managed identity produces setup-required readiness and prevents Start;
2. deterministic successful staged-byte installation, protected receipt, and final common-verifier acceptance;
3. runtime verification of that exact installation;
4. root-owned-looking different bytes rejected by digest;
5. missing, malformed, unsafe, or multiply linked receipt rejected;
6. identical reinstall is deterministic and idempotent;
7. a distinct identity requires an explicit protected predecessor digest and reports update;
8. unsafe destination, source symlink, mutable release ancestor, and interrupted generation rejected;
9. canonical home first initialization;
10. normal relaunch reuses the exact directory/inode;
11. environment override cannot redirect the verified home;
12. missing/tampered/malformed/linked/wrong-mode binding fails without repair;
13. Sign in receives the verified executable and home pair;
14. Worker receives and re-verifies the same pair;
15. Auditor receives and re-verifies the same pair;
16. PATH/NVM Codex remains irrelevant;
17. unsafe prerequisites block creation/consumption of Start authority and later launch;
18. credential paths are disjoint from campaign and export source allowlists;
19. installer and runtime source contracts share the same canonical constants and verifier;
20. checks that require actual root ownership, fixed `/usr/bin`/`/etc`, two UIDs, systemd, or network remain honestly marked qualification-only.

The deterministic installer tests substitute isolated ownership authority and paths to exercise pure lifecycle logic. They are not represented as real-host root qualification. Earlier fake-root component-installer tests that could not establish the new fixed protected release boundary were converted to explicit qualification-only skips instead of manufacturing root trust.

## Files in the uncommitted R0-SETUP-1/R0-SETUP-1R working tree

Security contract and installer:

- `src/research_automation_supervisor/managed_codex.py`
- `src/research_automation_supervisor/managed_codex_installer.py`
- `scripts/verify-protected-release.py`
- `scripts/install-managed-codex.sh`
- `scripts/install-research-supervisor.sh`
- `scripts/install-core-authority-service.sh`
- `scripts/prepare-managed-codex-home.py`
- `scripts/custodian-bootstrap.sh`
- `launch-research-supervisor.ps1`
- `pyproject.toml`

Runtime integration:

- `src/research_automation_supervisor/custodian.py`
- `src/research_automation_supervisor/custodian_bootstrap.py`
- `src/research_automation_supervisor/custodian_server.py`
- `src/research_automation_supervisor/qualified_campaign.py`
- `src/research_automation_supervisor/qualified_runner.py`
- `src/research_automation_supervisor/replay_campaign_engine.py`
- `src/research_automation_supervisor/secure_cli.py`
- `src/research_automation_supervisor/workflow_engine.py`

Tests and documentation:

- `tests/test_managed_codex_security.py`
- `tests/test_custodian.py`
- `tests/test_custodian_server.py`
- `tests/test_pa5c4_transactional_core.py`
- `tests/test_windows_launcher.py`
- `tests/test_workflow_engine.py`
- `README.md`
- `README_FIRST.md`
- `bootstrap.sh`
- `docs/campaign_custodian.md`
- this report

Some listed edits pre-existed as the uncommitted R0-SETUP-1 implementation; this closure was intentionally performed in that same working tree. No commit or push was made.

## Validation

### Focused security checks

- Focused final security/source/Custodian journey selection: **23 passed, 1 skipped**.
- Broader focused R0 setup/runtime selections during repair: **59 passed, 6 skipped, 3 deselected**.
- The skip is the explicit real-host managed-Codex identity qualification requiring root-owned fixed `/usr/bin` and `/etc` state.

### Relevant regression family

The complete relevant managed-Codex, Windows launcher, Custodian, Custodian server/models, qualified campaign, linked-worktree, prelaunch authority, Core authority, PA-5C4 transactional Core, and workflow-engine family completed as:

`1 failed, 165 passed, 8 skipped in 17.88s`

The only failure was:

`tests/test_pa5c4_transactional_core.py::test_complete_production_git_inventory_has_no_unclassified_callsite`

The eight skips were:

- 1 real-host managed-Codex `/usr/bin` and `/etc` ownership qualification;
- 4 fixed-path protected installer/root qualification cases;
- 2 actual root/two-UID Core service qualification cases;
- 1 explicit network qualification case.

No test prerequisite was fabricated as real-host qualification.

### Inherited inventory result

The process-inventory failure was not rebaselined, suppressed, or repaired. The exact current inventory remained:

- inventory SHA-256: `f8af9d25eb89712326248105eee732df8dc56a84e8bdcc5b793caea657dc998b`;
- normalized failure signature: `7ac0707a865519a1c1f89ec957cbea162896ecb257b60236501e2f0448b7433c`;
- counts: POST 25, PRE 4, qualified 11, unclassified 4.

This is the independently established target/baseline result. The closure introduced no new process callsite and did not change its signature.

### Static and syntax checks

- Ruff on all changed/relevant Python source and tests: **pass**.
- strict mypy on 11 changed/relevant Python source files: **pass** (`Success: no issues found in 11 source files`).
- POSIX shell syntax for all changed shell scripts: **pass**.
- Windows PowerShell parser validation for the changed launcher: **pass**.
- Python byte compilation for changed Python sources: **pass**.
- `git diff --check`: **pass** before editing and after implementation; the final post-report check is recorded in the handoff.

No unrelated expensive suite or scientific campaign was run.

## Remaining real-host steps

An administrator/release qualification must still:

1. independently provision and attest the fixed root-protected release, approval manifest, Codex artifact, product wheel, wheelhouse, scripts, and service unit;
2. exercise fresh installation, identical reinstall, explicitly approved update, interrupted-install recovery, and hostile pre-existing/symlink cases at the real fixed paths;
3. confirm actual root ownership/modes, two-UID service boundaries, systemd/cgroup containment, and final `/usr/bin/codex` plus `/etc` receipts;
4. initialize the real operator home once, relaunch without repair, tamper each protected/home binding state, and confirm Start remains unavailable;
5. perform the separately authorized network/authentication qualification without allowing credentials into campaign authority;
6. run an independent Auditor against this repaired working tree.

## Why this is not an R0 PASS claim

This is a bounded implementation closure of the four reported MAJOR findings. The independent Auditor report remains FAIL and unchanged; no replacement independent audit was launched. Real-host administrator, ownership, service, and authentication qualifications remain pending, and the inherited process-inventory test remains an acknowledged unchanged failure. Therefore this report establishes a repair candidate and regression evidence, not final R0 qualification.

## Token usage

This interactive runtime exposed no authoritative non-interactive `turn.completed.usage` receipt. In accordance with fail-closed accounting, no values are inferred from conversation text, elapsed work, or test output:

- `input_tokens`: unavailable
- `output_tokens`: unavailable
- `combined_tokens`: unavailable
- `cached_input_tokens`: unavailable
- `cache_write_input_tokens`: unavailable
- `reasoning_output_tokens`: unavailable
- prior Worker session: unavailable
- prior Auditor session: unavailable
- current repair session: unavailable
- retries, repairs, and repeated audit-round breakdown: unavailable

No local counter was fabricated. This accounting absence remains a separate RAS/product qualification issue and does not alter the security or validation results above.
