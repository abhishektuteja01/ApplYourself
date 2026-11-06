#!/bin/bash
# Discover stage: `uv run discover` into its own logs/discovery_*.log, wrapped so
# the run folder records its code and time. Runs ONLY the deterministic, LLM-free
# discovery step (R7).
set -uo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/lib_pipeline.sh"
stage_start discover
if [[ "$DRY_RUN" == 1 ]]; then
  echo "[dry-run] uv run discover >> logs/discovery_<stamp>.log"
  stage_end 0 "dry-run"; exit 0
fi

LOGDIR="$REPO/logs"
mkdir -p "$LOGDIR"
STAMP="$(now)"
DLOG="$LOGDIR/discovery_$STAMP.log"

# Keep log_keep_days (profile/discovery.yaml) of logs. A config that fails to
# load skips the prune; preflight reports why.
KEEP="$("$UV" run config get discovery log_keep_days 2>/dev/null)"
if [[ "$KEEP" =~ ^[0-9]+$ ]]; then
  find "$LOGDIR" -maxdepth 1 -name 'discovery_*.log' -mtime +"$KEEP" -delete
fi

echo "=== discovery start $STAMP (repo=$REPO uv=$UV) ===" | tee -a "$DLOG"
"$UV" run discover >>"$DLOG" 2>&1
CODE=$?
echo "=== discovery end code=$CODE $(now) ===" | tee -a "$DLOG"

stage_end "$CODE" ""
exit "$CODE"
