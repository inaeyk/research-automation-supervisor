Implement Stage 4 of the Research Automation Supervisor.

Read `STAGE_4_CONTRACT.md`, the completed Stage 0–3 implementation and tests,
all committed production hotfixes, `pyproject.toml`, and current documentation.
Implement the frozen contract exactly. Do not edit any Stage 0/1/2/3/4 contract
or implementation-prompt file.

Stage 4 is live quarantined shadow observation:

- one authoritative Stage 2 run executes unchanged;
- observe only durable Stage 2 journal intents;
- freeze point-in-time evidence envelopes;
- launch one persistent read-only supervisor in a repository-free quarantine
  workspace;
- never send supervisor proposals to workers or auditors;
- never wait for or depend on shadow output before Stage 2 continues;
- compare only after both proposal and authoritative action finalize;
- preserve immutable human reviews and informational readiness;
- automation remains disabled.

Prefer journal observation over blocking Stage 2 hooks. Any optional Stage 2
observer interface must be no-op by default, exception-contained, non-awaiting,
and preserve byte-identical Stage 2 behavior.

Use existing Stage 3 proposal, review, confidentiality, UUID, Structured
Outputs, lock, integrity, and reporting helpers where appropriate. Avoid
duplicating policy logic.

Do not add supervised handoff, automatic prompt replacement, model-generated
contracts, Git automation, notifications, background services, network access,
API calls, or project-specific logic.

Tests must use fake Codex and deterministic Stage 2 fixtures. Do not install
packages or invoke a real model.

Run `ruff check .`, `mypy src`, and one full `pytest -q` because this stage wraps
shared Stage 2/3 execution. Report every contract-required item, exact test
counts, assumptions, deviations, and blockers.
