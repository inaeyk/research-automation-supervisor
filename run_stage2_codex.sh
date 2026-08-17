#!/usr/bin/env bash
set -euo pipefail

repository_root="$(git rev-parse --show-toplevel 2>/dev/null || true)"
[[ -n "$repository_root" && "$repository_root" -ef . ]] || {
  echo "Run this script from the repository root." >&2
  exit 2
}
[[ -f STAGE_2_CONTRACT.md ]] || {
  echo "STAGE_2_CONTRACT.md is missing." >&2
  exit 2
}
[[ -f CODEX_STAGE_2_PROMPT.md ]] || {
  echo "CODEX_STAGE_2_PROMPT.md is missing." >&2
  exit 2
}
[[ -x .venv/bin/python ]] || {
  echo "Virtual environment missing." >&2
  exit 2
}

git rev-parse -q --verify stage0-complete >/dev/null || {
  echo "The stage0-complete tag is missing." >&2
  exit 2
}
git rev-parse -q --verify stage1-complete >/dev/null || {
  echo "The stage1-complete tag is missing." >&2
  exit 2
}

if [[ -n "$(git status --porcelain)" ]]; then
  cat >&2 <<'MSG'
The repository is not clean. Commit the frozen Stage 2 contract and prompt
before launching the implementation worker.
MSG
  exit 2
fi

mkdir -p runs
RUN_ID="$(date -u +'%Y%m%dT%H%M%SZ')"
EVENTS="runs/${RUN_ID}-stage2-worker-events.jsonl"
STDERR_LOG="runs/${RUN_ID}-stage2-worker-stderr.log"
REPORT="runs/${RUN_ID}-stage2-worker-report.md"

set +e
codex \
  --ask-for-approval never \
  exec \
  --json \
  --output-last-message "$REPORT" \
  --model gpt-5.6-sol \
  -c model_reasoning_effort=xhigh \
  -c 'web_search="disabled"' \
  -c sandbox_workspace_write.network_access=false \
  -c features.skill_mcp_dependency_install=false \
  --sandbox workspace-write \
  --ignore-user-config \
  --ignore-rules \
  --strict-config \
  - < CODEX_STAGE_2_PROMPT.md \
  > "$EVENTS" \
  2> "$STDERR_LOG"
STATUS=$?
set -e

printf 'Codex exit status: %s\n' "$STATUS"
printf 'Events: %s\n' "$EVENTS"
printf 'Stderr: %s\n' "$STDERR_LOG"
printf 'Final report: %s\n' "$REPORT"

if [[ "$STATUS" -ne 0 ]]; then
  exit "$STATUS"
fi

printf '\nRun independent gates after reviewing the worker report:\n'
printf '  source .venv/bin/activate\n'
printf '  ruff check .\n'
printf '  mypy src\n'
printf '  pytest -q\n'
printf '  git diff --check\n'
printf '  git status --short\n'
printf '  git diff --stat\n'
