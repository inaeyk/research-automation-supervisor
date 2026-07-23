Implement Stage 2 of the Research Automation Supervisor.

Read `STAGE_2_CONTRACT.md`, the completed Stage 0/1 implementation and tests,
`pyproject.toml`, and current documentation. Implement the frozen contract
exactly. Do not edit any Stage 0, Stage 1, or Stage 2 contract/prompt file.

Keep the boundary strict: Stage 2 is a deterministic single-substage workflow
using exact human-written prompts, one persistent worker thread resumed only by
its explicit thread ID, fresh ephemeral auditors, fixed argv-based tests,
Git/scope evidence, bounded repair rounds, durable state, and human pauses.

Do not add an intelligent supervisor, model-generated prompts, contract changes,
multi-substage advancement, Git commits/worktrees, notifications, background
services, API calls, or project-specific logic.

Use the existing environment and dependencies. Do not install packages or
invoke a real model during tests. Extend the fake Codex and use fake test
executables for deterministic coverage.

Run `ruff check .`, `mypy src`, and `pytest -q` before finishing. Report changed
files, architecture, state transitions, exact-ID session behavior, auditor
freshness, prompt assembly, tests/Git evidence, crash recovery, quality gates,
assumptions, deviations, and blockers.
