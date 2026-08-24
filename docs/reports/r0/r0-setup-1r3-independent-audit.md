# R0-SETUP-1R3 final independent privileged Python boundary audit

Date: 2026-08-23
Mode: Independent Auditor
Repository: `/home/inaeyk/researchrepo/ras-context-integration`
Branch: `feature/context-economy-runtime-integration`
Baseline: `df59e2818c3519a5ba7dab69dd067b91b202e936`

## Verdict

**FAIL**

- **BLOCKER A — Physics Auditor identity: CLOSED**
- **BLOCKER B — privileged Python trust root: CLOSED**
- **BLOCKER C — shell-to-Python production-wired testing: OPEN**
- **May R0-SETUP-1R3 proceed to real-host qualification? NO**

The R3 source repair closes the caller-controlled Python interpreter and import-path
defect. The supported privileged path fixes `/usr/bin/python3`, requires isolated mode,
starts it under `env -i` from a protected working directory, and passes an absolute
protected entrypoint which inserts only the validated protected release source root.
No mutable checkout, caller CWD, caller `PYTHON*` value, user site, or PATH-selected
Python remains application-code authority in that path.

The new tests accurately exercise that import transition, but the claimed
production-wired chain still contains a test-only seam. The qualification subprocess
returns after importing and reporting the protected modules and immediately before the
production call to `installer_module.main()`. The same test then calls
`install_managed_codex()` directly in the pytest process from the checkout-imported
module. Consequently, no real shell-to-isolated-Python subprocess test exercises the
protected installer implementation used by production. That is explicitly blocking
under this audit contract.

This verdict does not authorize R0, Attempt 005, PA-5D/PA-5D0, a campaign, a commit,
a push, administrator installation, or host/service mutation.

## Findings by severity

### MAJOR R0-SETUP-1R3-C1 — production-wired subprocess stops before the production installer

The real boundary tests are materially better than the R2 tests:

- `tests/test_managed_codex_security.py:282-301` uses real `subprocess.run()` with
  `/bin/sh`, the installed simulated protected payload, the caller CWD, and the caller
  environment. It does not monkeypatch subprocess.
- `test_actual_protected_shell_to_python_boundary_ignores_hostile_cwd_and_environment`
  invokes both `install-managed-codex.sh` and
  `install-core-authority-service.sh` from hostile and normal CWDs and proves the
  protected package/module paths are selected with no malicious marker execution.
- The installed simulated release is produced by the real
  `install_approved_release()` implementation from exact candidate bytes and then
  verified.

However, `scripts/protected-managed-codex-entry.py:101-120` makes qualification mode
print import metadata and return `0`. Only the next production-only statement at line
121 calls `installer_module.main(sys.argv[1:])`. Thus the qualification subprocess
never executes `managed_codex_installer.main()`, `install_managed_codex()`,
`verify_managed_codex_installation()`, or `bind_managed_codex_home_authority()`.

The nominal end-to-end test shows the seam directly:

1. `tests/test_managed_codex_security.py:1200-1212` invokes and finishes the shell
   qualification subprocess.
2. `tests/test_managed_codex_security.py:1213-1220` creates a simulated layout and
   directly calls the pytest process's already imported `install_managed_codex()`.

The protected subprocess did authenticate which module file Python imported, and the
simulated release bytes are copied from the current source, but this is still a splice
between two different processes and two different import authorities. Direct calls to
Python functions may supplement the boundary test; the audit instructions prohibit
them from replacing subprocess execution of the production installer implementation.
The adjacent Core payload has the same limitation: its test crosses the shell-to-Python
import boundary, but qualification returns before Core's production `verify` operation.

There is also no adversarial subprocess case removing
`protected-managed-codex-entry.py` from the temporary installed release, although that
case is deterministic without host mutation. The source check would fail closed, but
the mandatory test matrix does not exercise it. The existing missing-module case removes
`managed_codex_installer.py`, not the entrypoint.

No CRITICAL or separately classified MINOR finding was found. C1 is MAJOR and blocking.

## 1. Frozen candidate

At the initial audit freeze:

```text
git rev-parse HEAD
df59e2818c3519a5ba7dab69dd067b91b202e936

git branch --show-current
feature/context-economy-runtime-integration

git diff --check
PASS
```

`HEAD` is exactly the stated baseline and the candidate remains uncommitted. Before this
report was added, the tracked diff was:

```text
27 files changed, 1017 insertions(+), 248 deletions(-)
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

Untracked files at freeze were the prior R0 reports, the managed-Codex/protected-release
implementation and tests, and these R3-relevant files:

```text
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

The complete R3-relevant shell launchers, entrypoint, protected manifest/verification
logic, managed installer imports/CLI, adversarial tests, Core changes, packaging list,
and supplemental Windows/setup assertions were inspected. Enough R1/R2 execution,
managed-home, and Physics authority code was retraced to check closed-boundary
regressions. No product code was edited.

## 2. Blocker B — privileged Python import boundary

### Exact protected command shapes

Managed-Codex production reaches the shared launcher as:

```text
/bin/sh /opt/research-supervisor-release/scripts/run-protected-python.sh install
```

After fixed-path metadata and release verification, the launcher performs:

```text
cd /opt/research-supervisor-release
exec /usr/bin/env -i \
  PATH=/usr/sbin:/usr/bin:/sbin:/bin LANG=C.UTF-8 LC_ALL=C.UTF-8 \
  /usr/bin/python3 -I -B \
  /opt/research-supervisor-release/scripts/protected-managed-codex-entry.py install
```

Core uses the same command for `verify` and later `bind-home OPERATOR`. Its additional
Python transitions are fixed protected operations:

```text
/usr/bin/env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin \
  LANG=C.UTF-8 LC_ALL=C.UTF-8 HOME=/nonexistent \
  /usr/bin/python3 -I -S -B -m venv /opt/research-supervisor-core/venv

/usr/bin/env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin \
  LANG=C.UTF-8 LC_ALL=C.UTF-8 HOME=/nonexistent PIP_CONFIG_FILE=/dev/null \
  /opt/research-supervisor-core/venv/bin/python -I -B -m pip install \
  --disable-pip-version-check --no-index --only-binary=:all: \
  --find-links /opt/research-supervisor-release/wheelhouse --upgrade \
  /opt/research-supervisor-release/artifacts/research_automation_supervisor-0.2.0-py3-none-any.whl
```

### Interpreter authority and isolation

Production assigns `protected_python=/usr/bin/python3`; the test-only environment
override is read only in the non-root qualification branch. The root branch never
falls back to `python`, `python3`, `/usr/bin/env python3`, PATH lookup, or a caller path.
It requires the resolved interpreter to match a versioned `/usr/bin/python3.X` target,
requires `/usr` and `/usr/bin` to be root-owned mode `0755`, and requires the
dereferenced interpreter to be a root-owned regular executable with mode `0755`.

On this runtime, `/usr/bin/python3` resolves to `/usr/bin/python3.14` and reports Python
3.14.4. Sandbox UID remapping displayed `/usr`, `/usr/bin`, and the interpreter as UID/
GID 65534 with mode `0755`; the production code requires `0:0` and therefore leaves
actual host ownership to real-host qualification rather than accepting the remapped
observation as root proof.

The runtime's own help states that `-I` implies `-E`, `-P`, and `-s`. A direct isolated
probe observed `isolated=1`, `ignore_environment=1`, `safe_path=true`,
`no_user_site=1`, `site.ENABLE_USER_SITE=false`, and no CWD entry. `-B` set
`dont_write_bytecode=1`. The protected release verifier rejects unapproved extra files,
and no privileged bytecode-cache write occurred or created a mutable-code path.

### Environment and CWD

The actual Python `exec` uses `/usr/bin/env -i` and reintroduces exactly fixed `PATH`,
`LANG`, and `LC_ALL`. It does not re-add `PYTHONPATH`, `PYTHONHOME`, `PYTHONUSERBASE`,
`PYTHONSTARTUP`, `PYTHONINSPECT`, `PYTHONWARNINGS`, `PYTHONPYCACHEPREFIX`, arbitrary
`PATH`, `HOME`, `XDG_CONFIG_HOME`, `CODEX_HOME`, release root, or destination.

`cd "$release_root"` occurs before `exec`; `set -e` makes a failed `cd` fatal. In the
root branch `release_root` is the fixed `/opt/research-supervisor-release`. The Python
entrypoint independently requires `Path.cwd().resolve(strict=True) == release_root` and
rejects `""` or the CWD in its inherited `sys.path`.

### Absolute entrypoint and transitive imports

The entrypoint is the absolute
`/opt/research-supervisor-release/scripts/protected-managed-codex-entry.py`. The shell
rejects a missing file, symlink, wrong owner/group/mode, unsafe ancestry, wrong launcher
path, or failed installed-release verification. At effective UID 0 the entrypoint also
requires its resolved path to equal the fixed production path.

The repository-wide targeted search found one privileged import-path modification:

```text
scripts/protected-managed-codex-entry.py:92
sys.path.insert(0, str(source_root))
```

There is no privileged `sys.path.append`, `importlib` path loader, `PYTHONPATH`
reconstruction, `site.addsitedir`, `runpy`, or external-source `exec`/`eval` in this
path. `scripts/prepare-managed-codex-home.py` inserts a checkout `src`, but it is an
ordinary-user preparation script and is not invoked by the protected launcher.

The inserted path is derived from the resolved absolute entrypoint as
`actual_entrypoint.parents[1] / "src"`; it is not an argument or environment value. The
shell requires the release, source, and package directories to be root-owned mode
`0755`, and the immediately preceding external verifier checks the complete approved
tree, exact file bytes/modes, no symlinks/hardlinks, and no unapproved extras. The
mandatory external manifest includes the entrypoint, package initializer,
`managed_codex_installer.py`, `managed_codex.py`, `doctor.py`, `custodian_errors.py`,
and `errors.py`.

Imports therefore resolve as:

```text
/opt/research-supervisor-release/src
/usr/lib/python314.zip
/usr/lib/python3.14
/usr/lib/python3.14/lib-dynload
/usr/local/lib/python3.14/dist-packages
/usr/lib/python3/dist-packages
```

The protected application modules come first from the approved release. Their other
imports are standard library except `packaging.version`, observed at
`/usr/lib/python3/dist-packages/packaging/__init__.py`. System site processing under
`-I` cannot use the caller's user site or `PYTHON*` variables; system `.pth` or
`sitecustomize` authority and distribution `packaging` provenance are root/distribution
host assumptions still requiring qualification. They are not ordinary-operator
authority in the stated contract.

Protected Python does not import from the mutable Git checkout. Missing interpreter,
entrypoint, module, or safe release authority exits nonzero; no fallback path follows.
The tests cover missing interpreter, unsafe interpreter, writable source root, and
missing application module. The missing-entrypoint behavior is present in source but,
as noted in C1, lacks the requested subprocess test.

**BLOCKER B — privileged Python trust root: CLOSED.**

## 3. Blocker C — actual shell-to-Python coverage

The hostile subprocess test genuinely supplies caller-controlled state to the actual
payload before production sanitization. It covers:

| Required condition | Evidence |
|---|---|
| Hostile CWD top-level package | Real marker package; not imported |
| Hostile CWD transitive dependency | `hashlib.py` marker; not imported |
| Hostile `PYTHONPATH` | Package and transitive markers; not imported |
| Hostile `PYTHONHOME` | Missing caller path reaches payload; ignored |
| User-site / `.pth` | Caller `PYTHONUSERBASE` and `.pth` hook; not executed |
| Startup variables | `PYTHONSTARTUP`, `PYTHONINSPECT`, `BASH_ENV`, and `ENV`; marker not executed |
| Fake PATH Python | Fake `python3` marker; not executed |
| Normal and hostile CWD | Both invoked; results equal |
| Protected CWD | Reported as installed simulated release root |
| Intended protected modules | Exact installed `managed_codex.py` and installer paths reported |
| Malicious markers | All seven markers remain absent |
| Missing/unsafe interpreter | Nonzero, fail-closed qualification cases |
| Missing protected module | Nonzero import failure |
| Managed-Codex shell transition | Real `/bin/sh` to isolated Python subprocess |
| Adjacent Core shell transition | Real `/bin/sh` through the same launcher |

It does not cover a missing entrypoint, and—more importantly—it stops before executing
the installer production function as described in C1. Hostile state is not sanitized by
the test fixture before reaching the payload: `_invoke_protected_import_probe()` passes
the supplied `cwd` and `env` directly to `subprocess.run()`.

Independent results:

```text
boundary hostile/fail-closed/production-wired slice: 3 passed in 0.87s
complete managed-Codex security file: 36 passed, 1 qualification-only skip in 1.31s
```

The skip is not counted as PASS.

**BLOCKER C — shell-to-Python production-wired testing: OPEN.**

## 4. Production trust-chain disposition

The repaired source chain is coherent:

```text
external protected helper
  -> approved protected shell payload
  -> isolated protected Python
  -> protected managed installer implementation
  -> approved managed-Codex identity
  -> installed executable + protected receipt
  -> runtime verifier + readiness
  -> Sign in
  -> Worker / ordinary Auditor / Physics Auditor
  -> retry and resume verification
```

The existing deterministic function tests continue to cover exact managed installation,
receipt/digest verification, canonical home, readiness, Sign in preparation, replay
Worker/Auditor preparation, qualified environment sealing, Physics selection, and
retry/resume. The subprocess test proves the intended protected module is selected, but
then terminates before the production installer call. The test resumes the chain in the
pytest process. Therefore the source trust chain is not disproved, but the required
single production-wired test chain is not established.

## 5. Blocker A bounded regression

The current production fresh and safe-resume paths still select
`_invoke_qualified_codex` when no explicitly injected test invoker exists. Immediately
before launch, both call `resolve_qualified_physics_auditor_codex()`, which verifies the
protected receipt-backed executable and canonical managed home, rejects a conflicting
legacy request pin, and overwrites `PATH`, `HOME`, and `CODEX_HOME`. The exact verified
executable is passed to `run_prepared_codex`; the legacy selector is reachable only
behind the explicit injected-test seam.

Adversarial tests in the 36-test managed security file cover hostile PATH, arbitrary and
`/usr/bin/codex` pins, missing/tampered receipt, executable digest mismatch, missing/
unsafe canonical home, fail-before-launch, and fresh/resume pair reuse. The real-adapter
and scoring slice passed 24/24 with host bubblewrap capability.

No evidence of a new executable selector, environment override, arbitrary absolute-path
authority, or retry/resume bypass was found.

**BLOCKER A — Physics Auditor identity: CLOSED.**

## 6. Managed `CODEX_HOME` regression

Ambient `CODEX_HOME` remains ignored when deriving qualified authority. Normal relaunch
uses verification-only preparation; missing or altered authority/binding fails closed
rather than being silently rebuilt. Sign in, qualified replay Worker/ordinary Auditor,
the qualified runner, and Physics resolution all derive the same canonical managed
home. The Physics sandbox's private `CODEX_HOME` is an isolation projection with the
canonical auth file mounted read-only, not a second host authority.

Authentication material remains under the separate private managed home. The composed
production-preparation test observes no credential bytes in readiness, auth command/
environment, Physics environment, or sealed environment, and campaign/export source
allowlists exclude `auth.json`.

No regression was found.

## 7. Adjacent Core payload

The Core payload had the identical R2 vulnerability in managed-Codex verify/bind-home
and a PATH-selected `python3 -m venv`, so repairing it is justified supporting scope.
Managed verify and home binding now use the shared fixed launcher. Venv creation uses
fixed system Python with `env -i`, `-I -S -B`, the inherited protected release CWD, and
the fixed Core venv destination. Offline pip uses the fixed protected venv interpreter,
`env -i`, `-I -B`, a nonexistent HOME, disabled pip configuration, `--no-index`, binary
wheels only, and fixed protected release artifact/wheelhouse paths.

The venv/site-packages destination is beneath fixed root-owned
`/opt/research-supervisor-core`; the script recursively fixes root ownership and modes.
Actual ownership, interruption/reinstall behavior, dependency provenance, and two-UID
service behavior remain real-host qualification items. No unrelated R3 scope expansion
was found.

Its adversarial test reaches the same real shell-to-Python import boundary, but shares
the C1 qualification-return limitation.

## 8. Inherited inventory

The inventory verifier was run once and exited `1` with:

```text
inventory_sha256:
f8af9d25eb89712326248105eee732df8dc56a84e8bdcc5b793caea657dc998b

categories: POST-SNAPSHOT 25, PRE-SNAPSHOT 4,
QUALIFIED-SANDBOX-INTERNAL 11, UNCLASSIFIED 4

normalized signature:
7ac0707a865519a1c1f89ec957cbea162896ecb257b60236501e2f0448b7433c
```

Both values exactly match the supplied inherited authority. The four normalized
unclassified identities remain `process_enforcement._run_systemctl`,
`semantic_replay.execute_replay`, `semantic_replay._git`, and
`systemd_launch_helper.main`. This is **inherited/unrelated**. It was not suppressed,
rebaselined, or investigated further.

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
/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe  5.1.26100.9168
```

Results:

- Actual shell-to-Python hostile/fail-closed/production-wired slice: **3 passed** in
  0.87s.
- Complete managed-Codex security file: **36 passed, 1 skipped** in 1.31s. The explicit
  real-root qualification skip is not counted as PASS.
- Physics real-adapter/scoring slice: **24 passed** in 4.68s with the required host
  bubblewrap capability. Its initial restricted-sandbox run produced 8 downstream
  failures and 16 passes; the same 24-test slice then passed outside only the restricted
  process sandbox without host mutation.
- Relevant 19-file family, with the already-run inherited inventory test deselected,
  inside the mandatory restricted/no-network environment: **284 passed, 104 failed,
  8 skipped, 1 deselected** in 69.95s. Failures were capability denials and their
  cascades: WSL vsock, loopback/Unix socket creation, and bubblewrap netlink/isolation
  were denied. The 8 qualification skips are not passes. A request to rerun the broad
  aggregate with host access was refused because that suite also contains service,
  socket, WSL, and network-backed cases outside this audit's authority. This explains
  the discrepancy from the Worker's host-capable 388-pass aggregate; no unsafe
  workaround was used.
- Standalone inherited inventory: expected inherited exit `1`, with both exact hashes
  reproduced as recorded above.
- Ruff over all 25 changed/new Python production and test files: **PASS**.
- Strict mypy over all 16 changed/new production/script modules: **PASS**, no issues.
- Read-only `compile()` over all 25 changed/new Python files: **PASS**.
- Bash and POSIX shell syntax over all 6 changed/new shell payloads: **PASS**.
- Windows PowerShell parser over `launch-research-supervisor.ps1`: **PASS**.
- Pre-report and post-report `git diff --check`: **PASS**.

The green boundary and focused security results do not cure finding C1; conversely, the
restricted aggregate's capability failures are not classified as product regressions.

## 10. Real-host and distribution assertions still pending

This deterministic audit does not establish:

- externally protected distribution helper/verifier/approval provenance and bytes;
- actual root ownership, modes, links, ancestry, and mutability of protected release
  authority;
- actual `/usr/bin/python3`, standard-library, system-site, `.pth`/`sitecustomize`, and
  distribution `packaging` ownership/provenance assumptions;
- actual protected entrypoint/package ownership and exact installed release behavior;
- real root-owned managed-Codex installation, executable, receipt, and update authority;
- reinstall, update, partial-venv, and interruption/recovery behavior on the host;
- actual canonical managed `CODEX_HOME` authentication state and credential containment;
- Windows-to-WSL launcher and first-run/relaunch behavior;
- Core service installation, protected venv/wheelhouse compatibility, and two-UID
  behavior; or
- real credential containment across service, campaign, Core, artifact, and export
  boundaries.

Those are real-host/distribution qualification items and are not reasons Blocker B
remains open. Real-host qualification cannot begin while the deterministic
production-wired test blocker remains open.

## 11. Token usage

### ACCOUNTING OBSERVATION — authoritative counters unavailable

`CODEX_THREAD_ID` was `01a03188-d830-7c51-9a8b-a9dc700dd992`. The persistent
`/home/inaeyk/.codex/bin/codex-task` and durable ledger root were present, but no
`TaskUsageReceipt.json` matched that exact thread identity. Existing receipts belong to
other tasks and were not attributed to this audit. No raw rollout transcript was read,
no model agent was launched, and no count was estimated or reconstructed. Final-output
usage cannot be authoritative until this turn's `turn.completed` event exists.

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

Accounting absence does not alter the security verdict.

## Terminal disposition

**R0-SETUP-1R3 may not proceed to protected-distribution / real-host administrator
qualification.** Blocker A remains closed and Blocker B is closed, but Blocker C remains
open. No repair, commit, push, sudo operation, host installation, service change,
campaign, Attempt 005, PA-5D/PA-5D0 action, or follow-on work was performed.
