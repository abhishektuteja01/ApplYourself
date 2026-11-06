#!/usr/bin/env bash
# /apply gate: any Bash command that names `apply` and `--submit` asks the user
# first, however it is spelled (`uv run --with x apply run --submit`,
# `python -m src.apply_cli run --submit`). The settings ask rule matches only
# one spelling.
set -uo pipefail
. "$(dirname "$0")/lib.sh"

read_hook_json
CMD="$(hook_field tool_input command)"
[ -n "$CMD" ] || exit 0

printf '%s' "$CMD" | grep -Eq -- '(^|[^[:alnum:]_-])--submit([^[:alnum:]_-]|$)' || exit 0
printf '%s' "$CMD" | grep -Eq -- 'apply' || exit 0

cat <<'JSON'
{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "ask", "permissionDecisionReason": "This command can submit a job application (apply ... --submit). Approve only if you mean to send it."}}
JSON
exit 0
