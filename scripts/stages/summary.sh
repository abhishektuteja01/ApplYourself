#!/bin/bash
# Summary stage: rebuild the sheet's statuses (finished roles leave it), then
# write summary.md from tonight's records. Always runs, even after a failure.
set -uo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/lib_pipeline.sh"
stage_start summary

if [[ "$DRY_RUN" == 1 ]]; then
  OUT_ARGS=(--out "$RUN_DIR/summary.md")   # a dry run never overwrites the real one
  echo "[dry-run] uv run apply sheet (skipped)"
else
  OUT_ARGS=()
  "$UV" run apply sheet || echo "apply sheet failed: the summary reads the sheet as it is"
fi
"$UV" run summary --run-dir "$RUN_DIR" ${OUT_ARGS[@]+"${OUT_ARGS[@]}"}
CODE=$?
stage_end "$CODE" ""
exit "$CODE"
