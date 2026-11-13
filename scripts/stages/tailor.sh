#!/bin/bash
# Tailor stage: rewrite the sheet from the work queue, tailor every New role
# `night tailor-pick` passes (tailor_parallel at a time), rebuild the sheet.
# The roles it leaves New are `<id> thin|dupe|gone` lines; the summary lists them.
# STAGE_LIMIT=N (env) caps the roles a run takes, for a first live test.
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
RESULTS="$RUN_DIR/tailor_results.txt"
if [[ "${NIGHT_RERUN:-0}" == 1 && -f "$RESULTS" ]]; then
  # A rerun after a usage-limit wait: the earlier pass's ok and failed roles are done.
  grep -E ' (ok|fail)$' "$RESULTS" > "$RESULTS.tmp"; mv "$RESULTS.tmp" "$RESULTS"
else
  : > "$RESULTS"   # a resumed night starts its roles afresh
  rm -f "$RUN_DIR/tailor_skipped.json"
fi
PRIOR=$(grep -c . "$RESULTS" || true); PRIOR=${PRIOR:-0}
LEFT=$(( ${STAGE_LIMIT:-100000} - PRIOR )); [[ $LEFT -gt 0 ]] || LEFT=0
if [[ -n "${ONLY_IDS:-}" ]]; then
  IDS="$ONLY_IDS"
else
  NEW="$("$UV" run apply sheet --list New | grep -vxFf <(cut -d' ' -f1 "$RESULTS"))"
  # Thin JDs, duplicates of a role in play and gone postings are left New (night tailor-pick).
  PICK_ARGS=(--run-dir "$RUN_DIR" --limit "$LEFT"); [[ "$DRY_RUN" == 1 ]] && PICK_ARGS+=(--dry-run)
  IDS="$("$UV" run night tailor-pick "${PICK_ARGS[@]}" <<< "$NEW")" \
    || { echo "night tailor-pick failed: taking every New role"; IDS="$(head -n "$LEFT" <<< "$NEW")"; }
fi
SKIPPED=$(grep -cE ' (thin|dupe|gone)$' "$RESULTS" || true); SKIPPED=${SKIPPED:-0}
N=$(grep -c . <<< "$IDS")
CAP="$(night_cap tailor_max_per_night)"
echo "New roles: $N, $PRIOR done in an earlier pass (one every ${STAGGER}s, at most $PAR at once, cap ${CAP/#0/none})"
if [[ $N -gt 0 ]]; then
  PIDS=(); STARTED=0
  alive() {  # how many started roles are still running
    local n=0 p
    for p in ${PIDS[@]+"${PIDS[@]}"}; do kill -0 "$p" 2>/dev/null && n=$((n + 1)); done
    echo "$n"
  }
  for id in $IDS; do
    while [[ "$(alive)" -ge "$PAR" ]]; do sleep 2; done
    # Over the cap or past the usage limit: left New for the rerun after the reset, or the next night.
    if quota_hit || [[ $CAP -gt 0 && $((PRIOR + STARTED)) -ge $CAP ]]; then
      echo "$id deferred" >> "$RUN_DIR/tailor_results.txt"; continue
    fi
    "${STAGES_DIR:-$REPO/scripts/stages}/tailor_one.sh" "$id" &
    PIDS+=($!); STARTED=$((STARTED + 1))
    [[ "$DRY_RUN" == 1 ]] || sleep "$STAGGER"
  done
  for p in ${PIDS[@]+"${PIDS[@]}"}; do wait "$p"; done   # not the pdf helper
fi
[[ "$DRY_RUN" == 1 ]] || "$UV" run apply sheet
N=$((N + PRIOR))
OK=$(grep -c ' ok$' "$RUN_DIR/tailor_results.txt" 2>/dev/null || true); OK=${OK:-0}
DEF=$(grep -c ' deferred$' "$RUN_DIR/tailor_results.txt" 2>/dev/null || true); DEF=${DEF:-0}
DETAIL="$OK/$N tailored"; [[ $DEF -gt 0 ]] && DETAIL="$DETAIL, $DEF deferred"
[[ $SKIPPED -gt 0 ]] && DETAIL="$DETAIL, $SKIPPED skipped"
# One role failing is logged; only "none worked out of some" fails the stage.
if [[ "$DRY_RUN" != 1 && $N -gt 0 && $OK -eq 0 ]]; then stage_end 1 "$DETAIL" fail "$DEF"; exit 1; fi
STATUS=ok; [[ "$DRY_RUN" != 1 && $OK -lt $N ]] && STATUS=partial
stage_end 0 "$DETAIL" "$STATUS" "$DEF"
