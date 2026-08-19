# Context Economy / Desktop Handoff — 2026-08-19

## Scientific workflow status

PA-5D is PAUSED.

The existing PA-5D0 preregistration draft is stale for execution authority
because model-launch/context/token-accounting behavior changed afterward.

Do not approve or run PA-5D1 from the stale authority.

After Context Economy qualification:
1. regenerate PA-5D0;
2. review/freeze preregistration;
3. run PA-5D1;
4. if PASS, proceed to PA-6.

## Token-accounting bootstrap baseline

Original bootstrap:

- input: 25,502,447
- cached input: 25,181,952
- uncached input: 320,495
- output: 55,927
- reasoning output: 18,179
- combined: 25,558,374
- inference samples: 157
- command executions: 118
- command-event chars: 2,234,679
- median inference input: 182,939
- maximum inference input: 218,845
- compactions: 0

No-work Codex baseline:

- input: 16,733
- cached input: 6,912
- output: 6

## 64k same-task replay

Same bootstrap workload with early compaction:

- input: 4,610,706
- cached input: 4,273,152
- uncached input: 337,554
- output: 46,008
- reasoning output: 12,362
- combined: 4,656,714
- inference samples: 117
- command executions: 172
- command-event chars: 902,694
- median inference input: 39,543
- maximum inference input: 64,100
- compactions: 10

Input reduction versus original: approximately 81.9%.

Conclusion:
runaway cumulative token use was dominated by repeated processing of a large
accumulated context. Early compaction fixes most of the catastrophic multiplier.

## Context Economy implementation run

- input: 3,912,997
- cached input: 3,593,216
- uncached input: 319,781
- output: 42,164
- reasoning output: 9,995
- combined: 3,955,161
- inference samples: 97
- median inference input: 44,235
- maximum inference input: 63,561
- compactions: 10
- command executions: 134
- command-event chars: 1,116,944

## Current optimization direction

64k context control is the proven first mechanism.

Next optimization should add semantic task decomposition:

- substantial stages normally become roughly 2–6 coherent subtasks;
- fresh model context at semantic subtask boundaries;
- repository/artifacts/receipts/hashes are durable memory;
- conversational history is ephemeral working memory;
- Worker -> Auditor transfers compact HandoffV1, not Worker conversation;
- repairs receive concise audit findings plus candidate state;
- bounded model-visible tool output;
- batch related inspection/tests;
- avoid still-valid test reruns;
- B4 concise Supervisor prompts by default.

The next controlled experiment should replay the same bootstrap task using:
1. semantic decomposition;
2. fresh subtask sessions;
3. compact handoffs;
4. the proven ~64k context ceiling.

Hypothesis to test:
reduce the same broad workload from ~4.61M toward ~1–3M input tokens without
quality loss.

## Token-accounting rule

All future Codex tasks must use authoritative runtime-derived token accounting.

Never estimate token use.

Report:
- input;
- cached input;
- uncached input;
- output;
- reasoning output;
- combined;
- role/session/retry breakdown where available.

Do not ingest raw Codex rollout transcripts into model context just to count
tokens.
