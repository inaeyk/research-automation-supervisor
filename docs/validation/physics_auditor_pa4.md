# Physics Auditor PA-4 workflow qualification

Date: 2026-08-04

Base commit: `01cbecc7ca8ec07e9a089aa2307c68c6d303f775`

Branch: `feature/physics-auditor-v1-workflow-integration`

PA-4 qualifies opt-in schema-version-2 single-substage physics workflow integration in
package `0.2.0`. It does not change schema-version-1 workflow records, PA-1 routing,
PA-2 completion proofs, PA-3 action proofs, campaign scheduling, or protected historical
evaluation.

## Qualified boundary

The integration runs the unchanged version-1 Worker, visible-test, and Code Auditor
workflow before contract-required PA-2 trusted intents and one fresh projected PA-3
Physics Auditor per round. The unchanged PA-1 router is authoritative. Completion
requires every software gate, every current-workspace oracle proof, a verified `pass`
route, no unresolved pause, and an exact final workspace-identity match.

PA-4 adds disjoint versioned configuration, specification, state, result, oracle-evidence,
human-review packet/decision, and journal models. Old journal forms and model
serialization are not extended or rewritten. Per-round nested software results are
snapshotted into immutable PA-4 evidence before journaling.

Repair resumes the exact Worker thread under the existing shared repair counter, then
reruns visible tests and a fresh Code Auditor. The Worker-facing physics continuation is
closed to validated finding IDs, bounded summaries, evidence references, and required
repairs. A workspace mutation invalidates every PA-2 proof bound to the old complete
workspace identity; unchanged full identities preserve evidence. Historical records are
retained but cannot satisfy the current completion gate.

Human/evidence/repair-limit pauses bind an immutable review packet. Decisions support
`approve_existing_contract`, `revise_contract`, `request_additional_evidence`,
`accept_with_caveat`, and `reject_candidate`. Convention changes, unresolved
gauge/constraint ambiguity, new interpretation, conflicting evidence, and contract
weakening cannot automatically repair or complete.

## Recovery and integrity

Crash injection covered initial state/result snapshots, software intent/completion/gate,
oracle intent/launch/completion/proof refresh, Physics Auditor intent/launch/completion/
route, repair routing, stale-evidence invalidation, human/evidence pauses, human-decision
recording, and completion. Each action has one deterministic intent and at most one
completion. Proved actions finalize without relaunch; ambiguous launches stop as
infrastructure without blaming candidate code.

Completed status recursively reruns the qualified PA-2 and PA-3 standalone verifiers,
not merely the PA-4 top-level artifact hashes. Tests replace both journal-cited proofs
and supporting action records/provider evidence, and mutate the accepted workspace.

## Qualification results

- Focused PA-4: 41 passed.
- Dedicated PA-1/PA-2/PA-3/compatibility/CLI/PA-4 group after the bounded repair:
  235 passed.
- Frozen Worker, Code Auditor, state, journal, integrity, recovery, and 0.2.0
  compatibility group: 291 passed.
- Final complete suite after the bounded repair: 1339 passed, 1 privilege-dependent
  isolation skip.
- Ruff: passed.
- Strict mypy: passed across 51 source files.
- Documentation links: 4 passed.
- Wheel and sdist: built successfully as version 0.2.0.
- Clean wheel install: package version, imports, root help, substage help,
  `review-physics-substage` help, and `audit-physics` help passed.

The five bounded scripted calibrations passed: clean completion, one sign repair and
fresh re-audit, durable convention review, evidence-specific pause, and infrastructure
stop without candidate blame. No real provider or protected fixture was used.

The independent read-only audit initially found three blockers: contract-weakening
packet actionability, recursive terminal proof verification, and excess repair-prompt
metadata. The one permitted bounded repair closed all three. Independent repair
verification then passed with 41 focused tests.

## Recommendation

Proceed to PA-5 only as a separate versioned qualification. Keep PA-4 synchronous and
single-substage; do not weaken PA-1 routing, PA-2/PA-3 proofs, full-workspace evidence
binding, fresh-session isolation, or schema-version-1 compatibility. PA-5 should focus
on broader public physics-quality calibration and operational ergonomics before any
campaign-level scheduling or publication-grade autonomy.
