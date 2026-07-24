#!/usr/bin/env bash
set -euo pipefail

[[ -d .git ]] || {
  echo "Run this script from the repository root." >&2
  exit 2
}
[[ -f STAGE_4_CONTRACT.md ]] || {
  echo "STAGE_4_CONTRACT.md is missing." >&2
  exit 2
}
[[ -f CODEX_STAGE_4_PROMPT.md ]] || {
  echo "CODEX_STAGE_4_PROMPT.md is missing." >&2
  exit 2
}
[[ -x .venv/bin/python ]] || {
  echo "Virtual environment missing." >&2
  exit 2
}

for tag in stage0-complete stage1-complete stage2-complete stage3-complete; do
  git rev-parse -q --verify "$tag" >/dev/null || {
    echo "The $tag tag is missing." >&2
    exit 2
  }
done

if [[ -n "$(git status --porcelain)" ]]; then
  cat >&2 <<'EOF'
The repository is not clean. Commit the frozen Stage 4 contract and prompt
before launching the implementation worker.
EOF
  exit 2
fi

mkdir -p runs
RUN_ID="$(date -u +'%Y%m%dT%H%M%SZ')"
EVENTS="runs/${RUN_ID}-stage4-worker-events.jsonl"
STDERR_LOG="runs/${RUN_ID}-stage4-worker-stderr.log"
REPORT="runs/${RUN_ID}-stage4-worker-report.md"

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
  - < CODEX_STAGE_4_PROMPT.md \
  > "$EVENTS" \
  2> "$STDERR_LOG"
STATUS=$?
set -e

printf 'Codex exit status: %s\n' "$STATUS"
printf 'Events: %s\n' "$EVENTS"
printf 'Stderr: %s\n' "$STDERR_LOG"
printf 'Final report: %s\n' "$REPORT"

exit "$STATUS"
