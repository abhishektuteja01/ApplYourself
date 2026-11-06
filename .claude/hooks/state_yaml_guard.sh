#!/usr/bin/env bash
# R10 gate: /track is the sole writer of state transitions, and it writes
# through src/state_io.py via Bash -- never through the Edit/Write tools. So an
# Edit or Write aimed at a state.yaml or at pipeline/events.jsonl is a rule
# break by construction.
set -uo pipefail
. "$(dirname "$0")/lib.sh"

read_hook_json
ROOT="$(repo_root)"
FILE="$(relative_to_repo "$(hook_field tool_input file_path)" "${ROOT:-/}")"

case "$FILE" in
  pipeline/*/state.yaml|*/pipeline/*/state.yaml|pipeline/events.jsonl|*/pipeline/events.jsonl)
    {
      echo "R10 VIOLATION -- do not edit $FILE directly."
      echo
      echo "/track is the sole writer of state transitions:"
      echo "  uv run track <job_id> <state> [--note \"...\"]"
      echo
      echo "Side lists (tailored_dirs[], outreach[]) are"
      echo "appended through src/state_io.py helpers, also via Bash."
      echo "pipeline/events.jsonl is append-only, written by state_io alone."
    } >&2
    exit 2
    ;;
esac
exit 0
