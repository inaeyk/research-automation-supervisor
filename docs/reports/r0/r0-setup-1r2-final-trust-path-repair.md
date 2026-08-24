# R0-SETUP-1R2 Final Managed Codex Trust-Path Repair

Date: 2026-08-23

Branch: `feature/context-economy-runtime-integration`

Qualified baseline / current `HEAD`: `df59e2818c3519a5ba7dab69dd067b91b202e936`

## Scope and verdict

This Worker repair addresses only the three findings left open by the
R0-SETUP-1R independent audit: the Schema-v2 Physics Auditor executable bypass,
the privileged bootstrap trust root, and their missing production-wired tests.
It does not reopen the closed `CODEX_HOME` work, redesign Supervisor
architecture, touch PA-5D/PA-5D0, start Attempt 005, launch a campaign, perform
a protected installation, commit, push, or use the network.

Candidate dispositions:

- **MAJOR-A — repaired in this candidate.** Qualified Physics Auditor launches
  now resolve the same receipt-backed managed Codex identity and canonical
  managed home as readiness, Sign in, the qualified runner, Worker, and the
  ordinary Auditor. Schema-v2 request/configuration data, `PATH`, arbitrary
  environment variables, and legacy pins are not executable authority.
- **MAJOR-B — repaired at the product-contract and implementation boundary in
  this candidate.** The supported first privileged byte is a fixed,
  distribution-installed protected helper. Source-checkout tools are
  unprivileged preparation or protected-release payload only. The repository
  does not claim that a mutable checkout can establish its own provenance.
- **MAJOR-C — repaired in deterministic test evidence in this candidate.** New
  direct bypass tests and an end-to-end production-function contract test cover
  protected approval through all managed launch preparations, including
  Physics fresh and resume paths.

These are Worker dispositions, not an independent-audit closure and not an R0
PASS. Real-host protected distribution and installation qualification remain
pending.

## Physics Auditor launch-path inspection

The production paths inspected were:

1. Schema-v2 workflow dispatch through `workflow_engine.run_substage`, including
   new execution, retry/re-entry, `continue_after_review`, and recovery.
2. `PhysicsWorkflowServices` fresh and resume operations through
   `run_physics_auditor` and `resume_physics_auditor`.
3. The prompt-finalized launch boundary in `_continue_action`, shared by fresh,
   retry, and resume execution.
4. Physics benchmark/campaign wrappers and their child Auditor calls.
5. Qualified-campaign construction of production replay services for Worker and
   ordinary Auditor execution.
6. The qualified runner's sealed production environment and ordinary
   Worker/Auditor managed-identity verification.
7. Standalone Physics Auditor entry paths, which use the qualified resolver by
   default.
8. Repository-wide Codex launch/select sites, including generic development and
   shadow helpers. Remaining generic `PATH` selectors are not reachable as
   executable authority from qualified Physics execution.

All real Physics launches converge immediately before process creation on
`resolve_qualified_physics_auditor_codex`. That function uses the common
`verify_managed_codex_installation` verifier and
`verified_managed_codex_home`; it does not call `shutil.which` and does not
select an executable from request data.

The retained `trusted_executable` schema field is legacy compatibility data. In
qualified production execution it cannot grant authority. If present, its path
and digest must exactly agree with the protected receipt-backed identity;
otherwise execution fails before process launch. Thus a request pin such as
`/usr/bin/codex` is rejected when the receipt selects different bytes. It is not
silently ignored.

An explicitly injected test adapter remains possible only through Python test
dependency seams. The production Schema-v2 request cannot populate those
seams. Real-adapter tests use a separate, explicitly named test-qualified
identity builder; scripted adapters remain isolated test transports.

The fresh, retry, and resume branches all reach the same prompt-finalized
resolver. Completed or ambiguous recovery states do not launch a process.
Immediately before a real launch, the executable is the exact verified receipt
path and the environment is resealed with the canonical managed `CODEX_HOME`,
`HOME=/nonexistent`, and the fixed system `PATH`. Exact receipt, file digest,
home identity, or legacy-pin disagreement fails closed first.

## Final managed-Codex trust chain

The qualified launch chain is now:

```text
externally protected approved release identity
    -> protected release installation and receipt
    -> protected managed-Codex approval
    -> protected managed-Codex installation receipt
    -> exact installed executable digest and metadata verification
    -> canonical managed executable
    -> canonical protected managed CODEX_HOME
    -> readiness / Sign in / qualified Worker / ordinary Auditor /
       Schema-v2 Physics Auditor
```

The same verifier supplies the executable identity to each qualified consumer.
The same home verifier supplies the managed home. No qualified consumer may
replace either member of the pair with `PATH`, request, operator environment,
or a standalone absolute-path pin.

## Privileged bootstrap trust-root architecture

The repository now separates unprivileged source-tree preparation from
protected release installation.

The fixed production contract is:

- privileged installer:
  `/usr/libexec/research-supervisor/install-protected-release`;
- privileged verifier:
  `/usr/libexec/research-supervisor/verify-protected-release`;
- approved-release authority:
  `/usr/share/research-supervisor-release-authority/approved-release-v1.json`;
- candidate-data root:
  `/var/tmp/research-supervisor-release-candidate`;
- protected destination:
  `/opt/research-supervisor-release`;
- protected installation receipt:
  `/var/lib/research-supervisor-release-authority/installed-release-v1.json`.

The installer and verifier, and the approved-release metadata they trust, must
already have been installed by an external protected distribution authority.
Their ownership, modes, links, ancestors, and stable file identity are checked.
The ordinary operator cannot select those paths, the approval authority, the
destination, or the protected receipt location.

The checkout side may prepare candidate Codex bytes, digests, and a candidate
tree, but these are untrusted data. They acquire no authority from a digest the
checkout asserts. The privileged helper independently loads protected approval
metadata and accepts only the exact approved path/digest/mode set.

Candidate files are opened relative to a no-follow directory descriptor. The
same open descriptor is read into a root-owned staging tree while hashing;
source identity is checked before and after the copy. The staged exact bytes
are then checked for approved digest, mode, owner, link count, and a complete
no-extra-files manifest before atomic placement. This avoids validating one
path lookup and copying a later substitution. The installed tree and protected
receipt are reverified before success.

The fixed destination is installed before its matching protected receipt. If
the operation is interrupted between those steps, the generation is split and
runtime verification rejects it. Reinstall of the same approved identity is an
explicit no-op; replacing a different installed identity requires deliberate
external distribution recovery rather than an operator-selected update. No
privileged network installer is used.

The release payload scripts now rely on the externally installed verifier.
They are not bootstrap trust roots and their headers explicitly prohibit
executing a mutable source-checkout copy with `sudo`. Product documentation no
longer directs root to interpret a repository or release-tree shell script.
The only documented first privileged command selects the fixed external
helper. Historical audit reports were intentionally left unchanged and are not
current product instructions.

No self-hash, owner check, path check, or preamble inside a mutable privileged
script is claimed as security. The first privileged interpreter input must
already come from the external protected distribution. This distribution
provenance cannot be established by this Git repository and remains an honest
real-host prerequisite.

No separate, newly discovered out-of-scope historical admin bootstrap issue was
found. The Core and managed-Codex payload scripts directly involved in this
release path were placed behind the same external authority contract; unrelated
historical reports were not redesigned or edited.

## Production-wired test architecture

The deterministic contract fixture creates separate simulated protected
authority, candidate-data, release destination, managed-Codex destination,
receipt, and managed-home roots. It then invokes production functions rather
than duplicating their decisions:

```text
protected approved-release metadata
    -> install_approved_release
    -> exact installed managed-Codex approval and candidate artifact
    -> install_managed_codex
    -> verify_managed_codex_installation
    -> Custodian readiness
    -> Sign-in launch preparation
    -> qualified Worker launch preparation
    -> ordinary Auditor launch preparation
    -> Schema-v2 Physics Auditor launch preparation
```

The contract tests distinguish simulated deterministic evidence from pending
root-owned host qualification. They cover:

- an approved artifact succeeding through every production verification layer;
- candidate/artifact substitution after approval failing;
- protected release receipt substitution failing;
- malformed/tampered managed receipt and installed digest mismatch failing;
- an operator `PATH` Codex having no effect;
- arbitrary and `/usr/bin/codex` Schema-v2 pins failing closed;
- absent managed identity preventing Physics process launch;
- exact canonical executable and `CODEX_HOME` propagation;
- unsafe or missing managed home failing closed;
- retry/resume retaining the exact executable/home pair;
- pre-Start preparation remaining non-launchable without managed identity;
- credential material remaining absent from projections and launch
  environments;
- fixed protected-helper/documentation selection and source payloads remaining
  data/payload rather than the privileged trust root.

One directly related launch-verification defect was also corrected: the Physics
Auditor's independent Bubblewrap command reconstruction expected isolation
arguments in a different order from the canonical adapter. The expected order
now matches the actual canonical command. No option or isolation policy was
weakened.

## Validation environment

No package was installed or modified for validation, and no network access was
used.

- Python: `/home/inaeyk/researchrepo/ras-regression-venv/bin/python` — 3.14.4
- pytest: `/home/inaeyk/researchrepo/ras-regression-venv/bin/pytest` — 9.1.1
- Ruff: `/home/inaeyk/researchrepo/ras-regression-venv/bin/ruff` — 0.16.4
- mypy: `/home/inaeyk/researchrepo/ras-regression-venv/bin/mypy` — 2.3.1
- PowerShell parser:
  `/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe`
- Bubblewrap: `/usr/bin/bwrap` — 0.11.1
- Python imports/types: `PYTHONPATH=src`, `MYPYPATH=src`

Exact results:

- Focused managed-Codex/trust-root/Physics bypass tests:
  **34 passed, 1 skipped in 0.88 s**.
- Direct real-adapter Physics Auditor/benchmark/scoring tests after the common
  launch repair: **24 passed in 4.55 s**.
- Relevant R0-SETUP-1, PA-5C4/PA-5C4-U, Custodian, Core, qualified-runner,
  workflow/recovery, and Physics Auditor/benchmark family (19 files):
  **386 passed, 8 skipped, 1 inherited failure in 107.74 s**.
- Ruff on the changed/relevant Python files: **all checks passed**.
- Strict mypy on the changed production modules: **success, no issues found**.
- Python compilation checks on changed production and test modules: **passed**.
- Bash and POSIX-shell syntax checks on the bootstrap/installer scripts:
  **passed**.
- PowerShell parse check using the installed executable: **passed**.
- `git diff --check`: **passed**.

The broad Physics adapter tests required host execution because the managed
sandbox blocks Bubblewrap's network-namespace operation; no campaign or network
operation was performed. The final relevant family was therefore rerun through
the already-approved local qualification command.

### Qualification-only skips

The eight skips in the relevant family were visible and retained:

- one real-host root-owned managed-Codex identity qualification;
- four fixed-path protected/root installer simulations in Windows-launcher
  coverage;
- two actual-root/two-UID Core authority service qualifications;
- one network-dependent qualification.

The focused security result's single skip is the same real-host protected
`/usr/bin`/`/etc` identity qualification. None of these skips was converted into
mock evidence or hidden.

### Inherited process inventory

The expected inventory failure was encountered once and not investigated
further after confirming it was unchanged:

- inventory hash:
  `f8af9d25eb89712326248105eee732df8dc56a84e8bdcc5b793caea657dc998b`;
- normalized signature:
  `7ac0707a865519a1c1f89ec957cbea162896ecb257b60236501e2f0448b7433c`;
- categories: POST 25, PRE 4, QUALIFIED 11, UNCLASSIFIED 4;
- unchanged unclassified sites:
  `process_enforcement._run_systemctl`, `semantic_replay.execute_replay`,
  `semantic_replay._git`, and `systemd_launch_helper.main`.

The exact inventory hash, category counts, and callsite identities matched the
established R0 evidence. This inherited failure is unrelated to this repair.

## Real-host work still pending

Before this can support an R0 PASS decision, a qualified external distribution
must place and protect the fixed privileged helper, verifier, and approval
metadata. A real administrator must then perform the fixed protected install
on a clean host, followed by ownership/mode/digest/receipt verification and the
already-defined real-host managed-Codex/home qualifications. The remaining
qualification-only tests must run in their required root/two-UID/network host
environment. An independent Auditor must review this candidate.

No such installation, administrator action, campaign, or independent audit was
performed by this Worker.

## Token usage

**ACCOUNTING OBSERVATION:** this interactive Worker runtime exposes no
authoritative `turn.completed.usage` receipt, and the task prohibited launching
nested agents or additional model sessions. Exact runtime counters therefore
cannot be reported without fabrication.

- Worker input tokens: **unavailable**
- Worker output tokens: **unavailable**
- Worker combined tokens (`input_tokens + output_tokens`): **unavailable**
- Worker cached input tokens: **unavailable**
- Worker cache-write input tokens: **unavailable**
- Worker reasoning output tokens: **unavailable**
- Auditor tokens: **unavailable; no Auditor was launched**
- Supervisor/Custodian model-session tokens: **unavailable; no such model
  session was launched**
- Other/nested-session tokens: **unavailable; no nested session was launched**
- Retry/repair/repeated-audit-round token breakdown: **unavailable**

No token value above is estimated. This accounting observation is separate
from the security repair and its validation evidence.
