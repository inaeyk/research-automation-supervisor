# R0-DIST-R1 PEP 668 preparation closure

Date: 2026-08-24

Repository: `/home/inaeyk/researchrepo/ras-context-integration`

Baseline / unchanged `HEAD`: `8a3a0297824c16d5ec4c16d5e5a5395f5b394ffb`

Scope: repair and validation of the sole R0-DIST independent-audit blocker. No
Auditor was run. Previous R0-SETUP blockers A, B, and C remain closed and were not
reopened.

## Result

**REPAIR AND VALIDATION PASS**

The documented ordinary-user preparation path now succeeds when its fixed shebang
selects an externally managed `/usr/bin/python3`. The bootstrap interpreter creates
or reuses a private preparation virtual environment and all pip dependency-resolution
work executes through that environment. System pip is not invoked or mutated, and
`--break-system-packages` is absent.

This is Worker repair evidence, not an independent audit or real-host administrator
qualification verdict.

## Repair

The default candidate root places its deterministic private preparation environment
at:

```text
/var/tmp/research-supervisor-release-preparation-venv
```

The preparation implementation now:

1. creates the environment with the shebang-selected interpreter using isolated
   `python -I -m venv --copies` and the standard bundled `ensurepip` path;
2. makes the environment root private (`0700`), requires ordinary-user ownership,
   and rejects links, unsafe metadata, system-site enablement, a stale bootstrap
   Python version, an unsafe copied interpreter, or unavailable pip;
3. validates and reuses the same environment on later preparation attempts in the
   same application/build parent;
4. invokes resolution only as the private copied Python's `-I -m pip`;
5. retains `--isolated`, `--dry-run`, `--ignore-installed`,
   `--disable-pip-version-check`, `--no-cache-dir`, `--no-index`,
   `--only-binary=:all:`, and the explicit offline `--find-links` wheelhouse;
6. fails before publishing either candidate output when venv creation, `ensurepip`,
   private pip, or the compatible offline wheel closure is unavailable; and
7. reports the exact private resolution interpreter in successful CLI JSON.

Existing immutable-output behavior remains intact: preparation does not overwrite an
existing candidate or authority staging tree. Repeat coverage uses new immutable
output names beneath the same build parent and proves that the private venv is reused
without replacement.

The private environment is only unprivileged preparation tooling. It is outside the
candidate manifest and adds no interpreter, executable, receipt, destination, or
other caller-controlled authority to the privileged installer or verifier. The fixed
protected-release and managed-Codex trust paths are unchanged.

## Regression coverage

`tests/test_protected_release_distribution.py` now starts the actual checkout CLI by
executing `scripts/prepare-protected-release.py`; its fixed
`#!/usr/bin/python3 -I` shebang therefore supplies system-Python semantics even though
pytest itself runs in the regression venv.

The tests prove:

1. the system-Python CLI succeeds while using a private copied venv Python;
2. resolution names the private-venv Python/pip path, not `/usr/bin/python3` system
   pip;
3. a second CLI preparation reuses the same private Python device/inode and succeeds;
4. the resolution argument vector has no `--break-system-packages`;
5. no-index, binary-only offline resolution remains enforced, including rejection of
   a deliberately missing transitive dependency;
6. missing private pip fails explicitly before candidate publication; and
7. a CLI-produced candidate verifies, enters the real protected-release copy/receipt
   functions, invokes the protected managed-Codex shell/Python boundary, and verifies
   the resulting managed executable and receipt.

## Host-style smoke

The host has `/usr/lib/python3.14/EXTERNALLY-MANAGED`. A direct smoke invoked the
supported implementation explicitly through `/usr/bin/python3 -I`, using a complete
synthetic offline wheelhouse and resolved regular `/usr/bin/python3.14` ELF as the
non-production Codex stand-in:

```bash
/usr/bin/python3 -I scripts/prepare-protected-release.py prepare \
  --repository /home/inaeyk/researchrepo/ras-context-integration \
  --candidate-root /tmp/r0-dist-r1-host-smoke.U9bvif/research-supervisor-release-candidate \
  --authority-staging-root /tmp/r0-dist-r1-host-smoke.U9bvif/research-supervisor-release-authority-candidate \
  --release-id ras-r0-dist-r1-host-smoke \
  --codex-artifact /usr/bin/python3.14 \
  --codex-version 3.14.4 \
  --wheelhouse-source /tmp/r0-dist-r1-host-smoke.U9bvif/input-wheelhouse
/usr/bin/python3 -I scripts/prepare-protected-release.py verify \
  --candidate-root /tmp/r0-dist-r1-host-smoke.U9bvif/research-supervisor-release-candidate \
  --approval /tmp/r0-dist-r1-host-smoke.U9bvif/research-supervisor-release-authority-candidate/approved-release-v1.json
```

Both commands passed. Preparation reported the private resolution interpreter at
`/tmp/r0-dist-r1-host-smoke.U9bvif/research-supervisor-release-preparation-venv/bin/python`;
verification accepted approval SHA-256
`5997965e9fdf181d09f5b9c1a044ca84c3b7e448abc7075ead135e7ee0c13695`.
The scratch directory was removed after validation.

## Exact supported next real-host command

Run this as the ordinary user from the reviewed checkout, substituting only the four
capitalized input values with the independently audited real inputs. The two fixed
output roots must not already contain an older candidate:

```bash
./scripts/prepare-protected-release.py prepare \
  --release-id RELEASE_ID \
  --codex-artifact /ABSOLUTE/PATH/TO/NATIVE_CODEX_ELF \
  --codex-version CODEX_VERSION \
  --wheelhouse-source /ABSOLUTE/PATH/TO/AUDITED_OFFLINE_WHEELHOUSE
./scripts/prepare-protected-release.py verify
```

The first invocation creates
`/var/tmp/research-supervisor-release-preparation-venv` if needed; a valid existing
private environment is reused. Do not use sudo, a network, a package manager, or
`--break-system-packages` for this step.

## Validation

Tooling came from `/home/inaeyk/researchrepo/ras-regression-venv` except for the
explicit host-style `/usr/bin/python3` smoke and fixed shell paths exercised by the
existing tests.

- Focused R0-DIST distribution tests: **8 passed in 8.91 s**.
- Complete R0-DIST plus managed-Codex security tests: **46 passed, 1 skipped in
  10.42 s**. The skip remains the existing real-root `/usr/bin` and `/etc`
  qualification, and is not counted as a pass.
- R4 production-wired, hostile-environment, missing/unsafe-authority,
  missing-entrypoint, and privileged qualification-backend rejection slice:
  **5 passed in 1.15 s**.
- Explicit `/usr/bin/python3 -I` PEP 668 host smoke and candidate verification:
  **PASS**.
- Ruff `0.16.4` over all six changed/new Python files: **PASS**.
- Mypy `2.3.1 --strict --no-incremental` over the package and preparation CLI:
  **PASS, 88 source files**.
- Python bytecode syntax compilation for all six changed/new Python files, with cache
  output redirected to `/tmp`: **PASS**.
- Tracked `git diff --check`: **PASS**. Separate no-index `--check` runs for every
  untracked candidate file emitted no whitespace diagnostics (their status `1` is the
  expected difference-from-`/dev/null` status).
- Process-inventory alias/shell/split-literal enforcement test: **1 passed in 0.41
  s**. The reviewed inventory digest is now
  `025dfcb0abeadb306b69bf060a170bcb749cd9b1008742b5b296b2b2a416dddb`;
  all four `release_preparation.py` subprocess callsites are `PRE-SNAPSHOT` and none
  is Git-capable. The inventory command continues to report only the same four
  baseline-unclassified callsites in `process_enforcement.py`,
  `semantic_replay.py` (two), and `systemd_launch_helper.py`; this inherited condition
  was documented by the independent audit and was not expanded or repaired here.

No sudo, network access, host protected-path mutation, system-Python mutation,
package-manager change, campaign, commit, push, Auditor, Supervisor, or Custodian was
used. No protected-release, installer, receipt, `CODEX_HOME`, Physics Auditor, or
privileged trust boundary was weakened.

## Token usage

### Authoritative counters unavailable

The exact current thread identity is
`01a0347d-6fd3-7300-8180-e573a01fc784`. `CODEX_HOME` is unset; the persistent
`/home/inaeyk/.codex/bin/codex-task` entrypoint and durable task-ledger root are
present. No `task.json` or `TaskUsageReceipt.json` below that ledger matches this
thread. Raw rollout transcripts were not read, other tasks' receipts were not
attributed, and no usage was estimated or reconstructed. Final-output usage cannot be
authoritative until this turn emits `turn.completed`.

```text
input_tokens: unavailable
cached_input_tokens: unavailable
output_tokens: unavailable
reasoning_output_tokens: unavailable
combined_tokens: unavailable
```

Per-session and retry attribution:

- Worker session: unavailable; no matching completed runtime receipt exists.
- Auditor session: unavailable / not applicable; none was launched.
- Supervisor/Custodian session: unavailable / not applicable; none was launched.
- Other or delegated model sessions: unavailable / not applicable; none was launched.
- Model retries, repairs, and repeated audit rounds: unavailable; no authoritative
  matching receipt exists, and validation-command retries are not model sessions.

Accounting therefore fails closed as incomplete without fabricating a total. Its
unavailability does not change the repair or validation result.

## Terminal disposition

The R0-DIST-R1 PEP 668 preparation blocker is repaired and locally validated. Work
stops here as requested, without independent audit, real-host privileged
qualification, commit, or push.
