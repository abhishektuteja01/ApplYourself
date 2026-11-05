#!/usr/bin/env bash
# R7 scan: no module under src/ may call an LLM. Exits 1 on a hit.
#
# Matches on import lines and on LLM API endpoints, never on bare substrings --
# src/ is full of legitimate R7 docstrings, the `score-judge:<model>` stamp
# score_store reads from the judge agent's frontmatter. A substring grep flags
# all of them. Shared by the Claude Code R7 hook, pre-commit and CI.
set -uo pipefail

ROOT="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
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
    echo "command session (.claude/commands/) and have it"
    echo "call the deterministic helper via Bash."
  } >&2
  exit 1
fi
exit 0
