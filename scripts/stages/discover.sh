#!/bin/bash
# Discover stage: the existing nightly_discovery.sh (which keeps its own
# logs/discovery_*.log), wrapped so the run folder records its code and time.
set -uo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/lib_pipeline.sh"
stage_start discover
if [[ "$DRY_RUN" == 1 ]]; then
  echo "[dry-run] scripts/nightly_discovery.sh"
  stage_end 0 "dry-run"; exit 0
fi
"$REPO/scripts/nightly_discovery.sh"
CODE=$?
stage_end "$CODE" ""
exit "$CODE"
