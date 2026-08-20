#!/usr/bin/env bash
set -euo pipefail

# Human-launched only. This deliberately makes three tiny real Codex turns on
# one durable task so the retained policy and cumulative snapshots can be
# inspected without involving the semantic/bootstrap replay.
smoke_workspace=${1:-"$PWD"}
smoke_root=$(mktemp -d)
smoke_task_id="resume-accounting-smoke-$(date -u +%Y%m%dT%H%M%SZ)-$$"
codex_task_bin=${CODEX_TASK_BIN:-"${CODEX_HOME:?CODEX_HOME must be set}/bin/codex-task"}

printf '%s\n' 'Reply exactly OK1. Do not use tools.' >"$smoke_root/turn-1.md"
printf '%s\n' 'Reply exactly OK2. Do not use tools.' >"$smoke_root/turn-2.md"
printf '%s\n' 'Reply exactly OK3. Do not use tools.' >"$smoke_root/turn-3.md"

"$codex_task_bin" run "$smoke_task_id" "$smoke_workspace" "$smoke_root/turn-1.md" \
  --model gpt-5.6-sol \
  -c model_reasoning_effort=high \
  --sandbox workspace-write \
  --ask-for-approval never \
  -c model_auto_compact_token_limit=64000 \
  -c tool_output_token_limit=2048 \
  --add-dir "$smoke_root"
"$codex_task_bin" resume "$smoke_task_id" "$smoke_root/turn-2.md"
"$codex_task_bin" resume "$smoke_task_id" "$smoke_root/turn-3.md"

printf 'task_id: %s\nledger: %s/task-ledgers/%s/TaskUsageReceipt.json\nstate: %s/task-ledgers/%s/task.json\n' \
  "$smoke_task_id" "$CODEX_HOME" "$smoke_task_id" "$CODEX_HOME" "$smoke_task_id"
