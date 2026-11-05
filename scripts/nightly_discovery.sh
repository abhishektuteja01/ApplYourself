#!/bin/bash
# Discovery runner: the discover stage of scripts/nightly_pipeline.sh
# (scripts/stages/discover.sh), and runnable by hand. Writes its own
# logs/discovery_*.log. Runs ONLY the deterministic, LLM-free discovery step (R7).
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

# Keep log_keep_days (profile/discovery.yaml) of logs. A config that fails to
# load skips the prune; preflight reports why.
KEEP="$("$UV" run config get discovery log_keep_days 2>/dev/null)"
if [[ "$KEEP" =~ ^[0-9]+$ ]]; then
  find "$LOGDIR" -maxdepth 1 -name 'discovery_*.log' -mtime +"$KEEP" -delete
fi

echo "=== discovery start $STAMP (repo=$REPO uv=$UV) ===" | tee -a "$LOG"
"$UV" run discover >>"$LOG" 2>&1
CODE=$?
echo "=== discovery end code=$CODE $(date +%Y-%m-%d_%H%M%S) ===" | tee -a "$LOG"

exit "$CODE"
