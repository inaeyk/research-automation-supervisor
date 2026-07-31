# Five-task direct historical replay validation

Date: 2026-07-31

Qualified source commit: `d3bcadd47aacf522e842c076b7915de330bfe289`

This is the authoritative safe metadata record for the completed visible-only
campaign. It intentionally excludes protected fixtures, gold source, exact reference
contents, hidden-test source, reconstructed workspaces, and raw evaluator output.

## Identity

- Campaign: `gl-five-visible-campaign-v1`
- Run token: `3283da26577c167ed3f93dd5be0aae09`
- Candidate manifest SHA-256:
  `7f891080fc205341dba3b9b0e0e56f24c9fb55d0e8a9b8173baef2056d6b7405`
- Evaluator authority: the original historical functional evaluator run directly on
  the qualified host after all model processes stopped, using disposable reconstructed
  workspaces.

## Results

| Task | Functional | Hidden | Visible | Changed path | Exact identity |
| --- | --- | --- | --- | --- | --- |
| `reduced-vars-gp` | pass | pass | pass | pass | false |
| `hidden-cleanup` | pass | pass | pass | pass | false |
| `cell-storage` | pass | pass | pass | pass | false |
| `hat-gamma-x` | pass | pass | pass | pass | false |
| `stage4ao-b` | pass | pass | pass | pass | false |

Totals:

- historical functional replay: 5/5;
- hidden acceptance: 5/5;
- visible acceptance: 5/5;
- changed-path scope: 5/5; and
- exact historical identity: 0/5.

Functional correctness means the historical hidden and visible acceptance behavior and
changed-path expectations passed. Exact identity asks whether the implementation is
byte-for-byte the historical reference. The latter is not required for a functionally
valid alternative implementation.

## Qualification caveat

The first direct `hidden-cleanup` attempt was unevaluated: candidate files copied into
the disposable workspace retained mode `0400`, so the hidden fixture overlay could not
replace them. Adding owner-write permission only to the disposable workspace allowed
the unchanged candidate to pass hidden and visible acceptance. The candidate and its
sealed export were not modified. This was evaluator-workspace permission
infrastructure, not a candidate defect.

## Superseded results

Packaged Bubblewrap evaluator outputs of 0/5 and 4/5 were intermediate experimental
infrastructure results. They are superseded and are not the campaign score. In
particular, `cell-storage` is not a confirmed candidate failure. The packaged evaluator
remains useful research infrastructure but is non-authoritative for this result.

The machine-readable companion is
[`five_task_historical_replay.json`](five_task_historical_replay.json).
