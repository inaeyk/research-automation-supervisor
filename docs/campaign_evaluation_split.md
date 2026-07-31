# Visible campaigns and offline historical evaluation

Campaign execution and historical evaluation are separate products with a
one-way file boundary.

## Visible campaign executor

`research-supervisor run-visible-campaign CAMPAIGN.yaml` runs the ordered
visible workflow:

1. supervisor prompt;
2. worker action;
3. deterministic scope checks and visible acceptance tests;
4. fresh auditor action;
5. at most the configured bounded repair;
6. terminal visible task record.

After every configured task is terminal, the executor writes
`final-candidate/`. Candidate finalization is the campaign completion
transition. The executor has no evaluation-package input and performs no
historical comparison.

A visible campaign specification contains only task contracts, project
context, path authority, visible acceptance commands, source provenance, model
settings, and production-profile metadata. Legacy evaluation fields are
accepted only when empty. A gold-bearing legacy manifest is rejected before a
model launch.

Visible campaign acceptance commands use the registered
`/usr/bin/python3 <visible-script>` runner. The script and its working directory
must be inside the visible package; shell, `-c`, `-m`, absolute, parent-traversal,
and external-authority arguments are rejected at campaign load. Supervisor
instructions containing absolute, parent-traversal, or offline-evaluation
locators are rejected before they reach a Worker or Auditor.

The candidate contains a canonical manifest, deterministic changed-file
overlays and evidence, baseline commit/tree identities, visible test results,
scope evidence, and terminal worker/auditor summaries. It excludes Codex
session caches, credentials, and campaign control state. Each task's changed
bytes are durably sealed before its terminal campaign transition; final
publication and crash recovery consume those immutable inputs rather than a
live workspace.

Source provenance and execution identity are deliberately separate. A task
records the original source commit/tree/archive digest and the fresh
one-commit execution repository's baseline commit/tree. Their commit IDs may
differ after archive reconstruction; their tree IDs must match. The offline
package repeats the original source commit/tree and independently archives the
qualified one-commit tree. Those archive bytes are package-manifest pinned,
and the evaluator independently derives the extracted archive's Git tree
before any test runs.

## Offline evaluator

Historical authority is first materialized by a separate host-side command
after campaign completion:

```text
prepare-historical-replay-evaluation-package \
  --source-prepared-campaign /preserved/pre-split-campaign \
  --output /private/gl-five-historical-replay-v1
```

The preparer imports no campaign engine or model adapter. It recognizes the
legacy five-task manifest structurally, requires clean one-commit task
workspaces, archives committed Git trees, copies functional fixtures and the
historical evaluator with independent inodes, constructs deterministic exact
reference archives, snapshots pinned dependency commits, and records source
provenance and production-profile authority. Source identity and content are
fingerprinted before and after preparation. Publication is atomic and fails
if source authority changes, contains unsupported objects or links, has
ambiguous task mappings, or is incomplete.

Every package contains `evaluation-package-manifest.json`. Its canonical,
versioned payload records ordered paths, file roles, object types, modes, byte
lengths, and SHA-256 values. Prepared payload files are mode `0400`,
directories are mode `0500`, and the evaluator verifies the complete manifest
before extracting an archive or running a functional test. Preparation output
contains only the package path and manifest digest, never protected content.

`evaluate-historical-replay` is a separate console program:

```text
evaluate-historical-replay \
  --candidate /path/to/final-candidate \
  --evaluation-package /private/gl-five-historical-replay-v1 \
  --output /path/to/new-report-directory
```

The command accepts no campaign run or session argument. It imports no campaign
engine, Supervisor, Worker, Auditor, or model adapter. It verifies the immutable
candidate, reconstructs evaluation workspaces from digest-pinned baseline
archives, applies the candidate changed-file overlays, runs the package's
deterministic tests, optionally compares an exact reference archive, and emits
one standalone report. Its output must be disjoint from both inputs.

The package configuration is
`evaluation-config/offline-evaluation.json`. It has schema version 1, a package
ID, and ordered task records. Each task pins a baseline archive by SHA-256 and
declares tests through the audited `python_script_v1` runner. Package files are
non-executable except for executable-mode files copied from pinned dependency
commits. Each script is digest-pinned and runs inside a fresh Bubblewrap
namespace containing only the reconstructed workspace, the read-only evaluation
package, system Python/toolchain files, private temporary storage, private proc,
and no network. Arbitrary package-defined host commands are not supported.
Optional exact-reference archives and expected changed paths are also digest-
or value-pinned.

The offline runner mounts an audited Python, Git, C++, Fortran, assembler,
linker, and `make` runtime profile rather than the host `/usr` tree. Git is a
single qualified executable operating only on an action-owned ephemeral
repository; user/system configuration, credentials, host repositories, and
Git helper directories are absent. Python starts isolated without site
packages. A single read-only `/bin/sh` is present for declared test-tool
compatibility, while package-selected host commands, Node, campaign packages,
model adapters, user installation roots, and host campaign state are absent.
Evaluation output must be a new child of an existing exact, non-symlink parent
disjoint from both inputs.

Standalone report schema version 2 separates functional quality from historical
identity. `passed` remains as a compatibility alias for `all_functional_passed`;
`strict_combined_passed` retains the former functional-plus-exact interpretation.
Per-task stdout and stderr are drained without unbounded memory capture.
Content is treated as untrusted because a test can print protected workspace
bytes: the private report's deterministic `artifacts/` records contain stream
lengths, hashes, truncation state, and strictly parsed safe exception fields,
but never raw process output. Their paths and hashes are recorded in the report.

Evaluation results never create campaign transitions, resume a campaign, or
feed a model. The evaluator is intended to be invoked only after all campaign
model processes have terminated.

`report-historical-replay-evaluation-commands` reports next steps from
explicit host paths. When the package is missing it emits both the exact
preparation command and the subsequent evaluation command. It reports direct
evaluation readiness only after an existing package manifest validates.
Visible campaign reports record offline evaluation as `not_performed` and do
not invent an evaluation-package path.

## Physical layout

Visible execution authority and offline evaluation authority use disjoint
roots:

```text
runs/prepared-campaigns/gl-five-visible-campaign-v1/
  visible/
  control/
  launch/
  runtime/
  final-candidate/

offline-evaluation/gl-five-historical-replay-v1/
  evaluation-package-manifest.json
  baseline-archives/
  dependencies/
  evaluation-config/
  protected-fixtures/
  exact-reference/
  evaluators/
  provenance/
```

An offline-evaluation root is never passed to the visible campaign loader or a
model process. Campaign execution is prepared and completed before a private
offline package is made available to the separate evaluator invocation; the
visible executor deliberately retains the normal Worker/Auditor sandbox policy
instead of the retired gold-bearing hostile-runtime design.
