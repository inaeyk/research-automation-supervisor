# Trusted Physics Oracle execution (PA-2)

PA-2 adds a model-free qualification surface for fixed, operator-owned physics
oracles while the package remains `0.2.0`. It does not add Physics Auditor model
execution, a physics workflow state, workflow integration, a repair loop, a provider
adapter, or project-specific scientific behavior.

## Authority boundary

A [Physics Task Contract v1](physics_auditor_foundations.md) may declare an oracle ID
and the checks that refer to it. The contract cannot contain argv, executable,
environment, isolation, or output-authority fields. A Physics Audit Report remains an
untrusted typed observation and cannot define or modify an intent.

Only a separately supplied `PhysicsOracleCatalogV1` is executable authority. The
developer running PA-2 is responsible for treating that catalog as trusted
operator-owned input. A catalog selects a hash-pinned system Python 3 executable, a
hash-pinned workspace program, and the closed `PhysicsOracleExecutionPolicyV1`.
Version 1 deliberately supports no shell, command string, PATH lookup, arbitrary
executable, arbitrary environment mapping, provider, or model-selected command.

The model is not the scientific oracle. A fixed analytic, symbolic, or numerical
program produces bounded evidence under human-frozen conventions. Its result can be
wrong and remains evidence for later human and Physics Auditor review; PA-2 does not
make a scientific release decision.

## Catalog and intent v1

All models are frozen, strict, versioned Pydantic models with unknown fields forbidden.
Catalog YAML rejects duplicate mapping keys. Collections are canonicalized by ID and
hashed with the existing qualified canonical JSON serializer.

An intent contains:

- a unique oracle ID that must also exist in the Physics Task Contract;
- exact executable path and SHA-256 under `/usr/bin` or `/bin`, restricted to system
  Python 3 under the `isolated_system_python_v1` executable policy;
- a relative, traversal-free trusted program path and SHA-256;
- fixed argv beginning with the exact executable, `-I -S -B`, and trusted program;
- a closed execution policy with workspace-root cwd, read-only workspace,
  scratch-only output, disabled network, bounded timeout and stdout/stderr, accepted
  exit codes, environment-profile reference, optional structured output, and exact
  artifact declarations.

Shell metacharacters in later argv elements are literal Python arguments. They are
never parsed, expanded, redirected, globbed, or interpolated. Execution uses
`subprocess.Popen` with an argv sequence, `shell=False`, disabled stdin, a new process
session, bounded streaming capture, and TERM/grace/KILL process-group cleanup. There
is no automatic retry.

Before intent persistence, the program is opened with no-follow semantics, bounded,
hashed from a stable file descriptor, and copied into engine-owned control storage.
The workspace pathname is never executed. Bubblewrap read-only binds only the sealed
copy at `/oracle/trusted-program.py`, closing the validate-to-launch replacement race;
recovery rehashes the sealed copy before doing any work.

## Environment and credentials

The child receives a newly constructed `minimal_python_v1` environment, not a filtered
copy of the parent:

- `HOME=/tmp/home`;
- `LANG=C.UTF-8` and `LC_ALL=C.UTF-8`;
- `PATH=/usr/bin` (argv still names the exact executable);
- `PYTHONDONTWRITEBYTECODE=1` and `PYTHONNOUSERSITE=1`;
- `RAS_ORACLE_SCRATCH=/scratch` and `TMPDIR=/scratch`;
- Bubblewrap supplies `PWD=/workspace` after the fixed chdir.

API keys, tokens, cookies, sessions, authorization values, cloud credentials, model
provider credentials, Git configuration/credential overrides, SSH-agent variables,
proxy variables, user configuration, and every other inherited variable are absent.
Credential-shaped parent values are used only by the in-process diagnostic redactor;
their names and values are not placed in requests, results, action records, proofs, or
logs. Raw process output is not embedded in any semantic object.

## Actual network enforcement

`network: disabled` is enforced by the existing system Bubblewrap primitive with
`--unshare-all`, which creates a separate network namespace. The command also uses
`--die-with-parent`, `--new-session`, and `--clearenv`. PA-2 actively probes the same
minimal mount construction before launch. The canonical proof records the backend
policy identity, Bubblewrap version and executable hash, and `enforced` capability.

If Bubblewrap is missing, untrusted, unsupported, or cannot create the required
namespace/mount topology, the oracle is not launched. PA-2 returns a typed
`infrastructure_failure` with `network_isolation_unavailable` and records capability
`unavailable`. It never silently falls back to shared networking or an environment
flag. Qualification includes a production-path connection attempt to a host loopback
listener; the isolated oracle cannot connect.

This is intentionally not a general hermetic sandbox. PA-2 mounts only the exact
Python executable, its standard library, the platform runtime-library directory and
dynamic loader, the workspace read-only, and scratch writable. It does not expose a
host executable directory, Codex, model credentials, a compiler toolchain, or a
project-specific scientific dependency closure. Known unrelated runtime-helper,
Node.js, GUI/media, Python test-suite, and Python build-configuration directories are
masked inside the otherwise required runtime-library mounts.

## Scratch and workspace integrity

The workspace is a read-only bind mount at `/workspace`. The action-owned scratch root
is the only host-backed writable mount. Every scratch regular file, symlink, and
directory receives a deterministic manifest entry with relative path, kind, mode,
length, and SHA-256. Declared artifacts also retain their operator-owned artifact ID.
Undeclared output, symlinks, special objects, missing required artifacts, oversized
declared artifacts, or scratch-path traversal make the result an
`output_contract_failure`. Artifacts remain separate from the canonical result and are
verified by hash.

Collection is itself bounded to 100 entries, 64 MiB across declared artifacts, the
declared per-file limits, and 1 MiB for any undeclared regular file before hashing.
An output that exceeds a collection bound is durably classified as an output-contract
failure but is intentionally non-completable: PA-2 issues no completion proof and
recovery fails closed until an operator discards that isolated action output. This
avoids claiming independent verification for scratch state that could not be fully
collected within the trusted bounds.

PA-2 independently collects `PhysicsOracleWorkspaceIdentityV1` before and after each
attempt. It contains no absolute workspace or temporary path. The identity binds:

- HEAD and branch/detached state plus Git object format;
- SHA-256 of the index file and canonical `git ls-files --stage -z` manifest;
- canonical binary/full-index tracked diff from HEAD;
- content, mode, kind and symlink-target hashes for every tracked worktree path;
- content, mode, kind and symlink-target hashes for every non-ignored untracked path;
- NUL-delimited porcelain status and recursive submodule status hashes.

Identity collection disables Git configuration, credentials, prompts, hooks through
protocol use, optional locks, and cross-filesystem discovery. It verifies stable
anchors around collection. Any before/after difference overrides a passing process and
becomes `workspace_integrity_failure`. PA-2 never repairs, reverts, chmods, or deletes a
project path.

## Result and completion proof v1

`PhysicsOracleExecutionResultV1` uses one of: `passed`, `functional_failure`,
`timed_out`, `infrastructure_failure`, `workspace_integrity_failure`,
`output_contract_failure`, `cancelled`, or `indeterminate_recovery`. Validators reject
contradictory status, timeout, structured-result, artifact, and identity combinations.

The result contains only the strict request, process status/exit code, observed stream
lengths and digests, separately captured redacted-prefix lengths and digests,
truncation flags, structured-output status/digest/outcome, scratch manifest, before
and after identities, environment-profile hash, network-enforcement identity, and
completion-proof hash. Diagnostic stdout/stderr files are separate bounded artifacts.

`PhysicsOracleCompletionProofV1` canonically binds the task, Physics Task Contract,
oracle ID, trusted intent, execution policy, network enforcement, environment profile,
initial identity, process result, stream facts, structured result, artifact manifest,
final identity, and integrity verdict. It excludes PID, process start ticks, hostname,
timestamps, absolute temporary paths, and wall time. `verify_physics_oracle_completion`
reparses exact canonical files, verifies the immutable action-record chain, rehashes
every retained diagnostic and scratch entry, and compares every proof field with the
strict result. Substitution or tampering fails closed.

## Durable recovery

PA-2 does not extend or rewrite the qualified workflow journal. Each explicit output
directory owns a separate sequence of immutable, self-hashed and hash-chained
`PhysicsOracleActionRecordV1` records:

1. `intent_accepted`;
2. `execution_prepared`;
3. `process_launch_attempted`;
4. `process_running`;
5. `process_exit_observed`;
6. `output_captured`;
7. `workspace_rechecked`;
8. `completion_proof_finalized`.

Recovery verifies every record, request, contract, intent, policy, executable,
program, environment, diagnostic, artifact, result, proof, and workspace binding.
Pre-launch accepted/prepared work may continue because no launch ambiguity exists.
Once launch may have occurred, recovery never reruns. A live process is signaled only
when Linux PID and `/proc` start ticks match the durable identity. Missing, stale, or
reused identity produces `indeterminate_recovery`; reused identities are never
signaled. A valid finalized proof is returned without another process launch, including
after a crash between proof creation and caller acknowledgement.

Immediately after `Popen`, PA-2 persists PID, process group, and start ticks before any
injected crash boundary. Bubblewrap's `--die-with-parent` covers abrupt supervisor
process death; in-process exception/crash injection deterministically terminates and
reaps the process group before control unwinds. A collection-bound failure stops at the
typed `output_captured` record and can never be resumed into a completion proof.

## Developer command

The command is a model-free qualification interface and never accepts argv:

```console
research-supervisor run-physics-oracle \
  --catalog trusted-catalog.yaml \
  --contract physics-contract.yaml \
  --oracle-id fixed-oracle \
  --task-id stable-task \
  --workspace /canonical/git/worktree \
  --output /new/action-output \
  --json
```

The output directory must not exist and must not overlap the workspace. There is no
overwrite. Internal callers may use `resume_physics_oracle` and
`verify_physics_oracle_completion` for deterministic recovery and verification.

## Current limitations

- Only hash-pinned system Python 3 programs using the standard library are supported.
- The Linux Bubblewrap backend and supported x86-64/aarch64 runtime layout are
  required; other platforms fail closed.
- The untracked manifest follows Git's normal ignored-path semantics.
- This substrate executes one oracle at a time and has no workflow or campaign state.
- Physics Auditor model execution still does not exist. No model/provider is invoked
  by PA-2, and ordinary `0.2.0` workflows remain unchanged.
