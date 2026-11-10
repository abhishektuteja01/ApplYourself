#!/bin/bash
# The /apply eval: `apply-eval stage`, one headless apply-judge per slice
# (throttled by judge_parallel), one retry of any slice with no valid answer
# file, then `apply-eval grade`.
#
# Usage: scripts/eval_apply.sh SET [MODEL] [REPEAT_RUN_DIR]
#   SET     public | private
#   MODEL   claude --model value (default: the agent's own model)
#   REPEAT  an earlier run dir of the same cases, for repeat agreement
# DRY_RUN=1 prints the commands only.
set -uo pipefail
SET="${1:?usage: eval_apply.sh public|private [MODEL] [REPEAT_RUN_DIR]}"
MODEL="${2:-}"
REPEAT="${3:-}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[[ "$SET" == public || "$SET" == private ]] || { echo "SET is public or private" >&2; exit 2; }

if [[ "${DRY_RUN:-0}" == 1 ]]; then
  RUN_DIR="$REPO/applications/evals/apply/runs/dry-run"
fi
# shellcheck source=SCRIPTDIR/lib_pipeline.sh
source "$REPO/scripts/lib_pipeline.sh"
STAGE=eval_apply

MODEL_FLAGS=()
[[ -n "$MODEL" ]] && MODEL_FLAGS=(--model "$MODEL")
GRADE_FLAGS=()
[[ -n "$REPEAT" ]] && GRADE_FLAGS=(--repeat "$REPEAT")

if [[ "$DRY_RUN" == 1 ]]; then
  LOG=/dev/null
  echo "[dry-run] uv run apply-eval stage --set $SET"
  echo "[dry-run] per judge line (at most $(cfg judge_parallel) at once):"
  run_claude "slice_<n>" "Read,Write" "judge <packet> -> <out>" --agent apply-judge ${MODEL_FLAGS[@]+"${MODEL_FLAGS[@]}"}
  echo "[dry-run] uv run apply-eval owed <run_dir>; each owed line rerun once"
  echo "[dry-run] uv run apply-eval grade <run_dir> ${GRADE_FLAGS[*]:-}"
  exit 0
fi

PARALLEL="$(cfg judge_parallel)" || exit 2
[[ "$PARALLEL" =~ ^[1-9][0-9]*$ ]] || { echo "judge_parallel must be a positive integer" >&2; exit 2; }

OUT="$("$UV" run apply-eval stage --set "$SET" 2>&1)"; CODE=$?
echo "$OUT"
[[ $CODE -eq 0 ]] || exit "$CODE"
RUN_DIR="$(sed -n 's/^run //p' <<< "$OUT" | head -1)"
[[ -d "$RUN_DIR" ]] || { echo "stage printed no run dir" >&2; exit 2; }
export RUN_DIR
export LOG="$RUN_DIR/$STAGE.log"

run_judges() {  # run_judges "<judge lines>" TAG_PREFIX
  local line n=0 pids=() pid
  while IFS= read -r line; do
    [[ "$line" == judge\ * ]] || continue
    while [[ $(jobs -rp | wc -l) -ge $PARALLEL ]]; do sleep 2; done
    quota_hit && break
    n=$((n + 1))
    run_claude "$2_$n" "Read,Write" "$line" --agent apply-judge ${MODEL_FLAGS[@]+"${MODEL_FLAGS[@]}"} &
    pids+=($!)
  done <<< "$1"
  for pid in ${pids[@]+"${pids[@]}"}; do wait "$pid" || true; done
  echo "judges finished: $n"
}

run_judges "$OUT" slice
if ! quota_hit; then
  OWED="$("$UV" run apply-eval owed "$RUN_DIR")"
  [[ -n "$OWED" ]] && { echo "retrying:"; echo "$OWED"; run_judges "$OWED" retry; }
fi
quota_hit && echo "usage limit hit: unjudged slices grade as misses"

"$UV" run apply-eval grade "$RUN_DIR" ${GRADE_FLAGS[@]+"${GRADE_FLAGS[@]}"}
