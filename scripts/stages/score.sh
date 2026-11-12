#!/bin/bash
# Score stage: `score start`, one headless score-judge per `judge` line in
# parallel, `score finish`, and one retry of whatever it says is still owed.
# The same three steps as .claude/commands/score.md, run by the script.
# STAGE_LIMIT=N (env) caps the judges a run starts; the rest stay owed.
# With --vertical V, retries stay in lane V.
set -uo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/lib_pipeline.sh"
stage_start score

if [[ "$DRY_RUN" == 1 ]]; then
  echo "[dry-run] uv run score start $*"
  echo "[dry-run] per judge line: claude -p \"<slice files>\" --agent score-judge  (parallel, then wait)"
  echo "[dry-run] uv run score finish; owed lines rerun up to $(cfg retries) time(s)"
  stage_end 0 "dry-run"; exit 0
fi

RETRIES="$(cfg retries)" || { stage_end 2 "config"; exit 2; }
JUDGE_TOOLS="Read,Write"
JUDGE_PARALLEL="$(cfg judge_parallel 2>/dev/null)" || JUDGE_PARALLEL=8
[[ "$JUDGE_PARALLEL" =~ ^[1-9][0-9]*$ ]] || JUDGE_PARALLEL=8
STARTED=0
LANE="" prev=""
for a in "$@"; do
  [[ "$prev" == --vertical ]] && LANE="$a"
  [[ "$a" == --vertical=* ]] && LANE="${a#--vertical=}"
  prev="$a"
done

run_judges() {  # run_judges "<judge lines>" [tag prefix]
  local pids=() n=0 line lines=()
  while IFS= read -r line; do
    [[ "$line" == judge\ * ]] && lines+=("${line#judge }")
  done <<< "$1"
  for line in ${lines[@]+"${lines[@]}"}; do
    # At most JUDGE_PARALLEL judges at once (an --all run stages hundreds).
    while [[ $(jobs -rp | wc -l) -ge $JUDGE_PARALLEL ]]; do sleep 2; done
    quota_hit && break  # past the usage limit: start no more; the rest stay owed
    [[ -n "${STAGE_LIMIT:-}" && $STARTED -ge $STAGE_LIMIT ]] && break
    n=$((n + 1)); STARTED=$((STARTED + 1))
    run_claude "${2:-judge}_$n" "$JUDGE_TOOLS" "$line" --agent score-judge &
    pids+=($!)
  done
  local pid
  for pid in ${pids[@]+"${pids[@]}"}; do wait "$pid" || true; done
  echo "judges finished: $n"
}

OUT="$("$UV" run score start "$@" 2>&1)"; CODE=$?
echo "$OUT"
if [[ $CODE -ne 0 ]]; then stage_end "$CODE" "score start failed"; exit "$CODE"; fi
DROPS="$(head -1 <<< "$OUT" | grep -oE 'filtered_[a-z-]+=[0-9]+' | tr '\n' ' ')"
if grep -q '^done ' <<< "$OUT"; then
  stage_end 0 "$(grep '^done ' <<< "$OUT") ${DROPS% }"; exit 0
fi
run_judges "$OUT"

attempt=0
while :; do
  FIN="$("$UV" run score finish 2>&1)"; FCODE=$?
  echo "$FIN"
  if [[ $FCODE -eq 0 ]]; then stage_end 0 "$(head -1 <<< "$FIN") ${DROPS% }"; exit 0; fi
  attempt=$((attempt + 1))
  if [[ -n "${STAGE_LIMIT:-}" && $STARTED -ge $STAGE_LIMIT ]]; then
    stage_end 0 "$STARTED judges (STAGE_LIMIT); $(grep -c '^judge ' <<< "$FIN") judge lines still owed" partial
    exit 0
  fi
  # A judge hit the usage limit: a retry now would hit it too.
  if quota_hit; then
    stage_end 1 "usage limit hit: owed slices deferred to the next run" fail
    exit 1
  fi
  if [[ $attempt -gt $RETRIES ]]; then
    stage_end 1 "still owed after $RETRIES retry: $(grep '^bad ' <<< "$FIN" | head -3 | tr '\n' ' ')"
    exit 1
  fi
  OWED="$FIN"
  [[ -n "$LANE" ]] && OWED="$("$UV" run score start --vertical "$LANE" 2>&1)"
  run_judges "$OWED" "retry${attempt}_judge"
done
