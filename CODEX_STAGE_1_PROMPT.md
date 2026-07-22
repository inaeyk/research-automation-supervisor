Implement Stage 1 of the Research Automation Supervisor.

Read `STAGE_1_CONTRACT.md`, the current Stage 0 implementation, `pyproject.toml`,
and existing tests. Implement the frozen contract exactly. Do not edit any
Stage 0 or Stage 1 contract/prompt file.

Keep the scope narrow: one exact human-written prompt file enters a deterministic
`codex exec` adapter and produces redacted JSONL, a final response, metadata,
and a normalized result. Do not add prompt generation, handoffs, retries,
worktrees, notifications, background services, or project-specific logic.

Use the existing dependencies and virtual environment. Do not install packages,
access the network, invoke a real model during tests, or weaken existing tests.
Use a fake Codex executable and injected boundaries for the test suite.

Run `ruff check .`, `mypy src`, and `pytest -q` before finishing. Report changed
files, design, exact gates and test count, assumptions, deviations, and blockers.
