#!/bin/bash
# Nightly discovery runner. Portable: the macOS-only bits are guarded.
#
# macOS: install with scripts/launchagent.example.plist; see /onboarding's
#        "Nightly discovery (macOS only)" section.
# Linux: schedule it with cron or a systemd timer, pointing at this same script.
#
# Runs ONLY the deterministic, LLM-free discovery step (R7). Scoring is a slash
# command and needs a Claude Code session, so it stays a morning decision.
set -uo pipefail

# Derive the repo from this script's own location, so nothing here needs editing.
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# launchd (and cron) give a process a minimal PATH that excludes every common uv
# install location, so `uv` is not on it. Cover uv's standalone installer plus
# Homebrew on Apple Silicon and Intel, then resolve once and fail loudly if it
# is absent. The Homebrew entries are inert on a Linux box that has no such dirs.
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
UV="${UV:-$(command -v uv || true)}"
if [[ -z "$UV" ]]; then
  echo "FATAL: uv not found on PATH ($PATH)" >&2
  exit 127
fi

LOGDIR="$REPO/logs"
mkdir -p "$LOGDIR"
STAMP="$(date +%Y-%m-%d_%H%M%S)"
LOG="$LOGDIR/discovery_$STAMP.log"

cd "$REPO" || { echo "FATAL: cannot cd to $REPO" >&2; exit 1; }

echo "=== discovery start $STAMP (repo=$REPO uv=$UV) ===" | tee -a "$LOG"
# caffeinate -s: hold off system sleep on AC power. -i: hold off idle sleep.
# -t 32400 (9h): covers discovery plus the score/tailor/apply-prep Desktop
# routines chained after it (through ~4:30 AM), not just the discover call —
# those routines run hours after this script exits, and idle sleep would
# otherwise resume the moment discovery finishes. Expires on its own; nothing
# else needs to kill it. macOS-only — elsewhere it does not exist, and an
# unguarded call would exit 127 without ever running discovery, so fall
# through to a plain run.
if command -v caffeinate >/dev/null 2>&1; then
  caffeinate -s -i -t 32400 &
fi
"$UV" run discover >>"$LOG" 2>&1
CODE=$?
echo "=== discovery end code=$CODE $(date +%Y-%m-%d_%H%M%S) ===" | tee -a "$LOG"

# Open Claude Desktop the moment discovery is actually done, rather than
# guessing a clock time it should be done by — the score routine's own
# completion check still gates /score regardless of when the app opens.
if [[ "$CODE" -eq 0 ]] && command -v open >/dev/null 2>&1; then
  open -a "Claude"
fi

exit "$CODE"
