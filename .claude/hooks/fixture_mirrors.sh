#!/usr/bin/env bash
# The two synthetic verticals fixtures must stay byte-identical. The suite
# already enforces it (tests/test_verticals.py::test_mirrors_are_identical),
# but that fails minutes later and far from the edit; this fails immediately.
set -uo pipefail
. "$(dirname "$0")/lib.sh"

read_hook_json
ROOT="$(repo_root)"
[ -n "$ROOT" ] || exit 0

FILE="$(relative_to_repo "$(hook_field tool_input file_path)" "$ROOT")"
case "$FILE" in
  tests/fixtures/verticals.yaml|tests/discovery/fixtures/verticals.yaml) ;;
  *) exit 0 ;;
esac

cd "$ROOT" || exit 0
A=tests/fixtures/verticals.yaml
B=tests/discovery/fixtures/verticals.yaml
[ -f "$A" ] && [ -f "$B" ] || exit 0

if ! cmp -s "$A" "$B"; then
  {
    echo "FIXTURE MIRROR DRIFT -- these two files must be byte-identical:"
    echo "  $A"
    echo "  $B"
    echo
    echo "First differing line:"
    diff "$A" "$B" | head -20
    echo
    echo "Mirror the change into both files in this same edit."
  } >&2
  exit 2
fi
exit 0
