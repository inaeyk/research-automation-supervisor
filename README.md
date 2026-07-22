# Research Automation Supervisor

This repository will contain a reusable local supervisor for human-approved,
worker-implemented, independently audited research software substages.

The bootstrap files define Stage 0 only. Stage 0 creates the deterministic
foundation: a Python package, CLI, environment diagnostics, and stage-contract
validation. It deliberately does not launch nested Codex agents.

See:

- `STAGE_0_CONTRACT.md`
- `CODEX_STAGE_0_PROMPT.md`
- `README_FIRST.md`
