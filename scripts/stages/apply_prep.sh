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
N=$(grep -c . <<< "$IDS"); FAILED=0
echo "Resume ready: $N"

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
  run_claude "$id" "$TOOLS" "/apply $id" || { FAILED=$((FAILED + 1)); echo "apply $id failed"; }
  snapshot "$id"
done
[[ "$DRY_RUN" == 1 ]] || "$UV" run apply sheet
DETAIL="$((N - FAILED))/$N prepared"
if [[ "$DRY_RUN" != 1 && $N -gt 0 && $FAILED -eq $N ]]; then stage_end 1 "$DETAIL"; exit 1; fi
stage_end 0 "$DETAIL"
