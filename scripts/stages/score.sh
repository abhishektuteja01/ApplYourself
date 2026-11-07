#!/bin/bash
# Score stage: `score start`, one headless score-judge per `judge` line in
# parallel, `score finish`, and one retry of whatever it says is still owed.
# The same three steps as .claude/commands/score.md, run by the script.
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

run_judges() {  # run_judges "<judge lines>" [tag prefix]
  local pids=() n=0 line lines=()
  while IFS= read -r line; do
    [[ "$line" == judge\ * ]] && lines+=("${line#judge }")
  done <<< "$1"
  for line in ${lines[@]+"${lines[@]}"}; do
    n=$((n + 1))
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
  # A judge hit the usage limit: a retry now would hit it too.
  if quota_hit; then
    stage_end 1 "usage limit hit: owed slices deferred to the next run" fail
    exit 1
  fi
  if [[ $attempt -gt $RETRIES ]]; then
    stage_end 1 "still owed after $RETRIES retry: $(grep '^bad ' <<< "$FIN" | head -3 | tr '\n' ' ')"
    exit 1
  fi
  run_judges "$FIN" "retry${attempt}_judge"
done
