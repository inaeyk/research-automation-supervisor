# Stage 4 setup

Stage 4 observes a live authoritative Stage 2 workflow while a quarantined,
persistent supervisor independently proposes prompts. Shadow output never
reaches the authoritative worker or auditor.

## Extract

```bash
cd ~/researchrepo/research-automation-supervisor

unzip -o \
  /mnt/c/Users/inaeyk/Downloads/research-automation-supervisor-stage4.zip \
  -d .

chmod +x run_stage4_codex.sh
```

## Review and freeze

```bash
less STAGE_4_CONTRACT.md
```

Press `q` to exit.

```bash
git add \
  STAGE_4_CONTRACT.md \
  CODEX_STAGE_4_PROMPT.md \
  README_STAGE_4.md \
  run_stage4_codex.sh

git diff --cached --check
git diff --cached --stat

git commit -m "Freeze Stage 4 live shadow observation contract"
git status --short
```

The repository must be clean.

## Launch implementation

```bash
./run_stage4_codex.sh
```

Monitor from another WSL terminal:

```bash
cd ~/researchrepo/research-automation-supervisor

EVENTS="$(ls -1t runs/*stage4-worker-events.jsonl | head -n1)"
STDERR="$(ls -1t runs/*stage4-worker-stderr.log | head -n1)"

stat -c '%y  %s bytes  %n' "$EVENTS" "$STDERR"
tail -n 8 "$EVENTS"
cat "$STDERR"
```

## Verify

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
  STAGE_3_CONTRACT.md CODEX_STAGE_3_PROMPT.md \
  STAGE_4_CONTRACT.md CODEX_STAGE_4_PROMPT.md \
  pyproject.toml

git status --short
git diff --stat

REPORT="$(ls -1t runs/*stage4-worker-report.md | head -n1)"
STDERR="$(ls -1t runs/*stage4-worker-stderr.log | head -n1)"
EVENTS="$(ls -1t runs/*stage4-worker-events.jsonl | head -n1)"

cat "$REPORT"
wc -c "$STDERR"
cat "$STDERR"
wc -l -c "$EVENTS"
tail -n 8 "$EVENTS"
```
