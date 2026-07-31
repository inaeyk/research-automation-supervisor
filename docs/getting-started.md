# Getting started

## Install and diagnose

From a source checkout:

```bash
python3 -m venv ".venv"
".venv/bin/python" -m pip install --upgrade pip
".venv/bin/python" -m pip install -e ".[dev]"
".venv/bin/research-supervisor" --version
".venv/bin/research-supervisor" doctor
```

`doctor --json` is useful in automation. A nonzero result means Python, Git, Codex,
repository state, or normalized Codex login readiness needs attention. The synthetic
quick start does not require a real Codex process, but normal workflows do.

## Run the bundled synthetic workflow

Materialize the installed example into a new directory and create its clean baseline:

```bash
quickstart_root="$PWD/ras-synthetic-quickstart"
research-supervisor init-example --output "$quickstart_root"
git -C "$quickstart_root/project" init --initial-branch=main
git -C "$quickstart_root/project" config user.name "Synthetic Quick Start"
git -C "$quickstart_root/project" config user.email "synthetic@example.invalid"
git -C "$quickstart_root/project" add .
git -C "$quickstart_root/project" commit -m "synthetic baseline"
```

Validate, run, and inspect:

```bash
research-supervisor validate-substage "$quickstart_root/config/substage.yaml"
PATH="$quickstart_root/project/tools:$PATH" research-supervisor run-substage "$quickstart_root/config/substage.yaml" --runs-dir "$quickstart_root/runs"
run_root="$(find "$quickstart_root/runs" -mindepth 1 -maxdepth 1 -type d -name 'synthetic-quickstart-*' -print -quit)"
research-supervisor substage-status "$run_root" --json
```

The bundled `project/tools/codex` is a deterministic test double. It creates the one
allowed file as a synthetic Worker, lets the engine run the fixed acceptance test,
then responds as a fresh synthetic Auditor. It does not use credentials, network, or a
model service. A completed result proves the installed package can locate its resource
data and execute the normal state machine; it does not measure model quality.

## Prepare a real project

Use a dedicated clean worktree whenever practical:

```bash
git -C "project" worktree add "../project-supervised" -b "supervised/task" "main"
```

Inside the worktree, create protected control files for the contract and three role
prompts. Put the substage YAML beside or outside the project and resolve every locator
relative to that YAML. Freeze:

- a stable `substage_id` and title;
- the exact workspace;
- the contract, Worker initial/repair prompts, and Auditor prompt;
- Worker and Auditor model/reasoning/timeout settings;
- ordered fixed acceptance commands and unique IDs;
- allowed paths and protected paths;
- `max_repair_rounds` and checkpoint behavior.

Use `examples/workflows/minimal-substage.yaml` in a source checkout or the bundled
synthetic YAML as a shape reference. `validate-substage` performs no writes or process
launches.

## First real run

Confirm Codex CLI is installed and authenticated, then:

```bash
research-supervisor doctor
research-supervisor validate-substage "control/substage.yaml"
research-supervisor run-substage "control/substage.yaml" --runs-dir "runs/workflows"
```

Review `state.json`, `result.json`, `journal.jsonl`, fixed-test results, Git evidence,
and structured Auditor findings. Do not hand-edit run evidence.

If the run pauses, inspect status and its escalation package. Resume only interrupted
nonterminal work. A human pause requires a separately authored continuation file:

```bash
research-supervisor continue-substage "runs/workflows/RUN" --instruction "control/continuation.md"
```

## Next steps

- [Architecture](architecture.md)
- [Campaigns](campaigns.md)
- [Evaluation](evaluation.md)
- [Security](security.md)
- [Troubleshooting](troubleshooting.md)
