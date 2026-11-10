#!/bin/bash
# Apply-prep stage: `apply triage --fill` fills, with no model, every Resume ready
# role whose answers are all known (never submits). Each remaining role gets the
# apply-judge; a role whose judged plan parks nothing is filled (no submit). A role
# that needs a cover letter or company answers gets a full /apply session. Then the
# sheet is rebuilt; a role with answers to confirm shows "Needs your OK".
# STAGE_LIMIT=N (env) caps the roles a run takes, for a first live test.
set -uo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/lib_pipeline.sh"
stage_start apply_prep
# shellcheck disable=SC2034  # read by lib_pipeline.sh
STAGE=apply_prep   # log/json names
pdf_helper_up

TOOLS='Agent,Bash(uv run apply prepare *),Bash(uv run apply plan *),Bash(uv run apply judge-pack *),Bash(uv run apply judge-check *),Bash(uv run apply run *),Bash(uv run apply learn *),Bash(uv run app *),Bash(uv run track *),Bash(cd *),Bash(git rev-parse *),Read,Write,Edit,Skill,WebSearch,WebFetch'
JUDGE_TOOLS='Read,Write'
if [[ "$DRY_RUN" == 1 ]]; then echo "[dry-run] uv run apply sheet"; else
  "$UV" run apply sheet || { stage_end 1 "apply sheet failed"; exit 1; }
fi
# Roles with every answer known are filled here, no model; the rest stay Resume ready.
if [[ "$DRY_RUN" == 1 ]]; then
  echo "[dry-run] uv run apply triage --fill"
else
  LIMIT_ARGS=(); [[ -n "${STAGE_LIMIT:-}" ]] && LIMIT_ARGS=(--limit "$STAGE_LIMIT")
  "$UV" run apply triage --fill ${LIMIT_ARGS[@]+"${LIMIT_ARGS[@]}"} || echo "apply triage failed: every role goes to the judge"
  "$UV" run apply sheet >/dev/null
fi
# `<id> ok|fail` per role finished; a rerun after a usage-limit wait goes on from it.
TRIED="$RUN_DIR/apply_prep.tried"
[[ "${NIGHT_RERUN:-0}" == 1 ]] || : > "$TRIED"
touch "$TRIED"
PRIOR=$(grep -c . "$TRIED" || true); PRIOR=${PRIOR:-0}
LEFT=$(( ${STAGE_LIMIT:-100000} - PRIOR )); [[ $LEFT -gt 0 ]] || LEFT=0
IDS="$("$UV" run apply sheet --list 'Resume ready' | grep -vxFf <(cut -d' ' -f1 "$TRIED") | head -n "$LEFT")"
N=$(grep -c . <<< "$IDS"); DEF=0; STARTED=0
FAILED=$(grep -c ' fail$' "$TRIED" || true); FAILED=${FAILED:-0}
CAP="$(night_cap apply_prep_max_per_night)"
echo "Resume ready: $N, $PRIOR done in an earlier pass (cap ${CAP/#0/none})"

# What is still unresolved after the role, for summary.md: the plan as JSON,
# with the drafted answers applied when there are any. No browser.
snapshot() {
  [[ "$DRY_RUN" == 1 ]] && { echo "[dry-run] snapshot plan_after.json for $1"; return 0; }
  local dir ans=()
  dir="$("$UV" run app dir "$1" | "$UV" run python -c 'import json,sys; print(json.load(sys.stdin)["dir"])')" || return 0
  [[ -n "$dir" && -f "$dir/answers_override.json" ]] && ans=(--answers "$dir/answers_override.json")
  "$UV" run apply plan "$1" --json ${ans[@]+"${ans[@]}"} > "$dir/plan_after.json" 2> "$dir/plan_after.err" || true
}

# needs FILE KIND: 0 when the plan JSON parks on KIND (letter | company).
needs() {
  "$UV" run python - "$1" "$2" <<'PY'
import json, sys
plan = json.load(open(sys.argv[1], encoding="utf-8"))
rows = plan.get("unmapped") or []
if sys.argv[2] == "letter":
    hit = any(r.get("id") == "cover_letter" for r in rows)
else:
    hit = any("company_answers.yaml" in (r.get("reason") or "") for r in rows)
sys.exit(0 if hit else 1)
PY
}

# parked FILE: 0 when the plan JSON still parks a required field.
parked() {
  "$UV" run python -c 'import json,sys; sys.exit(0 if json.load(open(sys.argv[1]))["parked"] else 1)' "$1"
}

# judge ID: judge-pack, one apply-judge, judge-check; one retry. 5 = usage limit.
judge() {
  local line rc attempt
  line="$("$UV" run apply judge-pack "$1")" || return 1
  echo "$line"
  for attempt in 1 2; do
    run_claude "${1}_judge$attempt" "$JUDGE_TOOLS" "$line" --agent apply-judge; rc=$?
    [[ $rc -eq 5 ]] && return 5
    "$UV" run apply judge-check "$1" && return 0
  done
  return 1
}

# prep_role ID: 0 ok, 1 fail, 5 deferred.
prep_role() {
  local id="$1" out ovr rc
  if [[ "$DRY_RUN" == 1 ]]; then
    echo "[dry-run] uv run apply prepare $id"
    echo "[dry-run] uv run apply plan $id --json > <OUT_DIR>/plan.json"
    echo "[dry-run] cover_letter parked: claude -p \"/apply $id\" instead"
    echo "[dry-run] uv run apply judge-pack $id"
    run_claude "${id}_judge1" "$JUDGE_TOOLS" "judge <packet> -> <out>" --agent apply-judge
    echo "[dry-run] uv run apply judge-check $id  (one retry of the judge on exit 1)"
    echo "[dry-run] uv run apply plan $id --json --answers <OVERRIDES_FILE> > <OUT_DIR>/plan_judged.json"
    echo "[dry-run] company_answers.yaml parked: claude -p \"/apply $id\" instead"
    echo "[dry-run] nothing parked: uv run apply run --job-id $id --answers <OVERRIDES_FILE> --yes --manifest <OUT_DIR>/form_manifest.json"
    return 0
  fi
  local prep
  prep="$("$UV" run apply prepare "$id")" || { echo "apply prepare $id failed"; return 1; }
  out="$("$UV" run python -c 'import shlex,sys
d=dict(l.split("=",1) for l in sys.stdin.read().splitlines() if "=" in l)
print(shlex.split(d[sys.argv[1]])[0])' OUT_DIR <<< "$prep")" || return 1
  ovr="$out/answers_override.json"
  "$UV" run apply plan "$id" --json > "$out/plan.json"; rc=$?
  if [[ $rc -eq 2 ]]; then echo "apply $id: posting expired"; return 0; fi
  [[ $rc -eq 0 ]] || { echo "apply plan $id failed"; return 1; }
  if needs "$out/plan.json" letter; then
    echo "apply $id: cover letter needed, full /apply session"
    run_claude "$id" "$TOOLS" "/apply $id"; return $?
  fi
  judge "$id"; rc=$?
  [[ $rc -eq 0 ]] || { echo "apply $id: judge failed"; return "$rc"; }
  "$UV" run apply plan "$id" --json --answers "$ovr" > "$out/plan_judged.json" \
    || { echo "apply plan $id --answers failed"; return 1; }
  if needs "$out/plan_judged.json" company; then
    echo "apply $id: company answers needed, full /apply session"
    run_claude "$id" "$TOOLS" "/apply $id"; return $?
  fi
  if parked "$out/plan_judged.json"; then
    echo "apply $id: parked, needs your OK"; return 0
  fi
  "$UV" run apply run --job-id "$id" --answers "$ovr" --yes --manifest "$out/form_manifest.json"
}

for id in $IDS; do
  # Over the cap or past the usage limit: stays Resume ready for the rerun after the reset, or the next night.
  if quota_hit || [[ $CAP -gt 0 && $((PRIOR + STARTED)) -ge $CAP ]]; then DEF=$((DEF + 1)); continue; fi
  STARTED=$((STARTED + 1))
  prep_role "$id"; RC=$?
  if [[ $RC -eq 5 ]]; then DEF=$((DEF + 1)); echo "apply $id deferred"
  elif [[ $RC -ne 0 ]]; then FAILED=$((FAILED + 1)); echo "apply $id failed"; echo "$id fail" >> "$TRIED"
  else echo "$id ok" >> "$TRIED"; fi
  snapshot "$id"
done
[[ "$DRY_RUN" == 1 ]] || "$UV" run apply sheet
N=$((N + PRIOR))
DONE=$((N - FAILED - DEF))
DETAIL="$DONE/$N prepared"; [[ $DEF -gt 0 ]] && DETAIL="$DETAIL, $DEF deferred"
if [[ "$DRY_RUN" != 1 && $N -gt 0 && $DONE -eq 0 ]]; then stage_end 1 "$DETAIL" fail "$DEF"; exit 1; fi
STATUS=ok; [[ "$DRY_RUN" != 1 && $DONE -lt $N ]] && STATUS=partial
stage_end 0 "$DETAIL" "$STATUS" "$DEF"
