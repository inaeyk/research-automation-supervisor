# Stage 2 setup

Extract these files into the root of the existing
`research-automation-supervisor` repository after Stage 1 is tagged and pushed.

```bash
cd ~/researchrepo/research-automation-supervisor

unzip -o \
  /mnt/c/Users/<WINDOWS_USERNAME>/Downloads/research-automation-supervisor-stage2.zip \
  -d .

chmod +x run_stage2_codex.sh
```

Review and freeze the contract:

```bash
less STAGE_2_CONTRACT.md

git add \
  STAGE_2_CONTRACT.md \
  CODEX_STAGE_2_PROMPT.md \
  README_STAGE_2.md \
  run_stage2_codex.sh

git diff --cached --check
git diff --cached --stat
git commit -m "Freeze Stage 2 deterministic workflow contract"
git status --short
```

The repository must be clean before launching the worker:

```bash
./run_stage2_codex.sh
```

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
  CODEX_STAGE_1_PROMPT.md \
  STAGE_2_CONTRACT.md \
  CODEX_STAGE_2_PROMPT.md

git status --short
git diff --stat

STAGE2_REPORT="$(ls -1t runs/*stage2-worker-report.md | head -n1)"
STAGE2_STDERR="$(ls -1t runs/*stage2-worker-stderr.log | head -n1)"

cat "$STAGE2_REPORT"
wc -c "$STAGE2_STDERR"
cat "$STAGE2_STDERR"
```
