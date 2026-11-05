#!/bin/bash
# Tailor stage: add newly scored roles to the sheet, tailor every New row
# STAGE_LIMIT=N (env) caps the roles a run takes, for a first live test.
# (tailor_parallel at a time), rebuild the sheet.
set -uo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/lib_pipeline.sh"
stage_start tailor
pdf_helper_up

PAR="$(cfg tailor_parallel)" || { stage_end 2 "config"; exit 2; }
STAGGER="$(cfg tailor_stagger_seconds)" || { stage_end 2 "config"; exit 2; }
if [[ "$DRY_RUN" == 1 ]]; then
  echo "[dry-run] uv run apply sheet --add"
else
  "$UV" run apply sheet --add || { stage_end 1 "apply sheet --add failed"; exit 1; }
fi
IDS="${ONLY_IDS:-$("$UV" run apply sheet --list New | head -n "${STAGE_LIMIT:-100000}")}"
N=$(grep -c . <<< "$IDS")
CAP="$(night_cap tailor_max_per_night)"
echo "New roles: $N (one every ${STAGGER}s, at most $PAR at once, cap ${CAP/#0/none})"
if [[ $N -gt 0 ]]; then
  : > "$RUN_DIR/tailor_results.txt"
  PIDS=(); STARTED=0
  alive() {  # how many started roles are still running
    local n=0 p
    for p in ${PIDS[@]+"${PIDS[@]}"}; do kill -0 "$p" 2>/dev/null && n=$((n + 1)); done
    echo "$n"
  }
  for id in $IDS; do
    while [[ "$(alive)" -ge "$PAR" ]]; do sleep 2; done
    # Over the cap or past the usage limit: left New for the next night.
    if quota_hit || [[ $CAP -gt 0 && $STARTED -ge $CAP ]]; then
      echo "$id deferred" >> "$RUN_DIR/tailor_results.txt"; continue
    fi
    "${STAGES_DIR:-$REPO/scripts/stages}/tailor_one.sh" "$id" &
    PIDS+=($!); STARTED=$((STARTED + 1))
    [[ "$DRY_RUN" == 1 ]] || sleep "$STAGGER"
  done
  for p in ${PIDS[@]+"${PIDS[@]}"}; do wait "$p"; done   # not the pdf helper
fi
[[ "$DRY_RUN" == 1 ]] || "$UV" run apply sheet
OK=$(grep -c ' ok$' "$RUN_DIR/tailor_results.txt" 2>/dev/null || true); OK=${OK:-0}
DEF=$(grep -c ' deferred$' "$RUN_DIR/tailor_results.txt" 2>/dev/null || true); DEF=${DEF:-0}
DETAIL="$OK/$N tailored"; [[ $DEF -gt 0 ]] && DETAIL="$DETAIL, $DEF deferred"
# One role failing is logged; only "none worked out of some" fails the stage.
if [[ "$DRY_RUN" != 1 && $N -gt 0 && $OK -eq 0 ]]; then stage_end 1 "$DETAIL" fail "$DEF"; exit 1; fi
STATUS=ok; [[ "$DRY_RUN" != 1 && $OK -lt $N ]] && STATUS=partial
stage_end 0 "$DETAIL" "$STATUS" "$DEF"
