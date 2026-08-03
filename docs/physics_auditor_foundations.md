# Physics Auditor PA-1 foundations

PA-1 adds model-free data contracts, validation, canonical serialization, and
deterministic verdict routing to package version `0.2.0`. It does **not** add a Physics
Auditor to ordinary workflows. There is no Physics Auditor model execution, Codex
invocation, oracle execution, repair loop, workflow state, journal form, provider
dispatch, or GL-specific production behavior in this stage.

The schemas are used by the separate [PA-2 trusted oracle execution
substrate](physics_oracle_execution.md). PA-2 closes declared oracle IDs over an
operator-owned catalog without changing these PA-1 contract/report/routing semantics.
Neither stage approves publication-level scientific claims.

## Authority and decision boundary

The three PA-1 layers have different authority:

1. `PhysicsTaskContractV1` is human-authored task authority. Its bounded statements
   declare conventions, assumptions, review targets, required evidence, and forbidden
   claims. Free-form statements are not executed and are not treated as scientific
   proof.
2. `PhysicsAuditReportV1` is an untrusted typed observation. Its prose and self-declared
   verdict cannot select a route.
3. `derive_physics_audit_decision` validates closed references and applies the explicit
   policy. Its typed canonical proof is the authoritative PA-1 route.

Even a future clean physics route will not itself release scientific work. The roadmap
requires a separate versioned workflow and an immutable human scientific gate.

## Physics Task Contract v1

A standalone contract has `schema_version: 1` and the one PA-1 profile
`physics_implementation`. Unknown profiles and schema versions are rejected. The
standalone root fields are:

| Field | Type and purpose |
| --- | --- |
| `schema_version` | Literal `1` |
| `profile` | Literal `physics_implementation` |
| `conventions` | Nonempty, unique `PhysicsConventionV1` entries |
| `assumptions` | Nonempty, unique `PhysicsAssumptionV1` entries |
| `required_identities` | Nonempty required checks and evidence kinds |
| `limiting_cases` | Nonempty required limiting-case checks |
| `evidence` | Optional declared test IDs and path-backed evidence |
| `oracles` | Nonempty declared oracle references; PA-1 never executes them |
| `forbidden_claims` | Nonempty bounded human-authored claim guards |
| `human_gate` | All three mandatory v1 human triggers |
| `audit_policy` | Optional explicit deterministic advisory/evidence policy |
| `auditor_role_ref` | Optional opaque future role reference; no backend dispatch |

The mandatory human triggers are `convention_change`,
`unresolved_gauge_constraint_ambiguity`, and `new_physical_interpretation`.

Every model is frozen, strict, and configured with `extra="forbid"`. IDs are bounded,
nonblank identifiers and globally unambiguous inside a contract. References between
checks, evidence, claims, and oracles must close over declared IDs. Duplicate IDs,
duplicate normalized paths, contradictory `not_applicable` conventions, missing
profile fields, and missing mandatory human gates are invalid.

Paths are normalized relative POSIX workspace paths. Absolute paths, drive-qualified
paths, URIs, network-style paths, parent traversal, and workspace-root locators are
rejected.

See the public synthetic [minimal contract](../tests/fixtures/physics/minimal_contract.yaml)
and [full contract](../tests/fixtures/physics/full_contract.yaml).

## Physics Audit Report v1

The report has these top-level verdicts:

- `pass`
- `fail_repairable`
- `human_review`
- `blocked_insufficient_evidence`
- `infrastructure_failure`

Evidence sufficiency is one of `sufficient`, `partial`, `insufficient`, or
`conflicting`. Each report must assess exactly every required identity, limiting case,
and required oracle from its contract. Duplicate or extra checks and missing required
checks are invalid.

Finding severity is `critical`, `high`, `medium`, `low`, or `informational`. Finding
categories are closed in `PhysicsFindingCategory`; they cover conventions, signs and
normalization, dimensions, tensors/indices, identities, limiting cases,
continuum/discrete mismatches, numerical evidence, gauge/constraint ambiguity,
physical interpretation, unsupported claims, oracle failures, missing evidence, and
report integrity. Disposition is separately bounded as `repairable`, `human_review`,
`evidence_blocking`, or `infrastructure_failure`. Category/disposition contradictions
are rejected before routing.

Evidence references have one of these exact forms:

| Kind | Required locator |
| --- | --- |
| `task_contract` | A declared top-level field or `<collection>.<id>` |
| `source` | A declared relative path and positive, ordered line range |
| `test` | A declared test ID |
| `artifact` | A declared artifact ID |
| `oracle` | A declared oracle ID |
| `derivation` / `document` | A declared relative path, optionally with a line range |
| `numerical` | A declared machine-readable numerical evidence ID |

Findings must contain evidence. Negative/reversed line ranges, unsafe paths, undeclared
references, duplicate findings/checks, and incomplete cross-references are invalid.
A report cannot declare `pass` with a non-passing required check, insufficient or
conflicting evidence, an unresolved critical/high finding, a report-integrity error,
an unresolved question, or a human-gate trigger.

The generated `PHYSICS_AUDIT_REPORT_OUTPUT_SCHEMA` passes the package's existing
production Structured Outputs schema validator. PA-1 only exposes and tests that
schema; it does not submit it to a model.

## Canonical serialization

Contract/report collections and reference tuples are canonicalized by ID or typed
locator. The qualified `durable_state.canonical_json` serializer sorts mapping keys,
uses fixed separators, rejects non-finite numbers, emits ASCII, and appends one newline.
`to_canonical_json()` and `canonical_sha256()` therefore produce identical bytes and
hashes for semantically identical input ordering.

## Deterministic routing

`derive_physics_audit_decision(contract, policy, report)` is a pure function. It does
not read or write files, run subprocesses, call a model/adapter, use Git, or access a
network. It returns `PhysicsRoutingDecisionV1` with one of:

- `pass`
- `request_repair`
- `require_human_review`
- `block_insufficient_evidence`
- `infrastructure_failure`

Routing precedence is fail-closed:

1. Invalid contract/policy/report input, a contract-policy mismatch, an unclosed report
   reference, or a report-integrity finding routes to `infrastructure_failure`.
2. Convention change, gauge/constraint ambiguity, new interpretation, unresolved
   scientific questions, or human-disposition findings route to human review.
3. Insufficient/conflicting required evidence routes exactly as the frozen policy says.
4. Open critical/high repairable implementation findings and failed required checks
   request repair when no higher-precedence condition exists.
5. Medium/low/informational repairable findings follow their explicit policy; there is
   no model-discretion default.
6. Only all passing required checks with sufficient evidence and no blocker can pass.

The report's verdict is compared with the derived route. A valid but lower-precedence
self-verdict is recorded with `report_verdict_overridden`; it never changes the route.
Rule proofs contain only bounded reason codes, declared IDs, canonical input hashes,
and the authoritative outcome.

## Developer validation commands

Both commands are read-only and require no Codex, GRChombo, Chombo, historical
toolchain, project-specific dependency, or model credential:

```console
research-supervisor validate-physics-contract contract.yaml
research-supervisor validate-physics-audit \
  --contract contract.yaml \
  --report report.json
```

Add `--json` for stable machine-readable diagnostics and routing proof. These commands
validate synthetic inputs only; there is deliberately no `run-physics-audit` command.

## Backward compatibility

PA-1 does not modify `SubstageSpecification`, `PendingAction`, `WorkflowServices`,
`WorkflowState`, `CodexActionRecord`, `AuditorModelResult`, campaign schema/state, or
any workflow journal semantic form. A schema-version-1 substage still rejects an
unknown `physics` field. Physics opt-in requires later versioned workflow dispatch;
ordinary `0.2.0` substages require no migration.

The public `tests/fixtures/compatibility_0_2_0` snapshots freeze current canonical
model hashes, model schemas, Code Auditor parsing, action proof, state, campaign
configuration/state, all journal semantic forms, output schemas, service injection
surface, CLI version behavior, and the ordinary successful transition sequence. Old
journals remain readable without reinterpretation or rewriting.
