#!/usr/bin/env bash
set -euo pipefail

repository_root="$(git rev-parse --show-toplevel 2>/dev/null || true)"
[[ -n "$repository_root" && "$repository_root" -ef . ]] || {
  echo "Run this script from the repository root." >&2
  exit 2
}
[[ -f STAGE_0_CONTRACT.md ]] || {
  echo "STAGE_0_CONTRACT.md is missing." >&2
  exit 2
}
[[ -f CODEX_STAGE_0_PROMPT.md ]] || {
  echo "CODEX_STAGE_0_PROMPT.md is missing." >&2
  exit 2
}
[[ -x .venv/bin/python ]] || {
  echo "Virtual environment missing. Run ./bootstrap.sh first." >&2
  exit 2
}

if [[ -n "$(git status --porcelain)" ]]; then
  cat >&2 <<'EOF'
The repository is not clean. Commit the bootstrap baseline before starting
Codex so the Stage 0 implementation diff is unambiguous.
EOF
  exit 2
fi

mkdir -p runs
RUN_ID="$(date -u +'%Y%m%dT%H%M%SZ')"
REPORT="runs/${RUN_ID}-stage0-worker-report.md"

PROMPT="$(cat CODEX_STAGE_0_PROMPT.md)"

codex exec \
  --model gpt-5.6-sol \
  -c model_reasoning_effort=xhigh \
  --sandbox workspace-write \
  -o "$REPORT" \
  "$PROMPT"

printf '\nWorker report: %s\n' "$REPORT"
printf 'Review the diff, then run:\n'
printf '  source .venv/bin/activate\n'
printf '  ruff check .\n'
printf '  mypy src\n'
printf '  pytest -q\n'
printf '  git status --short\n'
