# Authoritative Codex token accounting

Codex token counts are observational runtime data. They do not participate in
scientific routing, acceptance, scoring, proof construction, or qualified
artifact hashes.

## Global task wrapper

`$CODEX_HOME/bin/codex-task` runs non-interactive Codex tasks with JSON events:

```text
codex-task run <task-id> <working-directory> <prompt-file> [Codex options...]
codex-task resume <task-id> <prompt-file> [Codex options...]
```

The wrapper requires an explicit model for a new task. It durably stores raw
event streams in `$CODEX_HOME/task-ledgers/<task-id>/events/`, per-turn and
latest final assistant messages, and `TaskUsageReceipt.json`. The receipt binds
the task, Codex thread, requested model, CLI version, source-event SHA-256
hashes, completed-turn count, and every exact `turn.completed.usage` object.
It is marked incomplete for malformed JSONL, missing usage, duplicate events or
sources, thread conflicts, or a missing final assistant message. Its
machine-generated footer is emitted only after the Codex process has completed.

`combined_tokens` is exactly `input_tokens + output_tokens`.
`cached_input_tokens` is already included in `input_tokens` and is never added
again. `reasoning_output_tokens` is retained as a submetric and is never added
again.

## Research Automation Supervisor

`CodexUsageReceiptV1` is the strict per-action schema. It binds the durable
campaign/task identifier (from campaign/task state where available), semantic
role, initial-versus-repair action kind,
action ID, Codex thread/resume identity, selected model, CLI version, canonical
event-log path and hash, event counts, completeness, every exact turn counter,
and exact totals.

`TaskTokenLedgerV1` is the strict, canonical campaign/task aggregate. It
contains Worker, Coding Auditor, Physics Auditor, repair/retry, other-model, and
grand-total views. The repair/retry view is an overlapping action-kind view;
the grand total counts each receipt exactly once. Both models recompute and
validate all totals rather than trusting serialized aggregate values.

All real model launches pass through `codex_adapter.run_prepared_codex`.
Worker, Coding Auditor, replay/shadow supervisor, repair, retry, and resumed
actions therefore use one accounting hook. The Physics Auditor supplies its
qualified task binding and ledger root to that same hook. RAS parses its
existing confidentiality-preserving canonical/redacted JSONL rather than
retaining prohibited raw content; the numeric usage fields themselves remain
exact.

On recovery, an action with an identical durable receipt reuses that receipt.
A contradictory receipt or a second event log for the same action fails
closed. Identical event-log hashes are deduplicated so a resumed recovery
cannot count the same runtime event stream twice. An incomplete receipt remains
durable and makes the aggregate incomplete, but token values never influence
scientific decisions.
