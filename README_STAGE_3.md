# Stage 3 setup

Stage 3 performs retrospective blind supervisor calibration. It never sends a
supervisor proposal to a worker or auditor.

```bash
cd ~/researchrepo/research-automation-supervisor

unzip -o /mnt/c/Users/<WINDOWS_USERNAME>/Downloads/research-automation-supervisor-stage3.zip -d .
chmod +x run_stage3_codex.sh

less STAGE_3_CONTRACT.md
```

Freeze:

```bash
git add STAGE_3_CONTRACT.md CODEX_STAGE_3_PROMPT.md README_STAGE_3.md run_stage3_codex.sh
git diff --cached --check
git diff --cached --stat
git commit -m "Freeze Stage 3 blind supervisor calibration contract"
git status --short
```

Launch:

```bash
./run_stage3_codex.sh
```

Monitor in another WSL window:

```bash
cd ~/researchrepo/research-automation-supervisor
EVENTS="$(ls -1t runs/*stage3-worker-events.jsonl | head -n1)"
STDERR="$(ls -1t runs/*stage3-worker-stderr.log | head -n1)"
stat -c '%y  %s bytes  %n' "$EVENTS" "$STDERR"
tail -n 8 "$EVENTS"
cat "$STDERR"
```

Verify after completion:

```bash
source .venv/bin/activate
ruff check .
mypy src
pytest -q
git diff --check
git diff --exit-code -- \
  STAGE_0_CONTRACT.md CODEX_STAGE_0_PROMPT.md \
  STAGE_1_CONTRACT.md CODEX_STAGE_1_PROMPT.md \
  STAGE_2_CONTRACT.md CODEX_STAGE_2_PROMPT.md \
  STAGE_3_CONTRACT.md CODEX_STAGE_3_PROMPT.md
git status --short
git diff --stat

REPORT="$(ls -1t runs/*stage3-worker-report.md | head -n1)"
STDERR="$(ls -1t runs/*stage3-worker-stderr.log | head -n1)"
EVENTS="$(ls -1t runs/*stage3-worker-events.jsonl | head -n1)"
cat "$REPORT"
wc -c "$STDERR"
cat "$STDERR"
wc -l -c "$EVENTS"
tail -n 8 "$EVENTS"
```
