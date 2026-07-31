# Development

## Environment

```bash
python3 -m venv ".venv"
".venv/bin/python" -m pip install --upgrade pip
".venv/bin/python" -m pip install -e ".[dev]"
```

Python 3.11 is the minimum. The release is qualified on Linux. Tests use synthetic
fixtures and process doubles; ordinary development must not open or execute real
protected historical material.

## Checks

Focused tests while changing a component:

```bash
".venv/bin/pytest" -q tests/test_direct_historical_replay.py tests/test_example_bundle.py tests/test_cli.py tests/test_workflow_cli.py
```

Release validation:

```bash
".venv/bin/ruff" check .
".venv/bin/mypy" src/research_automation_supervisor
".venv/bin/pytest" -q
".venv/bin/python" -m build
```

Ruff enforces import, correctness, modernization, bugbear, and simplification rules at
100 columns. mypy is strict for the installable package.

## Package smoke test

Create a temporary virtual environment outside the repository, install only the wheel,
and verify imports, every installed console entry point's `--help`, `doctor`, and the
bundled synthetic workflow. Do not rely on source-checkout `examples/`, `tests/`, or
the working directory after wheel installation.

Inspect wheel and source-distribution contents before release. Neither artifact may
contain local runs, candidates, prepared campaigns, gold, protected fixtures, reports,
worktrees, virtual environments, caches, credentials, or machine-specific
configuration. Generate SHA-256 for both artifacts.

## Direct replay development rule

Use only synthetic prepared campaigns/candidates. A regression must cover read-only
candidate payload mode `0400`, disposable owner-write adjustment, structured contract
classification, default cleanup, optional workspace retention, and input immutability.
Do not make the experimental packaged evaluator a dependency of the supported direct
command.

## Change discipline

Preserve user work in a dirty tree, use exact shell-free subprocess arguments, keep
all new state durable and canonical, and add focused regressions. Do not rewrite Git
history, publish packages, create release tags, or expose protected material as part of
routine development.
