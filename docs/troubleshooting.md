# Troubleshooting

## `doctor` is not ready

Run `research-supervisor doctor --json`. Confirm Python is at least 3.11, Git is on
`PATH`, Codex CLI is at least 0.144.0, and `codex login status` succeeds. The command
normalizes login state and intentionally omits raw authentication output.

The bundled synthetic quick start can exercise package and workflow mechanics without
a real Codex installation by placing its test double first on `PATH` for that command.

## Unexpected approval prompts

Workflow role commands are noninteractive and fixed. An approval prompt usually means
the installed Codex command-line behavior is incompatible or a different executable is
being resolved. Run `which codex`, `codex --version`, and `doctor`; do not work around
the issue with `--yolo`.

## Dirty worktree rejected

The baseline includes untracked files. Commit intended inputs, move local outputs to an
ignored directory, stash unrelated changes, or create a dedicated worktree. Do not
weaken the clean-baseline check; it is required for complete scope evidence and safe
resume.

## Acceptance-test ID mismatch

Required checks use exact frozen YAML IDs, including case and punctuation. Update the
human-owned specification/policy before a new run; never alter frozen authority or a
durable run to match model output.

## Interrupted run

Inspect `substage-status` or `visible-campaign-status`. Use `resume-*` only when the
state is nonterminal and not a human/limit/checkpoint pause. If an action intent exists
without provable completion, the engine pauses rather than repeating the process.
Preserve the escalation record and ask a human to decide.

## Human or repair-limit pause

Read the pause reason and evidence. A human continuation must be a new exact file and
continues the existing Worker session. Repair limits are deliberate; raising the limit
requires a new frozen specification/run rather than mutating the old one.

## Dependency or environment failure

Separate workflow dependencies from candidate behavior. Missing Codex/Git/Python is an
environment error. A fixed test can also require a compiler, library, shell tool, or
project setup absent from the host. Record the missing dependency and qualify the host
before interpreting a task result.

For historical replay, `evaluator_infrastructure_failure` and
`no_structured_result` are not candidate functional failures. Inspect private captured
output without sending it to a model.

## Exported candidate files are `0400`

That mode is intentional. Do not modify the candidate. `run-direct-historical-replay`
copies overlays to a disposable workspace and adds owner-write permission there so a
hidden fixture overlay can replace an existing candidate file. The original candidate
and prepared authority are fingerprinted before and after replay.

## Candidate/prepared mapping rejected

Confirm the complete final-candidate directory was copied, its canonical manifest was
not reformatted, task order matches the preserved campaign, and source commit/tree
provenance refers to the same prepared baseline. Do not repair the candidate manually;
restore it from the immutable export.

## Experimental packaged evaluator qualification failed

Treat the result as an infrastructure outcome. The packaged Bubblewrap evaluator is
not the supported campaign path and its compiler/runtime closure is intentionally not
being expanded during release closure. Use the qualified host-side direct evaluator
after all model processes stop.
