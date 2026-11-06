#!/usr/bin/env bash
# R7 gate: no module under src/ may call an LLM.
#
# Fires after an edit to any src/**/*.py and runs scripts/r7_scan.sh, the same
# scan pre-commit and CI run.
set -uo pipefail
. "$(dirname "$0")/lib.sh"

read_hook_json
ROOT="$(repo_root)"
[ -n "$ROOT" ] || exit 0

FILE="$(relative_to_repo "$(hook_field tool_input file_path)" "$ROOT")"
case "$FILE" in
  src/*.py) ;;  # case's * also matches /, so this covers every depth
  *) exit 0 ;;
esac

[ -x "$ROOT/scripts/r7_scan.sh" ] || exit 0
# Exit 2 feeds the scan's stderr back to the session.
"$ROOT/scripts/r7_scan.sh" "$ROOT" || exit 2
exit 0
