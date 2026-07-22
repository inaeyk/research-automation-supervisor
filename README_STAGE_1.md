# Stage 1 setup

Extract these files into the root of the existing
`research-automation-supervisor` repository.

Then:

```bash
cd ~/researchrepo/research-automation-supervisor
chmod +x run_stage1_codex.sh

git add \
  STAGE_1_CONTRACT.md \
  CODEX_STAGE_1_PROMPT.md \
  README_STAGE_1.md \
  run_stage1_codex.sh

git commit -m "Freeze Stage 1 Codex adapter contract"
git status --short
```

The status must be clean before launching the worker:

```bash
./run_stage1_codex.sh
```

The runner uses a fixed prompt through standard input, JSONL output,
`--ask-for-approval never`, a workspace-write sandbox, disabled web/network
configuration, and no user rules/config. It saves the worker event stream,
stderr, and final report under `runs/`.

After completion:

```bash
source .venv/bin/activate
ruff check .
mypy src
pytest -q
git diff --check
git diff --exit-code -- \
  STAGE_0_CONTRACT.md \
  CODEX_STAGE_0_PROMPT.md \
  STAGE_1_CONTRACT.md \
  CODEX_STAGE_1_PROMPT.md
git status --short
git diff --stat
```
