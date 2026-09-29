#!/bin/bash
# Tailor stage: add newly scored roles to the sheet, tailor every New row
# STAGE_LIMIT=N (env) caps the roles a run takes, for a first live test.
# (tailor_parallel at a time), rebuild the sheet.
set -uo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/lib_pipeline.sh"
stage_start tailor

PAR="$(cfg tailor_parallel)" || { stage_end 2 "config"; exit 2; }
STAGGER="$(cfg tailor_stagger_seconds)" || { stage_end 2 "config"; exit 2; }
if [[ "$DRY_RUN" == 1 ]]; then
  echo "[dry-run] uv run apply sheet --add"
else
  "$UV" run apply sheet --add || { stage_end 1 "apply sheet --add failed"; exit 1; }
fi
IDS="${ONLY_IDS:-$("$UV" run apply sheet --list New | head -n "${STAGE_LIMIT:-100000}")}"
N=$(grep -c . <<< "$IDS")
echo "New roles: $N (one every ${STAGGER}s, at most $PAR at once)"
if [[ $N -gt 0 ]]; then
  : > "$RUN_DIR/tailor_results.txt"
  PIDS=()
  alive() {  # how many started roles are still running
    local n=0 p
    for p in ${PIDS[@]+"${PIDS[@]}"}; do kill -0 "$p" 2>/dev/null && n=$((n + 1)); done
    echo "$n"
  }
  for id in $IDS; do
    while [[ "$(alive)" -ge "$PAR" ]]; do sleep 2; done
    "${STAGES_DIR:-$REPO/scripts/stages}/tailor_one.sh" "$id" &
    PIDS+=($!)
    [[ "$DRY_RUN" == 1 ]] || sleep "$STAGGER"
  done
  wait
fi
[[ "$DRY_RUN" == 1 ]] || "$UV" run apply sheet
OK=$(grep -c ' ok$' "$RUN_DIR/tailor_results.txt" 2>/dev/null || true); OK=${OK:-0}
DETAIL="$OK/$N tailored"
# One role failing is logged; only "none worked out of some" stops the chain.
if [[ "$DRY_RUN" != 1 && $N -gt 0 && $OK -eq 0 ]]; then stage_end 1 "$DETAIL"; exit 1; fi
stage_end 0 "$DETAIL"
