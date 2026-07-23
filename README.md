# Research Automation Supervisor

This repository contains a reusable local supervisor for human-approved,
worker-implemented, independently audited research-software substages.

Stage 0 provides strict contracts and environment diagnostics. Stage 1 provides
the deterministic one-prompt Codex process adapter. Stage 2 adds a durable,
single-substage workflow with one exact-ID persistent worker, fresh ephemeral
auditors, fixed argv tests, Git/scope evidence, bounded repairs, and explicit
human pauses. Stage 3 adds retrospective blind calibration with one persistent
canonical-UUID, read-only supervisor, complete confidentiality preflight,
post-finalization authoritative comparisons, structured human reviews, and
informational-only readiness.

See:

- `STAGE_0_CONTRACT.md`
- `STAGE_1_CONTRACT.md`
- `STAGE_2_CONTRACT.md`
- `STAGE_3_CONTRACT.md`
- `CODEX_STAGE_0_PROMPT.md`
- `docs/codex_adapter.md`
- `docs/workflow_engine.md`
- `docs/shadow_calibration.md`
