Pause PA-5D scientific/preregistration work.

This task is ONLY to implement exact Codex token accounting globally and in
Research Automation Supervisor.

Do not approve/finalize PA-5D0 authority.
Do not launch benchmark or GL model sessions.

GOAL

Never again rely on a model to remember/estimate its own token usage.

Use authoritative Codex runtime events.

Official Codex non-interactive JSON mode emits:

turn.completed.usage:
- input_tokens
- cached_input_tokens
- output_tokens
- reasoning_output_tokens

Implement two layers.

1. GLOBAL CODEX TASK WRAPPER

Create a persistent global helper under:

$CODEX_HOME/bin/codex-task

with durable ledgers under:

$CODEX_HOME/task-ledgers/

It must support at least:

codex-task run <task-id> <working-directory> <prompt-file> [Codex options...]
codex-task resume <task-id> <prompt-file>

Internally use:

codex exec --json

and for continuation:

codex exec resume <thread-id> ...

Requirements:

- retain raw JSONL events;
- retain final assistant message;
- capture thread/session identity;
- extract every turn.completed.usage object;
- aggregate usage across resumed turns belonging to the same task;
- write a compact authoritative TaskUsageReceipt JSON;
- hash the source JSONL files;
- never infer tokens from text;
- never double-count cached_input_tokens;
- combined_tokens = input_tokens + output_tokens;
- retain cached_input_tokens and reasoning_output_tokens as submetrics;
- record Codex CLI version, model, task id, thread id, and turn count;
- fail closed or mark usage incomplete when a completed turn lacks a usage
  event.

After a Codex turn finishes, append/display a MACHINE-GENERATED Token usage
footer based on the receipt.

The model itself must not be responsible for writing the exact final total,
because its own final-output usage is only known after turn.completed.

Update global $CODEX_HOME/AGENTS.md accordingly:
- authoritative runtime receipt is the source of truth;
- never estimate;
- never ingest raw rollout transcripts merely to count tokens;
- final token footer may be appended externally after model completion.

2. RESEARCH AUTOMATION SUPERVISOR NATIVE LEDGER

Add strict models such as:

CodexUsageReceiptV1
TaskTokenLedgerV1

Integrate usage capture into every Codex model-launch path used by:
- Worker;
- Coding Auditor;
- Physics Auditor;
- repairs/retries;
- other model-backed campaign actions.

Use Codex JSON events as authority.

Preserve existing:
- prompt bytes;
- model selection;
- reasoning effort;
- sandbox;
- approval policy;
- session freshness/resume semantics;
- PA-5A recovery;
- PA-5C1 blindness;
- PA-5C2 scoring/proof semantics;
- PA-5C3 orchestration;
- PA-5C4 security boundaries.

Token accounting is observational only and must not affect scientific routing
or acceptance.

Each model action must durably bind its usage receipt to:
- campaign/task;
- role;
- action id;
- Codex thread/session id;
- model;
- event-log hash.

Campaign/task aggregate must report:
- Worker input/output/combined;
- Coding Auditor input/output/combined;
- Physics Auditor input/output/combined;
- repairs/retries;
- other model sessions;
- total input;
- total cached input;
- total output;
- total reasoning output;
- total combined.

No double counting across resume/recovery.

A recovered action with an existing verified usage receipt must reuse that
receipt.

3. TESTING

Add deterministic tests for:
- documented turn.completed usage schema;
- multiple turns;
- resume;
- cached-token handling;
- reasoning-token handling;
- duplicate event rejection;
- missing usage;
- malformed JSONL;
- recovery/reuse;
- Worker/Auditor aggregation;
- exact final combined total.

Use fake JSONL for regression tests.

You may run ONE tiny real Codex JSON-mode smoke if needed to verify the
installed CLI event schema. Record its usage too.

Do not launch PA-5D benchmark/GL sessions.

Run focused tests, Ruff, strict mypy, and relevant regressions.

IMPORTANT PA-5D CONSEQUENCE

Because runtime/model-launch instrumentation changes after the PA-5D0 draft was
hashed, declare the current PA-5D0 draft stale for execution authority.

Do not silently reuse its hashes.

After token instrumentation qualifies, PA-5D0 authority must be regenerated
and reviewed before PA-5D1.

FINAL REPORT

Report:
- global wrapper paths;
- receipt schemas;
- exact launch paths instrumented;
- recovery semantics;
- tests;
- whether any qualified scientific semantics changed;
- PA-5D0 staleness consequence.

Do not self-report token totals from memory.

The external codex-task/runtime wrapper will append the authoritative Token
usage footer after this turn completes.
