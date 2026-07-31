# Evaluator migration: direct original historical replay

## Decision

Keep the visible-only campaign/evaluator split and use the preserved campaign's
original historical functional evaluator directly on the qualified host. Invoke it
through `run-direct-historical-replay` only after all model processes stop.

## Why the split remains correct

Visible campaign execution should contain only authority that Worker, Auditor, and
Supervisor models may see. Candidate finalization produces a narrow immutable
changed-files handoff. Historical gold, hidden fixtures, and exact references stay
outside campaign manifests, model workspaces, prompts, sessions, and run transitions.

Evaluation neither resumes nor scores campaign transitions. That separation prevents
evaluation material from becoming model context and allows the candidate to remain
immutable.

## Why direct replay is authoritative

The original evaluator already embodies the historical functional checks and was
qualified with the original project environment. Direct replay supplies it a
disposable committed baseline plus only the candidate overlay, captures a strict JSON
contract, and checks that candidate/prepared inputs remain unchanged.

This is the method used for the final five-task replay after all model processes had
stopped. It established 5/5 functional success and 0/5 exact historical identity.

## Why the packaged evaluator is experimental

The packaged Bubblewrap design investigated stronger filesystem isolation, sealed
dependency snapshots, bounded diagnostics, and reproducible exact comparison. The
work was informative, especially around compiler drivers, make tools, dependency
adjacency, and ignored installation artifacts. Full historical toolchain portability
and closure remain environment-specific.

The implementation and tests are preserved, but it is not the recommended path and no
new namespace/compiler-closure hardening is part of release closure. A failed package
qualification means evaluation was skipped because infrastructure was unqualified;
it must not be reported as a candidate failure.

## Actual security boundary

The required security boundary is process separation: stop Supervisor, Worker,
Auditor, Codex, and all other model processes before protected evaluation authority is
available. Keep candidate, prepared campaign, report, and disposable evaluator
workspaces outside model-readable roots.

Hermeticity and portability are valuable defense-in-depth and research properties, but
they are not required to establish the final five-task functional result.
