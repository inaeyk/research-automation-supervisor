# R0-DIST — minimal protected release bootstrap

Date: 2026-08-24

Baseline / unchanged `HEAD`: `8a3a0297824c16d5ec4c16d5e5a5395f5b394ffb`

## Scope and outcome

This Worker candidate adds the missing distribution bridge between the qualified
source release and the already-designed protected host layout. It does not redesign
the runtime/security architecture, alter Physics Auditor, `CODEX_HOME`, containment,
token-accounting, or workflow behavior, run a campaign, or mutate the host.

This report does **not** claim R0 PASS. Real-host preparation, independent audit,
administrator trust installation, and real-host qualification remain pending.

## Implemented distribution path

The unprivileged entrypoint is `scripts/prepare-protected-release.py`. It creates two
separate immutable-on-success outputs and refuses to replace an existing output:

- `/var/tmp/research-supervisor-release-candidate`: untrusted release data;
- `/var/tmp/research-supervisor-release-authority-candidate`: still-untrusted staged
  installer, verifier, approval, and bootstrap inventory.

The complete candidate contains:

- the fixed protected shell/Python payloads and Core systemd unit;
- the complete package source used by the qualified privileged Python boundary;
- a deterministic `research_automation_supervisor-0.2.0-py3-none-any.whl` built
  unprivileged from this release, including the configured Physics example data;
- the caller-supplied native Codex ELF and its exact managed-Codex approval;
- every supplied dependency wheel under `wheelhouse/`.

Preparation verifies the native ELF version unprivileged and runs isolated pip with
`--dry-run --ignore-installed --no-index --only-binary=:all:`. This proves compatible
transitive wheel closure without installing a package or accessing an index.

`approved-release-v1.json` lists every candidate file by canonical relative path,
`0644`/`0755` mode, and SHA-256. `bootstrap-files-v1.json` separately lists the exact
staged authority bytes, modes, and fixed destinations and explicitly records
`"authority_is_trusted":false`. The generated approval acquires authority only after
explicit administrator review and trusted-system-tool installation into the protected
authority path.

The installer and verifier are identical, standard-library-only copies of
`protected_release.py`, dispatched only by their fixed installed basename. Their exact
candidate digest is:

```text
f0b1d98f8988540ce0a6ff6d1e8d2c2697032e21c5cfebe730f55764c34bf290
```

Each selects the fixed `/usr/bin/python3` with isolated mode from its shebang. The
installer rejects an alternate authority/verifier, unsafe authority ancestry, missing,
extra, linked, mistyped, mode-mismatched, or digest-mismatched candidate objects. It
then retains the qualified descriptor-relative copy, staged-byte hashing, exact
installed-tree verification, atomic generation, protected receipt, and fixed product
installer transition.

No privileged path performs a network fetch, npm operation, package build,
PATH-selected interpreter launch, or checkout-script execution.

## Deterministic evidence

`tests/test_protected_release_distribution.py` proves:

- a complete candidate can be prepared from this exact source release using supplied
  offline inputs;
- every manifest entry exactly matches candidate bytes and modes and no candidate file
  is omitted;
- helper, verifier, bootstrap inventory, native artifact, product wheel, and wheelhouse
  are present;
- tampered, missing, extra, and mode-changed candidate files fail verification;
- the protected installer itself rejects an extra candidate file;
- fixed bootstrap destinations and non-authority staging semantics are preserved;
- supported documentation never directs root to execute a checkout script; and
- simulated protected authority installation enters the existing protected installer,
  managed-Codex installer, protected receipt, and runtime identity-verification chain.

The production process inventory classifies the new helper execution, offline pip dry
run, and unprivileged Codex version probe as pre-snapshot and pins the reviewed inventory
digest `fce827dd9eb5da213c6349979b5e64f0e63dc7b8c68ef3d7a71637644b9343e5`.

## Validation

Tools came only from `/home/inaeyk/researchrepo/ras-regression-venv` and the existing
system. No package was installed and no network or campaign was used.

- New R0-DIST plus complete managed-Codex security file: **42 passed, 1
  qualification-only real-root skip in 2.84 s**.
- Existing R4 main/hostile/fail-closed/production-wired boundary slice: **5 passed in
  1.21 s**.
- Final report-inclusive R0-DIST plus R4 boundary slice: **9 passed in 2.33 s**.
- Relevant 19-file R0/R4/Custodian/Core/qualified-runner/Physics family: **390 passed,
  8 qualification-only skips, 1 inherited inventory failure in 125.21 s**. The failure
  now reports only the four callsites in `process_enforcement.py`,
  `semantic_replay.py`, and `systemd_launch_helper.py` already unclassified at the
  baseline; all R0-DIST callsites are classified and the expected inventory digest
  matches.
- Strict mypy over all 94 package modules: **PASS**.
- Whole-repository Ruff: the only finding is the inherited `SIM102` at untouched
  `semantic_decomposition.py:562`; the changed/new Python selection passes.
- Python compilation over all changed/new Python files: **PASS**.
- GNU Bash and POSIX-shell parsing over the protected shell payloads: **PASS**.
- Windows PowerShell parsing of `launch-research-supervisor.ps1`: **PASS**.
- `git diff --check`: **PASS**.

Visible broad-family skips are unchanged: one real root-owned managed-Codex identity,
four fixed-path Windows/root installer cases, two actual root/two-UID Core service
cases, and one explicit network qualification.

## Exact short administrator procedure after audit

First complete unprivileged preparation and independent audit. The audit must record
the exact approval-manifest SHA-256 and confirm the helper/verifier digest printed
above. The staged `bootstrap-files-v1.json` is review evidence, not authority.

Then run exactly these trusted-system-tool commands. They copy new code as data only:

```bash
sudo /usr/bin/install -d -o root -g root -m 0755 /usr/libexec/research-supervisor
sudo /usr/bin/install -d -o root -g root -m 0755 /usr/share/research-supervisor-release-authority
sudo /usr/bin/install -d -o root -g root -m 0755 /var/lib/research-supervisor-release-authority
sudo /usr/bin/install -o root -g root -m 0755 /var/tmp/research-supervisor-release-authority-candidate/install-protected-release /usr/libexec/research-supervisor/install-protected-release
sudo /usr/bin/install -o root -g root -m 0755 /var/tmp/research-supervisor-release-authority-candidate/verify-protected-release /usr/libexec/research-supervisor/verify-protected-release
sudo /usr/bin/install -o root -g root -m 0644 /var/tmp/research-supervisor-release-authority-candidate/approved-release-v1.json /usr/share/research-supervisor-release-authority/approved-release-v1.json
sudo /usr/bin/sha256sum /usr/libexec/research-supervisor/install-protected-release /usr/libexec/research-supervisor/verify-protected-release /usr/share/research-supervisor-release-authority/approved-release-v1.json
```

Compare all three root-owned output hashes with the independent audit record and stop
on any mismatch. Only after that comparison succeeds, replace `OPERATOR` with the
existing ordinary account name and execute the first newly installed privileged code:

```bash
sudo /usr/libexec/research-supervisor/install-protected-release OPERATOR
```

## Host state and remaining qualification

This Worker used no `sudo`, made no changes below `/usr/bin`, `/usr/libexec`,
`/usr/share`, `/opt`, or existing receipt paths, and did not create the real `/var/tmp`
candidate. It performed no network access, package installation, commit, push, Auditor
launch, or campaign run.

After independent audit, the host procedure above and the existing real-root/two-UID
qualification skips must be executed and evidenced before any higher-level R0 verdict.

## Token usage

No completed authoritative `turn.completed.usage` receipt for this interactive Worker
session is available before model completion. Per the fail-closed accounting rule:

- Worker input tokens: **unavailable**
- Worker cached input tokens: **unavailable**
- Worker output tokens: **unavailable**
- Worker reasoning output tokens: **unavailable**
- Worker combined tokens: **unavailable**
- Auditor session: **not launched**
- Supervisor/Custodian sessions: **not launched**
- retries, repairs, and repeated audit rounds: **unavailable**

No token total is estimated or reconstructed. An external machine-generated footer may
append exact final usage only after the authoritative completion event exists.
