#!/usr/bin/env bash
# Stop gate: the suite must be green before the turn ends, but only when this
# session actually touched src/ or one of the two verticals fixtures. Every
# other kind of change (prose, command files, notes) skips the gate entirely.
#
# Fail-open on a toolchain that is not set up yet. A fresh clone with no `uv`,
# no venv, or no tests/ directory must not be blocked by a gate it cannot run.
set -uo pipefail
. "$(dirname "$0")/lib.sh"

read_hook_json

# Never re-fire on our own block, or the turn can never end.
[ "$(hook_field '' stop_hook_active)" = "True" ] && exit 0
[ "$(hook_field '' stop_hook_active)" = "true" ] && exit 0

ROOT="$(repo_root)"
[ -n "$ROOT" ] || exit 0
cd "$ROOT" || exit 0
[ -d tests ] || exit 0
command -v uv >/dev/null 2>&1 || exit 0

WATCHED="src tests/fixtures tests/discovery/fixtures"
# --porcelain, not `git diff`: a brand-new untracked module under src/ is
# exactly the case a diff would miss.
DIRTY="$(git status --porcelain -- $WATCHED 2>/dev/null || true)"
[ -n "$DIRTY" ] || exit 0

# tests/discovery needs libpostal, which is the opt-in `discovery` dep group.
# Without it those tests error out, so narrow the run rather than fail the gate.
IGNORE=""
if ! uv run python -c "import postal.parser" >/dev/null 2>&1; then
  IGNORE="--ignore=tests/discovery"
fi

RUNNER=(uv run pytest tests -q -x)
[ -n "$IGNORE" ] && RUNNER+=("$IGNORE")
if command -v timeout >/dev/null 2>&1; then
  RUNNER=(timeout 600 "${RUNNER[@]}")
fi

OUT="$("${RUNNER[@]}" 2>&1)"
STATUS=$?
if [ "$STATUS" -ne 0 ]; then
  {
    echo "TEST SUITE RED -- src/ or a verticals fixture changed and the suite fails."
    [ -n "$IGNORE" ] && echo "(ran with $IGNORE: libpostal is not installed here)"
    echo
    printf '%s\n' "$OUT" | tail -30
    echo
    echo "Fix the failure before finishing. Full run: uv run pytest tests -q"
  } >&2
  exit 2
fi
exit 0
