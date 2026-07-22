# Stage 0 Contract — Deterministic Foundation

Status: human-approved implementation contract  
Contract schema version: 1  
Stage ID: `AUTOMATION-0`

## Goal

Create the smallest maintainable Python foundation for a reusable research
automation supervisor. This stage covers only deterministic configuration,
validation, diagnostics, CLI behavior, and tests.

## Non-goals

Do not implement any of the following in Stage 0:

- launching Codex or any other model;
- worker/auditor orchestration;
- automatic prompt generation;
- Git worktrees or branches;
- email, toast, Slack, or other notifications;
- background services or scheduling;
- retry loops or state-machine execution;
- project-specific Gregory–Laflamme logic;
- network access;
- API-key handling.

## Required repository structure

The implementation must use a `src/` package layout and create at least:

```text
src/research_automation_supervisor/
    __init__.py
    cli.py
    contract.py
    doctor.py
    errors.py
tests/
    test_cli.py
    test_contract.py
    test_doctor.py
examples/contracts/minimal.yaml
docs/architecture.md
runs/.gitkeep
```

Additional small modules are allowed when they improve separation of concerns.

## Required CLI

The installed command is `research-supervisor`.

### `research-supervisor --version`

Print the package version and exit successfully.

### `research-supervisor doctor [--json]`

Run read-only environment checks and report:

- supported Python version (`>=3.11`);
- Git executable presence and version;
- whether the current directory is inside a Git repository;
- repository root when present;
- repository cleanliness;
- Codex executable presence and version;
- whether the installed Codex version is at least `0.144.0`;
- result of `codex login status`, without exposing credentials or secret material.

The command must degrade gracefully if Git or Codex is missing. Human-readable
output is the default. `--json` must emit stable JSON.

### `research-supervisor validate-contract PATH [--json]`

Load a YAML stage contract, validate it, print a useful result, and perform no
writes. Human-readable output is the default. `--json` must emit stable JSON.

## Stage contract model

Implement a typed model containing at least:

- `schema_version: int`, currently exactly `1`;
- `stage_id: str`;
- `title: str`;
- `goal: str`;
- `allowed_paths: list[str]`;
- `protected_paths: list[str]`;
- `acceptance_tests: list[AcceptanceTest]`;
- `max_repair_rounds: int`;
- `checkpoint_after: bool`.

`AcceptanceTest` contains:

- `id: str`;
- `command: str`;
- `timeout_seconds: int`.

Validation requirements:

- required strings remain non-empty after trimming;
- `stage_id` and acceptance-test IDs use a conservative identifier format;
- acceptance-test IDs are unique;
- command strings are non-empty;
- timeouts are positive and bounded;
- `max_repair_rounds` is between 0 and 10 inclusive;
- path patterns are non-empty and normalized for comparison;
- the same normalized pattern cannot appear in both `allowed_paths` and
  `protected_paths`;
- unknown fields are rejected;
- malformed YAML and validation failures produce useful errors.

## Exit-code contract

- `0`: success;
- `2`: invalid user input or invalid contract;
- `3`: required environment dependency missing or unsupported;
- unexpected internal failures may use `1`.

## Engineering constraints

- Python `>=3.11`;
- use the dependencies already declared in `pyproject.toml`;
- no network access;
- no shell invocation when an argument-vector subprocess call is possible;
- subprocess calls need explicit timeouts;
- credentials, tokens, and full authentication files must never be printed;
- type annotations are required;
- functions should be small and testable;
- diagnostics must support dependency injection or mocking;
- no global mutable workflow state;
- no edits to this contract or to `CODEX_STAGE_0_PROMPT.md`.

## Required tests

Tests must not require a real Codex login and must not depend on network access.
Mock subprocess and filesystem boundaries where appropriate.

Cover at least:

- valid minimal YAML;
- malformed YAML;
- unknown fields;
- duplicate test IDs;
- invalid repair limits and timeouts;
- allowed/protected path conflict;
- missing Git;
- missing Codex;
- old Codex version;
- failed `codex login status`;
- clean and dirty repository states;
- CLI human-readable output;
- CLI JSON output;
- exit codes for success and expected failures.

## Required quality gates

Before completion, all must pass:

```bash
ruff check .
mypy src
pytest -q
```

## Completion report

The worker's final response must state:

- files changed;
- architecture implemented;
- exact commands run and their results;
- assumptions;
- any unmet requirement or deviation.

A silent deviation is a failure.
