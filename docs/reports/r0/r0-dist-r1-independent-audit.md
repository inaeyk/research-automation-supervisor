# R0-DIST-R1 PEP 668 independent micro-audit

Date: 2026-08-24

Mode: Independent read-only Auditor

Repository: `/home/inaeyk/researchrepo/ras-context-integration`

Branch: `feature/context-economy-runtime-integration`

Baseline / unchanged `HEAD`: `8a3a0297824c16d5ec4c16d5e5a5395f5b394ffb`

## Verdict

**PASS**

**May we proceed to administrator installation? YES**

R0-DIST-R1 may proceed immediately to real-host administrator bootstrap. The
documented preparation implementation now works under the host's PEP 668
externally-managed `/usr/bin/python3`, and the resulting candidate retains the
existing protected-release and managed-Codex contract. Previous R0-SETUP security
architecture remains closed and was not reopened.

## Exact preparation command tested

The host has Python `3.14.4`, `/usr/bin/python3` resolves to the regular executable
`/usr/bin/python3.14`, and `/usr/lib/python3.14/EXTERNALLY-MANAGED` is present. The
direct host-style command documented by the R1 closure was run as the ordinary user:

```bash
/usr/bin/python3 -I scripts/prepare-protected-release.py prepare \
  --repository /home/inaeyk/researchrepo/ras-context-integration \
  --candidate-root /tmp/r0-dist-r1-independent-audit.s6EuMv/research-supervisor-release-candidate-first \
  --authority-staging-root /tmp/r0-dist-r1-independent-audit.s6EuMv/research-supervisor-release-authority-candidate-first \
  --release-id ras-r0-dist-r1-independent-audit \
  --codex-artifact /usr/bin/python3.14 \
  --codex-version 3.14.4 \
  --wheelhouse-source /tmp/r0-dist-r1-independent-audit.s6EuMv/input-wheelhouse
```

As in the repair report's host smoke, the resolved system Python ELF and a complete
synthetic local wheelhouse were non-production stand-ins. They exercised the real
entrypoint, PEP 668 behavior, candidate construction, and verification path without
claiming approval for those inputs. The actual administrator bootstrap must use the
independently audited native Codex ELF and wheelhouse.

## First-run result

**PASS.** The command exited `0` and reported:

```text
resolution_python=/tmp/r0-dist-r1-independent-audit.s6EuMv/research-supervisor-release-preparation-venv/bin/python
approval_sha256=bb003c16aeac47c0dd93d5db2adc2e1a590526c8b4bf39c8b99ae85af1736d40
```

The private environment was a plain directory owned by the ordinary user
(`1000:1000`) with mode `0700`. Its `bin/python` was a user-owned regular copied file,
not a symlink, with link count one. `pyvenv.cfg` recorded Python `3.14.4` and
`include-system-site-packages = false`. Pip identified itself as `25.1.1` loaded from
that environment's own `lib/python3.14/site-packages/pip`.

The actual resolution vector selected that private Python followed by
`-I -m pip --isolated install --dry-run --ignore-installed
--disable-pip-version-check --no-cache-dir --no-index --only-binary=:all:
--find-links <candidate-wheelhouse> <product-wheel>`. It contained neither
`--break-system-packages` nor an index URL. The process environment is reduced to a
fixed system `PATH`, `/nonexistent` home, fixed locale, and
`PIP_CONFIG_FILE=/dev/null`. Thus resolution was private-venv-only, dry-run-only, and
offline-only.

The metadata of `/usr/lib/python3.14`, its `EXTERNALLY-MANAGED` marker,
`/usr/lib/python3/dist-packages`, and `/usr/local/lib/python3.14` was unchanged before
and after both preparations. No system pip, system package, protected path, or system
Python mutation occurred, and no `--break-system-packages` override was used.

## Repeat-run result

**PASS.** Preparation was run again with only fresh immutable candidate and authority
output names (`...-candidate-repeat`) beneath the same build parent. It exited `0`,
reported the same private resolution Python, and reused the exact venv directory and
copied Python device/inode pairs:

```text
venv:   device 72, inode 2401613
python: device 72, inode 2401623
```

Both approvals had SHA-256
`bb003c16aeac47c0dd93d5db2adc2e1a590526c8b4bf39c8b99ae85af1736d40`.
The complete first and repeat candidate trees compared equal, establishing
deterministic repeat behavior while preserving immutable-output semantics.

## Candidate verification result

**PASS.** The documented `/usr/bin/python3 -I
scripts/prepare-protected-release.py verify` path accepted both candidates and returned
the same approval SHA-256. Each manifest covered 111 files with only `0644` and `0755`
modes, including the Codex artifact and seven supplied wheels.

The exact first candidate was also passed through the production
`install_approved_release()` and `verify_installed_release()` code paths in an
unprivileged scratch layout. Installation returned `installed`; verification returned
`unchanged`; both retained release ID `ras-r0-dist-r1-independent-audit` and the exact
manifest digest above. The staged installer and verifier bytes were identical, with
SHA-256 `f0b1d98f8988540ce0a6ff6d1e8d2c2697032e21c5cfebe730f55764c34bf290`.
The bootstrap inventory retained `"authority_is_trusted":false` and the existing
fixed production destinations.

The installed scratch release then traversed the protected shell, isolated
`/usr/bin/python3`, real managed-Codex installer, protected receipt write, and runtime
identity verifier. The verified managed identity matched the candidate artifact:
release ID `ras-r0-dist-r1-independent-audit-codex`, version `3.14.4`, and SHA-256
`b8d8288faefdd300201f43fcf00f6f539a27218eeed3a3dff5ab10b9c4c99700`.
This confirms that the PEP 668 repair adds only private unprivileged preparation
tooling and does not weaken or redirect privileged release or managed-Codex authority.

## Focused test results

- Focused R0-DIST distribution suite:
  `tests/test_protected_release_distribution.py` — **8 passed in 8.85 s**.
- R4 production-wired, hostile-environment, missing/unsafe-authority,
  missing-entrypoint, and privileged qualification-backend rejection slice —
  **5 passed in 1.36 s**.
- Final `git diff --check`: **PASS**.

No sudo, network, host/software/package installation, package-manager operation,
commit, push, campaign, host protected-path mutation, repair, or follow-on work was
performed. The required release installation verification was an unprivileged `/tmp`
simulation. Audit artifacts were confined to `/tmp`; the requested report is the only
repository write by the Auditor.

## Token usage

Authoritative counters are unavailable. The Auditor thread identity is
`01a03489-e268-7592-aa0a-f9c513ba9049`; `CODEX_HOME` is unset, while the persistent
`/home/inaeyk/.codex/bin/codex-task` and durable task-ledger root are present. No
`task.json` or `TaskUsageReceipt.json` below that ledger matches this exact thread.
Other tasks' receipts were not attributed, raw rollout transcripts were not read, and
no usage was estimated. Final-output usage cannot be authoritative before this turn
emits `turn.completed`.

```text
input_tokens: unavailable
cached_input_tokens: unavailable
output_tokens: unavailable
reasoning_output_tokens: unavailable
combined_tokens: unavailable
```

Per-session and retry attribution:

- Worker session: unavailable; the closure report is not an authoritative matching
  runtime receipt.
- Auditor session: unavailable; no matching completed runtime receipt exists.
- Supervisor/Custodian and delegated sessions: unavailable / not applicable; none was
  launched.
- Retries, repairs, and repeated model audit rounds: unavailable; no matching receipt
  exists and validation-command repetitions are not model sessions.

## Terminal disposition

**PASS**

**May we proceed to administrator installation? YES**

Stop. No repairs or follow-on work were performed.
