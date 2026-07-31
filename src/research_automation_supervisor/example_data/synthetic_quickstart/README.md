# Synthetic quick start

This project exercises the complete single-substage state machine without contacting
a model service. `project/tools/codex` is a deterministic test double: the Worker
creates `src/ready.txt`, the acceptance test verifies it, and a separate synthetic
Auditor returns a passing structured review.

From this directory:

```bash
example_root="$PWD"
git -C "$example_root/project" init --initial-branch=main
git -C "$example_root/project" config user.name "Synthetic Quick Start"
git -C "$example_root/project" config user.email "synthetic@example.invalid"
git -C "$example_root/project" add .
git -C "$example_root/project" commit -m "synthetic baseline"
research-supervisor validate-substage "$example_root/config/substage.yaml"
PATH="$example_root/project/tools:$PATH" research-supervisor run-substage "$example_root/config/substage.yaml" --runs-dir "$example_root/runs"
```

The `PATH` prefix is deliberate and applies only to this synthetic invocation. Omit it
for real work so the Supervisor resolves your installed and authenticated Codex CLI.
