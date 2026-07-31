# Historical evaluation

## Supported command

The authoritative campaign method runs the prepared campaign's original functional
evaluator directly on the qualified host:

```bash
run-direct-historical-replay --candidate "private/final-candidate" --prepared-campaign "private/prepared-campaign" --output "private/direct-replay-report"
```

Before invoking it, stop and verify that every Supervisor, Worker, Auditor, Codex, and
other model process has exited. This process boundary—not packaging or container
portability—is what prevents models from accessing protected evaluation authority.

The command accepts explicit roots and a new output directory. It imports no campaign
engine or model adapter and starts no model process. Inputs and output must be disjoint.

## Replay algorithm

For each configured task, direct replay validates the candidate/prepared task and
source-tree mapping, exports the committed baseline with Git, reconstructs it under a
private disposable root, and initializes a fresh filter-independent Git baseline. It
then validates and applies only candidate `changes.json` operations.

Candidate payloads remain sealed. After overlay, the command adds owner read/write
permission to regular files and owner read/write/execute permission to directories only
inside the disposable workspace. This allows historical hidden overlay to replace an
exported file whose candidate mode is `0400`. It does not chmod the candidate,
prepared campaign, gold, source workspace, or source repository.

The evaluator command is taken from the prepared campaign's one declared `functional`
mapping. Its original evaluator, fixture root, Python executable, task ID, and working
directory must match the prepared authority. Raw output is captured privately; the
final nonempty stdout line must satisfy the historical functional JSON contract.

## Outcomes and exit codes

| Outcome | Meaning | Exit |
| --- | --- | --- |
| `passed` | Parsed contract, zero process exit, and all three checks true | 0 |
| `functional_failure` | Parsed/consistent contract with one or more checks false | 1 |
| input error | Candidate, prepared authority, mapping, output, or provenance invalid | 2 |
| `evaluator_infrastructure_failure` | Setup, launch, timeout, malformed contract, inconsistent exit, or input mutation | 3 |
| `no_structured_result` | Evaluator emitted no parseable final JSON contract | 4 |

An infrastructure result is not a candidate functional failure. A missing structured
result is also explicit rather than inferred from arbitrary compiler/test prose.

## Report layout

```text
direct-replay-report/
  report.json
  summary.md
  tasks/TASK/stdout.log
  tasks/TASK/stderr.log
  tasks/TASK/structured-result.json
  workspaces/TASK/workspace/        only with --keep-workspaces
```

`report.json` and `summary.md` contain only bounded result values, exit/timeout state,
hashes, counts, reason codes, and relative artifact names. Raw logs are mode `0600` and
may contain untrusted or protected-derived evaluator output; do not publish them or
send them to a model. Reports and directories are private owner-only by default.

The summary records candidate/prepared/evaluator provenance hashes and whether input
immutability was verified after replay. Workspace paths, gold contents, protected
fixture contents, and raw output are excluded from summaries.

## Functional and exact interpretation

The functional contract contains `hidden_tests_passed`, `visible_tests_passed`, and
`changed_path_match`; `passed` must equal their conjunction. Direct replay deliberately
runs only that functional phase.

Exact historical identity is a separate question. A candidate can satisfy all behavior
and scope checks through a different valid implementation. The qualified campaign is
5/5 functional and 0/5 exact identity. See the
[validation record](validation/five_task_historical_replay.md).

## Experimental package path

The following commands remain installable so the research can be reproduced and
extended, but they are explicitly experimental and non-authoritative:

```text
prepare-historical-replay-evaluation-package
evaluate-historical-replay
report-historical-replay-evaluation-commands
```

They explore sealed evaluation packages and a Bubblewrap compiler/runtime closure.
Portability remains unresolved for project-specific historical toolchains. A failed
environment qualification yields `evaluator_infrastructure_failure`, null functional
results, and no task evaluator launch. The earlier packaged 0/5 and 4/5 results are
superseded; do not cite them as the completed campaign score.

See the [evaluator migration note](migration/direct-historical-replay.md).
