#!/usr/bin/env bash
# R7 gate: no module under src/ may call an LLM.
#
# Fires after an edit to any src/**/*.py. Matches on import lines and on LLM
# API endpoints, never on bare substrings -- src/ is full of legitimate R7
# docstrings, the "claude-sonnet-5" model string score_cli prints for the judge
# subagent, and the "B0-LLM" answer tier labels. A substring grep flags all of
# them.
set -uo pipefail
. "$(dirname "$0")/lib.sh"

read_hook_json
ROOT="$(repo_root)"
[ -n "$ROOT" ] || exit 0

FILE="$(relative_to_repo "$(hook_field tool_input file_path)" "$ROOT")"
case "$FILE" in
  src/*.py|src/**/*.py) ;;
  *) exit 0 ;;
esac

cd "$ROOT" || exit 0
[ -d src ] || exit 0

SDK_HITS="$(grep -rnE \
  '^[[:space:]]*(import|from)[[:space:]]+(anthropic|openai|litellm|langchain[_.a-z]*|cohere|mistralai|ollama|google\.(generativeai|genai)|replicate|together|groq)\b' \
  src --include='*.py' 2>/dev/null || true)"

URL_HITS="$(grep -rniE \
  '(api\.anthropic\.com|api\.openai\.com|generativelanguage\.googleapis|bedrock-runtime|/v1/(messages|chat/completions))' \
  src --include='*.py' 2>/dev/null || true)"

if [ -n "$SDK_HITS$URL_HITS" ]; then
  {
    echo "R7 VIOLATION -- src/ must never call an LLM."
    echo
    [ -n "$SDK_HITS" ] && { echo "LLM SDK imports:"; echo "$SDK_HITS"; echo; }
    [ -n "$URL_HITS" ] && { echo "LLM API endpoints:"; echo "$URL_HITS"; echo; }
    echo "src/ is deterministic plumbing. Move the judgment into the slash"
    echo "command session (.claude/commands/ or .claude/skills/) and have it"
    echo "call the deterministic helper via Bash."
  } >&2
  exit 2
fi
exit 0
