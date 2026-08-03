# Standalone Codex Physics Auditor (PA-3)

PA-3 adds one standalone, fresh-session, read-only Codex Physics Auditor action to
package version `0.2.0`. It consumes a strict Physics Task Contract v1 and independently
verified PA-2 completion proofs, validates an untrusted Physics Audit Report v1, then
uses the unchanged PA-1 router as the sole routing authority.

This action is deliberately separate from the Worker, Code Auditor, ordinary substage
state machine, campaign engine, `WorkflowState`, `PendingAction`, `CodexActionRecord`,
and workflow journal. It does not repair code or send `request_repair` to a Worker.
`require_human_review` is reported to the operator but does not create a workflow pause
state in PA-3.

## Architecture and authority boundaries

The trusted inputs are:

- a `PhysicsTaskContractV1`;
- a `PhysicsAuditorExecutionConfigV1` owned by the operator;
- an exact Git worktree and its PA-2 workspace identity;
- a root containing zero or more finalized PA-2 oracle action directories; and
- a new standalone output directory.

The engine constructs the evidence index and prompt. Neither a Worker nor another
model can supply a prompt, Codex argv, oracle argv, role policy, output schema, or
executable policy. The Physics Auditor can inspect declared workspace files using its
normal read-only Codex workspace tools. No PA-2 oracle executable surface is added to
the model process. The sealed PA-2 intent and argv remain outside the workspace and
prompt. As a fail-closed backstop, PA-3 scans the qualified adapter's canonical command
events for every sealed oracle program path; any matching command makes the action an
`infrastructure_failure` with `oracle_execution_attempted`, and no report is routed.

PA-3 is Codex-specific. It is not a provider-neutral action or generic model-exec
adapter. Heterogeneous provider selection remains planned for a later stage.

## Execution configuration v1

`PhysicsAuditorExecutionConfigV1` is strict (`extra="forbid"`) and canonical-hashed.
Its closed fields are:

| Field | V1 authority |
| --- | --- |
| `schema_version` | exactly `1` |
| `backend` | exactly `codex_cli` |
| `model`, `reasoning_effort` | bounded current Codex adapter identities |
| `timeout_seconds` | 30 through 14,400 seconds |
| `max_stdout_bytes`, `max_stderr_bytes` | 1 KiB through 100 MiB |
| `sandbox_policy` | exactly `read_only` |
| `approval_policy` | exactly `never` |
| `network_policy` | `disabled_by_codex_policy_not_kernel_enforced` |
| `output_schema_id` | exactly `physics_audit_report_v1` |
| `prompt_template_version` | exactly `physics_auditor_prompt_v1` |
| `session_policy` | exactly `fresh_ephemeral` |
| `structured_output_policy` | exactly `strict` |
| `trusted_executable` | optional absolute Codex path plus SHA-256 |
| `environment_allowlist_profile` | exactly `codex_cli_minimal_v1` |

There is no command, argv, prompt, environment mapping, resume ID, continuation ID,
workspace-write policy, `danger-full-access`, or provider extension field. The backend
selector is versioned without claiming that any other backend exists.

The child environment is built from a small runtime allowlist and is then passed
through the qualified adapter's credential-name filter. In particular, an outer
`CODEX_THREAD_ID`, API key, token, proxy, cloud credential, SSH agent, or Git credential
override is not inherited. Provider credential values are rejected/redacted at the
adapter boundary and are absent from the result, action record, and proof.

## Codex Physics Auditor role policy

The PA-3 policy is a Codex-specific semantic layer over the unchanged adapter role
`auditor`:

- `--sandbox read-only`;
- `--ask-for-approval never`;
- `--ephemeral` on every action;
- no `resume`, `--last`, or `--all`;
- no Worker or Code Auditor session identifier input;
- no `--yolo`, `--full-auto`, or `danger-full-access`;
- a writable action-owned temporary directory outside the project; and
- strict `PhysicsAuditReportV1` Structured Outputs.

The fixed role-policy canonical SHA-256 is
`953c71299fa1d0add6a7c3a400f481037661a63ce1f48a06356022b9f9fc45e3`.
The adapter supplies `web_search="disabled"` and
`sandbox_workspace_write.network_access=false`. This is a Codex policy, not a separate
kernel network namespace, so PA-3 does **not** claim kernel-enforced network isolation.
PA-2 oracle execution retains its separately qualified Bubblewrap network namespace.

## Safe evidence index v1

`PhysicsAuditorEvidenceIndexV1` contains only canonical machine-readable authority:

- hashes and IDs for the contract and workspace identity;
- convention, assumption, required-identity, limiting-case, and forbidden-claim IDs;
- declared test, artifact, numerical, derivation, and document entries;
- relative declared and changed workspace paths with kind, mode, size, SHA-256, and
  bounded line count;
- one entry for every contract oracle, marked `verified` or explicitly `missing`;
- safe PA-2 status, outcome, structured-result digest, completion-proof identity,
  intent/policy hashes, and artifact metadata; and
- fixed statements that raw streams, machine temporary paths, and protected historical
  material are excluded.

Raw stdout/stderr, build logs, environment values, source contents, hidden fixtures,
historical gold, and absolute machine paths are not embedded. Every supplied PA-2
directory is passed through `verify_physics_oracle_completion` before model launch.
The result must bind the same task, contract, oracle ID, sealed intent, execution
policy, unchanged before/after workspace, and current auditor workspace identity.
Duplicate evidence IDs and undeclared oracles fail closed.

After model completion, every report citation is checked against both the unchanged
PA-1 contract validator and this index. A missing oracle cannot be reported as an
observed pass. Source line ranges must fit a present declared file. Invented IDs,
paths, tests, artifacts, numerical evidence, oracles, and contract locators invalidate
the report.

## Deterministic prompt

The human-written `physics_auditor_prompt_v1` template has these fixed sections:

1. role and independence;
2. audit scope;
3. explicit non-goals;
4. canonical Physics Task Contract v1;
5. canonical safe evidence index;
6. verified oracle summaries and proof identities;
7. relative workspace paths and changed-path manifest;
8. evidence citation rules;
9. mandatory human-gate rules;
10. strict PhysicsAuditReportV1 requirements and output schema; and
11. the insufficient-evidence stop condition.

Collections are canonical-sorted and all inserted objects use the qualified canonical
JSON serializer. The prompt has exactly one final newline and contains no timestamp,
PID, hostname, provider session ID, absolute workspace/output path, or model-generated
instruction. It does not request hidden chain of thought.

The template SHA-256 is
`e3e444ad4b9a798f14cd0b67727150a503fba8e87651c75f6a77277a667b804c`.
The strict output-schema SHA-256 is
`82ffc2fe49e3929678368733c6200933d072c27abcd548d65cb52dbe62121297`.
The public missing-evidence golden prompt is recorded in
`examples/physics_auditor/synthetic/prompt-golden.json`.

## Request, result, record, and proof

`PhysicsAuditorActionRequestV1` binds the action/task IDs, contract/config/workspace,
changed-path and evidence-index hashes, unique PA-2 result/proof/intent/policy bindings,
declared derivation/document paths, template identity, output schema, attempt, and the
literal standalone output-directory identity. It contains no absolute temporary path,
command, credential, hidden evaluation locator, or provider session ID.

`PhysicsAuditorActionRecordV1` is a separate immutable, self-hashed chain. It may keep
the exact provider session/thread and Linux PID/start-tick identity as operational
evidence. Those volatile values are excluded from `PhysicsAuditorActionProofV1`.

`PhysicsAuditorActionResultV1` distinguishes:

- `routing_completed` with `model_process_completed`, `report_validated`, and the
  authoritative `routing_decision`;
- `report_invalid`;
- `workspace_integrity_failure`;
- `evidence_integrity_failure`;
- `infrastructure_failure`; and
- `indeterminate_recovery`.

A successful Codex process with malformed or unclosed structured output is
`report_invalid`, an infrastructure-class failure, never a pass. For a valid report,
the unchanged `derive_physics_audit_decision` function is authoritative even when it
overrides the model's self-declared verdict.

The Codex-specific action proof binds the request, contract, config, backend executable
and version identity, model/effort, role policy, template and rendered prompt, output
schema, initial/post-model/final workspace identities, evidence and oracle-proof
manifests, bounded model output, parsed report, routing decision, action status, and
integrity verdict, including the oracle-command detection verdict. Verification also
revalidates the complete qualified-adapter artifact manifest and its exact read-only,
ephemeral, no-resume command policy. PID, time, hostname, terminal width, absolute
temporary paths, progress output, and provider session IDs are excluded from the
canonical semantic proof.

## Workspace integrity and recovery

The qualified PA-2 identity is collected before launch, immediately after the Codex
action returns, and again before accepting the report for routing. It covers tracked
contents, staging/index state, mode, symlinks, all non-ignored untracked paths, status,
and recursive submodule status. Any change overrides a model pass. PA-3 never reverts,
deletes, chmods, or repairs a project path.

The standalone phases are:

1. `action_accepted`
2. `evidence_verified`
3. `prompt_finalized`
4. `model_launch_attempted`
5. `model_running`
6. `model_exit_observed`
7. `output_captured`
8. `report_validated`
9. `workspace_rechecked`
10. `routing_completed`
11. `action_proof_finalized`

Accepted, evidence-verified, and prompt-finalized actions can continue because no
launch could have occurred. Once launch is possible, PA-3 never reruns blindly and
never resumes a Codex session. A missing, stale, live, or reused PID in an ambiguous
post-launch record becomes `indeterminate_recovery`; a matching live process is
terminated without continuation. Durable output recorded after an observed model exit
may continue through parsing and routing. A finalized valid proof is returned without
another model launch. Input substitution, record corruption, prompt/output/report/
route tampering, or workspace drift fails closed.

## CLI and public synthetic fixtures

The command is intentionally absent from the main quick start:

```console
research-supervisor audit-physics \
  --contract examples/physics_auditor/synthetic/contract.yaml \
  --execution-config examples/physics_auditor/synthetic/execution-config.yaml \
  --task-id synthetic-task \
  --workspace /absolute/public-synthetic-git-worktree \
  --oracle-evidence /absolute/completed-pa2-evidence-root \
  --output /absolute/new-pa3-action \
  --json
```

Add `--validate-only` to verify the contract, config, workspace, PA-2 proofs, evidence
index, request, and in-memory prompt without locating Codex, creating the output, or
launching a model. Add `--resume` only for an existing action directory. The command
accepts no arbitrary prompt, command, oracle command, token, or API key argument.

The public fixtures cover a clean implementation, seeded sign error, missing evidence,
convention change, gauge/constraint ambiguity, unsupported discovery claim, correct
alternative implementation, and adversarial output. They do not contain GL-with-AI,
protected historical material, or machine-specific scientific dependencies.

## Current limitations

- Physics Auditor is not integrated into normal substages or campaigns.
- It does not repair code, route work to a Worker, or mutate workflow state.
- It does not run, select, modify, or redefine physics oracles.
- It does not approve publication-level claims or scientific release.
- Human-gate routing is reported but not integrated into workflow state.
- Only Codex CLI is supported in PA-3; no provider-neutral claim is made.
- Codex network disablement is policy-level, not a PA-2-style kernel namespace.
- Full physics-quality qualification, seeded-defect benchmarks, protected evaluation,
  and the GL pilot remain later work.
