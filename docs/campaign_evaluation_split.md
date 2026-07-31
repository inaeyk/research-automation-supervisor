# Visible campaign and evaluation split

This document records the security and product decision that visible campaign
execution and protected historical evaluation are separate processes with a one-way
candidate boundary.

## Supported architecture

`research-supervisor run-visible-campaign` receives visible project context, contracts,
prompts, path authority, fixed visible acceptance tests, source provenance, model
settings, and production-profile metadata. Gold-bearing or offline-evaluator fields
are rejected before model launch. After every task is terminal, it publishes a sealed
`final-candidate/`.

The candidate export is the only campaign output needed by evaluation. It contains
manifest-pinned per-task changed-file overlays and safe visible evidence. Candidate
finalization is the campaign completion transition; evaluation cannot change, resume,
or reinterpret campaign state.

After all Supervisor, Worker, Auditor, Codex, and other model processes stop, the
supported evaluation command is:

```bash
run-direct-historical-replay --candidate "private/final-candidate" --prepared-campaign "private/prepared-campaign" --output "private/direct-replay-report"
```

For each declared task, the command:

1. verifies the candidate manifest and prepared task mapping;
2. exports the prepared one-commit baseline into a disposable workspace;
3. creates private, filter-independent ephemeral Git metadata;
4. applies only the immutable candidate `changes.json` overlay;
5. adds owner-write permission only inside the disposable tree;
6. invokes the original declared `functional` evaluator on the qualified host;
7. parses its bounded schema-version-1 final JSON contract;
8. records pass, functional failure, evaluator infrastructure failure, or no
   structured result;
9. verifies candidate and prepared authority fingerprints after replay; and
10. removes workspaces by default.

Raw stdout/stderr are private per-task artifacts because evaluator output is untrusted.
The JSON and Markdown summaries contain only result booleans, exit/timeout state,
bounded reason codes, hashes, byte counts, provenance, and relative artifact names.

## Authoritative five-task result

The original historical evaluator was run directly after all model processes stopped,
using disposable reconstructed workspaces and the qualified host GRChombo/Chombo
environment. All five tasks passed hidden acceptance, visible acceptance, and
changed-path scope. Exact historical identity was false for all five tasks.

The first direct `hidden-cleanup` attempt was unevaluated because candidate files in
the disposable tree retained mode `0400`, preventing hidden overlay. Adding owner
write permission only to the disposable workspace allowed the unchanged candidate to
pass. This was evaluator-workspace infrastructure, not a candidate defect. The direct
command includes a synthetic regression for this case.

See [the safe validation record](validation/five_task_historical_replay.md).

## Functional correctness versus exact identity

Functional replay asks whether hidden acceptance, visible acceptance, and changed-path
scope pass in the historical environment. Exact identity asks whether the candidate is
byte-for-byte identical to the historical reference. A valid alternative
implementation can be functionally correct without being an exact reproduction.

For the completed campaign, functional replay is 5/5 and exact identity is 0/5. The
exact score does not reduce the functional score and should not be presented as a
candidate failure.

## Experimental packaged evaluator

`prepare-historical-replay-evaluation-package`, `evaluate-historical-replay`, and
`report-historical-replay-evaluation-commands` are retained as experimental research
infrastructure. They explore sealed archives, manifest-pinned dependency snapshots,
Bubblewrap namespaces, a qualified compiler/runtime profile, safe diagnostic
envelopes, and exact-reference comparison.

That work revealed useful portability and toolchain-closure constraints, especially
around the historical cell-storage environment. It is not on the supported path and
will not be namespace- or compiler-closure-hardened during release closure. Packaged
results of 0/5 and 4/5 are superseded by the direct replay. When packaged environment
qualification fails, its report uses `evaluator_infrastructure_failure` and null
per-task functional results; skipped evaluation must never be described as candidate
failure.

## Protected data rule

Prepared campaigns, gold, hidden tests, protected fixtures, exact references, and raw
private evaluator output remain outside the wheel, source distribution, public docs,
model prompts, and model-accessible workspaces. Preserve historical logs as evidence,
but label intermediate diagnoses and experimental scores as superseded rather than
rewriting them.
