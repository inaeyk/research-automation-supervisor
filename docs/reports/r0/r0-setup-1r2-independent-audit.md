# R0-SETUP-1R2 final independent trust-path audit

Date: 2026-08-23
Mode: Independent Auditor
Repository: `/home/inaeyk/researchrepo/ras-context-integration`
Branch: `feature/context-economy-runtime-integration`
Baseline: `df59e2818c3519a5ba7dab69dd067b91b202e936`

## Verdict

**FAIL**

- **BLOCKER A — Physics Auditor identity: CLOSED**
- **BLOCKER B — privileged trust root: OPEN**
- **BLOCKER C — production-wired tests: OPEN**
- **May R0-SETUP-1R2 proceed to real-host qualification? NO**

The Physics Auditor executable-authority bypass is closed. The candidate does not,
however, yet establish a fail-closed privileged transition from the protected shell
payload to the protected Python implementation. The supported payload invokes
`/usr/bin/python3 -m ...` while retaining an operator-controlled current directory and
without Python safe-path/isolation controls. On the interpreter actually used for
validation, the current directory precedes the protected release source in `sys.path`.
An operator-controlled Python package can therefore become privileged implementation
authority unless the external helper provides an undocumented ambient mitigation.

The tests do not exercise that transition, so the production-wired test blocker also
remains open. This is a source/product-contract failure, not merely the honestly pending
proof of external distribution provenance.

## Findings by severity

### MAJOR R0-SETUP-1R2-B1 — protected payload permits ambient Python module substitution

The fixed external helper and protected release-tree shell scripts are a sound first
step, and current product documentation no longer tells an administrator to interpret a
mutable checkout script. The next privileged transition is not sealed, however:

- `scripts/install-managed-codex.sh:40-43` verifies the protected release, exports
  `PYTHONPATH=/opt/research-supervisor-release/src`, and executes
  `/usr/bin/python3 -m research_automation_supervisor.managed_codex_installer install`.
- `scripts/install-core-authority-service.sh:35-38,77-82,97-98` performs the same
  module invocation for verification and home binding and also invokes
  `python3 -m venv`.
- Neither payload changes to a protected working directory, uses `-P` or `-I`, enables
  `PYTHONSAFEPATH`, nor specifies that the external helper must do so. The scripts also
  do not establish a clean Python environment themselves.
- The documented command is normally issued from an arbitrary caller directory:
  `sudo /usr/libexec/research-supervisor/install-protected-release OPERATOR`. The
  documented external helper contract says that the helper later executes the protected
  payload, but does not make a safe working directory/import environment part of the
  contract.

A read-only probe from this operator-writable checkout reproduced the authority order:

```text
PYTHONPATH=/opt/research-supervisor-release/src /usr/bin/python3 -c ...
["", "/opt/research-supervisor-release/src", ...]

PYTHONPATH=/opt/research-supervisor-release/src /usr/bin/python3 -P -c ...
["/opt/research-supervisor-release/src", ...]
```

The empty first entry represents the current directory. Thus an ordinary operator can
prepare a top-level `research_automation_supervisor` package (or another import hook) in
the caller directory and have it considered before the exact protected release package
when the privileged `-m` command starts. The `python3 -m venv` invocation has the same
class of ambient module-selection problem. The release verifier runs first, but it does
not authenticate what Python subsequently imports from the caller directory.

This defeats the intended statement that mutable operator code remains data under the
supported privileged workflow. A qualified external helper might happen to change to a
safe directory and scrub the environment, but that behavior is neither present in the
reviewed payload nor required by the documented external-helper contract. Security
cannot depend on that unstated ambient behavior.

### MAJOR R0-SETUP-1R2-C1 — tests do not cover the privileged interpreter transition

`tests/test_managed_codex_security.py:423-475` directly exercises the protected-release
Python copy/verify functions in temporary layouts. The test named
`test_production_wired_protected_release_to_every_launch_preparation`
(`tests/test_managed_codex_security.py:929-1012`) calls protected-release installation,
managed-Codex installation, readiness, mocked Sign in, replay-service preparation, the
Physics resolver, and qualified-runner sealing. It does not execute the distribution
helper, a protected payload shell script, or the payload's Python module lookup. It also
does not drive actual Worker, ordinary Auditor, Schema-v2 workflow dispatch, and
retry/resume in that one production-function chain; those behaviors are covered by
separate direct or monkeypatched tests.

The privileged-entrypoint test at `tests/test_managed_codex_security.py:478-506` checks
fixed constants and documentation/source strings. It does not establish the runtime
import authority. The qualification-only installer tests remain skipped, and their shim
design does not qualify real root provenance in any event. Consequently, the actual
defect above is invisible to the reported green test slices.

No CRITICAL or MINOR finding was separately classified. The direct impact of B1 is
privileged code execution, but it is classified MAJOR because the external helper
artifact is outside this checkout and could impose an as-yet undocumented safe ambient
contract. The absence of that requirement is nevertheless blocking.

## 1. Frozen candidate

At audit freeze, before adding only this report:

```text
git rev-parse HEAD
df59e2818c3519a5ba7dab69dd067b91b202e936

git branch --show-current
feature/context-economy-runtime-integration
```

`HEAD` is exactly the stated baseline, so all candidate work remains uncommitted.
`git diff --check` was clean. The tracked diff was:

```text
27 files changed, 962 insertions(+), 244 deletions(-)
```

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
scripts/install-managed-codex.sh
scripts/install-research-supervisor.sh
scripts/prepare-managed-codex-home.py
src/research_automation_supervisor/managed_codex.py
src/research_automation_supervisor/managed_codex_installer.py
src/research_automation_supervisor/protected_release.py
tests/test_managed_codex_security.py
```

The complete tracked diff and the security-relevant untracked implementation, tests,
scripts, documentation, and prior reports were inspected. No unrelated scope expansion
was identified. The `live_shadow_isolation.py` change is part of the same protected
launch/authentication contract rather than an unrelated feature.

## 2. Physics Auditor authority — Blocker A

### Production trace

The production Schema-v2 path enters the Physics workflow from `workflow_engine.py`.
`PhysicsWorkflowServices` defaults to `run_physics_auditor`,
`resume_physics_auditor`, and a `None` `physics_auditor_codex_invoker`
(`physics_workflow.py:136-151`). Fresh/retry output uses the default runner, existing
recoverable output uses the default resumer, and recovered completed output is verified
without launch (`physics_workflow.py:1348-1365`).

Both `run_physics_auditor()` and `resume_physics_auditor()` convert the `None` production
invoker into `_invoke_qualified_codex` and set `qualified_managed_codex=True`
(`physics_auditor_execution.py:311-377,380-462`). At `prompt_finalized`, before recording
`model_launch_attempted`, `_continue_action()` calls
`resolve_qualified_physics_auditor_codex()` (`physics_auditor_execution.py:725-778`).
That resolver calls the common `verify_managed_codex_installation()` and
`verified_managed_codex_home()`, rejects any conflicting legacy pin, and overwrites
`PATH`, `HOME`, and `CODEX_HOME` with the qualified values
(`physics_auditor_execution.py:1919-1944`).

The exact returned executable is passed to `_invoke_qualified_codex`, then as the exact
`codex_executable` argument to `run_prepared_codex`
(`physics_auditor_execution.py:1332-1488`). The canonical home is used to resolve and
confidentially load the canonical `auth.json` before launch. The Physics bubblewrap
process intentionally sees a private isolated `CODEX_HOME`, with that canonical auth
file mounted read-only; this is namespace isolation, not a second home authority
(`live_shadow_isolation.py:184-205,420-439`).

Repository-wide process/selector search found no second production Physics Auditor
Codex launch site. Actual Physics model creation converges on `run_prepared_codex`, whose
single adapter process launch uses the passed executable. Other subprocess sites belong
to the Physics Oracle, developer/shadow tooling, tests, or generic adapters and do not
select the qualified Physics Auditor executable. The legacy selector at
`physics_auditor_execution.py:2011-2023` is reachable only when Python test code injects
a non-`None` invoker; production workflow request/configuration data cannot populate
that service seam.

### Mandatory adversarial disposition

| Check | Result | Evidence |
|---|---|---|
| 1. PATH Codex cannot control launch | PASS | Resolver verifies the receipt-backed executable and replaces `PATH` with `/usr/bin:/bin`; direct full-run no-launch test covers absent receipt plus hostile PATH. |
| 2. Request executable pin cannot control launch | PASS | A legacy pin is comparison-only and conflicting values raise before launch. |
| 3. Environment executable override cannot control launch | PASS | No environment executable selector reaches this production path; the exact verified path is passed explicitly. |
| 4. Arbitrary absolute executable cannot control launch | PASS | Conflicting arbitrary absolute path/digest is rejected. |
| 5. Conflicting `/usr/bin/codex` request pin fails closed | PASS | A conflicting digest/path pair is rejected; an exact matching redundant pin adds no authority. |
| 6. Missing receipt fails before process launch | PASS | Common verifier raises before `model_launch_attempted` and invocation. |
| 7. Malformed/tampered receipt fails closed | PASS | Strict protected-file metadata and exact receipt schema are enforced. |
| 8. Installed executable digest mismatch fails closed | PASS | Exact opened executable bytes are hashed and compared with the protected receipt. |
| 9. Canonical receipt plus executable launches exact managed executable | PASS | Resolver identity flows unchanged into the adapter's explicit executable argument. |
| 10. Canonical CODEX_HOME is used | PASS | Protected home authority supplies the auth source; isolated child home is intentional and cannot redirect the host authority. |
| 11. Retry/resume uses the same pair | PASS | Fresh and safely recoverable resume converge on the same resolver. Ambiguous post-attempt recovery does not relaunch. |
| 12. No second Physics process-launch site bypasses verifier | PASS | Repository-wide selector and `Popen` trace converges on the common adapter launch. |

**BLOCKER A — Physics Auditor identity: CLOSED.**

## 3. Privileged trust root — Blocker B

The revised architecture truthfully places the first supported privileged command at the
fixed external path `/usr/libexec/research-supervisor/install-protected-release`. Product
documentation explicitly states that this helper, its verifier, and the protected
approval are external distribution artifacts whose provenance is not established by the
checkout. No current product instruction tells the operator to run a checkout/release
shell or Python file directly with `sudo`.

The in-repository protected-release implementation has useful properties: fixed paths;
strict protected approval parsing; descriptor-relative, no-follow candidate opens; a
single held source descriptor while hashing/copying; exact tree verification; atomic
destination installation; a protected receipt after tree verification; explicit
same-version no-op; fail-closed changed-release recovery; and no network fetch. The
managed-Codex installer likewise uses a protected approval, held exact artifact bytes,
fixed `/usr/bin/codex`, pending-generation state, final receipt/digest/metadata
verification, explicit same-version no-op, and protected `update_from_sha256` authority.

Those properties do not cure finding B1. The effective privileged implementation can be
reselected during Python startup before the intended protected module begins executing.

| Required property | Disposition |
|---|---|
| 1. First privileged bytes are externally protected | Boundary is explicit and acceptable in source; real distribution proof remains pending. |
| 2. No supported mutable checkout script is run as root | Satisfied by current product/docs. Historical reports are not current instructions. |
| 3. Mutable checkout remains preparation/data only | **FAIL:** caller-directory Python modules can become privileged code during the protected payload. |
| 4. Helper/entrypoint location fixed or securely selected | Fixed in source/docs, but downstream module authority is not sealed. |
| 5. Operator cannot replace helper, metadata, digest, receipt authority, destination | Filesystem paths are protected in the intended functions; **end-to-end not proven** because substituted privileged Python can bypass them. |
| 6. Operator-supplied hash does not create Codex trust | Satisfied by the intended protected approval flow; bypassed implementation would evade that flow. |
| 7. No privileged network installer | Satisfied: no curl/sh, npm install, or remote fetch. Core pip is offline `--no-index` from protected payload data. |
| 8. Staged Codex remains data until independently approved | Satisfied by intended protected-release/managed approval flow. |
| 9. Validated bytes are installed bytes | Satisfied in intended code by held descriptors and copy-time digesting. |
| 10. Destination fixed | Satisfied in intended code. |
| 11. Final owner/mode/digest verified | Satisfied in intended code. |
| 12. Receipt only after final verification | Satisfied by staged/tree verification followed by receipt and final verification. |
| 13. Interrupted install cannot appear runtime-acceptable | Satisfied by split-generation/pending receipt checks in intended code. |
| 14. Same-version reinstall explicit | Explicit no-op. |
| 15. Approved update explicit | Managed Codex requires protected `update_from_sha256`; protected release requires external recovery for a changed identity. |
| 16. Unapproved replacement/update fails closed | Satisfied in intended code, subject to B1. |
| 17. Normal Windows/Custodian launch cannot mutate identity | Satisfied: normal launch is verification-only; administrator install is separate. |

**BLOCKER B — privileged trust root: OPEN.**

## 4. Production-wired test contract — Blocker C

The new tests provide substantial and useful production-function coverage:

- exact managed install, same-version reinstall, approved update, pending interruption,
  receipt corruption, executable substitution, hardlink/symlink/mode/ancestor failures;
- protected-release approval/candidate/receipt/tree substitution through the simulated
  Python boundary;
- canonical home creation, strict reuse, environment irrelevance, binding/authority
  tampering, and unsafe home failure;
- hostile PATH and ambient `CODEX_HOME`, arbitrary and conflicting system pins, missing
  receipt, malformed receipt, and installed digest mismatch;
- full `run_physics_auditor()` fail-before-launch behavior and safe resume pair reuse;
- real adapter/bubblewrap namespace and Physics scoring paths;
- readiness, Sign in preparation, qualified replay preparation, Core-consumption
  ordering, and credential-value exclusion from the composite launch-preparation result.

Coverage against the requested minimum:

| Contract | Disposition |
|---|---|
| A. protected approval through retry/resume chain | PARTIAL: production functions are composed through launch preparation, with direct Physics run/resume elsewhere; no actual external-helper/payload transition and no single Worker/Auditor/Schema-v2 workflow chain. |
| B. PATH irrelevant | Covered. |
| C. Schema-v2 pin rejected/irrelevant | Covered at the Physics execution boundary. |
| D. executable substitution fails | Covered. |
| E. receipt substitution/tampering fails | Covered. |
| F. canonical CODEX_HOME everywhere | Covered across preparation/resolver paths and real Physics isolation tests. |
| G. missing/unsafe home fails closed | Covered. |
| H. prerequisites block authority/action before Codex | Covered for readiness, replay preparation, Physics launch, and Core consumption ordering. |
| I. credential material excluded | Partly behavioral and partly path/source-string evidence; no credential bytes were observed in tested composite outputs. |
| J. protected privileged entrypoint contract honestly guarded | **FAIL:** fixed strings and simulated Python functions do not execute or constrain the privileged interpreter transition; real-host skips remain honest but do not fill the gap. |

Brittle-only evidence remains in the documentation entrypoint assertion
(`tests/test_managed_codex_security.py:493-506`) and the export-source assertion that
searches `qualified_campaign.py` for the literal `auth.json`
(`tests/test_managed_codex_security.py:1046-1066`). Those assertions supplement but do
not prove runtime behavior. More importantly, no test starts the protected payload from
an operator-writable current directory with an adversarial Python package and proves
that only the protected release module can execute.

**BLOCKER C — production-wired tests: OPEN.**

## 5. Regression checks

No R2 regression was found in the previously closed managed-home architecture:

- ambient/ordinary `CODEX_HOME` is ignored when deriving qualified authority;
- missing or tampered home authority/binding fails closed and is not silently repaired;
- normal relaunch verifies and does not initialize or recreate authority;
- credentials remain under the separate private managed home, outside campaign/Core/
  export roots; the Physics sandbox receives only the read-only authentication mount and
  confidentiality filters.

## 6. Inherited inventory failure

The inventory verifier was run exactly once, separately from the broad suite. It exited
1 and reproduced:

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

The same four normalized unclassified path/scope/expression identities were present:
`process_enforcement.py::_run_systemctl`, `semantic_replay.py::execute_replay`,
`semantic_replay.py::_git`, and `systemd_launch_helper.py::main`. This exactly matches
the supplied and previous independent-audit authority. It is classified
**inherited/unrelated**. It was not rerun, investigated further, suppressed, or
rebaselined. The broad family therefore explicitly deselected only the already-verified
inventory test.

## 7. Validation

Exact project tool paths:

```text
Python: /home/inaeyk/researchrepo/ras-regression-venv/bin/python (3.14.4)
pytest: /home/inaeyk/researchrepo/ras-regression-venv/bin/pytest (9.1.1)
Ruff:   /home/inaeyk/researchrepo/ras-regression-venv/bin/ruff (0.16.4)
mypy:   /home/inaeyk/researchrepo/ras-regression-venv/bin/mypy (2.3.1)
PowerShell: /mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe
```

Results:

- Focused trust-path security: **34 passed, 1 skipped** in 1.04s. The skip is explicitly
  qualification-only and is **not counted as PASS**.
- Focused Physics real-adapter/scoring slice: **24 passed** in 4.88s.
- Relevant 19-file family, with only the already-run inherited inventory test
  deselected: **386 passed, 8 skipped, 1 deselected** in 117.00s. The eight skips are
  qualification/environment-only and are **not counted as PASS**.
- Standalone inventory verifier: expected inherited **exit 1**, exact digest/signature
  match as recorded above.
- Ruff on 24 changed/new Python source and test files: **PASS**.
- Strict mypy on 15 changed/new production/script modules: **PASS**, no issues.
- Read-only Python `compile()` on 24 changed/new Python files: **PASS**.
- `bash -n bootstrap.sh`: **PASS**.
- `sh -n` on `scripts/custodian-bootstrap.sh`,
  `scripts/install-core-authority-service.sh`, `scripts/install-managed-codex.sh`, and
  `scripts/install-research-supervisor.sh`: **PASS**.
- Windows PowerShell parser on `launch-research-supervisor.ps1`: **PASS**.
- Initial and pre-report `git diff --check`: **PASS**. Final post-report whitespace and
  freeze checks are recorded in the handoff response.

Passing syntax, type, and unit tests do not change the security verdict because the
privileged Python selection defect is not represented by those tests.

## 8. Real-host and distribution assertions still pending

Even after B1 and C1 are repaired and independently audited, this source audit cannot
establish:

- provenance and exact bytes of the distribution-installed privileged helper/verifier;
- their actual root ownership, modes, links, ancestry, safe working directory, and
  scrubbed interpreter environment;
- protected approved-release metadata placement and authorization;
- clean-host administrator installation behavior and interruption/recovery;
- real `/usr/bin/codex`, `/etc` receipt, canonical operator home, and two-UID Core/
  Custodian ownership and service behavior;
- the qualification-only Windows/WSL/root/network cases.

These are legitimate real-host/distribution qualifications, but the current source
contract must first close the ambient privileged Python authority.

This FAIL does not start Attempt 005, unblock R0, resume PA-5D/PA-5D0, launch a campaign,
or authorize administrator installation.

## 9. Token usage

**ACCOUNTING OBSERVATION — authoritative counters unavailable.**

The current interactive audit was not associated with a discoverable `codex-task`
task identity or a current durable `TaskUsageReceipt.json`. `CODEX_HOME` was not exported
in the session. Existing durable ledgers belonged to other named tasks and were not used.
Final-response usage cannot be authoritative until `turn.completed` occurs. No rollout
transcript was inspected and no count was estimated.

```text
input_tokens: unavailable
output_tokens: unavailable
combined_tokens: unavailable
cached_input_tokens: unavailable
cache_write_input_tokens: unavailable
reasoning_output_tokens: unavailable
per-session breakdown: unavailable
retry/repair/repeated-audit breakdown: unavailable
```

Accounting availability does not affect the security verdict.
