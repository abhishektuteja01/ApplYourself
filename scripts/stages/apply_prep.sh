#!/bin/bash
# Apply-prep stage: `apply triage --fill` fills, with no model, every Resume ready
# role whose answers are all known (never submits); /apply then handles each role that
# still has something open, one at a time. Then the sheet is rebuilt. A role with
# answers to confirm shows "Needs your OK" on the sheet.
# STAGE_LIMIT=N (env) caps the roles a run takes, for a first live test.
set -uo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/lib_pipeline.sh"
stage_start apply_prep
STAGE=apply_prep   # log/json names
pdf_helper_up

TOOLS='Bash(uv run *),Bash(cd *),Bash(git rev-parse *),Read,Write,Edit,Skill,WebSearch,WebFetch'
if [[ "$DRY_RUN" == 1 ]]; then echo "[dry-run] uv run apply sheet"; else
  "$UV" run apply sheet || { stage_end 1 "apply sheet failed"; exit 1; }
fi
# Roles with every answer known are filled here, no model; the rest stay Resume ready.
if [[ "$DRY_RUN" == 1 ]]; then
  echo "[dry-run] uv run apply triage --fill"
else
  LIMIT_ARGS=(); [[ -n "${STAGE_LIMIT:-}" ]] && LIMIT_ARGS=(--limit "$STAGE_LIMIT")
  "$UV" run apply triage --fill ${LIMIT_ARGS[@]+"${LIMIT_ARGS[@]}"} || echo "apply triage failed: every role goes to /apply"
  "$UV" run apply sheet >/dev/null
fi
IDS="$("$UV" run apply sheet --list 'Resume ready' | head -n "${STAGE_LIMIT:-100000}")"
N=$(grep -c . <<< "$IDS"); FAILED=0; DEF=0; STARTED=0
CAP="$(night_cap apply_prep_max_per_night)"
echo "Resume ready: $N (cap ${CAP/#0/none})"

# What is still unresolved after the session, for summary.md: the plan as JSON,
# with the drafted answers applied when there are any. No browser.
snapshot() {
  [[ "$DRY_RUN" == 1 ]] && { echo "[dry-run] snapshot plan_after.json for $1"; return 0; }
  local dir ans=()
  dir="$("$UV" run app dir "$1" | "$UV" run python -c 'import json,sys; print(json.load(sys.stdin)["dir"])')" || return 0
  [[ -n "$dir" && -f "$dir/answers_override.json" ]] && ans=(--answers "$dir/answers_override.json")
  "$UV" run apply plan "$1" --json ${ans[@]+"${ans[@]}"} > "$dir/plan_after.json" 2> "$dir/plan_after.err" || true
}

for id in $IDS; do
  # Over the cap or past the usage limit: stays Resume ready for the next night.
  if quota_hit || [[ $CAP -gt 0 && $STARTED -ge $CAP ]]; then DEF=$((DEF + 1)); continue; fi
  STARTED=$((STARTED + 1))
  run_claude "$id" "$TOOLS" "/apply $id"; RC=$?
  if [[ $RC -eq 5 ]]; then DEF=$((DEF + 1)); echo "apply $id deferred"
  elif [[ $RC -ne 0 ]]; then FAILED=$((FAILED + 1)); echo "apply $id failed"; fi
  snapshot "$id"
done
[[ "$DRY_RUN" == 1 ]] || "$UV" run apply sheet
DONE=$((N - FAILED - DEF))
DETAIL="$DONE/$N prepared"; [[ $DEF -gt 0 ]] && DETAIL="$DETAIL, $DEF deferred"
if [[ "$DRY_RUN" != 1 && $N -gt 0 && $DONE -eq 0 ]]; then stage_end 1 "$DETAIL" fail "$DEF"; exit 1; fi
STATUS=ok; [[ "$DRY_RUN" != 1 && $DONE -lt $N ]] && STATUS=partial
stage_end 0 "$DETAIL" "$STATUS" "$DEF"
