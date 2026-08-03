# Physics Auditor PA-3 qualification

Date: 2026-08-03

Base commit: `bb357cc83aae7b63461532d5c528a662f83ef7d8`

Recovery checkpoint: `b38cd750736d4182655cc6ded19de0ab9034fb86`

PA-3 passed its bounded standalone qualification cycle without using the permitted
repair pass. This qualifies the Codex-specific standalone action in package `0.2.0`;
it does not qualify workflow integration or broad scientific-review quality.

## Isolation and authority

Every real action used a new ephemeral Codex thread with `--sandbox read-only`,
approval `never`, no resume, and no yolo/full-access option. Bubblewrap mounted an
exact manifested projection at `/workspace` read-only. The source worktree, `.git`,
PA-2 action/catalog/program material, ignored and protected paths, host home, and
unrelated repositories were not mounted. A separate action scratch and isolated
runtime home were the only writable mounts.

The projection manifest binds every visible path, kind, mode, size, digest, and
authority. The action proof binds the source identity, projection, fixed Bubblewrap
policy and observed backend, role policy, prompt, verified PA-2 proof manifest, model
output, validated report, deterministic route, and final source/projection integrity.

The qualified Codex transport still receives its selected subscription-authentication
file as explicit read-only runtime material. It is not part of the projection, prompt,
semantic proof, or durable report; host credential locators and token environment
variables are absent, and exact-fragment leakage checks fail closed. PA-3 therefore
does not claim that the transport namespace contains no authentication material.

## Deterministic validation

- PA-3 focused tests: 68 passed.
- PA-1: 66 passed.
- PA-2: 54 passed.
- Frozen 0.2.0 compatibility: 10 passed.
- Existing Codex adapter/model: 100 passed.
- Code Auditor, workflow integrity, state, and journal: 196 passed.
- Existing live Bubblewrap isolation: 71 passed, 1 privilege-dependent skip.
- Complete suite: 1298 passed, 1 privilege-dependent skip.
- Ruff: passed.
- Strict mypy: passed across 56 source files.
- Documentation/example follow-up: 6 passed.
- Synthetic fake-adapter/production-namespace smoke: 3 passed.

Wheel and sdist built successfully. The wheel SHA-256 is
`48b6e864b2377e8c63fe4030085461d3f154542ecf0807d504a979e56dbf306a`;
the sdist SHA-256 is
`88ab544a113cfebe8e5e3e5a9d03c89f6d36f79fa5bf73bfe9855f2190e05932`.
A fresh Python 3.14 environment installed the wheel and passed package-version plus
installed root/audit-physics help checks.

## Real mechanism calibration

| Case | Session | Report | Deterministic route | Proof | Integrity |
| --- | --- | --- | --- | --- | --- |
| Clean | `019fc764-d2ed-7852-8dc7-3392e40f18f5` | `pass` | `pass` | `ff4083db109c7bb911179e5c54510b4ab5f1df67500475608588000bae9d9b72` | unchanged |
| Sign error | `019fc765-9ffd-72a3-9010-0c73566f9736` | `fail_repairable` | `request_repair` | `67169900b58ab49084ff85b04abefb2b5dc67d0bf6c92353e82639fea1f9974e` | unchanged |
| Missing evidence | `019fc766-9abd-7311-b2ff-cda7004f6294` | `blocked_insufficient_evidence` | `block_insufficient_evidence` | `e2aed82f2625b59d300d287a4a9ffeb171dad10b435bdf8ef2cb680493b59a49` | unchanged |

Each action recorded exactly one distinct matching `thread.started` identity, a valid
strict report, an unchanged source worktree and projection, and no model-side oracle
execution. Recovery returned each finalized proof without a relaunch.

The recovered sign-error failure was at the evidence/prompt boundary: aggregate oracle
failure did not expose the verified per-case boolean results or clearly close required
evidence kinds and the independent zero-force limiting case. PA-3 now includes those
bounded PA-2 check summaries and explicit citation rules. Report strictness and PA-1
routing were not weakened.

## Independent audit and next stage

The independent read-only final audit passed with no blocking finding or hard stop. It
confirmed exact projection enforcement, original-worktree and oracle-program absence,
fresh-session independence, no yolo inheritance, strict evidence/report validation,
authoritative routing, proof/recovery closure, compatibility, and package completeness.

PA-4 should integrate the already-qualified standalone result into a new versioned
physics workflow state/journal only: add the human-gate pause and bounded Worker repair
routing without changing PA-1, PA-2, PA-3 proofs, ordinary 0.2.0 workflows, or the
standalone isolation boundary. It should also evaluate a narrower credential-broker
mechanism while retaining the explicit current runtime-auth exception until qualified.

The machine-readable companion is
[`physics_auditor_pa3.json`](physics_auditor_pa3.json).
